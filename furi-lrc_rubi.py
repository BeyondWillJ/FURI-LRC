#!/usr/bin/env python3
"""furi-lrc — karaoke lyrics overlay for Windows 11
Smooth GPU-blit edition: pre-rendered pixmap cache + soft-correction clock.

Phase 2+3  Each active line is pre-rendered once into two QPixmap caches
           (unsung layer + sung layer). paintEvent only blits + clips a
           single rect — zero text shaping per frame.
Phase 4    Self-walking monotonic clock. SMTC ticks apply soft 15%
           correction for drift; hard sync fires only on play/pause
           transitions or seek jumps > 700 ms.
Phase 5    All heavy work (shadow parse, font metrics, color construction,
           block geometry) is done at load/resize/config time, not in the
           per-frame path. 60 fps timer suspends when paused & fully faded.
"""

import sys
import json
import re
import time
import ctypes
import asyncio
import threading
import datetime
import dataclasses
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QMenu, QDialog,
    QFormLayout, QLineEdit, QDialogButtonBox, QTabWidget,
    QDoubleSpinBox, QColorDialog, QPushButton, QFileDialog,
    QSpinBox, QCheckBox,
)
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QPoint, QPointF, QRectF, QRect,
    QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import (
    QColor, QCursor, QPainter, QPen, QFontDatabase, QFont, QFontMetricsF,
    QPixmap,
)

# ── winrt SMTC (optional) ───
WINRT_AVAILABLE = False
try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )
    WINRT_AVAILABLE = True
except ImportError:
    pass

def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

APP_DIR = _app_dir()

# ── Config ───
FONTS_DIR   = APP_DIR / "fonts"
CONFIG_PATH = APP_DIR / "settings.json"
DEFAULT_CFG = dict(
    x=50, y=50, w=580, h=320,
    opacity=0.88, locked=False,
    font_jp=str(FONTS_DIR / "NotoSerifJP-SemiBold.ttf"),
    font_zh=str(FONTS_DIR / "msyhbd.ttc"),
    font_size_jp=20, font_size_rt=10, font_size_zh=14,
    spacing_rt=2, spacing_zh=-8,
    color_sung="#4fc3f7", color_unsung="#888888", color_zh="#aaaaaa",
    lyrics_path="", hide_on_pause=True, unlock_zone=48,
    text_shadow="2px 2px 8px rgba(0,0,0,0.85)",
)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = {**DEFAULT_CFG, **json.loads(CONFIG_PATH.read_text("utf-8"))}
            for key in ("font_jp", "font_zh"):
                val = cfg[key]
                p = Path(val) if Path(val).is_absolute() else APP_DIR / val
                if p.suffix.lower() not in {".ttf", ".otf", ".ttc", ".woff", ".woff2"} or not p.exists():
                    cfg[key] = DEFAULT_CFG[key]
            return cfg
        except Exception:
            pass
    return DEFAULT_CFG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


# ── SMTC background worker ──
class SMTCWorker(QObject):
    time_updated  = pyqtSignal(float, bool)   # (position_sec, is_playing)
    track_changed = pyqtSignal(str, str)       # (title, artist)

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()

    def start(self):
        self._stop.clear()
        threading.Thread(target=self._thread_main, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._poll())
        loop.close()

    async def _poll(self):
        if not WINRT_AVAILABLE:
            return
        last_title = ""
        while not self._stop.is_set():
            try:
                mgr  = await MediaManager.request_async()
                sess = mgr.get_current_session()
                if sess is None:
                    self.time_updated.emit(0.0, False)
                    await asyncio.sleep(0.5)
                    continue

                props = await sess.try_get_media_properties_async()
                if props:
                    title = props.title or ""
                    if title != last_title:
                        self.track_changed.emit(title, props.artist or "")
                        last_title = title

                tl      = sess.get_timeline_properties()
                pb      = sess.get_playback_info()
                playing = int(pb.playback_status) == 3

                pos = tl.position
                sec = pos.total_seconds() if hasattr(pos, "total_seconds") else float(pos) / 1e7

                if playing:
                    lu = tl.last_updated_time
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    try:
                        if isinstance(lu, datetime.datetime):
                            elapsed = (now_utc - lu).total_seconds()
                        else:
                            ft_epoch = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
                            lu_utc   = ft_epoch + datetime.timedelta(microseconds=int(lu) / 10)
                            elapsed  = (now_utc - lu_utc).total_seconds()
                        sec = max(0.0, sec + elapsed)
                    except Exception:
                        pass

                self.time_updated.emit(sec, playing)
            except Exception:
                pass
            await asyncio.sleep(0.2)


# ── Layout data structures ──

@dataclasses.dataclass
class _MoraDraw:
    x:     float
    w:     float
    text:  str
    s_ms:  float
    e_ms:  float
    is_rt: bool    # True = furigana mora (drawn above jp baseline)


@dataclasses.dataclass
class _KanjiDraw:
    x:          float
    w:          float
    text:       str
    mora_refs:  List[_MoraDraw]


@dataclasses.dataclass
class _LineLayout:
    morae:   List[_MoraDraw]
    kanjis:  List[_KanjiDraw]
    zh_text: str
    zh_w:    float
    total_w: float


# ── Pure helpers ──

def _clamp01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _mora_progress(m: _MoraDraw, ms: float) -> float:
    return _clamp01((ms - m.s_ms) / max(1.0, m.e_ms - m.s_ms))


