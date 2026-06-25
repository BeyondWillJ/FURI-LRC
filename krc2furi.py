#!/usr/bin/env python3
"""krc2furi.py — convert 酷狗 KRC (or Enhanced LRC) → furi-lrc JSON

Usage:
    python krc2furi.py song.krc             # KRC → JSON (with furigana)
    python krc2furi.py song.lrc             # Enhanced LRC → JSON (with furigana)
    python krc2furi.py song.krc -o out.json # explicit output path

KRC format recap:
  Line header:  [offset_ms,duration_ms]
  Per-char:     <char_offset_ms,char_duration_ms>字

The script adds furigana via fugashi (MeCab) and splits ruby readings into
mora units (e.g. きょう → [きょ, う]).  Chinese translations must be added
manually — a placeholder "???" is inserted for each line.

Requirements (conda rubi env):
    pip install fugashi unidic-lite
    # OR: pip install fugashi[unidic]  followed by  python -m unidic download
"""

import re
import sys
import json
import gzip
import struct
import argparse
from pathlib import Path

# ── furigana / mora tools ─────────────────────────────────────────────────────

try:
    import fugashi
    _tagger = fugashi.Tagger()
    FUGASHI_OK = True
except ImportError:
    FUGASHI_OK = False
    print("[warn] fugashi not found — ruby fields will be null", file=sys.stderr)


_SMALL_KANA = set("ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ")
_PROLONGED  = "ー"
_SPECIAL    = "っっンン"   # geminate + nasal (each is 1 mora)


def reading_to_moras(reading: str) -> list[str]:
    """Split a hiragana/katakana reading string into mora units."""
    moras, i = [], 0
    while i < len(reading):
        ch = reading[i]
        if i + 1 < len(reading) and reading[i + 1] in _SMALL_KANA:
            moras.append(ch + reading[i + 1])
            i += 2
        else:
            moras.append(ch)
            i += 1
    return moras


def to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ン" else c for c in s)


def get_ruby(surface: str) -> str | None:
    """Return hiragana reading for surface, or None if no ruby needed."""
    if not FUGASHI_OK:
        return None
    # Skip if surface is already all-kana / punctuation
    if all(
        "぀" <= c <= "ヿ" or not c.isalpha() or c.isascii()
        for c in surface
    ):
        return None
    reading = ""
    for word in _tagger(surface):
        feat = word.feature
        # unidic feature[6] = 読み (reading in katakana)
        try:
            r = feat[6] if len(feat) > 6 else ""
            reading += to_hiragana(r) if r and r != "*" else word.surface
        except Exception:
            reading += word.surface
    # Only return ruby if it differs from surface
    return reading if reading != surface else None


# ── KRC parser ────────────────────────────────────────────────────────────────

_KRC_MAGIC = b"krc1"
_KRC_XOR   = bytes([64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105])


def decrypt_krc(data: bytes) -> bytes:
    if not data.startswith(_KRC_MAGIC):
        raise ValueError("Not a KRC file (wrong magic)")
    encrypted = data[4:]
    decrypted  = bytearray(len(encrypted))
    for i, b in enumerate(encrypted):
        decrypted[i] = b ^ _KRC_XOR[i % 16]
    return gzip.decompress(bytes(decrypted))


_LINE_RE = re.compile(r"\[(\d+),(\d+)\](.*)")
_CHAR_RE = re.compile(r"<(\d+),(\d+)>([^<\[]*)")


def parse_krc(text: str) -> list[dict]:
    """Parse KRC lyric body into raw timed-character lines."""
    lines = []
    for raw_line in text.splitlines():
        m = _LINE_RE.match(raw_line.strip())
        if not m:
            continue
        line_start = int(m.group(1))
        body       = m.group(3)
        chars = []
        for cm in _CHAR_RE.finditer(body):
            offset   = int(cm.group(1))
            duration = int(cm.group(2))
            text_ch  = cm.group(3)
            if text_ch:
                chars.append({
                    "s": line_start + offset,
                    "e": line_start + offset + duration,
                    "ch": text_ch,
                })
        if chars:
            lines.append({
                "start": line_start,
                "end":   chars[-1]["e"],
                "chars": chars,
            })
    return lines


# ── Enhanced LRC parser ───────────────────────────────────────────────────────

_ELRC_LINE  = re.compile(r"\[(\d+):(\d+\.\d+)\](<\d+:\d+\.\d+>[^[<]*)+")
_ELRC_STAMP = re.compile(r"<(\d+):(\d+\.\d+)>([^<\[]*)")


def ts_to_ms(mm: str, ss: str) -> int:
    return int(mm) * 60000 + round(float(ss) * 1000)


