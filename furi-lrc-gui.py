#!/usr/bin/env python3
"""
furi-lrc-gui — lyrics editor for the furi-lrc karaoke format
=============================================================
JP markup syntax (use in the JP field):
  {漢字|かんじ}  — ruby (furigana) annotation
  plain text    — no furigana
  example: {東京|とうきょう}へ{行|い}く

JSON output is compatible with furi-lrc.py.

Requirements (conda rubi):
  pip install PyQt6-Qt6Multimedia   # audio playback
  pip install librosa numpy         # waveform display (optional)

Keyboard shortcuts:
  Space          — play / pause
  Ctrl+S         — save
  Ctrl+Z / Y     — undo / redo
  J / L          — -500 ms / +500 ms
  , / .          — -100 ms / +100 ms
  Enter (tap)    — mark current line start (tap mode)
  Escape         — exit tap / unit-tap mode
"""

import sys
import json
import re
import copy
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem,
    QLabel, QLineEdit, QPushButton, QToolButton,
    QToolBar, QFileDialog, QMessageBox,
    QDialog, QDialogButtonBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSlider, QFrame, QSizePolicy,
    QScrollArea, QTextEdit, QGroupBox, QMenu, QComboBox,
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QUrl, QSize, QPoint,
)
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QKeySequence, QShortcut,
    QPainterPath, QAction, QLinearGradient, QFontMetrics, QIcon,
    QFontDatabase, QPalette,
)

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    HAS_MULTIMEDIA = True
except ImportError:
    HAS_MULTIMEDIA = False
    QMediaPlayer = None
    QAudioOutput = None


# ══════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════

def ms_to_str(ms: int) -> str:
    ms = max(0, int(ms))
    frac = ms % 1000
    s = ms // 1000
    m, s = divmod(s, 60)
    return f"{m:02d}:{s:02d}.{frac:03d}"


def str_to_ms(text: str) -> Optional[int]:
    text = text.strip()
    m = re.fullmatch(r'(\d+):(\d{2})\.(\d{1,3})', text)
    if m:
        base = int(m.group(1)) * 60000 + int(m.group(2)) * 1000
        return base + int(m.group(3).ljust(3, '0'))
    m = re.fullmatch(r'(\d+):(\d{2})', text)
    if m:
        return int(m.group(1)) * 60000 + int(m.group(2)) * 1000
    try:
        return int(float(text) * 1000)
    except ValueError:
        return None


_RUBY_RE = re.compile(r'\{([^|{}]+)\|([^{}]+)\}')

# Populated in main() after QFontDatabase.addApplicationFont
_NOTO_JP = ""  # NotoSansJP family name


def parse_jp_markup(text: str, t_start: int = 0, t_end: int = 5000) -> list:
    """
    Parse '{base|reading}plain' into furi-lrc segment list.
    Timing is spread linearly between t_start..t_end.
    """
    tokens: list = []
    pos = 0
    for m in _RUBY_RE.finditer(text):
        if m.start() > pos and text[pos:m.start()]:
            tokens.append(('plain', list(text[pos:m.start()])))
        tokens.append(('ruby', m.group(1), list(m.group(2))))
        pos = m.end()
    tail = text[pos:]
    if tail:
        tokens.append(('plain', list(tail)))

    total = sum(len(t[2]) if t[0] == 'ruby' else len(t[1]) for t in tokens)
    if total == 0:
        return []

    dur = max(1, t_end - t_start)
    udur = dur / total
    segs: list = []
    idx = 0

    for tok in tokens:
        if tok[0] == 'ruby':
            _, base, chars = tok
            units = []
            for k in chars:
                s = int(t_start + idx * udur)
                e = int(t_start + (idx + 1) * udur)
                units.append({'k': k, 's': s, 'e': e})
                idx += 1
            segs.append({'base': base, 'ruby': True, 'units': units})
        else:
            _, chars = tok
            units = []
            for k in chars:
                s = int(t_start + idx * udur)
                e = int(t_start + (idx + 1) * udur)
                units.append({'k': k, 's': s, 'e': e})
                idx += 1
            segs.append({'ruby': False, 'units': units})

    return segs


def segments_to_markup(segs: list) -> str:
    parts = []
    for seg in segs:
        if seg.get('ruby'):
            reading = ''.join(u['k'] for u in seg.get('units', []))
            parts.append(f"{{{seg['base']}|{reading}}}")
        else:
            parts.append(''.join(u['k'] for u in seg.get('units', [])))
    return ''.join(parts)


def merge_jp_timing(old_segs: list, new_segs: list,
                    t_start: int, t_end: int) -> list:
    """
    Preserve per-character timing when the JP markup is edited.

    Algorithm
    ---------
    1. Flatten both old and new segment lists into character sequences.
    2. Run LCS (Longest Common Subsequence) on the character strings to find
       which characters in the new text correspond to unchanged characters in
       the old text.
    3. Matched characters inherit the old (s, e) timestamps verbatim.
    4. Unmatched (new / replaced) characters are assigned timestamps by
       linearly interpolating between the surrounding matched anchors.
       Sentinel anchors at t_start and t_end cover the edges.
    """
    # Flatten old timing
    old_flat: list = []
    for seg in old_segs:
        for unit in seg.get('units', []):
            old_flat.append((unit['k'], unit['s'], unit['e']))

    new_segs = copy.deepcopy(new_segs)
    new_flat: list = []          # [(seg_idx, unit_idx, char)]
    for si, seg in enumerate(new_segs):
        for ui, unit in enumerate(seg.get('units', [])):
            new_flat.append((si, ui, unit['k']))

    no, nn = len(old_flat), len(new_flat)
    if no == 0 or nn == 0:
        return new_segs

    # ── LCS DP ──
    dp = [[0] * (nn + 1) for _ in range(no + 1)]
    for i in range(1, no + 1):
        oc = old_flat[i - 1][0]
        for j in range(1, nn + 1):
            if oc == new_flat[j - 1][2]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # Backtrack → matched (new_j, old_i) pairs (0-indexed)
    matches: list = []
    i, j = no, nn
    while i > 0 and j > 0:
        if old_flat[i - 1][0] == new_flat[j - 1][2]:
            matches.append((j - 1, i - 1))
            i -= 1
            j -= 1
        elif dp[i - 1][j] >= dp[i][j - 1]:
            i -= 1
        else:
            j -= 1
    matches.reverse()

    # timing[j] = (s, e) for matched new positions, None for unmatched
    timing: list = [None] * nn
    for nj, oi in matches:
        timing[nj] = (old_flat[oi][1], old_flat[oi][2])

    # ── Interpolate unmatched positions ──
    # Build anchor list with sentinels at both ends
    anchors: list = [(-1, t_start, t_start)]
    for nj in range(nn):
        if timing[nj] is not None:
            anchors.append((nj, timing[nj][0], timing[nj][1]))
    anchors.append((nn, t_end, t_end))

    for ai in range(len(anchors) - 1):
        lj, _ls, le = anchors[ai]      # left anchor: position, s, e
        rj, rs, _re = anchors[ai + 1]  # right anchor
        gap = list(range(lj + 1, rj))  # unmatched indices between the two anchors
        if not gap:
            continue
        dur = max(1, rs - le)
        udur = dur / len(gap)
        for k, gj in enumerate(gap):
            timing[gj] = (int(le + k * udur), int(le + (k + 1) * udur))

    # Write timing back into new_segs
    for nj, (si, ui, _) in enumerate(new_flat):
        if timing[nj] is not None:
            new_segs[si]['units'][ui]['s'] = timing[nj][0]
            new_segs[si]['units'][ui]['e'] = timing[nj][1]

    return new_segs


def line_end_ms(lines: list, idx: int) -> int:
    # Prefer explicitly stored end time
    if 0 <= idx < len(lines):
        explicit = lines[idx].get('end')
        if explicit is not None:
            return explicit
    if idx + 1 < len(lines):
        return lines[idx + 1]['start']
    return lines[idx]['start'] + 5000 if lines else 5000


# ══════════════════════════════════════════════════════════════
#  LRC import / export helpers
# ══════════════════════════════════════════════════════════════

_LRC_LINE_RE = re.compile(r'\[(\d{1,3}):(\d{2})\.(\d{1,3})\](.*)')


def parse_lrc(text: str) -> list:
    """Parse a standard LRC file into a furi-lrc line list."""
    raw: list = []
    for raw_line in text.splitlines():
        m = _LRC_LINE_RE.match(raw_line.strip())
        if m:
            ms = (int(m.group(1)) * 60000
                  + int(m.group(2)) * 1000
                  + int(m.group(3).ljust(3, '0')))
            raw.append({'start': ms, '_text': m.group(4).strip()})
    raw.sort(key=lambda x: x['start'])
    lines: list = []
    for i, r in enumerate(raw):
        t_start = r['start']
        t_end = raw[i + 1]['start'] if i + 1 < len(raw) else t_start + 5000
        segs = parse_jp_markup(r['_text'], t_start, t_end)
        lines.append({'start': t_start, 'jp': segs, 'zh': ''})
    return lines


def export_lrc(lines: list) -> str:
    """Export a furi-lrc line list to standard LRC format."""
    parts: list = []
    for line in lines:
        ms = line['start']
        mi = ms // 60000
        sec = (ms % 60000) // 1000
        cs = (ms % 1000) // 10
        markup = segments_to_markup(line.get('jp', []))
        plain = re.sub(r'\{([^|{}]+)\|[^{}]+\}', r'\1', markup)
        parts.append(f"[{mi:02d}:{sec:02d}.{cs:02d}]{plain}")
    return '\n'.join(parts)


# ══════════════════════════════════════════════════════════════
#  Background waveform loader
# ══════════════════════════════════════════════════════════════

class WaveformLoader(QThread):
    finished = pyqtSignal(object, int)   # (ndarray or None, sample_rate)

    def __init__(self, path: str):
        super().__init__()
        self._path = path

    def run(self):
        try:
            import numpy as np
            import librosa  # type: ignore
            y, sr = librosa.load(self._path, sr=None, mono=True, res_type='kaiser_fast')
            self.finished.emit(y, int(sr))
            return
        except Exception:
            pass
        try:
            import numpy as np
            import soundfile as sf  # type: ignore
            y, sr = sf.read(self._path, always_2d=False)
            if y.ndim > 1:
                y = y.mean(axis=1)
            self.finished.emit(y.astype('float32'), int(sr))
            return
        except Exception:
            pass
        self.finished.emit(None, 0)