def _parse_shadow(s: str) -> Optional[Tuple[float, float, QColor]]:
    if not s or s.lower() == "none":
        return None
    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    dx = float(nums[0]) if len(nums) > 0 else 2.0
    dy = float(nums[1]) if len(nums) > 1 else 2.0
    rgba_m = re.search(
        r"rgba?\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)", s
    )
    if rgba_m:
        r, g, b = float(rgba_m.group(1)), float(rgba_m.group(2)), float(rgba_m.group(3))
        a = float(rgba_m.group(4)) if rgba_m.group(4) else 1.0
        color = QColor(int(r), int(g), int(b), int(a * 255))
    else:
        hex_m = re.search(r"#[0-9a-fA-F]{3,8}", s)
        color  = QColor(hex_m.group()) if hex_m else QColor(0, 0, 0, 200)
    return (dx, dy, color)


def _load_qt_font(path_str: str, size: int,
                  weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    if path_str:
        p = Path(path_str)
        if not p.is_absolute():
            p = APP_DIR / p
        fid      = QFontDatabase.addApplicationFont(str(p))
        families = QFontDatabase.applicationFontFamilies(fid)
        if families:
            f = QFont(families[0])
            f.setPixelSize(size)
            f.setWeight(weight)
            return f
    f = QFont()
    f.setPixelSize(size)
    f.setWeight(weight)
    return f


# ── Lyrics canvas ──

class LyricsCanvas(QWidget):
    # Phase 4: soft-clock constants
    _SOFT_RATE   = 0.15    # fraction of SMTC drift absorbed each poll tick
    _HARD_THRESH = 700.0   # ms — jump larger than this → hard sync (seek)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self._cfg:     dict              = {}
        self._lyrics:  list              = []
        self._layouts: List[_LineLayout] = []

        # Phase 2+3: per-line pixmap caches (index-aligned with _layouts)
        self._px_unsung:    List[Optional[QPixmap]]             = []
        self._px_sung:      List[Optional[QPixmap]]             = []
        self._sweep_params: List[Optional[Tuple[float, float]]] = []  # (x_off, scale)
        self._cache_win_w:  int   = 0
        self._cache_win_h:  int   = 0
        self._cache_dpr:    float = 0.0

        # Phase 4: self-walking clock
        self._base_ms:    float = 0.0
        self._base_wall:  float = time.monotonic()
        self._playing:    bool  = False

        # Fade state
        self._active_i:    int   = -1
        self._fade_alpha:  float = 0.0
        self._fade_target: float = 1.0

        # Sweep smoothing: exponential low-pass filter to avoid jitter
        self._sweep_smooth:    float = 0.0   # smoothed sweep_x in window pixels
        self._sweep_last_li:   int   = -1    # line index sweep_smooth belongs to

        # Fonts (rebuilt in apply_cfg / _rebuild_fonts)
        self._font_jp = QFont()
        self._font_rt = QFont()
        self._font_zh = QFont()
        self._fm_jp   = QFontMetricsF(self._font_jp)
        self._fm_rt   = QFontMetricsF(self._font_rt)
        self._fm_zh   = QFontMetricsF(self._font_zh)

        # Phase 5: 60fps timer; suspends when paused & fully faded
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(16)
        self._frame_timer.timeout.connect(self._tick)
        self._frame_timer.start()

    # ── Phase 4: clock ──

    def sync_time(self, sec: float, playing: bool, force_hard: bool = False) -> None:
        """Update canvas clock from an SMTC reading.

        Soft-corrects small drift while self-walking; hard-syncs on
        play/pause transitions, large jumps (seeks), or explicit force.
        """
        new_ms      = sec * 1000.0
        was_playing = self._playing

        hard = force_hard or (playing != was_playing) or not playing
        if not hard:
            delta = new_ms - self._now_ms()
            if abs(delta) > self._HARD_THRESH:
                hard = True
            else:
                # Soft correction: absorb a fraction of the drift
                self._base_ms  += delta * self._SOFT_RATE
                self._base_wall = time.monotonic()
                self._playing   = playing
                self.update()
                return

        if hard:
            self._base_ms   = new_ms
            self._base_wall = time.monotonic()
            self._playing   = playing
        self.update()

    def _now_ms(self) -> float:
        if self._playing:
            return self._base_ms + (time.monotonic() - self._base_wall) * 1000.0
        return self._base_ms

    # ── Phase 5: suspend idle repaints ──

    def _tick(self) -> None:
        step = 0.07
        diff = self._fade_target - self._fade_alpha
        if abs(diff) < step:
            self._fade_alpha = self._fade_target
        else:
            self._fade_alpha += step if diff > 0 else -step
        # Skip repaint when paused and fade is stable — saves CPU + GPU
        if self._playing or abs(self._fade_target - self._fade_alpha) >= step:
            self.update()

    # ── Config & fonts ──

    def apply_cfg(self, cfg: dict) -> None:
        prev = self._cfg
        self._cfg = cfg
        font_changed = (
            cfg.get("font_jp")      != prev.get("font_jp")      or
            cfg.get("font_zh")      != prev.get("font_zh")      or
            cfg.get("font_size_jp") != prev.get("font_size_jp") or
            cfg.get("font_size_rt") != prev.get("font_size_rt") or
            cfg.get("font_size_zh") != prev.get("font_size_zh") or
            not prev
        )
        visual_changed = (
            cfg.get("color_sung")   != prev.get("color_sung")   or
            cfg.get("color_unsung") != prev.get("color_unsung") or
            cfg.get("color_zh")     != prev.get("color_zh")     or
            cfg.get("text_shadow")  != prev.get("text_shadow")  or
            cfg.get("spacing_rt")   != prev.get("spacing_rt")   or
            cfg.get("spacing_zh")   != prev.get("spacing_zh")
        )
        if font_changed:
            self._rebuild_fonts()
            self._rebuild_layouts()        # also rebuilds pixmap list
        elif visual_changed:
            self._invalidate_pixmap_cache()
        self.update()

    def _rebuild_fonts(self) -> None:
        cfg   = self._cfg
        sz_jp = cfg.get("font_size_jp", 20)
        sz_rt = cfg.get("font_size_rt", max(8, sz_jp // 2))
        sz_zh = cfg.get("font_size_zh", 14)
        jp_p  = cfg.get("font_jp", "")
        zh_p  = cfg.get("font_zh", "")
        self._font_jp = _load_qt_font(jp_p, sz_jp, QFont.Weight.DemiBold)
        self._font_rt = _load_qt_font(jp_p, sz_rt, QFont.Weight.DemiBold)
        self._font_zh = _load_qt_font(zh_p, sz_zh, QFont.Weight.Normal)
        self._fm_jp   = QFontMetricsF(self._font_jp)
        self._fm_rt   = QFontMetricsF(self._font_rt)
        self._fm_zh   = QFontMetricsF(self._font_zh)

    # ── Lyrics loading & layout ──

    def load_lyrics(self, lines: list) -> None:
        self._lyrics    = lines
        self._active_i  = -1
        self._fade_alpha = 0.0
        self._rebuild_layouts()
        self.update()

    def _rebuild_layouts(self) -> None:
        self._layouts = [self._layout_line(ln) for ln in self._lyrics]
        self._invalidate_pixmap_cache()
        self._cache_win_w = 0
        self._cache_win_h = 0

    def _layout_line(self, line: dict) -> _LineLayout:
        segs    = line.get("jp", [])
        zh_text = line.get("zh", "")
        fm_jp   = self._fm_jp
        fm_rt   = self._fm_rt
        fm_zh   = self._fm_zh

        morae:  List[_MoraDraw]  = []
        kanjis: List[_KanjiDraw] = []
        x = 0.0

        for seg in segs:
            units = seg.get("units", [])
            if seg.get("ruby"):
                base    = seg.get("base", "")
                furi    = "".join(u["k"] for u in units)
                Wb      = fm_jp.horizontalAdvance(base)
                Wr      = fm_rt.horizontalAdvance(furi)
                Wc      = max(Wb, Wr)
                kanji_x = x + (Wc - Wb) / 2
                furi_x  = x + (Wc - Wr) / 2
                seg_morae: List[_MoraDraw] = []
                cx = furi_x
                for u in units:
                    mw = fm_rt.horizontalAdvance(u["k"])
                    m  = _MoraDraw(x=cx, w=mw, text=u["k"],
                                   s_ms=float(u["s"]), e_ms=float(u["e"]), is_rt=True)
                    morae.append(m)
                    seg_morae.append(m)
                    cx += mw
                kanjis.append(_KanjiDraw(x=kanji_x, w=Wb, text=base, mora_refs=seg_morae))
                x += Wc
            else:
                for u in units:
                    uw = fm_jp.horizontalAdvance(u["k"])
                    m  = _MoraDraw(x=x, w=uw, text=u["k"],
                                   s_ms=float(u["s"]), e_ms=float(u["e"]), is_rt=False)
                    morae.append(m)
                    x += uw

        zh_w = fm_zh.horizontalAdvance(zh_text) if zh_text else 0.0
        return _LineLayout(morae=morae, kanjis=kanjis,
                           zh_text=zh_text, zh_w=zh_w, total_w=x)

    # ── Phase 5: resize invalidates caches ──

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._invalidate_pixmap_cache()

    def _invalidate_pixmap_cache(self) -> None:
        n = len(self._layouts)
        self._px_unsung    = [None] * n
        self._px_sung      = [None] * n
        self._sweep_params = [None] * n

    # ── Phase 2+3: pre-render one line into two pixmaps ──

    def _render_line_to_cache(self, li: int) -> None:
        """Render line li into px_unsung / px_sung. Called at most once per line
        per (window size × cfg) combination — not per frame."""
        layout = self._layouts[li]
        cfg    = self._cfg
        W, H   = self.width(), self.height()
        if W <= 0 or H <= 0:
            return

        # HiDPI: render at physical pixel resolution
        dpr  = self.devicePixelRatioF()
        PW   = int(W * dpr)
        PH   = int(H * dpr)

        # ── Phase 5: geometry computed here, not in paintEvent ──
        fm_jp   = self._fm_jp
        fm_rt   = self._fm_rt
        fm_zh   = self._fm_zh
        rt_h    = fm_rt.ascent() + fm_rt.descent()
        gap     = float(cfg.get("spacing_rt", 2))
        spacing = float(cfg.get("spacing_zh", -8))
        has_zh  = bool(layout.zh_text)

        if has_zh:
            block_h = (rt_h + gap + fm_jp.ascent() + fm_jp.descent()
                       + spacing + fm_zh.ascent() + fm_zh.descent())
        else:
            block_h = rt_h + gap + fm_jp.ascent() + fm_jp.descent()

        top_y     = (H - block_h) / 2
        rt_base_y = top_y + rt_h
        jp_base_y = rt_base_y + gap + fm_jp.ascent()
        zh_base_y = (jp_base_y + fm_jp.descent() + spacing + fm_zh.ascent()
                     if has_zh else 0.0)

        pad   = 18.0
        avail = W - 2 * pad
        scale = min(1.0, avail / layout.total_w) if layout.total_w > 1 else 1.0
        x_off = pad + (avail - layout.total_w * scale) / 2

        shadow       = _parse_shadow(cfg.get("text_shadow", ""))
        color_sung   = QColor(cfg.get("color_sung",   "#4fc3f7"))
        color_unsung = QColor(cfg.get("color_unsung", "#888888"))
        color_zh_c   = QColor(cfg.get("color_zh",     "#aaaaaa"))

        # ── Allocate two transparent pixmaps at physical resolution ──
        px_u = QPixmap(PW, PH)
        px_u.setDevicePixelRatio(dpr)
        px_u.fill(QColor(0, 0, 0, 0))
        px_s = QPixmap(PW, PH)
        px_s.setDevicePixelRatio(dpr)
        px_s.fill(QColor(0, 0, 0, 0))

        def _apply_transform(p: QPainter) -> None:
            if scale < 1.0:
                p.translate(x_off, H / 2.0 * (1.0 - scale))
                p.scale(scale, scale)
            else:
                p.translate(x_off, 0.0)

        # ── Unsung layer: shadow + all glyphs in unsung color + ZH ──
        p_u = QPainter(px_u)
        p_u.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p_u.save()
        _apply_transform(p_u)

        if shadow:
            sdx, sdy, sc = shadow
            p_u.setPen(sc)
            for m in layout.morae:
                p_u.setFont(self._font_rt if m.is_rt else self._font_jp)
                y = rt_base_y if m.is_rt else jp_base_y
                p_u.drawText(QPointF(m.x + sdx, y + sdy), m.text)
            if layout.kanjis:
                p_u.setFont(self._font_jp)
                for k in layout.kanjis:
                    p_u.drawText(QPointF(k.x + sdx, jp_base_y + sdy), k.text)

        p_u.setPen(color_unsung)
        for m in layout.morae:
            p_u.setFont(self._font_rt if m.is_rt else self._font_jp)
            p_u.drawText(QPointF(m.x, rt_base_y if m.is_rt else jp_base_y), m.text)
        if layout.kanjis:
            p_u.setFont(self._font_jp)
            for k in layout.kanjis:
                p_u.drawText(QPointF(k.x, jp_base_y), k.text)

        p_u.restore()   # pop scale/translate before drawing ZH

        if has_zh:
            zh_x = (W - layout.zh_w) / 2
            zh_y = (H / 2.0 * (1.0 - scale) + zh_base_y * scale
                    if scale < 1.0 else zh_base_y)
            if shadow:
                sdx, sdy, sc = shadow
                p_u.setFont(self._font_zh)
                p_u.setPen(sc)
                p_u.drawText(QPointF(zh_x + sdx, zh_y + sdy), layout.zh_text)
            p_u.setFont(self._font_zh)
            p_u.setPen(color_zh_c)
            p_u.drawText(QPointF(zh_x, zh_y), layout.zh_text)

        p_u.end()

        # ── Sung layer: all glyphs in sung color, no shadow, no ZH ──
        p_s = QPainter(px_s)
        p_s.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p_s.save()
        _apply_transform(p_s)

        p_s.setPen(color_sung)
        for m in layout.morae:
            p_s.setFont(self._font_rt if m.is_rt else self._font_jp)
            p_s.drawText(QPointF(m.x, rt_base_y if m.is_rt else jp_base_y), m.text)
        if layout.kanjis:
            p_s.setFont(self._font_jp)
            for k in layout.kanjis:
                p_s.drawText(QPointF(k.x, jp_base_y), k.text)

        p_s.restore()
        p_s.end()

        self._px_unsung[li]    = px_u
        self._px_sung[li]      = px_s
        self._sweep_params[li] = (x_off, scale)

    def _get_sweep_x(self, li: int, ms: float) -> float:
        """Return the karaoke reveal x in window pixel coordinates.

        Defined as the rightmost edge of any mora whose progress > 0,
        weighted by that mora's individual progress fraction.  Taking
        the max across all morae (rt + plain) produces a smooth
        left-to-right bar that is stable even when furigana x-ranges
        are slightly wider than their kanji bases.
        """
        layout = self._layouts[li]
        params = self._sweep_params[li]
        if params is None:
            return 0.0
        x_off, scale = params

        sweep_layout = 0.0
        for m in layout.morae:
            p = _mora_progress(m, ms)
            if p > 0.0:
                right = m.x + m.w * p
                if right > sweep_layout:
                    sweep_layout = right

        return x_off + sweep_layout * scale

    # ── Phase 3: two-layer blit paintEvent ──

    def paintEvent(self, _) -> None:    # noqa: N802
        if not self._lyrics or not self._layouts:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            if self._cfg:
                painter.setFont(self._font_jp)
            painter.setPen(QColor(136, 136, 136, 100))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "歌詞ファイルをドロップまたは右クリックで開く")
            return

        ms = self._now_ms()

        # Find active line (last line whose start ≤ ms)
        li = -1
        for i, ln in enumerate(self._lyrics):
            if ln.get("start", 0) <= ms:
                li = i
            else:
                break

        if li != self._active_i:
            self._active_i    = li
            self._fade_alpha  = 0.0
            self._fade_target = 1.0

        if li < 0 or li >= len(self._layouts):
            return

        W, H = self.width(), self.height()
        dpr  = self.devicePixelRatioF()

        # Invalidate caches if logical size or DPI changed (e.g. moved to different monitor)
        if self._cache_win_w != W or self._cache_win_h != H or self._cache_dpr != dpr:
            self._invalidate_pixmap_cache()
            self._cache_win_w = W
            self._cache_win_h = H
            self._cache_dpr   = dpr

        # Lazily render the active line
        if self._px_unsung[li] is None:
            self._render_line_to_cache(li)

        # Pre-warm next line to avoid first-frame cost on line transition
        nxt = li + 1
        if nxt < len(self._px_unsung) and self._px_unsung[nxt] is None:
            self._render_line_to_cache(nxt)

        target_x = self._get_sweep_x(li, ms)

        # Exponential low-pass smoothing: reset on line change, catch-up when
        # target is ahead (never let smoothed lag more than 40 px behind).
        _ALPHA = 0.55   # high alpha = near-instant tracking; only smooths sub-frame jitter
        if self._sweep_last_li != li:
            self._sweep_smooth  = target_x
            self._sweep_last_li = li
        else:
            diff = target_x - self._sweep_smooth
            # Instant catch-up on seek / fast-forward
            if diff > 20:
                self._sweep_smooth = target_x
            else:
                self._sweep_smooth += diff * _ALPHA
        sweep_x = self._sweep_smooth

        painter = QPainter(self)
        painter.setOpacity(self._fade_alpha)

        # Layer 1: full unsung pixmap (shadow + unsung text + ZH)
        painter.drawPixmap(0, 0, self._px_unsung[li])

        # Layer 2: sung pixmap clipped to the revealed left portion
        if sweep_x > 0.0:
            painter.setClipRect(QRectF(0.0, 0.0, sweep_x, float(H)))
            painter.drawPixmap(0, 0, self._px_sung[li])


# ── Settings dialog ────
class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None, on_preview=None):
        super().__init__(parent)
        self.setWindowTitle("furi-lrc 設定")
        self.cfg = dict(cfg)
        self._on_preview = on_preview
        if _MENU_FONT:
            self.setFont(_MENU_FONT)
        self._build()
        self._connect_preview()

    def _connect_preview(self):
        if not self._on_preview:
            return
        self.font_size_jp.valueChanged.connect(self._preview)
        self.font_size_rt.valueChanged.connect(self._preview)
        self.font_size_zh.valueChanged.connect(self._preview)
        self.spacing_rt.valueChanged.connect(self._preview)
        self.spacing_zh.valueChanged.connect(self._preview)
        self.opacity.valueChanged.connect(self._preview)
        self.hide_pause.toggled.connect(self._preview)
        self.unlock_zone.valueChanged.connect(self._preview)
        self.text_shadow.textChanged.connect(self._preview)

    def _preview(self, *_):
        if self._on_preview:
            self._on_preview(self.result_cfg())

    def _build(self):
        outer = QVBoxLayout(self)
        tabs  = QTabWidget()
        outer.addWidget(tabs)

        w1  = QWidget()
        f1  = QFormLayout(w1)
        self.font_jp_w    = self._font_row(self.cfg["font_jp"])
        self.font_zh_w    = self._font_row(self.cfg["font_zh"])
        self.font_size_jp = QSpinBox(); self.font_size_jp.setRange(8, 96); self.font_size_jp.setSuffix(" px"); self.font_size_jp.setValue(self.cfg["font_size_jp"])
        self.font_size_rt = QSpinBox(); self.font_size_rt.setRange(6, 72); self.font_size_rt.setSuffix(" px"); self.font_size_rt.setValue(self.cfg.get("font_size_rt", max(8, self.cfg["font_size_jp"] // 2)))
        self.font_size_zh = QSpinBox(); self.font_size_zh.setRange(8, 96); self.font_size_zh.setSuffix(" px"); self.font_size_zh.setValue(self.cfg["font_size_zh"])
        self.spacing_rt   = QSpinBox(); self.spacing_rt.setRange(-30, 60); self.spacing_rt.setSuffix(" px"); self.spacing_rt.setValue(self.cfg.get("spacing_rt", 2))
        self.spacing_zh   = QSpinBox(); self.spacing_zh.setRange(-60, 60); self.spacing_zh.setSuffix(" px"); self.spacing_zh.setValue(self.cfg["spacing_zh"])
        # 設定画面の表示名は日本語で統一すること。
        f1.addRow("振り仮名サイズ", self.font_size_rt)
        f1.addRow("振り仮名と日本語の間隔", self.spacing_rt)
        f1.addRow("日本語フォント",      self.font_jp_w)
        f1.addRow("中国語フォント",         self.font_zh_w)
        f1.addRow("日本語フォントサイズ", self.font_size_jp)
        f1.addRow("中国語フォントサイズ",   self.font_size_zh)
        f1.addRow("日中間距(px)",         self.spacing_zh)
        tabs.addTab(w1, "表示")
        w2  = QWidget()
        f2  = QFormLayout(w2)
        self.btn_sung   = self._color_btn(self.cfg["color_sung"])
        self.btn_unsung = self._color_btn(self.cfg["color_unsung"])
        self.btn_zh     = self._color_btn(self.cfg["color_zh"])
        self.opacity    = QDoubleSpinBox(); self.opacity.setRange(0.1, 1.0); self.opacity.setSingleStep(0.05); self.opacity.setValue(self.cfg["opacity"])
        self.text_shadow = QLineEdit(self.cfg.get("text_shadow", ""))
        self.text_shadow.setPlaceholderText("例: 2px 2px 8px rgba(0,0,0,0.85)  ※空欄で無効")
        self.text_shadow.setMinimumWidth(260)
        f2.addRow("歌唱色",         self.btn_sung)
        f2.addRow("未歌唱色",       self.btn_unsung)
        f2.addRow("訳文色",         self.btn_zh)
        f2.addRow("透明度",         self.opacity)
        f2.addRow("テキストシャドウ", self.text_shadow)
        tabs.addTab(w2, "色彩")

        w3  = QWidget()
        f3  = QFormLayout(w3)
        self.hide_pause  = QCheckBox(); self.hide_pause.setChecked(self.cfg["hide_on_pause"])
        self.unlock_zone = QSpinBox()
        self.unlock_zone.setRange(20, 200)
        self.unlock_zone.setSuffix(" px")
        self.unlock_zone.setValue(self.cfg.get("unlock_zone", 48))
        f3.addRow("停止時に隠す",       self.hide_pause)
        f3.addRow("解錠ゾーンサイズ",   self.unlock_zone)
        tabs.addTab(w3, "動作")

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _color_btn(self, color: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedSize(80, 24)
        btn._color = color
        btn.setStyleSheet(f"background:{color};")
        def pick():
            c = QColorDialog.getColor(QColor(btn._color), self)
            if c.isValid():
                btn._color = c.name()
                btn.setStyleSheet(f"background:{btn._color};")
                self._preview()
        btn.clicked.connect(pick)
        return btn

    def _font_row(self, path: str) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        edit = QLineEdit(path)
        edit.setReadOnly(True)
        edit.setMinimumWidth(220)
        btn = QPushButton("…")
        btn.setFixedWidth(28)
        def browse():
            p, _ = QFileDialog.getOpenFileName(
                self, "フォントファイルを選択",
                str(FONTS_DIR),
                "Font Files (*.ttf *.otf *.ttc *.woff2 *.woff)"
            )
            if p:
                edit.setText(p)
        btn.clicked.connect(browse)
        lay.addWidget(edit)
        lay.addWidget(btn)
        row._edit = edit
        return row

    def result_cfg(self) -> dict:
        return {
            **self.cfg,
            "font_jp":       self.font_jp_w._edit.text(),
            "font_zh":       self.font_zh_w._edit.text(),
            "font_size_jp":  self.font_size_jp.value(),
            "font_size_rt":  self.font_size_rt.value(),
            "font_size_zh":  self.font_size_zh.value(),
            "spacing_rt":    self.spacing_rt.value(),
            "spacing_zh":    self.spacing_zh.value(),
            "color_sung":    self.btn_sung._color,
            "color_unsung":  self.btn_unsung._color,
            "color_zh":      self.btn_zh._color,
            "opacity":       self.opacity.value(),
            "hide_on_pause": self.hide_pause.isChecked(),
            "unlock_zone":   self.unlock_zone.value(),
            "text_shadow":   self.text_shadow.text(),
        }


# ── Transparent mouse overlay ──
class _MouseOverlay(QWidget):
    def __init__(self, win):
        super().__init__(win)
        self._win = win
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

    def enterEvent(self, e):
        if not self._win._locked:
            self._win._hovering = True
            self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._win._hovering = False
        if not self._win._locked:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        w = self._win
        painter = QPainter(self)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 1))
        painter.drawRect(self.rect())
        if not (w._hovering and not w._locked) and not w._zone_preview:
            return
        if w._hovering and not w._locked:
            pen = QPen(QColor(255, 255, 255, 38))
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawRect(0, 0, self.width() - 1, self.height() - 1)
        if w._zone_preview:
            zone = w._preview_zone_size
            x0   = self.width() - zone
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 200, 0, 50))
            painter.drawRect(x0, 0, zone, zone)
            pen2 = QPen(QColor(255, 200, 0, 220))
            pen2.setWidth(2)
            painter.setPen(pen2)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(x0, 0, zone - 1, zone - 1)

    def mousePressEvent(self, e):   QApplication.sendEvent(self._win, e)
    def mouseMoveEvent(self, e):    QApplication.sendEvent(self._win, e)
    def mouseReleaseEvent(self, e): QApplication.sendEvent(self._win, e)
    def contextMenuEvent(self, e):  QApplication.sendEvent(self._win, e)
    def dragEnterEvent(self, e):    self._win.dragEnterEvent(e)
    def dropEvent(self, e):         self._win.dropEvent(e)


# ── Screen geometry helpers ──

def _fit_to_screen(x: int, y: int, w: int, h: int) -> Tuple[int, int, int, int]:
    screens = QApplication.screens()
    if not screens:
        return x, y, w, h
    screen = next(
        (s for s in screens if s.geometry().contains(QPoint(x, y))),
        QApplication.primaryScreen(),
    )
    avail = screen.availableGeometry()
    KEEP  = 80
    nx = max(avail.x() + KEEP - w, min(x, avail.right()  - KEEP))
    ny = max(avail.y(),             min(y, avail.bottom() - KEEP))
    return nx, ny, w, h


def _screen_relative_defaults() -> dict:
    screen = QApplication.primaryScreen()
    if not screen:
        return {}
    g = screen.availableGeometry()
    w = max(480, min(g.width() // 2, 900))
    h = max(180, g.height() // 5)
    return dict(x=g.x() + (g.width() - w) // 2, y=g.bottom() - h - 80, w=w, h=h)


# ── Main window ──
class LyricWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg              = load_config()
        if not CONFIG_PATH.exists():
            self.cfg.update(_screen_relative_defaults())
        self._locked          = self.cfg["locked"]
        self._drag_pos        = QPoint()
        self._is_playing      = False
        self._last_sec        = 0.0
        self._last_wall       = time.monotonic()
        self._fade_anim       = None
        self._unlock_visible  = False
        self._hovering        = False
        self._resize_dir      = (0, 0)
        self._resize_start_geo = None
        self._resize_start_pos = None
        self._zone_preview        = False
        self._preview_zone_size   = self.cfg.get("unlock_zone", 48)

        self._geo_save_timer = QTimer(self)
        self._geo_save_timer.setSingleShot(True)
        self._geo_save_timer.setInterval(600)
        self._geo_save_timer.timeout.connect(self._save_geometry)

        self._setup_window()
        self._setup_zone_btn()
        self._setup_canvas()
        self._setup_smtc()

        if self.cfg["lyrics_path"]:
            self._load_lyrics(self.cfg["lyrics_path"])

    # ── Setup ──

    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(self.cfg["opacity"])
        x, y, w, h = _fit_to_screen(
            self.cfg["x"], self.cfg["y"], self.cfg["w"], self.cfg["h"]
        )
        self.setGeometry(x, y, w, h)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)

    def _setup_zone_btn(self):
        btn = QPushButton(self)
        btn.setFixedSize(30, 30)
        btn.setStyleSheet(
            "QPushButton{"
            "  background:rgba(0,0,0,150); color:white; border:none;"
            "  border-radius:6px; font-size:15px;"
            "}"
            "QPushButton:hover{ background:rgba(60,60,60,200); }"
        )
        btn.clicked.connect(self._toggle_lock)
        btn.hide()
        btn.raise_()
        self._zone_btn = btn
        self._update_zone_btn_icon()
        self._reposition_zone_btn()

    def _update_zone_btn_icon(self):
        self._zone_btn.setText("🔓" if self._locked else "🔒")

    def _reposition_zone_btn(self):
        zone   = self.cfg.get("unlock_zone", 48)
        bw, bh = self._zone_btn.width(), self._zone_btn.height()
        x = self.width()  - zone + (zone - bw) // 2
        y = (zone - bh) // 2
        self._zone_btn.move(x, y)

    def _set_clickthrough(self, enabled: bool):
        hwnd  = int(self.winId())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        if enabled:
            style |=  0x00080020
        else:
            style &= ~0x00000020
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)

    def _setup_cursor_timer(self):
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(80)
        self._cursor_timer.timeout.connect(self._check_unlock_zone)
        self._cursor_timer.start()

    def _check_unlock_zone(self):
        geo    = self.geometry()
        cursor = QCursor.pos()
        ZONE   = self.cfg.get("unlock_zone", 48)
        in_zone = (
            geo.right() - ZONE <= cursor.x() <= geo.right() and
            geo.top()          <= cursor.y() <= geo.top() + ZONE
        )
        if in_zone and not self._unlock_visible:
            self._unlock_visible = True
            if self._locked:
                self._set_clickthrough(False)
            self._zone_btn.raise_()
            self._zone_btn.show()
        elif not in_zone and self._unlock_visible:
            self._unlock_visible = False
            self._zone_btn.hide()
            if self._locked:
                self._set_clickthrough(True)

    def _setup_canvas(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.canvas = LyricsCanvas(self)
        self.canvas.apply_cfg(self.cfg)
        layout.addWidget(self.canvas)

        self._overlay = _MouseOverlay(self)
        self._overlay.setGeometry(0, 0, self.width(), self.height())
        self._zone_btn.raise_()
        self._setup_cursor_timer()

    def _setup_smtc(self):
        self.smtc = SMTCWorker()
        self.smtc.time_updated.connect(self._on_time_updated)
        self.smtc.track_changed.connect(self._on_track_changed)
        self.smtc.start()

    # ── Lyrics loading ──

    def _load_lyrics(self, path: str):
        p = Path(path)
        if not p.exists():
            return
        try:
            raw   = json.loads(p.read_text("utf-8"))
            lines = raw.get("lines", raw) if isinstance(raw, dict) else raw
            self.cfg["lyrics_path"] = str(p)
            self.canvas.load_lyrics(lines)
        except Exception as e:
            print(f"[furi-lrc] load error: {e}", file=sys.stderr)

    # ── SMTC callbacks ──

    def _on_time_updated(self, sec: float, playing: bool):
        was_playing     = self._is_playing
        self._last_sec  = sec
        self._last_wall = time.monotonic()
        self._is_playing = playing

        # Phase 4: hard sync only on state transitions; soft-correct otherwise
        force_hard = (playing != was_playing)
        self.canvas.sync_time(sec, playing, force_hard=force_hard)

        if self.cfg["hide_on_pause"]:
            if playing and not was_playing:
                self._fade(show=True)
            elif not playing and was_playing:
                self._fade(show=False)

    def _on_track_changed(self, title: str, _: str):
        if not self.cfg["lyrics_path"]:
            return
        base = Path(self.cfg["lyrics_path"]).parent
        for p in base.glob("*.json"):
            if title.lower() in p.stem.lower():
                self._load_lyrics(str(p))
                return

    # ── Animation ──

    def _fade(self, show: bool):
        if self._fade_anim:
            self._fade_anim.stop()
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setDuration(400)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(self.cfg["opacity"] if show else 0.06)
        self._fade_anim.start()
        if show:
            self.show()

    # ── Resize helpers ──

    _RESIZE_MARGIN = 8

    def _get_resize_dir(self, pos):
        x, y, w, h, m = pos.x(), pos.y(), self.width(), self.height(), self._RESIZE_MARGIN
        dx = -1 if x < m else (1 if x > w - m else 0)
        dy = -1 if y < m else (1 if y > h - m else 0)
        return dx, dy

    def _cursor_for_dir(self, dx, dy):
        if dx == 0 and dy == 0:
            return Qt.CursorShape.OpenHandCursor
        if dx != 0 and dy == 0:
            return Qt.CursorShape.SizeHorCursor
        if dx == 0 and dy != 0:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.SizeBDiagCursor if dx * dy > 0 else Qt.CursorShape.SizeFDiagCursor

    def _do_resize(self, global_pos):
        dx, dy = self._resize_dir
        geo    = self._resize_start_geo
        ddx    = global_pos.x() - self._resize_start_pos.x()
        ddy    = global_pos.y() - self._resize_start_pos.y()
        MIN_W, MIN_H = 200, 100
        nx, ny, nw, nh = geo.x(), geo.y(), geo.width(), geo.height()
        if dx == -1: nw -= ddx; nx += ddx
        elif dx ==  1: nw += ddx
        if dy == -1: nh -= ddy; ny += ddy
        elif dy ==  1: nh += ddy
        if nw < MIN_W:
            if dx == -1: nx = geo.right() - MIN_W + 1
            nw = MIN_W
        if nh < MIN_H:
            if dy == -1: ny = geo.bottom() - MIN_H + 1
            nh = MIN_H
        self.setGeometry(nx, ny, nw, nh)

    # ── Drag / resize ──

    def mousePressEvent(self, e):
        if self._locked or e.button() != Qt.MouseButton.LeftButton:
            return
        dx, dy = self._get_resize_dir(e.position().toPoint())
        if dx != 0 or dy != 0:
            self._resize_dir       = (dx, dy)
            self._resize_start_geo = self.geometry()
            self._resize_start_pos = e.globalPosition().toPoint()
            self._drag_pos = QPoint()
        else:
            self._drag_pos   = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._resize_dir = (0, 0)
            self._overlay.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._locked:
            return
        pos = e.position().toPoint()
        if e.buttons() & Qt.MouseButton.LeftButton:
            if self._resize_dir != (0, 0) and self._resize_start_geo:
                self._do_resize(e.globalPosition().toPoint())
            elif not self._drag_pos.isNull():
                self.move(e.globalPosition().toPoint() - self._drag_pos)
        else:
            dx, dy = self._get_resize_dir(pos)
            self._overlay.setCursor(self._cursor_for_dir(dx, dy))

    def mouseReleaseEvent(self, e):
        self._drag_pos         = QPoint()
        self._resize_dir       = (0, 0)
        self._resize_start_geo = None
        self._resize_start_pos = None
        if not self._locked:
            dx, dy = self._get_resize_dir(e.position().toPoint())
            self._overlay.setCursor(self._cursor_for_dir(dx, dy))

    # ── Drag & drop JSON ──

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            if any(u.toLocalFile().lower().endswith(".json") for u in e.mimeData().urls()):
                e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith(".json"):
                self._load_lyrics(p)
                break

    # ── Context menu ──

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        if _MENU_FONT:
            menu.setFont(_MENU_FONT)
        a = menu.addAction("歌詞を開く…")
        a.triggered.connect(self._open_file)
        menu.addSeparator()
        a = menu.addAction("ロック解除" if self._locked else "位置をロック")
        a.triggered.connect(self._toggle_lock)
        a = menu.addAction("設定…")
        a.triggered.connect(self._show_settings)
        menu.addSeparator()
        a = menu.addAction("終了")
        a.triggered.connect(QApplication.instance().quit)
        menu.exec(e.globalPos())

    def _open_file(self):
        dlg = QFileDialog(self, "歌詞ファイルを選択")
        dlg.setNameFilter("JSON Files (*.json)")
        dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.resize(560, 380)
        if dlg.exec():
            files = dlg.selectedFiles()
            if files:
                self._load_lyrics(files[0])

    def _toggle_lock(self):
        self._locked          = not self._locked
        self.cfg["locked"]    = self._locked
        self._unlock_visible  = False
        self._zone_btn.hide()
        self._update_zone_btn_icon()
        if self._locked:
            self._hovering         = False
            self._resize_dir       = (0, 0)
            self._resize_start_geo = None
            self._resize_start_pos = None
            self._drag_pos         = QPoint()
            self._overlay.setCursor(Qt.CursorShape.ArrowCursor)
            self._overlay.update()
            self._set_clickthrough(True)
        else:
            self._set_clickthrough(False)

    def _show_settings(self):
        original_cfg = dict(self.cfg)

        def on_preview(preview_cfg: dict):
            self.setWindowOpacity(preview_cfg["opacity"])
            self.canvas.apply_cfg(preview_cfg)
            self._preview_zone_size = preview_cfg.get("unlock_zone", 48)
            self._zone_preview      = True
            self._overlay.update()
            self.cfg["unlock_zone"] = self._preview_zone_size
            self._reposition_zone_btn()

        dlg = SettingsDialog(self.cfg, self, on_preview=on_preview)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg = dlg.result_cfg()
            self.setWindowOpacity(self.cfg["opacity"])
            self.canvas.apply_cfg(self.cfg)
            save_config(self.cfg)
        else:
            self.cfg["unlock_zone"] = original_cfg.get("unlock_zone", 48)
            self.setWindowOpacity(original_cfg["opacity"])
            self.canvas.apply_cfg(original_cfg)
        self._zone_preview = False
        self._reposition_zone_btn()
        self._overlay.update()

    def showEvent(self, e):
        super().showEvent(e)
        if self._locked:
            self._set_clickthrough(True)

    def moveEvent(self, e):
        super().moveEvent(e)
        self._geo_save_timer.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reposition_zone_btn()
        self._overlay.setGeometry(0, 0, self.width(), self.height())
        self._geo_save_timer.start()

    def _save_geometry(self):
        self.cfg.update(x=self.x(), y=self.y(), w=self.width(), h=self.height())
        save_config(self.cfg)

    def closeEvent(self, e):
        self.cfg.update(x=self.x(), y=self.y(), w=self.width(), h=self.height(), locked=self._locked)
        save_config(self.cfg)
        self.smtc.stop()
        super().closeEvent(e)


# ── Entry ──
_MENU_FONT: QFont | None = None


def _load_menu_font() -> None:
    global _MENU_FONT
    path     = str(FONTS_DIR / "NotoSansJP-Regular.ttf")
    fid      = QFontDatabase.addApplicationFont(path)
    families = QFontDatabase.applicationFontFamilies(fid)
    if families:
        _MENU_FONT = QFont(families[0])


def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("furi-lrc")
    _load_menu_font()
    win = LyricWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