def parse_enhanced_lrc(text: str) -> list[dict]:
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        # Must have at least one inline <mm:ss.xx>
        if not re.search(r"<\d+:\d+\.\d+>", raw):
            continue
        lm = re.match(r"\[(\d+):(\d+\.\d+)\]", raw)
        if not lm:
            continue
        line_start = ts_to_ms(lm.group(1), lm.group(2))
        chars = []
        stamps = list(_ELRC_STAMP.finditer(raw))
        for i, sm in enumerate(stamps):
            t_start = ts_to_ms(sm.group(1), sm.group(2))
            text_ch = sm.group(3).rstrip()
            if not text_ch:
                continue
            t_end = ts_to_ms(stamps[i + 1].group(1), stamps[i + 1].group(2)) \
                    if i + 1 < len(stamps) else t_start + 300
            for ch in text_ch:
                if ch.strip():
                    dur = max(50, (t_end - t_start) // max(1, len(text_ch.strip())))
                    chars.append({"s": t_start, "e": t_start + dur, "ch": ch})
        if chars:
            lines.append({"start": line_start, "end": chars[-1]["e"], "chars": chars})
    return lines


# ── Build furi-lrc JSON segments from timed chars ─────────────────────────────

def chars_to_segments(timed_chars: list[dict]) -> list[dict]:
    """
    Group consecutive chars that share the same word (MeCab tokenisation),
    attach ruby reading, and split reading into mora-level units.
    Each unit carries its own (s, e) window.
    """
    if not timed_chars:
        return []

    # Reconstruct the plain text so MeCab can tokenise it properly
    plain = "".join(c["ch"] for c in timed_chars)

    # Build char-index → word mapping via fugashi
    char_word_map: list[int] = [-1] * len(plain)  # index → word_id
    word_surfaces: list[str] = []
    word_readings: list[str | None] = []

    if FUGASHI_OK:
        pos = 0
        for wid, word in enumerate(_tagger(plain)):
            surf = word.surface
            reading = None
            # Check if this word needs ruby (contains kanji)
            if any("一" <= c <= "鿿" for c in surf):
                feat = word.feature
                try:
                    r = feat[6] if len(feat) > 6 else ""
                    reading = to_hiragana(r) if r and r != "*" else None
                except Exception:
                    pass
            for i in range(len(surf)):
                if pos + i < len(char_word_map):
                    char_word_map[pos + i] = wid
            word_surfaces.append(surf)
            word_readings.append(reading)
            pos += len(surf)
    else:
        # Fallback: each char is its own segment
        for i, c in enumerate(timed_chars):
            char_word_map[i] = i
            word_surfaces.append(c["ch"])
            word_readings.append(None)

    # Group timed_chars by word_id
    from itertools import groupby
    groups = []
    for wid, group in groupby(enumerate(timed_chars), key=lambda x: char_word_map[x[0]] if x[0] < len(char_word_map) else -1):
        group = list(group)
        idxs  = [g[0] for g in group]
        chars = [g[1] for g in group]
        if wid >= 0 and wid < len(word_surfaces):
            surface = word_surfaces[wid]
            reading = word_readings[wid]
        else:
            surface = "".join(c["ch"] for c in chars)
            reading = None
        groups.append({"surface": surface, "reading": reading, "chars": chars})

    # Build segment list
    segments = []
    for grp in groups:
        surface = grp["surface"]
        reading = grp["reading"]
        chars   = grp["chars"]

        if reading:
            moras   = reading_to_moras(reading)
            n_moras = len(moras)
            n_chars = len(chars)
            # Distribute char time-windows across moras proportionally
            total_ms = chars[-1]["e"] - chars[0]["s"]
            mora_ms  = total_ms / n_moras if n_moras else total_ms
            t0       = chars[0]["s"]
            units = []
            for mi, mora in enumerate(moras):
                ms_s = round(t0 + mi * mora_ms)
                ms_e = round(t0 + (mi + 1) * mora_ms)
                units.append({"k": mora, "s": ms_s, "e": ms_e})
            segments.append({"base": surface, "ruby": reading, "units": units})
        else:
            # No ruby: one unit per character in the group
            units = [{"k": c["ch"], "s": c["s"], "e": c["e"]} for c in chars if c["ch"].strip()]
            if units:
                segments.append({"base": surface, "ruby": None, "units": units})

    return segments


# ── Main conversion ───────────────────────────────────────────────────────────

def convert(src: Path) -> dict:
    raw = src.read_bytes()

    if src.suffix.lower() == ".krc":
        text  = decrypt_krc(raw).decode("utf-8", errors="replace")
        timed = parse_krc(text)
    else:
        text  = raw.decode("utf-8", errors="replace")
        timed = parse_enhanced_lrc(text)

    lines = []
    for tl in timed:
        segs = chars_to_segments(tl["chars"])
        if not segs:
            continue
        lines.append({
            "start": tl["start"],
            "end":   tl["end"],
            "jp":    segs,
            "zh":    "???",   # fill in Chinese translation manually
        })

    return {
        "meta":  {"title": src.stem, "artist": "", "offset": 0},
        "lines": lines,
    }


def main():
    ap = argparse.ArgumentParser(description="Convert KRC/Enhanced-LRC → furi-lrc JSON")
    ap.add_argument("src", help="Input .krc or .lrc file")
    ap.add_argument("-o", "--out", help="Output .json path (default: same name)")
    args = ap.parse_args()

    src = Path(args.src)
    out = Path(args.out) if args.out else src.with_suffix(".json")

    result = convert(src)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    print(f"[krc2furi] wrote {len(result['lines'])} lines → {out}")
    print("[krc2furi] TODO: fill in zh translations, verify mora boundaries in Aegisub")


if __name__ == "__main__":
    main()