# ══════════════════════════════════════════════════════════════
#  Waveform display widget
# ══════════════════════════════════════════════════════════════

class WaveformWidget(QWidget):
    seek_requested = pyqtSignal(int)          # ms
    line_time_changed = pyqtSignal(int, int)  # line_idx, new_ms (during drag)
    drag_start = pyqtSignal(int)              # line_idx drag began
    drag_end = pyqtSignal()                   # drag released

    def __init__(self):
        super().__init__()
        self._samples = None
        self._sr = 44100
        self._duration_ms = 0
        self._position_ms = 0
        self._lines: list = []
        self._sel_idx = -1
        self._zoom = 1.0
        self._scroll_ms = 0.0
        self._hover_ms = -1
        self._envelope = None
        self._drag_line_idx = -1       # index of line marker being dragged
        self._hover_marker_idx = -1    # index of marker under cursor
        self.setMinimumHeight(80)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.setToolTip("左クリック: シーク  右ドラッグ: 歌詞マーカー移動  スクロール: 左右移動  Ctrl+スクロール: ズーム")

    # ─── public setters ───

    def set_audio(self, samples, sr: int):
        self._samples = samples
        self._sr = sr
        self._envelope = None
        self._zoom = 1.0
        self._scroll_ms = 0.0
        if samples is not None:
            self._duration_ms = int(len(samples) / sr * 1000)
        self._build_envelope()
        self.update()

    def set_duration(self, ms: int):
        if ms > 0 and self._samples is None:
            self._duration_ms = ms
        self.update()

    def set_position(self, ms: int):
        self._position_ms = ms
        v = self._vis_ms()
        lo, hi = self._scroll_ms, self._scroll_ms + v
        if ms < lo or ms > hi * 0.92:
            self._scroll_ms = max(0.0, ms - v * 0.08)
        self.update()

    def set_lines(self, lines: list, sel: int = -1):
        self._lines = lines
        self._sel_idx = sel
        self.update()

    # ─── internal ───

    def _build_envelope(self):
        try:
            import numpy as np
            s = self._samples
            n = 2000
            chunk = max(1, len(s) // n)
            end = (len(s) // chunk) * chunk
            arr = s[:end].reshape(-1, chunk)
            env = np.abs(arr).max(axis=1).astype('float32')
            mx = env.max()
            if mx > 0:
                env /= mx
            self._envelope = env
        except Exception:
            self._envelope = None

    def _vis_ms(self) -> float:
        return self._duration_ms / self._zoom if self._duration_ms > 0 else 60000.0

    def _ms_to_x(self, ms) -> float:
        v = self._vis_ms()
        return (float(ms) - self._scroll_ms) / v * self.width()

    def _x_to_ms(self, x) -> int:
        v = self._vis_ms()
        w = self.width() or 1
        return int(self._scroll_ms + float(x) / w * v)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()

        p.fillRect(0, 0, w, h, QColor(238, 242, 252))

        if self._envelope is not None:
            self._paint_env(p, w, h)
        else:
            p.setPen(QPen(QColor(180, 188, 215), 1))
            p.drawLine(0, h // 2, w, h // 2)

        # Center line
        p.setPen(QPen(QColor(195, 202, 225), 1))
        p.drawLine(0, h // 2, w, h // 2)

        # Lyric line markers
        for i, line in enumerate(self._lines):
            x = int(self._ms_to_x(line['start']))
            if -2 < x < w + 2:
                if i == self._drag_line_idx:
                    p.setPen(QPen(QColor(190, 50, 190), 2))
                elif i == self._sel_idx:
                    p.setPen(QPen(QColor(180, 110, 0), 2))
                elif i == self._hover_marker_idx:
                    p.setPen(QPen(QColor(160, 60, 210, 220), 2))
                else:
                    p.setPen(QPen(QColor(30, 140, 80, 200), 1))
                p.drawLine(x, 0, x, h - 16)
                # Draw a grab handle for hovered / dragged marker
                if i in (self._hover_marker_idx, self._drag_line_idx):
                    p.save()
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(QBrush(QColor(190, 50, 190, 200)))
                    p.drawRect(x - 4, 0, 8, 8)
                    p.restore()

        # Playhead
        px = int(self._ms_to_x(self._position_ms))
        p.setPen(QPen(QColor(200, 40, 40), 2))
        p.drawLine(px, 0, px, h)

        # Hover crosshair
        if self._hover_ms >= 0:
            hx = int(self._ms_to_x(self._hover_ms))
            p.setPen(QPen(QColor(60, 60, 200, 90), 1, Qt.PenStyle.DashLine))
            p.drawLine(hx, 0, hx, h - 16)

        self._paint_ticks(p, w, h)

    def _paint_env(self, p, w, h):
        try:
            import numpy as np
        except ImportError:
            return
        env = self._envelope
        n = len(env)
        v = self._vis_ms()
        d = float(self._duration_ms)
        i0 = max(0, int(self._scroll_ms / d * n))
        i1 = min(n, int((self._scroll_ms + v) / d * n) + 1)
        if i1 <= i0:
            return
        chunk = env[i0:i1]
        n_pts = max(2, min(w, len(chunk)))
        idx = np.linspace(0, len(chunk) - 1, n_pts, dtype=int)
        pts = chunk[idx]
        mid = h // 2
        scale = float(mid - 4)
        x_scale = (w - 1) / max(1, n_pts - 1)  # map sample index → pixel x

        path = QPainterPath()
        for i, v_val in enumerate(pts):
            amp = float(v_val) * scale
            x = i * x_scale
            if i == 0:
                path.moveTo(x, mid - amp)
            else:
                path.lineTo(x, mid - amp)
        for i in range(n_pts - 1, -1, -1):
            path.lineTo(i * x_scale, mid + float(pts[i]) * scale)
        path.closeSubpath()

        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor(80, 140, 220, 130))
        grad.setColorAt(0.5, QColor(100, 165, 245, 190))
        grad.setColorAt(1.0, QColor(80, 140, 220, 130))
        p.fillPath(path, QBrush(grad))

        top = QPainterPath()
        for i, v_val in enumerate(pts):
            x, y = i * x_scale, mid - float(v_val) * scale
            if i == 0:
                top.moveTo(x, y)
            else:
                top.lineTo(x, y)
        p.setPen(QPen(QColor(55, 115, 210, 220), 1))
        p.drawPath(top)

    def _paint_ticks(self, p, w, h):
        v = self._vis_ms()
        if v <= 0:
            return
        for iv in (100, 250, 500, 1000, 2000, 5000, 10000, 30000, 60000):
            if w / v * iv >= 50:
                tick_ms = iv
                break
        else:
            tick_ms = 60000

        p.setPen(QPen(QColor(130, 138, 165), 1))
        font = QFont("Consolas", 7)
        p.setFont(font)
        fm = QFontMetrics(font)

        start = int(self._scroll_ms // tick_ms) * tick_ms
        t = start
        while t <= self._scroll_ms + v:
            x = int(self._ms_to_x(t))
            if 0 <= x <= w:
                p.drawLine(x, h - 16, x, h - 10)
                lbl = ms_to_str(t)
                if x + fm.horizontalAdvance(lbl) + 3 < w:
                    p.drawText(x + 2, h - 2, lbl)
            t += tick_ms

    def mousePressEvent(self, e):
        px = e.position().x()
        if e.button() == Qt.MouseButton.LeftButton:
            ms = max(0, min(self._duration_ms, self._x_to_ms(px)))
            self.seek_requested.emit(ms)
        elif e.button() == Qt.MouseButton.RightButton:
            for i, line in enumerate(self._lines):
                x = int(self._ms_to_x(line['start']))
                if abs(int(px) - x) <= 6:
                    self._drag_line_idx = i
                    self.drag_start.emit(i)
                    break

    def mouseMoveEvent(self, e):
        px = e.position().x()
        self._hover_ms = self._x_to_ms(px)

        # Update hover-marker index (closest marker within ±6 px)
        self._hover_marker_idx = -1
        for i, line in enumerate(self._lines):
            x = int(self._ms_to_x(line['start']))
            if abs(int(px) - x) <= 6:
                self._hover_marker_idx = i
                break

        if self._drag_line_idx >= 0 and (e.buttons() & Qt.MouseButton.RightButton):
            ms = max(0, min(self._duration_ms, self._x_to_ms(px)))
            self.line_time_changed.emit(self._drag_line_idx, ms)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif e.buttons() & Qt.MouseButton.LeftButton:
            ms = max(0, min(self._duration_ms, self._hover_ms))
            self.seek_requested.emit(ms)
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif self._hover_marker_idx >= 0:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)
        self.update()

    def leaveEvent(self, _):
        self._hover_ms = -1
        self._hover_marker_idx = -1
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.RightButton and self._drag_line_idx >= 0:
            self._drag_line_idx = -1
            self.drag_end.emit()
            self.update()

    def wheelEvent(self, e):
        delta = e.angleDelta().y()
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.3 if delta > 0 else 1 / 1.3
            cursor_ms = float(self._x_to_ms(e.position().x()))
            self._zoom = max(1.0, min(300.0, self._zoom * factor))
            v = self._vis_ms()
            offset = e.position().x() / (self.width() or 1) * v
            self._scroll_ms = max(0.0, cursor_ms - offset)
        else:
            shift = self._vis_ms() * 0.15 * (-1 if delta > 0 else 1)
            self._scroll_ms = max(0.0, self._scroll_ms + shift)
        self.update()


# ══════════════════════════════════════════════════════════════
#  Time-edit input
# ══════════════════════════════════════════════════════════════

class TimeEdit(QLineEdit):
    """QLineEdit that displays/accepts MM:SS.mmm times."""
    time_changed = pyqtSignal(int)   # ms

    def __init__(self, ms: int = 0):
        super().__init__()
        self.setFixedWidth(110)
        self.setPlaceholderText("00:00.000")
        self.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.set_ms(ms)
        self.editingFinished.connect(self._on_edit)
        self.setToolTip("形式: MM:SS.mmm  (例: 01:23.456)")

    def set_ms(self, ms: int):
        self.blockSignals(True)
        self.setText(ms_to_str(ms))
        self.blockSignals(False)

    def _on_edit(self):
        v = str_to_ms(self.text())
        if v is None:
            self.set_ms(0)
        else:
            self.set_ms(v)
            self.time_changed.emit(v)


# ══════════════════════════════════════════════════════════════
#  Unit tap dialog
# ══════════════════════════════════════════════════════════════

class UnitTapDialog(QDialog):
    """
    Re-time units by tapping Space in sync with slowed-down playback.

    Flow
    ----
    Space ①  → start playback from line_start at selected speed
    Space ②…N+1 → stamp start of unit[0..N-1]
                   (each tap also closes the previous unit)
    Space N+2    → stamp end of unit[N-1] → playback pauses, done
    OK / Cancel  → confirm / discard
    """
    _SPEEDS = [0.25, 0.5, 0.75, 1.0]
    _SPEED_LABELS = ["0.25×", "0.5×", "0.75×", "1.0×"]

    def __init__(self, segs: list, line_start: int, line_end: int,
                 player, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ユニット打拍")
        self.setModal(True)
        self.resize(640, 360)
        self._segs = copy.deepcopy(segs)
        self._player = player
        self._line_start = line_start
        self._line_end = line_end
        self._flat: list = []
        self._tap_idx = 0        # index of next unit to receive a start-time stamp
        self._phase = 'ready'    # 'ready' | 'tapping' | 'done'
        self._orig_rate = 1.0    # saved playback rate; restored on close
        self.result_segs: Optional[list] = None

        self._flatten()
        self._build_ui()

    def _flatten(self):
        for si, seg in enumerate(self._segs):
            for ui, unit in enumerate(seg.get('units', [])):
                self._flat.append({'si': si, 'ui': ui, 'k': unit['k'],
                                   's': unit['s'], 'e': unit['e']})

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── Speed selector ──
        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("再生速度 (打拍用):"))
        self._speed_combo = QComboBox()
        for lbl in self._SPEED_LABELS:
            self._speed_combo.addItem(lbl)
        self._speed_combo.setCurrentIndex(0)   # default 0.25×
        self._speed_combo.setFixedWidth(90)
        speed_row.addWidget(self._speed_combo)
        speed_row.addStretch()
        layout.addLayout(speed_row)

        # ── Instruction ──
        instr = QLabel(
            "Space ①: 再生開始  →  "
            "Space ②〜: 各文字の開始タイミングを打拍  →  "
            "最後の Space: 最後の文字の終了時間を確定"
        )
        instr.setWordWrap(True)
        layout.addWidget(instr)

        # ── Unit buttons ──
        self._unit_frame = QWidget()
        flow = QHBoxLayout(self._unit_frame)
        flow.setSpacing(4)
        flow.setContentsMargins(0, 0, 0, 0)
        self._unit_btns: list = []
        for info in self._flat:
            btn = QPushButton(info['k'])
            btn.setFixedSize(46, 46)
            btn.setFont(QFont(_NOTO_JP or "", 16))
            btn.setEnabled(False)
            flow.addWidget(btn)
            self._unit_btns.append(btn)
        flow.addStretch()
        layout.addWidget(self._unit_frame)

        # ── Tap button ──
        self._tap_btn = QPushButton("▶  再生開始  (Space)")
        self._tap_btn.setFixedHeight(52)
        self._tap_btn.setFont(QFont("", 14))
        self._tap_btn.setStyleSheet(
            "QPushButton{background:#2e8b4a;color:white;border-radius:6px;}"
            "QPushButton:hover{background:#39a85c;}"
        )
        self._tap_btn.clicked.connect(self._tap)
        layout.addWidget(self._tap_btn)

        # ── Status ──
        self._status = QLabel("→ Space を押して再生を開始してください")
        layout.addWidget(self._status)

        # ── OK / Cancel ──
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self._on_cancel)
        layout.addWidget(bb)

        # Space shortcut (scoped to this dialog only)
        sc = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        sc.activated.connect(self._tap)

    def _get_speed(self) -> float:
        return self._SPEEDS[self._speed_combo.currentIndex()]

    def _restore_player(self):
        if self._player and HAS_MULTIMEDIA:
            if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self._player.pause()
            self._player.setPlaybackRate(self._orig_rate)

    def _highlight(self, active_idx: int):
        for i, btn in enumerate(self._unit_btns):
            if i < active_idx:
                btn.setStyleSheet("background:#c8e8d5;color:#1a6035;")
            elif i == active_idx:
                btn.setStyleSheet("background:#e6a800;color:white;font-weight:bold;")
            else:
                btn.setStyleSheet("")

    # ── tap state machine ──

    def _tap(self):
        # ─── Phase: ready → start playback ───
        if self._phase == 'ready':
            if self._player and HAS_MULTIMEDIA:
                self._orig_rate = self._player.playbackRate()
                self._player.setPlaybackRate(self._get_speed())
                self._player.setPosition(self._line_start)
                self._player.play()
            self._phase = 'tapping'
            self._speed_combo.setEnabled(False)
            if not self._flat:
                self._phase = 'done'
                self._tap_btn.setEnabled(False)
                self._status.setText("（ユニットがありません）")
                return
            self._highlight(0)
            self._tap_btn.setText(f"⏱  第 1 拍  (Space)")
            self._tap_btn.setStyleSheet(
                "QPushButton{background:#2e6fa0;color:white;border-radius:6px;}"
                "QPushButton:hover{background:#3a88c0;}"
            )
            self._status.setText(
                f"→ 「{self._flat[0]['k']}」の開始時間を打拍  (1 / {len(self._flat)})"
            )
            return

        # ─── Phase: tapping ───
        if self._phase != 'tapping':
            return
        now = self._player.position() if (self._player and HAS_MULTIMEDIA) else self._line_start

        if self._tap_idx < len(self._flat):
            # Stamp start of current unit; close previous
            self._flat[self._tap_idx]['s'] = now
            if self._tap_idx > 0:
                self._flat[self._tap_idx - 1]['e'] = now
            self._tap_idx += 1

            if self._tap_idx < len(self._flat):
                self._highlight(self._tap_idx)
                self._tap_btn.setText(f"⏱  第 {self._tap_idx + 1} 拍  (Space)")
                self._status.setText(
                    f"→ 「{self._flat[self._tap_idx]['k']}」の開始時間を打拍"
                    f"  ({self._tap_idx + 1} / {len(self._flat)})"
                )
            else:
                # All starts recorded — waiting for end of last unit
                for btn in self._unit_btns:
                    btn.setStyleSheet("background:#ffe066;color:#6b4800;")
                self._tap_btn.setText("⏱  終了時間  (Space)")
                self._status.setText(
                    f"→ 最後の文字「{self._flat[-1]['k']}」の終了時間を打拍"
                    "  (省略可: そのまま OK)"
                )
        else:
            # Stamp end of last unit → session complete
            self._flat[-1]['e'] = now
            self._phase = 'done'
            self._restore_player()
            for btn in self._unit_btns:
                btn.setStyleSheet("background:#c8e8d5;color:#1a6035;")
            self._tap_btn.setText("✔  完了")
            self._tap_btn.setEnabled(False)
            self._status.setText("✔ 打拍完了。OK を押して確定してください。")

    # ── accept / cancel ──

    def _accept(self):
        self._restore_player()
        # Fallback: last unit end if user skipped the final tap
        if self._flat and self._phase == 'tapping' and self._tap_idx >= len(self._flat):
            self._flat[-1]['e'] = self._line_end
        # Write timing back to segs
        for info in self._flat:
            self._segs[info['si']]['units'][info['ui']]['s'] = info['s']
            self._segs[info['si']]['units'][info['ui']]['e'] = info['e']
        self.result_segs = self._segs
        self.accept()

    def _on_cancel(self):
        self._restore_player()
        self.reject()


# ══════════════════════════════════════════════════════════════
#  Line editor (right panel)
# ══════════════════════════════════════════════════════════════

class LineEditor(QWidget):
    line_changed = pyqtSignal()   # ask main to update data

    def __init__(self):
        super().__init__()
        self._current_line: Optional[dict] = None
        self._line_idx = -1
        self._all_lines: list = []
        self._player = None
        self._playing_row = -1   # row index currently highlighted by playhead
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Header
        self._header = QLabel("（行を選択してください）")
        self._header.setStyleSheet("color:#555;font-weight:bold;")
        layout.addWidget(self._header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#ccc;")
        layout.addWidget(sep)

        # Time row
        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("開始:"))
        self._time_edit = TimeEdit()
        self._time_edit.time_changed.connect(self._on_time_changed)
        time_row.addWidget(self._time_edit)
        self._set_now_btn = QPushButton("⏱")
        self._set_now_btn.setToolTip("再生中の時間を開始時間に設定 (T)")
        self._set_now_btn.clicked.connect(self._set_to_now)
        self._set_now_btn.setFixedWidth(32)
        time_row.addWidget(self._set_now_btn)
        time_row.addSpacing(10)
        time_row.addWidget(QLabel("終了:"))
        self._end_edit = TimeEdit()
        self._end_edit.setToolTip(
            "字幕の終了時間。未設定の場合は次の行の開始時間を使用します。"
        )
        self._end_edit.time_changed.connect(self._on_end_changed)
        time_row.addWidget(self._end_edit)
        self._set_end_now_btn = QPushButton("⏱")
        self._set_end_now_btn.setToolTip("再生中の時間を終了時間に設定")
        self._set_end_now_btn.clicked.connect(self._set_end_to_now)
        self._set_end_now_btn.setFixedWidth(32)
        time_row.addWidget(self._set_end_now_btn)
        time_row.addStretch()
        layout.addLayout(time_row)

        # JP markup
        jp_grp = QGroupBox("日本語 (JP)  ← {漢字|かんじ} 形式でルビ付与")
        jp_lay = QVBoxLayout(jp_grp)
        self._jp_edit = QTextEdit()
        self._jp_edit.setFixedHeight(60)
        self._jp_edit.setPlaceholderText("{東京|とうきょう}へ{行|い}く")
        self._jp_edit.setFont(QFont(_NOTO_JP or "", 13))
        self._jp_edit.setAcceptRichText(False)
        self._jp_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._jp_edit.customContextMenuRequested.connect(self._jp_context_menu)
        jp_lay.addWidget(self._jp_edit)

        # Ctrl+R shortcut for ruby annotation
        sc_ruby = QShortcut(QKeySequence("Ctrl+R"), self._jp_edit)
        sc_ruby.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc_ruby.activated.connect(self._insert_ruby)
        apply_jp_btn = QPushButton("↻ JP解析・ユニット表更新")
        apply_jp_btn.setToolTip("テキストを解析してユニット表を再生成します（既存の時間は上書きされます）")
        apply_jp_btn.clicked.connect(self._apply_jp)
        jp_lay.addWidget(apply_jp_btn)
        layout.addWidget(jp_grp)

        # ZH translation
        zh_grp = QGroupBox("中文訳 (ZH)")
        zh_lay = QVBoxLayout(zh_grp)
        self._zh_edit = QLineEdit()
        self._zh_edit.setPlaceholderText("中文翻译（省略可）")
        self._zh_edit.setFont(QFont("Microsoft YaHei", 12))
        self._zh_edit.editingFinished.connect(self._on_zh_changed)
        zh_lay.addWidget(self._zh_edit)
        layout.addWidget(zh_grp)

        # Unit table
        unit_grp = QGroupBox("ユニット時間 (ダブルクリックで編集)")
        unit_lay = QVBoxLayout(unit_grp)

        self._unit_table = QTableWidget(0, 4)
        self._unit_table.setHorizontalHeaderLabels(["文字", "種別", "開始 (ms)", "終了 (ms)"])
        self._unit_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._unit_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._unit_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._unit_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._unit_table.setColumnWidth(0, 60)
        self._unit_table.setColumnWidth(1, 60)
        self._unit_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._unit_table.setMinimumHeight(160)
        self._unit_table.itemChanged.connect(self._on_unit_cell_changed)
        unit_lay.addWidget(self._unit_table)

        tap_row = QHBoxLayout()
        self._tap_units_btn = QPushButton("🎯 ユニット打拍")
        self._tap_units_btn.setToolTip("再生しながら各ユニットの開始時間をSpaceで打拍")
        self._tap_units_btn.clicked.connect(self._open_unit_tap)
        tap_row.addWidget(self._tap_units_btn)

        self._dist_btn = QPushButton("⟺ 均等配分")
        self._dist_btn.setToolTip("開始〜終了時間の間にユニットを均等配分")
        self._dist_btn.clicked.connect(self._distribute_units)
        tap_row.addWidget(self._dist_btn)
        unit_lay.addLayout(tap_row)
        layout.addWidget(unit_grp)
        layout.addStretch()

    def set_player(self, player):
        self._player = player

    def set_line(self, line: Optional[dict], idx: int, all_lines: list):
        self._current_line = line
        self._line_idx = idx
        self._all_lines = all_lines
        self._refresh()

    def _refresh(self):
        if self._current_line is None:
            self._header.setText("（行を選択してください）")
            self._time_edit.set_ms(0)
            self._end_edit.set_ms(0)
            self._jp_edit.setPlainText("")
            self._zh_edit.setText("")
            self._unit_table.setRowCount(0)
            return

        self._header.setText(f"行 {self._line_idx + 1}  /  {len(self._all_lines)}")
        self._time_edit.set_ms(self._current_line['start'])
        self._end_edit.set_ms(
            self._current_line.get('end',
                                   line_end_ms(self._all_lines, self._line_idx))
        )
        self._jp_edit.setPlainText(segments_to_markup(self._current_line.get('jp', [])))
        self._zh_edit.setText(self._current_line.get('zh', ''))
        self._refresh_unit_table()

    def _refresh_unit_table(self):
        self._playing_row = -1   # table is being rebuilt; highlights are gone
        segs = self._current_line.get('jp', []) if self._current_line else []
        self._unit_table.blockSignals(True)
        self._unit_table.setRowCount(0)
        for seg in segs:
            is_ruby = seg.get('ruby', False)
            for unit in seg.get('units', []):
                row = self._unit_table.rowCount()
                self._unit_table.insertRow(row)
                char_item = QTableWidgetItem(unit['k'])
                char_item.setFlags(char_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                char_item.setFont(QFont(_NOTO_JP or "", 14))
                kind_item = QTableWidgetItem("ルビ" if is_ruby else "普通")
                kind_item.setFlags(kind_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                kind_item.setForeground(QColor("#1a6aaa") if is_ruby else QColor("#888"))
                s_item = QTableWidgetItem(str(unit['s']))
                e_item = QTableWidgetItem(str(unit['e']))
                self._unit_table.setItem(row, 0, char_item)
                self._unit_table.setItem(row, 1, kind_item)
                self._unit_table.setItem(row, 2, s_item)
                self._unit_table.setItem(row, 3, e_item)
        self._unit_table.blockSignals(False)

    # ── playhead highlight ──

    def update_playhead(self, ms: int):
        """Highlight the unit row whose time window contains ms.
        Called on every QMediaPlayer.positionChanged signal."""
        if self._current_line is None:
            self._clear_unit_highlight()
            return
        t_start = self._current_line['start']
        t_end = line_end_ms(self._all_lines, self._line_idx)
        # Only act when playhead is inside this line's range
        if ms < t_start or ms >= t_end:
            self._clear_unit_highlight()
            return
        # Find matching row (first unit whose [s, e) contains ms)
        new_row = -1
        row = 0
        for seg in self._current_line.get('jp', []):
            for unit in seg.get('units', []):
                if unit['s'] <= ms < unit['e']:
                    new_row = row
                row += 1
            if new_row >= 0:
                break
        if new_row == self._playing_row:
            return  # no change needed
        self._clear_unit_highlight()
        self._playing_row = new_row
        if new_row >= 0 and new_row < self._unit_table.rowCount():
            for col in range(self._unit_table.columnCount()):
                item = self._unit_table.item(new_row, col)
                if item:
                    item.setBackground(QColor(255, 200, 60, 200))
            self._unit_table.scrollToItem(
                self._unit_table.item(new_row, 0),
                QAbstractItemView.ScrollHint.EnsureVisible,
            )

    def _clear_unit_highlight(self):
        if 0 <= self._playing_row < self._unit_table.rowCount():
            for col in range(self._unit_table.columnCount()):
                item = self._unit_table.item(self._playing_row, col)
                if item:
                    item.setData(Qt.ItemDataRole.BackgroundRole, None)
        self._playing_row = -1

    def _on_time_changed(self, ms: int):
        if self._current_line is None:
            return
        self._current_line['start'] = ms
        self.line_changed.emit()

    def _on_end_changed(self, ms: int):
        if self._current_line is None:
            return
        self._current_line['end'] = ms
        self.line_changed.emit()

    def _set_to_now(self):
        if self._current_line is None:
            return
        ms = self._player.position() if (self._player and HAS_MULTIMEDIA) else 0
        self._current_line['start'] = ms
        self._time_edit.set_ms(ms)
        self.line_changed.emit()

    def _set_end_to_now(self):
        if self._current_line is None:
            return
        ms = self._player.position() if (self._player and HAS_MULTIMEDIA) else 0
        self._current_line['end'] = ms
        self._end_edit.set_ms(ms)
        self.line_changed.emit()

    def _on_zh_changed(self):
        if self._current_line is None:
            return
        self._current_line['zh'] = self._zh_edit.text()
        self.line_changed.emit()

    def _insert_ruby(self):
        """Wrap selected text as {selected|} and place cursor after |."""
        cur = self._jp_edit.textCursor()
        sel = cur.selectedText()
        if sel:
            cur.insertText("{" + sel + "|}")
            # move cursor back one character (before closing })
            cur.movePosition(cur.MoveOperation.Left, cur.MoveMode.MoveAnchor, 1)
        else:
            cur.insertText("{|}")
            cur.movePosition(cur.MoveOperation.Left, cur.MoveMode.MoveAnchor, 2)
        self._jp_edit.setTextCursor(cur)

    def _jp_context_menu(self, pos):
        menu = QMenu(self._jp_edit)
        menu.setFont(QFont(_NOTO_JP or "", 10))

        act_ruby = menu.addAction("注音を付ける\t(Ctrl+R)")
        act_ruby.triggered.connect(self._insert_ruby)
        menu.addSeparator()

        std = self._jp_edit.createStandardContextMenu()
        _label_map = {
            "&Undo": "元に戻す",
            "&Redo": "やり直す",
            "Cu&t": "切り取り",
            "&Copy": "コピー",
            "&Paste": "貼り付け",
            "Delete": "削除",
            "Select All": "すべて選択",
        }
        for act in std.actions():
            if act.isSeparator():
                menu.addSeparator()
            else:
                text = act.text().replace("&", "").strip()
                jp = next((v for k, v in _label_map.items()
                           if k.replace("&", "") == text), None)
                if jp:
                    act.setText(jp)
                menu.addAction(act)

        menu.exec(self._jp_edit.mapToGlobal(pos))

    def _apply_jp(self):
        if self._current_line is None:
            return
        text = self._jp_edit.toPlainText().strip()
        t_start = self._current_line['start']
        t_end = line_end_ms(self._all_lines, self._line_idx)
        # Parse new markup (linear placeholder timing)
        new_segs = parse_jp_markup(text, t_start, t_end)
        # Preserve timing for unchanged characters via LCS diff
        merged = merge_jp_timing(self._current_line.get('jp', []), new_segs, t_start, t_end)
        self._current_line['jp'] = merged
        self._refresh_unit_table()
        self.line_changed.emit()

    def _on_unit_cell_changed(self, item: QTableWidgetItem):
        if self._current_line is None:
            return
        col = item.column()
        if col not in (2, 3):
            return
        # Map table row → seg/unit
        row = item.row()
        flat_idx = 0
        for seg in self._current_line.get('jp', []):
            for unit in seg.get('units', []):
                if flat_idx == row:
                    try:
                        val = int(item.text())
                        if col == 2:
                            unit['s'] = val
                        else:
                            unit['e'] = val
                        self.line_changed.emit()
                    except ValueError:
                        item.setText(str(unit['s'] if col == 2 else unit['e']))
                    return
                flat_idx += 1

    def _open_unit_tap(self):
        if self._current_line is None:
            return
        t_end = line_end_ms(self._all_lines, self._line_idx)
        dlg = UnitTapDialog(
            self._current_line.get('jp', []),
            self._current_line['start'],
            t_end,
            self._player,
            self
        )
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_segs is not None:
            self._current_line['jp'] = dlg.result_segs
            self._refresh_unit_table()
            self.line_changed.emit()

    def _distribute_units(self):
        if self._current_line is None:
            return
        t_start = self._current_line['start']
        t_end = line_end_ms(self._all_lines, self._line_idx)
        segs = self._current_line.get('jp', [])
        total = sum(len(seg.get('units', [])) for seg in segs)
        if total == 0:
            return
        dur = max(1, t_end - t_start)
        udur = dur / total
        idx = 0
        for seg in segs:
            for unit in seg.get('units', []):
                unit['s'] = int(t_start + idx * udur)
                unit['e'] = int(t_start + (idx + 1) * udur)
                idx += 1
        self._refresh_unit_table()
        self.line_changed.emit()

    def get_current(self) -> Optional[dict]:
        return self._current_line


# ══════════════════════════════════════════════════════════════
#  Lyrics list (left panel)
# ══════════════════════════════════════════════════════════════

class LyricsListPanel(QWidget):
    selection_changed = pyqtSignal(int)   # selected index
    add_requested = pyqtSignal()
    delete_requested = pyqtSignal(int)
    move_requested = pyqtSignal(int, int)   # from_idx, to_idx

    def __init__(self):
        super().__init__()
        self._tap_idx = -1
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        hdr = QLabel("歌詞リスト")
        hdr.setStyleSheet("font-weight:bold;color:#333;")
        layout.addWidget(hdr)

        self._list = QListWidget()
        self._list.setFont(QFont(_NOTO_JP or "", 11))
        self._list.setAlternatingRowColors(True)
        self._list.currentRowChanged.connect(self.selection_changed)
        layout.addWidget(self._list)

        btn_row = QHBoxLayout()
        self._add_btn = QPushButton("＋")
        self._add_btn.setToolTip("現在の再生位置に新しい行を追加 (Ctrl+Enter)")
        self._add_btn.setFixedWidth(36)
        self._add_btn.clicked.connect(self.add_requested)
        btn_row.addWidget(self._add_btn)

        self._del_btn = QPushButton("－")
        self._del_btn.setToolTip("選択行を削除 (Delete)")
        self._del_btn.setFixedWidth(36)
        self._del_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(self._del_btn)

        self._up_btn = QPushButton("▲")
        self._up_btn.setFixedWidth(36)
        self._up_btn.clicked.connect(lambda: self._move(-1))
        btn_row.addWidget(self._up_btn)

        self._down_btn = QPushButton("▼")
        self._down_btn.setFixedWidth(36)
        self._down_btn.clicked.connect(lambda: self._move(1))
        btn_row.addWidget(self._down_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _delete_selected(self):
        row = self._list.currentRow()
        if row >= 0:
            self.delete_requested.emit(row)

    def _move(self, direction: int):
        row = self._list.currentRow()
        target = row + direction
        if 0 <= target < self._list.count():
            self.move_requested.emit(row, target)

    def refresh(self, lines: list, sel: int = -1):
        self._list.blockSignals(True)
        self._list.clear()
        for i, line in enumerate(lines):
            preview = segments_to_markup(line.get('jp', []))[:18] or '（空）'
            text = f"{ms_to_str(line['start'])}  {preview}"
            item = QListWidgetItem(text)
            if i == self._tap_idx:
                item.setBackground(QColor(255, 245, 190))
                item.setForeground(QColor(130, 75, 0))
            self._list.addItem(item)
        if 0 <= sel < self._list.count():
            self._list.setCurrentRow(sel)
        self._list.blockSignals(False)

    def current_row(self) -> int:
        return self._list.currentRow()

    def set_current_row(self, row: int):
        if 0 <= row < self._list.count():
            self._list.setCurrentRow(row)

    def set_tap_idx(self, idx: int):
        self._tap_idx = idx


# ══════════════════════════════════════════════════════════════
#  Audio control bar
# ══════════════════════════════════════════════════════════════

class AudioControlBar(QWidget):
    seek_requested = pyqtSignal(int)     # ms

    def __init__(self):
        super().__init__()
        self._duration_ms = 0
        self._player: Optional[QMediaPlayer] = None
        self._audio_out: Optional[QAudioOutput] = None
        if HAS_MULTIMEDIA:
            self._player = QMediaPlayer()
            self._audio_out = QAudioOutput()
            self._player.setAudioOutput(self._audio_out)
            self._audio_out.setVolume(1.0)
            self._player.positionChanged.connect(self._on_position)
            self._player.durationChanged.connect(self._on_duration)
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # Waveform
        self.waveform = WaveformWidget()
        self.waveform.seek_requested.connect(self._seek)
        outer.addWidget(self.waveform, 1)

        # Transport row
        row = QHBoxLayout()

        def mk_btn(text, tip, width=36):
            b = QPushButton(text)
            b.setFixedWidth(width)
            b.setToolTip(tip)
            return b

        self._btn_rew = mk_btn("⏮", "先頭へ")
        self._btn_rew.clicked.connect(lambda: self._seek(0))
        row.addWidget(self._btn_rew)

        self._btn_back = mk_btn("◀5", "-5秒 (J)")
        self._btn_back.clicked.connect(lambda: self._seek_rel(-5000))
        row.addWidget(self._btn_back)

        self._btn_play = mk_btn("▶", "再生/一時停止 (Space)", 60)
        self._btn_play.setCheckable(True)
        self._btn_play.setStyleSheet(
            "QPushButton{background:#2e8b4a;color:white;border-radius:4px;font-size:16px;}"
            "QPushButton:checked{background:#c0392b;}"
            "QPushButton:hover{background:#39a85c;}"
            "QPushButton:checked:hover{background:#d9534f;}"
        )
        self._btn_play.clicked.connect(self._toggle_play)
        row.addWidget(self._btn_play)

        self._btn_fwd = mk_btn("5▶", "+5秒 (L)")
        self._btn_fwd.clicked.connect(lambda: self._seek_rel(5000))
        row.addWidget(self._btn_fwd)

        self._btn_end = mk_btn("⏭", "最後へ")
        self._btn_end.clicked.connect(lambda: self._seek(self._duration_ms))
        row.addWidget(self._btn_end)

        row.addSpacing(8)

        self._pos_label = QLabel("00:00.000 / 00:00.000")
        self._pos_label.setFont(QFont("Consolas", 10))
        self._pos_label.setFixedWidth(200)
        row.addWidget(self._pos_label)

        row.addSpacing(12)
        row.addWidget(QLabel("速度:"))
        self._speed_combo = None
        from PyQt6.QtWidgets import QComboBox
        self._speed_combo = QComboBox()
        for s in ("0.5×", "0.75×", "1.0×", "1.25×", "1.5×", "2.0×"):
            self._speed_combo.addItem(s)
        self._speed_combo.setCurrentIndex(2)
        self._speed_combo.currentIndexChanged.connect(self._on_speed_change)
        row.addWidget(self._speed_combo)

        row.addSpacing(8)
        row.addWidget(QLabel("音量:"))
        self._vol_slider = QSlider(Qt.Orientation.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(100)
        self._vol_slider.setFixedWidth(100)
        self._vol_slider.valueChanged.connect(self._on_vol_change)
        row.addWidget(self._vol_slider)

        row.addStretch()

        if not HAS_MULTIMEDIA:
            row.addWidget(QLabel("⚠ PyQt6-Qt6Multimedia 未インストール"))

        outer.addLayout(row)

    def load_audio(self, path: str):
        if not HAS_MULTIMEDIA or self._player is None:
            return
        self._player.setSource(QUrl.fromLocalFile(path))

    def get_player(self) -> Optional[QMediaPlayer]:
        return self._player

    def position(self) -> int:
        return self._player.position() if self._player else 0

    def is_playing(self) -> bool:
        if not HAS_MULTIMEDIA or self._player is None:
            return False
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def play_pause(self):
        if not HAS_MULTIMEDIA or self._player is None:
            return
        if self.is_playing():
            self._player.pause()
            self._btn_play.setChecked(False)
            self._btn_play.setText("▶")
        else:
            self._player.play()
            self._btn_play.setChecked(True)
            self._btn_play.setText("⏸")

    def _toggle_play(self):
        self.play_pause()

    def _seek(self, ms: int):
        if self._player:
            self._player.setPosition(max(0, ms))
        self.seek_requested.emit(max(0, ms))

    def _seek_rel(self, delta: int):
        cur = self._player.position() if self._player else 0
        self._seek(cur + delta)

    def _on_position(self, ms: int):
        self._pos_label.setText(f"{ms_to_str(ms)} / {ms_to_str(self._duration_ms)}")
        self.waveform.set_position(ms)

    def _on_duration(self, ms: int):
        self._duration_ms = ms
        self.waveform.set_duration(ms)

    def _on_speed_change(self, idx: int):
        speeds = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
        if self._player:
            self._player.setPlaybackRate(speeds[idx])

    def _on_vol_change(self, val: int):
        if self._audio_out:
            self._audio_out.setVolume(val / 100.0)


# ══════════════════════════════════════════════════════════════
#  Main Window
# ══════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self, init_file: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("furi-lrc-gui — 歌詞エディタ")
        self.resize(1200, 780)

        self._lines: list = []
        self._save_path: Optional[str] = None
        self._audio_path: Optional[str] = None
        self._dirty = False
        self._undo_stack: list = []
        self._redo_stack: list = []

        # Tap mode state
        self._tap_mode = False        # line-start tap mode
        self._tap_cur = 0             # current line index being tapped
        self._tap_undo_stack: list = []   # [{tap_cur, start}, ...]

        # Waveform loader
        self._wf_loader: Optional[WaveformLoader] = None
        self._pending_wf_zoom: Optional[float] = None
        self._pending_wf_scroll: Optional[float] = None

        self._build_ui()
        self._build_shortcuts()

        if init_file:
            if init_file.lower().endswith('.flproj'):
                self._load_project(init_file)
            else:
                self._load_lyrics(init_file)

    # ─── UI construction ───

    def _build_ui(self):
        # Audio bar (bottom)
        self._audio = AudioControlBar()
        self._audio.seek_requested.connect(self._on_seek)
        self._audio.waveform.drag_start.connect(self._on_waveform_drag_start)
        self._audio.waveform.line_time_changed.connect(self._on_waveform_line_drag)
        self._audio.waveform.drag_end.connect(self._on_waveform_drag_end)

        # Left: lyrics list
        self._list_panel = LyricsListPanel()
        self._list_panel.selection_changed.connect(self._on_line_selected)
        self._list_panel.add_requested.connect(self._add_line)
        self._list_panel.delete_requested.connect(self._delete_line)
        self._list_panel.move_requested.connect(self._move_line)

        # Right: line editor
        self._editor = LineEditor()
        self._editor.set_player(self._audio.get_player())
        self._editor.line_changed.connect(self._on_editor_changed)
        # Wire player position → unit table playhead highlight
        _pl = self._audio.get_player()
        if _pl:
            _pl.positionChanged.connect(self._editor.update_playhead)

        # Splitter
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._list_panel.setMinimumWidth(150)
        self._list_panel.setMaximumWidth(600)
        self._splitter.addWidget(self._list_panel)
        self._splitter.addWidget(self._editor)
        self._splitter.setSizes([220, 900])

        # Central layout
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        cv.addWidget(self._splitter, 1)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color:#ccc;")
        cv.addWidget(sep)

        self._audio.setMaximumHeight(240)
        self._audio.setMinimumHeight(160)
        cv.addWidget(self._audio, 0)

        self.setCentralWidget(central)

        # ── Row 1: Menu bar (file / LRC operations, collapsible) ──
        mb = self.menuBar()
        mb.setStyleSheet(
            "QMenuBar{"
            "background:#eef0f8;border-bottom:1px solid #d2d5ea;"
            "padding:2px 8px;spacing:2px;"
            "}"
            "QMenuBar::item{"
            "padding:4px 16px;border-radius:5px;background:transparent;"
            "color:#2a2c3e;"
            "}"
            "QMenuBar::item:selected{background:#dde0f4;}"
            "QMenuBar::item:pressed{background:#cdd0ec;}"
            "QMenu{"
            "background:#ffffff;border:1px solid #cdd0ea;"
            "border-radius:8px;padding:5px 0;"
            "}"
            "QMenu::item{"
            "padding:7px 36px 7px 20px;border-radius:4px;margin:1px 6px;"
            "color:#1a1c2e;"
            "}"
            "QMenu::item:selected{background:#eaecf8;}"
            "QMenu::item:disabled{color:#aaa;}"
            "QMenu::separator{height:1px;background:#e4e7f2;margin:5px 12px;}"
        )

        _fm = mb.addMenu("ファイル")

        _act_mp3 = QAction("音声を開く…", self)
        _act_mp3.setToolTip("MP3 / WAV / FLAC などの音声ファイルを開く")
        _act_mp3.triggered.connect(self._open_mp3)
        _fm.addAction(_act_mp3)

        _fm.addSeparator()

        _act_new = QAction("新規", self)
        _act_new.setToolTip("新しい歌詞ファイルを作成")
        _act_new.triggered.connect(self._new_file)
        _fm.addAction(_act_new)

        _act_open = QAction("開く…", self)
        _act_open.setShortcut(QKeySequence("Ctrl+O"))
        _act_open.setToolTip("プロジェクト (.flproj) または歌詞 JSON (.json) を開く")
        _act_open.triggered.connect(self._open_json)
        _fm.addAction(_act_open)

        self._save_act = QAction("保存", self)
        self._save_act.setShortcut(QKeySequence("Ctrl+S"))
        self._save_act.setToolTip("上書き保存")
        self._save_act.triggered.connect(self._save)
        _fm.addAction(self._save_act)

        _act_save_as = QAction("名前で保存…", self)
        _act_save_as.setShortcut(QKeySequence("Ctrl+Shift+S"))
        _act_save_as.setToolTip("名前を付けて保存")
        _act_save_as.triggered.connect(self._save_as)
        _fm.addAction(_act_save_as)

        _fm.addSeparator()

        _act_lrc_in = QAction("LRC 読込…", self)
        _act_lrc_in.setToolTip("標準 LRC をインポートして歌詞の底稿を作成")
        _act_lrc_in.triggered.connect(self._import_lrc)
        _fm.addAction(_act_lrc_in)

        _act_lrc_out = QAction("LRC 書出…", self)
        _act_lrc_out.setToolTip("現在の歌詞を標準 LRC としてエクスポート")
        _act_lrc_out.triggered.connect(self._export_lrc)
        _fm.addAction(_act_lrc_out)

        _act_json_out = QAction("歌詞 JSON として書出…", self)
        _act_json_out.setToolTip("現在の歌詞データを純粋な JSON ファイルに書き出す")
        _act_json_out.triggered.connect(self._export_json)
        _fm.addAction(_act_json_out)

        # ── Row 2: Toolbar (editing tools, always visible) ──
        tb = QToolBar("編集")
        tb.setIconSize(QSize(20, 20))
        tb.setMovable(False)
        self.addToolBar(tb)

        def add_act(text, tip, fn, shortcut=None):
            a = QAction(text, self)
            a.setToolTip(tip)
            a.triggered.connect(fn)
            if shortcut:
                a.setShortcut(shortcut)
            tb.addAction(a)
            return a

        self._undo_act = add_act("↩ 元に戻す", "Undo (Ctrl+Z)", self._undo, "Ctrl+Z")
        self._redo_act = add_act("↪ やり直し", "Redo (Ctrl+Y)", self._redo, "Ctrl+Y")
        tb.addSeparator()

        # Tap mode button
        self._tap_act = QAction("🎵 行打拍モード", self)
        self._tap_act.setToolTip(
            "行打拍モード: 再生中に Enter で各行の開始時間を自動設定\n"
            "BS: 1つ戻す  |  Esc: 終了"
        )
        self._tap_act.setCheckable(True)
        self._tap_act.triggered.connect(self._toggle_tap_mode)
        tb.addAction(self._tap_act)

        # Status bar
        self._status = self.statusBar()
        self._status.showMessage("準備完了")

    def _build_shortcuts(self):
        # Space = play/pause (only when no text-edit focused)
        sc_space = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        sc_space.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_space.activated.connect(self._on_space)

        # Ctrl+Space = play/pause global (works even while typing)
        sc_ctrl_space = QShortcut(QKeySequence("Ctrl+Space"), self)
        sc_ctrl_space.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc_ctrl_space.activated.connect(self._audio.play_pause)

        # J/L = -500/+500 ms seek
        QShortcut(QKeySequence("J"), self).activated.connect(
            lambda: self._audio._seek_rel(-500))
        QShortcut(QKeySequence("L"), self).activated.connect(
            lambda: self._audio._seek_rel(500))

        # ,/. = -100/+100 ms
        QShortcut(QKeySequence(","), self).activated.connect(
            lambda: self._audio._seek_rel(-100))
        QShortcut(QKeySequence("."), self).activated.connect(
            lambda: self._audio._seek_rel(100))

        # T = set selected line to now
        sc_t = QShortcut(QKeySequence("T"), self)
        sc_t.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_t.activated.connect(self._set_sel_to_now)

        # Enter = tap in tap mode
        self._tap_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Return), self)
        self._tap_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._tap_shortcut.activated.connect(self._on_tap_enter)
        self._tap_shortcut.setEnabled(False)

        # Escape = exit tap mode
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._esc_shortcut.activated.connect(self._exit_tap_mode)
        self._esc_shortcut.setEnabled(False)

        # Backspace = undo last tap (only active during tap mode)
        self._tap_undo_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Backspace), self)
        self._tap_undo_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._tap_undo_shortcut.activated.connect(self._tap_undo)
        self._tap_undo_shortcut.setEnabled(False)

        # Delete = delete selected line
        sc_del = QShortcut(QKeySequence(Qt.Key.Key_Delete), self)
        sc_del.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_del.activated.connect(lambda: self._delete_line(self._list_panel.current_row()))

        # Ctrl+Enter = add line
        sc_add = QShortcut(QKeySequence("Ctrl+Return"), self)
        sc_add.setContext(Qt.ShortcutContext.ApplicationShortcut)
        sc_add.activated.connect(self._add_line)

    # ─── file dialog helpers ───

    @staticmethod
    def _open_file_dialog(parent, title, directory, filters):
        dlg = QFileDialog(parent, title, directory, filters)
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.resize(680, 420)
        if dlg.exec():
            files = dlg.selectedFiles()
            return (files[0] if files else "", dlg.selectedNameFilter())
        return ("", "")

    @staticmethod
    def _save_file_dialog(parent, title, directory, filters):
        dlg = QFileDialog(parent, title, directory, filters)
        dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dlg.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dlg.resize(680, 420)
        if dlg.exec():
            files = dlg.selectedFiles()
            return (files[0] if files else "", dlg.selectedNameFilter())
        return ("", "")

    # ─── file ops ───

    def _open_mp3(self):
        path, _ = self._open_file_dialog(
            self, "MP3ファイルを開く", "",
            "Audio Files (*.mp3 *.m4a *.ogg *.wav *.flac);;All Files (*)"
        )
        if not path:
            return
        self._audio_path = path
        self._audio.load_audio(path)
        self._status.showMessage(f"音声: {Path(path).name}")

        # Load waveform in background
        if self._wf_loader:
            self._wf_loader.quit()
        self._wf_loader = WaveformLoader(path)
        self._wf_loader.finished.connect(self._on_waveform_loaded)
        self._wf_loader.start()
        self._status.showMessage(f"音声読み込み中: {Path(path).name} …")

    def _on_waveform_loaded(self, samples, sr: int):
        if samples is not None:
            self._audio.waveform.set_audio(samples, sr)
            # Restore zoom/scroll saved in project file (set_audio resets them)
            if self._pending_wf_zoom is not None:
                self._audio.waveform._zoom = self._pending_wf_zoom
                self._audio.waveform._scroll_ms = self._pending_wf_scroll or 0.0
                self._pending_wf_zoom = None
                self._pending_wf_scroll = None
                self._audio.waveform.update()
            self._status.showMessage("波形読み込み完了")
        else:
            self._status.showMessage("波形の読み込み失敗（librosa/soundfile が必要です）")

    def _new_file(self):
        if self._dirty:
            if not self._confirm_discard():
                return
        self._lines = []
        self._save_path = None
        self._dirty = False
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._refresh_all()
        self.setWindowTitle("furi-lrc-gui — 新規ファイル")
        self._status.showMessage("新規ファイルを作成しました")

    def _open_json(self):
        if self._dirty:
            if not self._confirm_discard():
                return
        path, _ = self._open_file_dialog(
            self, "ファイルを開く", self._save_path or "",
            "プロジェクト・歌詞 (*.flproj *.json);;プロジェクト (*.flproj);;JSON歌詞 (*.json);;All Files (*)"
        )
        if path:
            if path.lower().endswith('.flproj'):
                self._load_project(path)
            else:
                self._load_lyrics(path)

    def _load_lyrics(self, path: str):
        try:
            raw = json.loads(Path(path).read_text("utf-8"))
            lines = raw.get("lines", raw) if isinstance(raw, dict) else raw
            if not isinstance(lines, list):
                raise ValueError("lines は配列である必要があります")
            # Sort by start
            lines.sort(key=lambda x: x.get('start', 0))
            self._lines = lines
            self._save_path = path
            self._dirty = False
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._refresh_all()
            self.setWindowTitle(f"furi-lrc-gui — {Path(path).name}")
            self._status.showMessage(f"読み込み完了: {path}  ({len(lines)} 行)")
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"ファイルを読み込めませんでした:\n{e}")

    def _save(self):
        if not self._save_path:
            self._save_as()
            return
        self._write(self._save_path)

    def _save_as(self):
        default = self._save_path or "project.flproj"
        path, _ = self._save_file_dialog(
            self, "名前を付けて保存", default,
            "プロジェクト (*.flproj);;JSON歌詞 (*.json);;All Files (*)"
        )
        if path:
            if not (path.lower().endswith('.flproj') or path.lower().endswith('.json')):
                path += '.flproj'
            self._save_path = path
            self._write(path)

    def _write(self, path: str):
        if path.lower().endswith('.flproj'):
            self._write_project(path)
        else:
            self._write_json(path)

    def _write_json(self, path: str):
        try:
            data = {"lines": self._lines}
            Path(path).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                "utf-8"
            )
            self._dirty = False
            self.setWindowTitle(f"furi-lrc-gui — {Path(path).name}")
            self._status.showMessage(f"保存しました: {path}")
        except Exception as e:
            QMessageBox.critical(self, "保存エラー", str(e))

    def _write_project(self, path: str):
        try:
            geom = self.geometry()
            data = {
                "flproj_version": 1,
                "audio_path": self._audio_path or "",
                "lines": self._lines,
                "window": {
                    "x": geom.x(), "y": geom.y(),
                    "w": geom.width(), "h": geom.height(),
                },
                "splitter_sizes": self._splitter.sizes(),
                "waveform": {
                    "zoom": self._audio.waveform._zoom,
                    "scroll_ms": self._audio.waveform._scroll_ms,
                },
                "options": {
                    "speed_idx": (self._audio._speed_combo.currentIndex()
                                  if self._audio._speed_combo else 2),
                    "volume": (self._audio._vol_slider.value()
                               if hasattr(self._audio, '_vol_slider') else 100),
                },
            }
            Path(path).write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                "utf-8"
            )
            self._dirty = False
            self.setWindowTitle(f"furi-lrc-gui — {Path(path).name}")
            self._status.showMessage(f"プロジェクトを保存しました: {path}")
        except Exception as e:
            QMessageBox.critical(self, "保存エラー", str(e))

    def _load_project(self, path: str):
        try:
            raw = json.loads(Path(path).read_text("utf-8"))
            if raw.get("flproj_version") != 1:
                raise ValueError("未対応のプロジェクトバージョンです")
            lines = raw.get("lines", [])
            if not isinstance(lines, list):
                raise ValueError("lines は配列である必要があります")
            lines.sort(key=lambda x: x.get('start', 0))
            self._lines = lines
            self._save_path = path
            self._dirty = False
            self._undo_stack.clear()
            self._redo_stack.clear()
            # Restore window geometry
            win = raw.get("window", {})
            if win:
                self.setGeometry(
                    win.get("x", self.x()),
                    win.get("y", self.y()),
                    win.get("w", self.width()),
                    win.get("h", self.height()),
                )
            # Restore splitter
            sizes = raw.get("splitter_sizes")
            if sizes and len(sizes) == 2:
                self._splitter.setSizes(sizes)
            # Schedule waveform view restore (applied after waveform loads)
            wf = raw.get("waveform", {})
            if wf:
                self._pending_wf_zoom = max(1.0, float(wf.get("zoom", 1.0)))
                self._pending_wf_scroll = max(0.0, float(wf.get("scroll_ms", 0.0)))
            # Restore options
            opts = raw.get("options", {})
            if self._audio._speed_combo:
                idx = opts.get("speed_idx", 2)
                self._audio._speed_combo.setCurrentIndex(
                    max(0, min(idx, self._audio._speed_combo.count() - 1))
                )
            if hasattr(self._audio, '_vol_slider'):
                self._audio._vol_slider.setValue(opts.get("volume", 100))
            # Load audio
            audio_path = raw.get("audio_path", "")
            audio_ok = False
            if audio_path:
                p = Path(audio_path)
                if not p.is_absolute():
                    p = Path(path).parent / p
                if p.exists():
                    audio_ok = True
                    self._audio_path = str(p)
                    self._audio.load_audio(str(p))
                    if self._wf_loader:
                        self._wf_loader.quit()
                    self._wf_loader = WaveformLoader(str(p))
                    self._wf_loader.finished.connect(self._on_waveform_loaded)
                    self._wf_loader.start()
            self._refresh_all()
            self.setWindowTitle(f"furi-lrc-gui — {Path(path).name}")
            msg = f"プロジェクト読み込み完了: {Path(path).name}  ({len(lines)} 行)"
            if audio_path and not audio_ok:
                msg += f"  ⚠ 音声ファイルが見つかりません: {audio_path}"
            self._status.showMessage(msg)
        except Exception as e:
            QMessageBox.critical(self, "エラー", f"プロジェクトを読み込めませんでした:\n{e}")

    def _confirm_discard(self) -> bool:
        r = QMessageBox.question(
            self, "未保存の変更",
            "未保存の変更があります。続けますか？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        return r == QMessageBox.StandardButton.Yes

    # ─── undo/redo ───

    def _push_undo(self):
        self._undo_stack.append(copy.deepcopy(self._lines))
        if len(self._undo_stack) > 100:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._dirty = True

    def _undo(self):
        if not self._undo_stack:
            return
        self._redo_stack.append(copy.deepcopy(self._lines))
        self._lines = self._undo_stack.pop()
        self._refresh_all(preserve_sel=True)
        self._dirty = True

    def _redo(self):
        if not self._redo_stack:
            return
        self._undo_stack.append(copy.deepcopy(self._lines))
        self._lines = self._redo_stack.pop()
        self._refresh_all(preserve_sel=True)
        self._dirty = True

    # ─── line operations ───

    def _add_line(self):
        self._push_undo()
        ms = self._audio.position()
        new_line = {'start': ms, 'jp': [], 'zh': ''}
        self._lines.append(new_line)
        self._lines.sort(key=lambda x: x['start'])
        idx = next(i for i, l in enumerate(self._lines) if l is new_line)
        self._refresh_all(sel=idx)
        self._status.showMessage(f"行追加: {ms_to_str(ms)}")

    def _delete_line(self, idx: int):
        if idx < 0 or idx >= len(self._lines):
            return
        self._push_undo()
        self._lines.pop(idx)
        new_sel = min(idx, len(self._lines) - 1)
        self._refresh_all(sel=new_sel)
        self._status.showMessage("行を削除しました")

    def _move_line(self, from_idx: int, to_idx: int):
        if from_idx < 0 or to_idx < 0:
            return
        if from_idx >= len(self._lines) or to_idx >= len(self._lines):
            return
        self._push_undo()
        line = self._lines.pop(from_idx)
        self._lines.insert(to_idx, line)
        self._refresh_all(sel=to_idx)

    # ─── selection / editor sync ───

    def _on_line_selected(self, idx: int):
        if 0 <= idx < len(self._lines):
            self._editor.set_line(self._lines[idx], idx, self._lines)
            self._audio.waveform.set_lines(self._lines, idx)
        else:
            self._editor.set_line(None, -1, self._lines)
            self._audio.waveform.set_lines(self._lines, -1)

    def _on_editor_changed(self):
        self._dirty = True
        orig_line = self._editor.get_current()
        # Re-sort by start time and find where the line ended up
        if orig_line and self._lines:
            self._lines.sort(key=lambda x: x['start'])
            try:
                new_idx = next(i for i, l in enumerate(self._lines) if l is orig_line)
            except StopIteration:
                new_idx = self._list_panel.current_row()
        else:
            new_idx = self._list_panel.current_row()
        self._list_panel.refresh(self._lines, new_idx)
        self._audio.waveform.set_lines(self._lines, new_idx)

    def _on_seek(self, ms: int):
        # When seeking, try to select the line that's at/before that time
        if not self._lines:
            return
        idx = -1
        for i, line in enumerate(self._lines):
            if line['start'] <= ms:
                idx = i
        if idx >= 0 and idx != self._list_panel.current_row():
            self._list_panel.set_current_row(idx)

    def _set_sel_to_now(self):
        """T key: set selected line's start to current position."""
        idx = self._list_panel.current_row()
        if 0 <= idx < len(self._lines):
            ms = self._audio.position()
            target_line = self._lines[idx]
            self._push_undo()
            target_line['start'] = ms
            self._lines.sort(key=lambda x: x['start'])
            new_idx = next(
                (i for i, l in enumerate(self._lines) if l is target_line), idx
            )
            self._refresh_all(sel=new_idx)
            self._status.showMessage(f"開始時間を設定: {ms_to_str(ms)}")

    # ─── tap mode ───

    def _toggle_tap_mode(self, checked: bool):
        if checked:
            self._enter_tap_mode()
        else:
            self._exit_tap_mode()

    def _enter_tap_mode(self):
        self._tap_mode = True
        self._tap_cur = max(0, self._list_panel.current_row())
        self._tap_undo_stack.clear()
        self._tap_act.setChecked(True)
        self._tap_shortcut.setEnabled(True)
        self._esc_shortcut.setEnabled(True)
        self._tap_undo_shortcut.setEnabled(True)
        self._list_panel.set_tap_idx(self._tap_cur)
        self._list_panel.refresh(self._lines, self._tap_cur)
        self._status.showMessage(
            f"🎵 行打拍モード  |  行 {self._tap_cur + 1}/{len(self._lines)}  |  "
            "Enter: 現在位置を開始時間に設定  |  BS: 1つ戻す  |  Esc: 終了"
        )
        if not self._audio.is_playing():
            self._audio.play_pause()

    def _exit_tap_mode(self):
        self._tap_mode = False
        self._tap_act.setChecked(False)
        self._tap_shortcut.setEnabled(False)
        self._esc_shortcut.setEnabled(False)
        self._tap_undo_shortcut.setEnabled(False)
        self._tap_undo_stack.clear()
        self._list_panel.set_tap_idx(-1)
        self._list_panel.refresh(self._lines, self._list_panel.current_row())
        self._status.showMessage("打拍モード終了")

    def _on_tap_enter(self):
        if not self._tap_mode:
            return
        if not self._lines:
            self._exit_tap_mode()
            return
        ms = self._audio.position()
        # Save state for within-session tap undo
        self._tap_undo_stack.append({
            'tap_cur': self._tap_cur,
            'start': self._lines[self._tap_cur]['start'],
        })
        self._push_undo()
        self._lines[self._tap_cur]['start'] = ms

        self._tap_cur += 1
        if self._tap_cur >= len(self._lines):
            self._lines.sort(key=lambda x: x['start'])
            self._refresh_all(sel=len(self._lines) - 1)
            self._exit_tap_mode()
            self._status.showMessage("✔ 全行の打拍が完了しました")
            return

        self._lines.sort(key=lambda x: x['start'])
        self._list_panel.set_tap_idx(self._tap_cur)
        self._refresh_all(sel=self._tap_cur)
        self._status.showMessage(
            f"🎵 行打拍  |  行 {self._tap_cur + 1}/{len(self._lines)}  |  "
            "Enter: 次へ  |  BS: 1つ戻す  |  Esc: 終了"
        )

    def _tap_undo(self):
        """Undo the last line-tap within the current tap session."""
        if not self._tap_mode or not self._tap_undo_stack:
            self._status.showMessage("🎵 行打拍  |  戻せる操作がありません")
            return
        state = self._tap_undo_stack.pop()
        self._tap_cur = state['tap_cur']
        self._lines[self._tap_cur]['start'] = state['start']
        self._lines.sort(key=lambda x: x['start'])
        self._list_panel.set_tap_idx(self._tap_cur)
        self._refresh_all(sel=self._tap_cur)
        self._status.showMessage(
            f"🎵 行打拍  |  行 {self._tap_cur + 1} に戻しました  |  "
            "Enter: 次へ  |  BS: さらに戻す  |  Esc: 終了"
        )

    # ─── space key (play/pause, only when text edits don't have focus) ───

    def _on_space(self):
        focused = QApplication.focusWidget()
        if isinstance(focused, (QLineEdit, QTextEdit)):
            return   # let text edits consume space normally
        self._audio.play_pause()

    # ─── refresh helpers ───

    def _refresh_all(self, sel: int = -1, preserve_sel: bool = False):
        if preserve_sel:
            sel = self._list_panel.current_row()
        if sel < 0:
            sel = self._list_panel.current_row()
        sel = min(sel, len(self._lines) - 1)
        self._list_panel.refresh(self._lines, sel)
        self._audio.waveform.set_lines(self._lines, sel)
        if 0 <= sel < len(self._lines):
            self._editor.set_line(self._lines[sel], sel, self._lines)
        else:
            self._editor.set_line(None, -1, self._lines)

    # ─── waveform drag handlers ───

    def _on_waveform_drag_start(self, idx: int):
        self._push_undo()

    def _on_waveform_line_drag(self, idx: int, ms: int):
        if not (0 <= idx < len(self._lines)):
            return
        self._lines[idx]['start'] = ms
        self._dirty = True
        sel = self._list_panel.current_row()
        self._list_panel.refresh(self._lines, sel)
        self._audio.waveform.set_lines(self._lines, sel)
        # Keep editor time display in sync when the dragged line is selected
        if idx == sel:
            self._editor._time_edit.set_ms(ms)

    def _on_waveform_drag_end(self):
        """Sort lines and do a full refresh after a drag completes."""
        sel = self._list_panel.current_row()
        sel_line = self._lines[sel] if 0 <= sel < len(self._lines) else None
        self._lines.sort(key=lambda x: x['start'])
        if sel_line is not None:
            new_sel = next((i for i, l in enumerate(self._lines) if l is sel_line), 0)
        else:
            new_sel = max(0, min(sel, len(self._lines) - 1))
        self._refresh_all(sel=new_sel)

    # ─── LRC import / export ───

    def _import_lrc(self):
        if self._dirty:
            if not self._confirm_discard():
                return
        path, _ = self._open_file_dialog(
            self, "LRCファイルを開く", "",
            "LRC Files (*.lrc);;All Files (*)"
        )
        if not path:
            return
        try:
            text = Path(path).read_text('utf-8', errors='replace')
            lines = parse_lrc(text)
            if not lines:
                QMessageBox.warning(self, "LRC読込",
                                    "有効なタイムタグが見つかりませんでした。")
                return
            self._lines = lines
            self._save_path = None
            self._dirty = True
            self._undo_stack.clear()
            self._redo_stack.clear()
            self._refresh_all()
            self.setWindowTitle(
                f"furi-lrc-gui — {Path(path).stem}.json (LRCから変換)"
            )
            self._status.showMessage(
                f"LRCインポート完了: {Path(path).name}  ({len(lines)} 行)"
            )
        except Exception as e:
            QMessageBox.critical(self, "LRC読込エラー", str(e))

    def _export_lrc(self):
        if not self._lines:
            QMessageBox.warning(self, "LRC書出", "歌詞データが空です。")
            return
        default_name = (
            Path(self._save_path).stem + ".lrc"
            if self._save_path else "lyrics.lrc"
        )
        path, _ = self._save_file_dialog(
            self, "LRCとして保存", default_name,
            "LRC Files (*.lrc);;All Files (*)"
        )
        if not path:
            return
        if not path.endswith('.lrc'):
            path += '.lrc'
        try:
            content = export_lrc(self._lines)
            Path(path).write_text(content, 'utf-8')
            self._status.showMessage(f"LRCエクスポート完了: {path}")
        except Exception as e:
            QMessageBox.critical(self, "LRC書出エラー", str(e))

    def _export_json(self):
        if not self._lines:
            QMessageBox.warning(self, "JSON書出", "歌詞データが空です。")
            return
        stem = Path(self._save_path).stem if self._save_path else "lyrics"
        # Strip .flproj suffix so default name is clean
        if stem.lower().endswith('.flproj'):
            stem = stem[:-7]
        default_name = stem + ".json"
        path, _ = self._save_file_dialog(
            self, "歌詞 JSON として保存", default_name,
            "JSON Files (*.json);;All Files (*)"
        )
        if not path:
            return
        if not path.lower().endswith('.json'):
            path += '.json'
        try:
            Path(path).write_text(
                json.dumps({"lines": self._lines}, ensure_ascii=False, indent=2),
                'utf-8'
            )
            self._status.showMessage(f"JSON書出完了: {path}")
        except Exception as e:
            QMessageBox.critical(self, "JSON書出エラー", str(e))

    # ─── close ───

    def closeEvent(self, e):
        if self._dirty:
            r = QMessageBox.question(
                self, "終了確認",
                "未保存の変更があります。保存してから終了しますか？",
                QMessageBox.StandardButton.Save |
                QMessageBox.StandardButton.Discard |
                QMessageBox.StandardButton.Cancel
            )
            if r == QMessageBox.StandardButton.Save:
                self._save()
                e.accept()
            elif r == QMessageBox.StandardButton.Discard:
                e.accept()
            else:
                e.ignore()
        else:
            e.accept()


# ══════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════

def main():
    import os
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu-compositing")

    app = QApplication(sys.argv)
    app.setApplicationName("furi-lrc-gui")
    app.setStyle("Fusion")

    # ── Load UI font: NotoSansJP ──
    _font_path = str(Path(__file__).parent / "fonts" / "NotoSansJP-Regular.ttf")
    _fid = QFontDatabase.addApplicationFont(_font_path)
    _families = QFontDatabase.applicationFontFamilies(_fid)
    if _families:
        global _NOTO_JP
        _NOTO_JP = _families[0]
        app.setFont(QFont(_NOTO_JP, 10))

    # ── Light (day) palette ──
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor(245, 246, 250))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor(30,  32,  40))
    pal.setColor(QPalette.ColorRole.Base,            QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor(235, 237, 245))
    pal.setColor(QPalette.ColorRole.Text,            QColor(30,  32,  40))
    pal.setColor(QPalette.ColorRole.Button,          QColor(228, 230, 240))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor(30,  32,  40))
    pal.setColor(QPalette.ColorRole.BrightText,      QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor(50,  110, 200))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    pal.setColor(QPalette.ColorRole.Link,            QColor(30,  90,  180))
    pal.setColor(QPalette.ColorRole.ToolTipBase,     QColor(255, 255, 220))
    pal.setColor(QPalette.ColorRole.ToolTipText,     QColor(30,  32,  40))
    pal.setColor(QPalette.ColorRole.Mid,             QColor(180, 182, 195))
    pal.setColor(QPalette.ColorRole.Dark,            QColor(150, 152, 165))
    pal.setColor(QPalette.ColorRole.Shadow,          QColor(110, 112, 125))
    app.setPalette(pal)

    init_file = sys.argv[1] if len(sys.argv) > 1 else None
    win = MainWindow(init_file)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
