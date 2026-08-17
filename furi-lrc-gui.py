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
    QStyledItemDelegate, QStyle, QStyleOptionViewItem,
    QSlider, QFrame, QSizePolicy,
    QScrollArea, QTextEdit, QGroupBox, QMenu, QComboBox, QLayout,
)
from PyQt6.QtCore import (
    Qt, QTimer, QThread, pyqtSignal, QUrl, QSize, QPoint, QRect,
)
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QKeySequence, QShortcut,
    QPainterPath, QAction, QActionGroup, QLinearGradient, QFontMetrics, QIcon,
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

_BASE_DIR  = Path(__file__).parent
_DIR_PRIVATE = _BASE_DIR / "private"
_DIR_SONGS = _DIR_PRIVATE / "songs"
_DIR_FLRC  = _DIR_PRIVATE / "flrc"

# Populated in main() after QFontDatabase.addApplicationFont
_NOTO_JP = ""  # NotoSansJP family name
_MSYH_BD = ""  # Microsoft YaHei Bold family name (msyhbd.ttc)


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


def segments_to_display_text(segs: list) -> str:
    parts = []
    for seg in segs:
        if seg.get('ruby'):
            parts.append(seg.get('base') or ''.join(u['k'] for u in seg.get('units', [])))
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
    HEIGHT_MIN = 40
    HEIGHT_MAX = 180
    HEIGHT_DEFAULT = 110

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
        self.set_display_height(self.HEIGHT_DEFAULT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.setToolTip("左クリック: シーク  右ドラッグ: 歌詞マーカー移動  スクロール: 左右移動  Ctrl+スクロール: ズーム")

    # ─── public setters ───

    def set_display_height(self, height: int):
        height = max(self.HEIGHT_MIN, min(self.HEIGHT_MAX, int(height)))
        self.setFixedHeight(height)
        self.updateGeometry()
        self.update()

    def display_height(self) -> int:
        return self.height()

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
        self._scroll_ms = self._clamp_scroll(self._scroll_ms)
        self.update()

    def set_position(self, ms: int):
        self._position_ms = ms
        v = self._vis_ms()
        lo = self._scroll_ms
        left_trigger = lo + v * 0.03
        right_trigger = lo + v * 0.97
        if ms < left_trigger:
            self._scroll_ms = self._clamp_scroll(ms - v * 0.18)
        elif ms > right_trigger:
            self._scroll_ms = self._clamp_scroll(ms - v * 0.18)
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

    def _clamp_scroll(self, value: float) -> float:
        if self._duration_ms <= 0:
            return max(0.0, float(value))
        return max(0.0, min(float(value), max(0.0, self._duration_ms - self._vis_ms())))

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
            self._scroll_ms = self._clamp_scroll(cursor_ms - offset)
        else:
            shift = self._vis_ms() * 0.15 * (-1 if delta > 0 else 1)
            self._scroll_ms = self._clamp_scroll(self._scroll_ms + shift)
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

class FlowLayout(QLayout):
    """A small wrapping layout for the unit-tap character buttons."""

    def __init__(self, parent=None, margin=0, spacing=4):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        return size + QSize(left + right, top + bottom)

    def _do_layout(self, rect, test_only):
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(left, top, -right, -bottom)
        x, y = effective.x(), effective.y()
        line_height = 0
        spacing = self.spacing()

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if line_height and next_x - spacing > effective.right() + 1:
                x = effective.x()
                y += line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + bottom


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
    _SPEEDS = [0.1, 0.25, 0.5, 0.75, 1.0]
    _SPEED_LABELS = ["0.1×", "0.25×", "0.5×", "0.75×", "1.0×"]

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
        self._fit_unit_area_to_contents()

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
        self._speed_combo.setCurrentIndex(1)   # default 0.25×
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
        flow = FlowLayout(self._unit_frame, spacing=4)
        self._unit_btns: list = []
        for info in self._flat:
            btn = QPushButton(info['k'])
            btn.setFixedSize(46, 46)
            btn.setFont(QFont(_NOTO_JP or "", 16))
            btn.setEnabled(False)
            flow.addWidget(btn)
            self._unit_btns.append(btn)
        self._unit_scroll = QScrollArea()
        self._unit_scroll.setWidgetResizable(True)
        self._unit_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._unit_scroll.setWidget(self._unit_frame)
        self._unit_scroll.setMinimumHeight(54)
        self._unit_scroll.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        # Use surplus dialog height for the wrapping button area.  The scroll
        # bar remains a fallback for exceptionally long lyric lines.
        layout.addWidget(self._unit_scroll, 1)

        # ── Tap button ──
        self._tap_btn = QPushButton("▶  再生開始  (Space)")
        self._tap_btn.setFixedHeight(52)
        self._tap_btn.setFont(QFont(_NOTO_JP or "", 14))
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

    def _fit_unit_area_to_contents(self):
        """Give the wrapping button area enough initial height when practical."""
        button_pitch = 50  # 46 px button + 4 px flow-layout spacing
        usable_width = max(button_pitch, self.width() - 40)
        columns = max(1, usable_width // button_pitch)
        rows = max(1, (len(self._flat) + columns - 1) // columns)
        desired_area = rows * button_pitch + 8

        # Avoid opening an excessively tall dialog for unusually long lyrics;
        # those still remain accessible via the vertical scrollbar.
        base_height = 300
        target_height = min(800, max(self.height(), base_height + desired_area))
        self.resize(self.width(), target_height)

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
#  JP text edit (Enter → parse, Shift/Ctrl+Enter → newline)
# ══════════════════════════════════════════════════════════════

class JpTextEdit(QTextEdit):
    """
    QTextEdit that fires apply_requested on bare Enter,
    and inserts a real newline on Shift+Enter or Ctrl+Enter.
    """
    apply_requested = pyqtSignal()

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if e.modifiers() & (Qt.KeyboardModifier.ShiftModifier |
                                Qt.KeyboardModifier.ControlModifier):
                # Insert literal newline
                self.insertPlainText("\n")
            else:
                self.apply_requested.emit()
            return
        super().keyPressEvent(e)


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
        layout.setContentsMargins(8, 6, 8, 8)
        layout.setSpacing(6)

        # Compact line summary and timing controls
        meta_row = QHBoxLayout()
        meta_row.setSpacing(6)
        self._header = QLabel("（行を選択してください）")
        self._header.setStyleSheet("color:#444;font-weight:bold;font-size:13px;")
        meta_row.addWidget(self._header)
        meta_row.addStretch()

        start_label = QLabel("開始")
        start_label.setStyleSheet("color:#666;")
        meta_row.addWidget(start_label)
        self._time_edit = TimeEdit()
        self._time_edit.time_changed.connect(self._on_time_changed)
        self._time_edit.setFixedWidth(118)
        meta_row.addWidget(self._time_edit)
        self._set_now_btn = QPushButton("⏱")
        self._set_now_btn.setToolTip("再生中の時間を開始時間に設定 (T)")
        self._set_now_btn.clicked.connect(self._set_to_now)
        self._set_now_btn.setFixedSize(30, 28)
        meta_row.addWidget(self._set_now_btn)

        end_label = QLabel("終了")
        end_label.setStyleSheet("color:#666;")
        meta_row.addWidget(end_label)
        self._end_edit = TimeEdit()
        self._end_edit.setToolTip(
            "字幕の終了時間。未設定の場合は次の行の開始時間を使用します。"
        )
        self._end_edit.time_changed.connect(self._on_end_changed)
        self._end_edit.setFixedWidth(118)
        meta_row.addWidget(self._end_edit)
        self._set_end_now_btn = QPushButton("⏱")
        self._set_end_now_btn.setToolTip("再生中の時間を終了時間に設定")
        self._set_end_now_btn.clicked.connect(self._set_end_to_now)
        self._set_end_now_btn.setFixedSize(30, 28)
        meta_row.addWidget(self._set_end_now_btn)
        layout.addLayout(meta_row)

        # JP markup
        jp_panel = QWidget()
        jp_lay = QVBoxLayout(jp_panel)
        jp_lay.setContentsMargins(0, 0, 0, 0)
        jp_lay.setSpacing(4)
        jp_head = QHBoxLayout()
        jp_head.setSpacing(6)
        jp_title = QLabel("日本語 (JP)")
        jp_title.setStyleSheet("font-weight:bold;color:#333;")
        jp_head.addWidget(jp_title)
        jp_hint = QLabel("{漢字|かんじ} 形式")
        jp_hint.setStyleSheet("color:#777;font-size:11px;")
        jp_head.addWidget(jp_hint)
        jp_head.addStretch()
        apply_jp_btn = QPushButton("JP解析")
        apply_jp_btn.setToolTip("テキストを解析してユニット表を再生成します（既存の時間は上書きされます）")
        apply_jp_btn.clicked.connect(self._apply_jp)
        apply_jp_btn.setFixedHeight(28)
        jp_head.addWidget(apply_jp_btn)
        jp_lay.addLayout(jp_head)
        self._jp_edit = JpTextEdit()
        self._jp_edit.setFixedHeight(54)
        self._jp_edit.setPlaceholderText("{東京|とうきょう}へ{行|い}く  (Enter で JP 解析 / Shift+Enter で改行)")
        self._jp_edit.setFont(QFont(_NOTO_JP or "", 13))
        self._jp_edit.setAcceptRichText(False)
        self._jp_edit.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._jp_edit.customContextMenuRequested.connect(self._jp_context_menu)
        self._jp_edit.apply_requested.connect(self._apply_jp)
        jp_lay.addWidget(self._jp_edit)

        # Ctrl+R shortcut for ruby annotation
        sc_ruby = QShortcut(QKeySequence("Ctrl+R"), self._jp_edit)
        sc_ruby.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc_ruby.activated.connect(self._insert_ruby)
        layout.addWidget(jp_panel)

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
        unit_grp.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
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
        self._unit_table.setMinimumHeight(72)
        self._unit_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._unit_table.itemChanged.connect(self._on_unit_cell_changed)
        unit_lay.addWidget(self._unit_table, 1)

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
        layout.addWidget(unit_grp, 1)

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

LYRICS_LIST_TIME_ROLE = int(Qt.ItemDataRole.UserRole) + 1
LYRICS_LIST_SEGS_ROLE = int(Qt.ItemDataRole.UserRole) + 2


class RubyLyricsDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        base_font = option.font
        if base_font.pointSizeF() <= 0:
            base_font.setPointSizeF(11)
        ruby_font = QFont(base_font)
        ruby_font.setPointSizeF(max(5.5, base_font.pointSizeF() * 0.56))
        base_fm = QFontMetrics(base_font)
        ruby_fm = QFontMetrics(ruby_font)
        compact_h = ruby_fm.height() + base_fm.height() - 6
        return QSize(size.width(), max(size.height(), compact_h + 4))

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)

        painter.save()
        rect = option.rect.adjusted(6, 1, -6, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        text_color = opt.palette.color(
            QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
        )
        muted_color = QColor(text_color)
        muted_color.setAlpha(170)

        base_font = option.font
        if base_font.pointSizeF() <= 0:
            base_font.setPointSizeF(11)
        ruby_font = QFont(base_font)
        ruby_font.setPointSizeF(max(5.5, base_font.pointSizeF() * 0.56))

        time_text = index.data(LYRICS_LIST_TIME_ROLE) or ""
        time_font = QFont(base_font)
        time_font.setPointSizeF(max(7.0, base_font.pointSizeF() * 0.86))
        time_fm = QFontMetrics(time_font)
        time_width = time_fm.horizontalAdvance("00:00.000")
        time_y = rect.top() + (rect.height() + time_fm.ascent() - time_fm.descent()) // 2
        painter.setFont(time_font)
        painter.setPen(muted_color)
        painter.drawText(rect.left(), time_y, time_text)

        lyric_rect = rect.adjusted(time_width + 10, 0, 0, 0)
        segs = index.data(LYRICS_LIST_SEGS_ROLE) or []
        if not segs:
            painter.setFont(base_font)
            painter.setPen(text_color)
            fm = QFontMetrics(base_font)
            empty = fm.elidedText("（空）", Qt.TextElideMode.ElideRight, lyric_rect.width())
            painter.drawText(lyric_rect.left(), time_y, empty)
            painter.restore()
            return

        self._draw_segments(
            painter, lyric_rect, segs, base_font, ruby_font, text_color, muted_color
        )
        painter.restore()

    def _draw_segments(self, painter, rect, segs, base_font, ruby_font, text_color, ruby_color):
        base_fm = QFontMetrics(base_font)
        ruby_fm = QFontMetrics(ruby_font)
        x = rect.left()
        right = rect.right()
        overlap = 5
        block_h = ruby_fm.height() + base_fm.height() - overlap
        block_top = rect.top() + max(0, (rect.height() - block_h) // 2)
        ruby_y = block_top + ruby_fm.ascent()
        base_y = block_top + ruby_fm.height() - overlap + base_fm.ascent()
        ellipsis = "..."
        ellipsis_w = base_fm.horizontalAdvance(ellipsis)

        def draw_ellipsis():
            if x <= right:
                painter.setFont(base_font)
                painter.setPen(text_color)
                painter.drawText(x, base_y, ellipsis)

        for seg in segs:
            if seg.get('ruby'):
                base = seg.get('base') or ''.join(u['k'] for u in seg.get('units', []))
                ruby = ''.join(u['k'] for u in seg.get('units', []))
                base_w = base_fm.horizontalAdvance(base)
                ruby_w = ruby_fm.horizontalAdvance(ruby)
                w = max(base_w, ruby_w)
                if x + w > right + 1:
                    draw_ellipsis()
                    return
                painter.setFont(ruby_font)
                painter.setPen(ruby_color)
                painter.drawText(int(x + (w - ruby_w) / 2), ruby_y, ruby)
                painter.setFont(base_font)
                painter.setPen(text_color)
                painter.drawText(int(x + (w - base_w) / 2), base_y, base)
                x += w + 2
            else:
                text = ''.join(u['k'] for u in seg.get('units', []))
                if not text:
                    continue
                remaining = right - x + 1
                if remaining <= ellipsis_w:
                    draw_ellipsis()
                    return
                text_w = base_fm.horizontalAdvance(text)
                painter.setFont(base_font)
                painter.setPen(text_color)
                if text_w <= remaining:
                    painter.drawText(x, base_y, text)
                    x += text_w
                else:
                    painter.drawText(
                        x,
                        base_y,
                        base_fm.elidedText(text, Qt.TextElideMode.ElideRight, remaining),
                    )
                    return


class LyricsListPanel(QWidget):
    selection_changed = pyqtSignal(int)   # selected index
    jump_requested = pyqtSignal(int)      # double-clicked index
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
        self._list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setWordWrap(False)
        self._list.setItemDelegate(RubyLyricsDelegate(self._list))
        self._list.currentRowChanged.connect(self.selection_changed)
        self._list.itemDoubleClicked.connect(self._jump_to_item)
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

    def _jump_to_item(self, item: QListWidgetItem):
        row = self._list.row(item)
        if row >= 0:
            self.jump_requested.emit(row)

    def refresh(self, lines: list, sel: int = -1):
        self._list.blockSignals(True)
        self._list.clear()
        for i, line in enumerate(lines):
            preview = segments_to_display_text(line.get('jp', [])) or '（空）'
            time_text = ms_to_str(line['start'])
            text = f"{time_text}  {preview}"
            item = QListWidgetItem(text)
            item.setData(LYRICS_LIST_TIME_ROLE, time_text)
            item.setData(LYRICS_LIST_SEGS_ROLE, line.get('jp', []))
            item.setToolTip(text)
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
    CHROME_HEIGHT = 54

    seek_requested = pyqtSignal(int)     # ms
    status_message = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._duration_ms = 0
        self._waveform_height = WaveformWidget.HEIGHT_DEFAULT
        self._player: Optional[QMediaPlayer] = None
        self._audio_out: Optional[QAudioOutput] = None
        if HAS_MULTIMEDIA:
            self._player = QMediaPlayer()
            self._audio_out = QAudioOutput()
            self._player.setAudioOutput(self._audio_out)
            self._audio_out.setVolume(1.0)
            self._player.positionChanged.connect(self._on_position)
            self._player.durationChanged.connect(self._on_duration)
            self._player.playbackStateChanged.connect(self._on_playback_state_changed)
            self._player.errorOccurred.connect(self._on_media_error)
        self._build_ui()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        # Waveform
        self.waveform = WaveformWidget()
        self.waveform.seek_requested.connect(self._seek)
        self.waveform.set_display_height(self._waveform_height)
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
        self.set_waveform_height(self._waveform_height)

    def set_waveform_height(self, height: int):
        height = max(WaveformWidget.HEIGHT_MIN, min(WaveformWidget.HEIGHT_MAX, int(height)))
        self._waveform_height = height
        if hasattr(self, 'waveform'):
            self.waveform.set_display_height(height)
        chrome_height = self.CHROME_HEIGHT
        self.setMinimumHeight(WaveformWidget.HEIGHT_MIN + chrome_height)
        self.updateGeometry()

    def waveform_height(self) -> int:
        return self._waveform_height

    def waveform_chrome_height(self) -> int:
        return self.CHROME_HEIGHT

    def load_audio(self, path: str):
        if not HAS_MULTIMEDIA or self._player is None:
            self.status_message.emit("音声再生機能が利用できません（PyQt6-Qt6Multimedia を確認してください）")
            return
        if self._audio_out is None:
            self._audio_out = QAudioOutput()
        self._player.setAudioOutput(self._audio_out)
        self._audio_out.setVolume(self._vol_slider.value() / 100.0)
        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(path))
        self.status_message.emit(f"音声を読み込みました: {Path(path).name}")

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
        else:
            if self._audio_out is not None:
                self._player.setAudioOutput(self._audio_out)
                self._audio_out.setVolume(self._vol_slider.value() / 100.0)
            if self._vol_slider.value() <= 0:
                self.status_message.emit("音量が 0 です")
            self._player.play()

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

    def _on_playback_state_changed(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._btn_play.setChecked(playing)
        self._btn_play.setText("⏸" if playing else "▶")

    def _on_speed_change(self, idx: int):
        speeds = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
        if self._player:
            self._player.setPlaybackRate(speeds[idx])

    def _on_vol_change(self, val: int):
        if self._audio_out:
            self._audio_out.setVolume(val / 100.0)

    def _on_media_error(self, error, error_string: str = ""):
        if error == QMediaPlayer.Error.NoError:
            return
        msg = error_string or str(error)
        self.status_message.emit(f"音声再生エラー: {msg}")


# ══════════════════════════════════════════════════════════════
#  Replace Dialog
# ══════════════════════════════════════════════════════════════

class ReplaceDialog(QDialog):
    """
    全文置換ダイアログ。

    対象フィールド
    --------------
    ・JP（日本語ルビ読み）   → セグメントの units[].k / seg['base']
    ・ZH（中文訳）           → line['zh']
    ・両方

    オプション
    ----------
    ・大文字/小文字を区別する
    ・正規表現モード
    ・置換対象フィールドの選択（JP / ZH / 両方）
    """

    replaced = pyqtSignal(int)   # 置換件数を通知

    def __init__(self, get_lines_fn, push_undo_fn, refresh_fn, parent=None):
        super().__init__(parent)
        self._get_lines = get_lines_fn
        self._push_undo = push_undo_fn
        self._refresh = refresh_fn
        self.setWindowTitle("置換")
        self.setModal(False)
        self.resize(520, 310)
        self._history_find: list = []
        self._history_replace: list = []
        self._build_ui()
        self.replaced.connect(self._on_replaced)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── 検索文字列 ──
        find_row = QHBoxLayout()
        find_row.addWidget(QLabel("検索:"))
        self._find_combo = QComboBox()
        self._find_combo.setEditable(True)
        self._find_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._find_combo.setMinimumWidth(320)
        self._find_combo.lineEdit().setPlaceholderText("検索する文字列を入力")
        self._find_combo.lineEdit().returnPressed.connect(self._replace_next)
        find_row.addWidget(self._find_combo, 1)
        layout.addLayout(find_row)

        # ── 置換後文字列 ──
        rep_row = QHBoxLayout()
        rep_row.addWidget(QLabel("置換後:"))
        self._rep_combo = QComboBox()
        self._rep_combo.setEditable(True)
        self._rep_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._rep_combo.setMinimumWidth(320)
        self._rep_combo.lineEdit().setPlaceholderText("置換後の文字列を入力（空欄で削除）")
        self._rep_combo.lineEdit().returnPressed.connect(self._replace_next)
        rep_row.addWidget(self._rep_combo, 1)
        layout.addLayout(rep_row)

        # ── オプション ──
        opt_box = QGroupBox("オプション")
        opt_lay = QHBoxLayout(opt_box)
        opt_lay.setSpacing(16)

        self._chk_case = _mk_check("大文字/小文字を区別", False)
        self._chk_regex = _mk_check("正規表現", False)
        opt_lay.addWidget(self._chk_case)
        opt_lay.addWidget(self._chk_regex)
        opt_lay.addSpacing(24)

        opt_lay.addWidget(QLabel("対象:"))
        self._field_combo = QComboBox()
        self._field_combo.addItems(["JP（日本語）", "ZH（中文訳）", "JP + ZH 両方"])
        self._field_combo.setFixedWidth(160)
        opt_lay.addWidget(self._field_combo)
        opt_lay.addStretch()
        layout.addWidget(opt_box)

        # ── ステータス ──
        self._status_lbl = QLabel("検索文字列を入力してください")
        self._status_lbl.setStyleSheet("color:#555;font-size:11px;")
        layout.addWidget(self._status_lbl)

        # ── ボタン行 ──
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._btn_prev = QPushButton("◀ 前へ")
        self._btn_prev.setToolTip("前の一致箇所へ移動 (Shift+Enter)")
        self._btn_prev.clicked.connect(self._replace_prev)
        btn_row.addWidget(self._btn_prev)

        self._btn_next = QPushButton("次へ ▶")
        self._btn_next.setDefault(True)
        self._btn_next.setToolTip("次の一致箇所を置換して進む (Enter)")
        self._btn_next.clicked.connect(self._replace_next)
        btn_row.addWidget(self._btn_next)

        self._btn_all = QPushButton("すべて置換")
        self._btn_all.setStyleSheet(
            "QPushButton{background:#2e6fa0;color:white;border-radius:4px;}"
            "QPushButton:hover{background:#3a88c0;}"
        )
        self._btn_all.setToolTip("条件に一致するすべての箇所を置換")
        self._btn_all.clicked.connect(self._replace_all)
        btn_row.addWidget(self._btn_all)

        self._btn_count = QPushButton("件数確認")
        self._btn_count.setToolTip("置換せず一致件数のみを表示")
        self._btn_count.clicked.connect(self._count_matches)
        btn_row.addWidget(self._btn_count)

        btn_row.addStretch()

        btn_close = QPushButton("閉じる")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        # Shift+Enter = 前へ
        sc_prev = QShortcut(QKeySequence("Shift+Return"), self)
        sc_prev.activated.connect(self._replace_prev)

    # ── 内部ヘルパー ──

    def _find_text(self) -> str:
        return self._find_combo.currentText()

    def _rep_text(self) -> str:
        return self._rep_combo.currentText()

    def _push_find_history(self, text: str):
        if text and text not in self._history_find:
            self._history_find.insert(0, text)
            self._history_find = self._history_find[:20]
            self._find_combo.clear()
            self._find_combo.addItems(self._history_find)
            self._find_combo.setCurrentText(text)

    def _push_rep_history(self, text: str):
        if text not in self._history_replace:
            self._history_replace.insert(0, text)
            self._history_replace = self._history_replace[:20]
            self._rep_combo.clear()
            self._rep_combo.addItems(self._history_replace)
            self._rep_combo.setCurrentText(text)

    def _make_pattern(self, find: str):
        """検索パターンを返す。失敗時は None。"""
        flags = 0 if self._chk_case.isChecked() else re.IGNORECASE
        if self._chk_regex.isChecked():
            try:
                return re.compile(find, flags)
            except re.error as e:
                self._status_lbl.setText(f"正規表現エラー: {e}")
                self._status_lbl.setStyleSheet("color:red;font-size:11px;")
                return None
        else:
            return re.compile(re.escape(find), flags)

    def _field_mode(self) -> str:
        idx = self._field_combo.currentIndex()
        return ("jp", "zh", "both")[idx]

    def _iter_strings(self, lines: list):
        """
        Yield (line_idx, field, getter, setter) tuples for each replaceable string.
        getter() → current string
        setter(new_str) → write back
        """
        mode = self._field_mode()
        for li, line in enumerate(lines):
            if mode in ("jp", "both"):
                # JP: read/write the markup string through the segment structure
                def _get_jp(line=line):
                    return segments_to_markup(line.get('jp', []))

                def _set_jp(s, line=line, li=li):
                    t_start = line['start']
                    t_end = line_end_ms(lines, li)
                    new_segs = parse_jp_markup(s, t_start, t_end)
                    merged = merge_jp_timing(line.get('jp', []), new_segs, t_start, t_end)
                    line['jp'] = merged

                yield (li, 'jp', _get_jp, _set_jp)

            if mode in ("zh", "both"):
                def _get_zh(line=line):
                    return line.get('zh', '')

                def _set_zh(s, line=line):
                    line['zh'] = s

                yield (li, 'zh', _get_zh, _set_zh)

    def _count_matches(self):
        find = self._find_text()
        if not find:
            self._status_lbl.setText("検索文字列を入力してください")
            self._status_lbl.setStyleSheet("color:#555;font-size:11px;")
            return
        pat = self._make_pattern(find)
        if pat is None:
            return
        lines = self._get_lines()
        total = sum(
            len(pat.findall(getter()))
            for _, _, getter, _ in self._iter_strings(lines)
        )
        self._status_lbl.setText(f"一致件数: {total} 件")
        self._status_lbl.setStyleSheet(
            "color:#1a6aaa;font-size:11px;font-weight:bold;"
        )

    def _replace_all(self):
        find = self._find_text()
        if not find:
            self._status_lbl.setText("検索文字列を入力してください")
            self._status_lbl.setStyleSheet("color:#555;font-size:11px;")
            return
        pat = self._make_pattern(find)
        if pat is None:
            return
        rep = self._rep_text()
        lines = self._get_lines()
        # Check if anything matches first
        total_before = sum(
            len(pat.findall(getter()))
            for _, _, getter, _ in self._iter_strings(lines)
        )
        if total_before == 0:
            self._status_lbl.setText("一致する箇所が見つかりませんでした")
            self._status_lbl.setStyleSheet("color:#888;font-size:11px;")
            return
        self._push_undo()
        self._push_find_history(find)
        self._push_rep_history(rep)
        count = 0
        for _, _, getter, setter in self._iter_strings(lines):
            original = getter()
            new_str, n = pat.subn(rep, original)
            if n:
                setter(new_str)
                count += n
        self._refresh()
        self.replaced.emit(count)

    def _replace_next(self):
        self._step_replace(forward=True)

    def _replace_prev(self):
        self._step_replace(forward=False)

    def _step_replace(self, forward: bool):
        find = self._find_text()
        if not find:
            self._status_lbl.setText("検索文字列を入力してください")
            self._status_lbl.setStyleSheet("color:#555;font-size:11px;")
            return
        pat = self._make_pattern(find)
        if pat is None:
            return
        rep = self._rep_text()
        lines = self._get_lines()
        items = list(self._iter_strings(lines))
        if not forward:
            items = list(reversed(items))

        found_any = False
        for _, _, getter, setter in items:
            original = getter()
            if pat.search(original):
                found_any = True
                self._push_undo()
                self._push_find_history(find)
                self._push_rep_history(rep)
                new_str = pat.sub(rep, original, count=1)
                setter(new_str)
                self._refresh()
                self.replaced.emit(1)
                return

        if not found_any:
            self._status_lbl.setText("一致する箇所が見つかりませんでした")
            self._status_lbl.setStyleSheet("color:#888;font-size:11px;")

    def _on_replaced(self, count: int):
        self._status_lbl.setText(f"✔ {count} 件を置換しました")
        self._status_lbl.setStyleSheet("color:#1a8033;font-size:11px;font-weight:bold;")


def _mk_check(label: str, checked: bool):
    from PyQt6.QtWidgets import QCheckBox
    cb = QCheckBox(label)
    cb.setChecked(checked)
    return cb


# ══════════════════════════════════════════════════════════════
#  Batch Translate Dialog
# ══════════════════════════════════════════════════════════════

class BatchTranslateDialog(QDialog):
    """
    一括翻訳編集ダイアログ（強化版）

    左ペイン : JP/ZH対応テーブル（複数行選択・直接入力可）
    右ペイン : 一括貼り付けパネル
                - ZH字幕を改行区切りで貼り付け → テーブルに一括適用
                - 配置モード: 行1から / 選択行から / 空行のみ
                - 貼り付け側の空行スキップ / 既存ZH上書きオプション
    下ボタン : ZH↑/ZH↓（選択行ZHを上下にシフト）、ZH消去
    """

    def __init__(self, lines: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("一括翻訳編集")
        self.setModal(True)
        self.resize(1060, 560)
        self._lines = lines
        self._result: Optional[list] = None
        self._dirty = False
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        hint = QLabel(
            "左列: 日本語原文（編集不可）　右列: 中文訳（直接入力 / 複数行選択→右パネルで一括操作）"
        )
        hint.setStyleSheet("color:#555;font-size:11px;")
        layout.addWidget(hint)

        # ── 水平スプリッタ: テーブル | 貼り付けパネル ──
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── 左: テーブル + ZH シフトボタン ──
        table_widget = QWidget()
        table_lay = QVBoxLayout(table_widget)
        table_lay.setContentsMargins(0, 0, 4, 0)
        table_lay.setSpacing(4)

        self._table = QTableWidget(len(self._lines), 2)
        self._table.setHorizontalHeaderLabels(["日本語 (JP)", "中文訳 (ZH)"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setDefaultSectionSize(30)
        self._table.setTabKeyNavigation(True)

        jp_font = QFont(_NOTO_JP or "", 12)
        self._zh_font = QFont("Microsoft YaHei", 12)

        for row, line in enumerate(self._lines):
            jp_text = segments_to_display_text(line.get('jp', []))
            jp_item = QTableWidgetItem(jp_text)
            jp_item.setFlags(jp_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            jp_item.setForeground(QColor("#444"))
            jp_item.setFont(jp_font)
            jp_item.setToolTip(jp_text)
            self._table.setItem(row, 0, jp_item)

            zh_item = QTableWidgetItem(line.get('zh', ''))
            zh_item.setFont(self._zh_font)
            self._table.setItem(row, 1, zh_item)

        self._table.itemChanged.connect(self._on_cell_changed)
        self._table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        table_lay.addWidget(self._table, 1)

        # ZH 移動／消去ボタン行
        move_row = QHBoxLayout()
        self._sel_label = QLabel("（行を選択してください）")
        self._sel_label.setStyleSheet("color:#777;font-size:11px;")
        move_row.addWidget(self._sel_label)
        move_row.addStretch()
        for text, tip, fn in (
            ("ZH ↑", "選択行のZH訳を1行上に移動（上の行と循環入れ替え）", self._shift_zh_up),
            ("ZH ↓", "選択行のZH訳を1行下に移動（下の行と循環入れ替え）", self._shift_zh_down),
            ("ZH 消去", "選択行のZH訳をすべて消去", self._clear_selected_zh),
        ):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.setFixedHeight(28)
            btn.clicked.connect(fn)
            move_row.addWidget(btn)
        table_lay.addLayout(move_row)
        splitter.addWidget(table_widget)

        # ── 右: 一括貼り付けパネル ──
        paste_widget = QWidget()
        paste_widget.setMinimumWidth(230)
        paste_lay = QVBoxLayout(paste_widget)
        paste_lay.setContentsMargins(4, 0, 0, 0)
        paste_lay.setSpacing(6)

        paste_title = QLabel("一括貼り付け")
        paste_title.setStyleSheet("font-weight:bold;color:#222;font-size:13px;")
        paste_lay.addWidget(paste_title)

        paste_hint = QLabel(
            "ZH字幕を改行区切りで貼り付け。\n"
            "行数が一致しなくても問題ありません。"
        )
        paste_hint.setStyleSheet("color:#666;font-size:11px;")
        paste_hint.setWordWrap(True)
        paste_lay.addWidget(paste_hint)

        self._paste_edit = QTextEdit()
        self._paste_edit.setFont(QFont("Microsoft YaHei", 11))
        self._paste_edit.setPlaceholderText(
            "ここに複数行のZH字幕を貼り付け…\n\n"
            "例:\n"
            "第一行的翻译\n"
            "第二行的翻译\n"
            "（空行は空白ZHとして扱われます）"
        )
        self._paste_edit.textChanged.connect(self._update_paste_count)
        paste_lay.addWidget(self._paste_edit, 1)

        self._paste_count_lbl = QLabel("貼り付け行数: 0 行")
        self._paste_count_lbl.setStyleSheet("color:#999;font-size:10px;")
        paste_lay.addWidget(self._paste_count_lbl)

        # 配置オプション
        opt_grp = QGroupBox("配置オプション")
        opt_lay = QVBoxLayout(opt_grp)
        opt_lay.setSpacing(5)

        opt_lay.addWidget(QLabel("開始位置:"))
        self._mode_combo = QComboBox()
        self._mode_combo.addItems([
            "行 1 から配置",
            "選択行から配置",
            "空行のみ埋める（全体）",
        ])
        self._mode_combo.setToolTip(
            "行1から: 貼り付け1行目 → テーブル行1\n"
            "選択行から: 選択中の最上行から配置\n"
            "空行のみ: ZHが空の行に順番に埋める"
        )
        opt_lay.addWidget(self._mode_combo)

        self._overwrite_check = _mk_check("既存のZHを上書き", True)
        self._overwrite_check.setToolTip("オフにすると、ZH入力済みの行はスキップします")
        opt_lay.addWidget(self._overwrite_check)

        self._skip_empty_paste_check = _mk_check("貼り付け側の空行をスキップ", False)
        self._skip_empty_paste_check.setToolTip(
            "オンにすると、貼り付けテキストの空行を無視して詰めて配置します"
        )
        opt_lay.addWidget(self._skip_empty_paste_check)
        paste_lay.addWidget(opt_grp)

        # 適用ボタン
        btn_apply = QPushButton("▼  適用")
        btn_apply.setStyleSheet(
            "QPushButton{background:#2e6fa0;color:white;border-radius:5px;"
            "font-size:13px;font-weight:bold;}"
            "QPushButton:hover{background:#3a88c0;}"
        )
        btn_apply.setFixedHeight(38)
        btn_apply.setToolTip("貼り付けテキストをテーブルのZH列に適用 (Ctrl+Enter)")
        btn_apply.clicked.connect(self._apply_paste)
        paste_lay.addWidget(btn_apply)

        # Ctrl+Enter shortcut for apply
        sc_apply = QShortcut(QKeySequence("Ctrl+Return"), self)
        sc_apply.activated.connect(self._apply_paste)

        btn_clear_paste = QPushButton("貼り付けエリアを消去")
        btn_clear_paste.setStyleSheet("color:#888;")
        btn_clear_paste.clicked.connect(self._paste_edit.clear)
        paste_lay.addWidget(btn_clear_paste)

        splitter.addWidget(paste_widget)
        splitter.setSizes([660, 370])
        layout.addWidget(splitter, 1)

        # OK / キャンセル
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(self._accept)
        bb.rejected.connect(self._on_cancel)
        layout.addWidget(bb)

    # ── シグナルハンドラ ──

    def _on_cell_changed(self, item: QTableWidgetItem):
        if item.column() == 1:
            self._dirty = True

    def _on_selection_changed(self):
        count = len(self._table.selectionModel().selectedRows())
        self._sel_label.setText(
            f"{count} 行選択中" if count else "（行を選択してください）"
        )

    def _update_paste_count(self):
        raw = self._paste_edit.toPlainText().splitlines()
        non_empty = sum(1 for l in raw if l.strip())
        self._paste_count_lbl.setText(
            f"貼り付け行数: {len(raw)} 行（非空白: {non_empty} 行）"
        )

    # ── 一括貼り付け ──

    def _apply_paste(self):
        raw = self._paste_edit.toPlainText().splitlines()
        if not raw:
            return
        skip_empty = self._skip_empty_paste_check.isChecked()
        paste_lines = [l for l in raw if l.strip()] if skip_empty else raw
        if not paste_lines:
            return

        mode = self._mode_combo.currentIndex()
        overwrite = self._overwrite_check.isChecked()
        n_rows = self._table.rowCount()
        self._table.blockSignals(True)

        if mode == 2:
            # 空行のみ埋める
            pi = 0
            for row in range(n_rows):
                if pi >= len(paste_lines):
                    break
                cur = self._get_zh(row)
                if not cur.strip():
                    self._set_zh(row, paste_lines[pi])
                    pi += 1
        else:
            if mode == 0:
                start = 0
            else:
                sel = self._table.selectionModel().selectedRows()
                start = min(r.row() for r in sel) if sel else 0
            for pi, text in enumerate(paste_lines):
                row = start + pi
                if row >= n_rows:
                    break
                if overwrite or not self._get_zh(row).strip():
                    self._set_zh(row, text)

        self._table.blockSignals(False)
        self._dirty = True

    # ── ZH テキストの取得・設定ヘルパー ──

    def _get_zh(self, row: int) -> str:
        item = self._table.item(row, 1)
        return item.text() if item else ''

    def _set_zh(self, row: int, text: str):
        item = self._table.item(row, 1)
        if item is not None:
            item.setText(text)
        else:
            new_item = QTableWidgetItem(text)
            new_item.setFont(self._zh_font)
            self._table.setItem(row, 1, new_item)

    # ── ZH シフト操作 ──
    # 選択行 + その直上/直下の行を「循環入れ替え」することで、
    # 選択ブロックの ZH テキストを 1 行上/下に移動する。

    def _shift_zh_up(self):
        sel = sorted(r.row() for r in self._table.selectionModel().selectedRows())
        if not sel or sel[0] == 0:
            return
        self._table.blockSignals(True)
        block = [sel[0] - 1] + sel
        texts = [self._get_zh(r) for r in block]
        rotated = texts[1:] + [texts[0]]        # 左回転: 選択ZHが1行上へ
        for r, t in zip(block, rotated):
            self._set_zh(r, t)
        self._table.blockSignals(False)
        self._dirty = True

    def _shift_zh_down(self):
        sel = sorted(r.row() for r in self._table.selectionModel().selectedRows())
        n = self._table.rowCount()
        if not sel or sel[-1] >= n - 1:
            return
        self._table.blockSignals(True)
        block = sel + [sel[-1] + 1]
        texts = [self._get_zh(r) for r in block]
        rotated = [texts[-1]] + texts[:-1]      # 右回転: 選択ZHが1行下へ
        for r, t in zip(block, rotated):
            self._set_zh(r, t)
        self._table.blockSignals(False)
        self._dirty = True

    def _clear_selected_zh(self):
        sel = self._table.selectionModel().selectedRows()
        if not sel:
            return
        self._table.blockSignals(True)
        for idx in sel:
            self._set_zh(idx.row(), '')
        self._table.blockSignals(False)
        self._dirty = True

    # ── 確定 / キャンセル ──

    def _accept(self):
        self._result = [
            self._table.item(row, 1).text() if self._table.item(row, 1) else ''
            for row in range(len(self._lines))
        ]
        self.accept()

    def _on_cancel(self):
        if self._dirty:
            r = QMessageBox.question(
                self,
                "キャンセルの確認",
                "編集内容が保存されていません。\nキャンセルして変更を破棄しますか？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        self.reject()

    def result_zh(self) -> Optional[list]:
        """Returns list of zh strings in line order, or None if cancelled."""
        return self._result


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
        self._ui_scale = 1.0
        self._scale_actions: dict = {}

        # Tap mode state
        self._tap_mode = False        # line-start tap mode
        self._tap_cur = 0             # current line index being tapped
        self._tap_undo_stack: list = []   # [{tap_cur, start}, ...]

        # Waveform loader
        self._wf_loader: Optional[WaveformLoader] = None
        self._pending_wf_zoom: Optional[float] = None
        self._pending_wf_scroll: Optional[float] = None
        self._replace_dialog: Optional[ReplaceDialog] = None

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
        self._audio.status_message.connect(self._status_audio)
        self._audio.waveform.drag_start.connect(self._on_waveform_drag_start)
        self._audio.waveform.line_time_changed.connect(self._on_waveform_line_drag)
        self._audio.waveform.drag_end.connect(self._on_waveform_drag_end)

        # Left: lyrics list
        self._list_panel = LyricsListPanel()
        self._list_panel.selection_changed.connect(self._on_line_selected)
        self._list_panel.jump_requested.connect(self._jump_to_line)
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

        # Central vertical splitter: editor area / audio area
        self._audio.set_waveform_height(self._audio.waveform_height())
        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.addWidget(self._splitter)
        self._main_splitter.addWidget(self._audio)
        self._main_splitter.setHandleWidth(7)
        self._main_splitter.setStretchFactor(0, 1)
        self._main_splitter.setStretchFactor(1, 0)
        self._main_splitter.setCollapsible(0, False)
        self._main_splitter.setCollapsible(1, False)
        self._main_splitter.splitterMoved.connect(self._sync_waveform_height_from_audio_panel)
        self._main_splitter.setStyleSheet(
            "QSplitter::handle:vertical{"
            "background:#f8f9fd;border-top:1px solid #cdd1e1;"
            "border-bottom:1px solid #eef0f8;"
            "}"
            "QSplitter::handle:vertical:hover{background:#e8ebf5;}"
        )
        self._main_splitter.setSizes([620, self._audio.minimumHeight()])

        self.setCentralWidget(self._main_splitter)

        # ── Row 1: Menu bar (file / LRC operations, collapsible) ──
        mb = self.menuBar()
        mb.setStyleSheet(
            "QMenuBar{"
            "background:#eef0f8;border-bottom:1px solid #d2d5ea;"
            "padding:1px 6px;spacing:1px;"
            "}"
            "QMenuBar::item{"
            "padding:3px 12px;border-radius:4px;background:transparent;"
            "color:#2a2c3e;"
            "}"
            "QMenuBar::item:selected{background:#dde0f4;}"
            "QMenuBar::item:pressed{background:#cdd0ec;}"
            "QMenu{"
            "background:#ffffff;border:1px solid #cdd0ea;"
            "border-radius:6px;padding:3px 0;"
            "}"
            "QMenu::item{"
            "padding:4px 24px 4px 12px;border-radius:3px;margin:1px 4px;"
            "color:#1a1c2e;"
            "}"
            "QMenu::item:selected{background:#eaecf8;}"
            "QMenu::item:disabled{color:#aaa;}"
            "QMenu::separator{height:1px;background:#e4e7f2;margin:3px 8px;}"
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
        _act_open.setToolTip("プロジェクト (.flproj) または歌詞 (.flrc) を開く")
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

        _act_json_out = QAction("歌詞 FLRC として書出…", self)
        _act_json_out.setToolTip("現在の歌詞データを FLRC ファイルに書き出す")
        _act_json_out.triggered.connect(self._export_json)
        _fm.addAction(_act_json_out)

        # ── 編集メニュー ──
        _em = mb.addMenu("編集")
        _act_replace = QAction("置換…", self)
        _act_replace.setShortcut(QKeySequence("Ctrl+H"))
        _act_replace.setToolTip("全文置換ダイアログを開く (Ctrl+H)")
        _act_replace.triggered.connect(self._open_replace_dialog)
        _em.addAction(_act_replace)

        _act_converge = QAction("収束…", self)
        _act_converge.setToolTip(
            "最後のユニットの終了時間が次の行の開始時間を超えている箇所を修正する"
        )
        _act_converge.triggered.connect(self._converge_last_units)
        _em.addAction(_act_converge)

        _act_batch_zh = QAction("一括翻訳編集…", self)
        _act_batch_zh.setShortcut(QKeySequence("Ctrl+T"))
        _act_batch_zh.setToolTip("全行の中文訳をまとめて編集する (Ctrl+T)")
        _act_batch_zh.triggered.connect(self._open_batch_translate_dialog)
        _em.addAction(_act_batch_zh)

        _wm = mb.addMenu("ウィンドウ")
        _scale_menu = _wm.addMenu("表示倍率")
        _scale_group = QActionGroup(self)
        _scale_group.setExclusive(True)
        for label, scale in (
            ("80%", 0.80),
            ("90%", 0.90),
            ("100%", 1.00),
            ("110%", 1.10),
        ):
            act = QAction(label, self)
            act.setCheckable(True)
            act.setData(scale)
            act.triggered.connect(lambda checked=False, s=scale: self._set_ui_scale(s))
            _scale_group.addAction(act)
            _scale_menu.addAction(act)
            self._scale_actions[scale] = act
        self._scale_actions[self._ui_scale].setChecked(True)

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

    def _sync_waveform_height_from_audio_panel(self, *_):
        target = self._audio.height() - self._audio.waveform_chrome_height()
        self._audio.set_waveform_height(target)

    def _status_audio(self, msg: str):
        if hasattr(self, '_status'):
            self._status.showMessage(msg)

    def _set_ui_scale(self, scale: float, resize_window: bool = True, mark_dirty: bool = True):
        scale = max(0.75, min(1.20, float(scale)))
        old_scale = self._ui_scale
        changed = abs(old_scale - scale) >= 0.001
        self._ui_scale = scale

        for s, act in self._scale_actions.items():
            act.setChecked(abs(s - scale) < 0.001)

        app = QApplication.instance()
        if app is not None:
            self._scale_widget_fonts(app.allWidgets(), scale)

        if resize_window and old_scale > 0:
            ratio = scale / old_scale
            geom = self.geometry()
            self.resize(
                max(760, int(geom.width() * ratio)),
                max(520, int(geom.height() * ratio)),
            )
        self.updateGeometry()
        if changed and mark_dirty and (
            self._save_path is None or self._save_path.lower().endswith('.flproj')
        ):
            self._dirty = True
            self._status.showMessage(f"表示倍率: {int(scale * 100)}%")

    def _scale_widget_fonts(self, widgets, scale: float):
        updates = []
        for widget in widgets:
            font = widget.font()
            base = widget.property("_base_point_size")
            if base is None:
                base = font.pointSizeF()
                if base <= 0:
                    base = 10.0
                widget.setProperty("_base_point_size", base)
            updates.append((widget, font, float(base)))
        for widget, font, base in updates:
            font.setPointSizeF(max(6.0, float(base) * scale))
            widget.setFont(font)

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
            self, "MP3ファイルを開く", str(_DIR_SONGS),
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
            self, "ファイルを開く", self._save_path or str(_DIR_FLRC),
            "プロジェクト・歌詞 (*.flproj *.flrc);;プロジェクト (*.flproj);;FLRC歌詞 (*.flrc);;All Files (*)"
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
            "プロジェクト (*.flproj);;FLRC歌詞 (*.flrc);;All Files (*)"
        )
        if path:
            if not (path.lower().endswith('.flproj') or path.lower().endswith('.flrc')):
                path += '.flproj'
            self._save_path = path
            self._write(path)

    def _write(self, path: str):
        if path.lower().endswith('.flproj'):
            self._write_project(path)
        else:
            self._write_flrc(path)

    def _write_flrc(self, path: str):
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
                "main_splitter_sizes": self._main_splitter.sizes(),
                "waveform": {
                    "zoom": self._audio.waveform._zoom,
                    "scroll_ms": self._audio.waveform._scroll_ms,
                    "height": self._audio.waveform_height(),
                },
                "options": {
                    "speed_idx": (self._audio._speed_combo.currentIndex()
                                  if self._audio._speed_combo else 2),
                    "volume": (self._audio._vol_slider.value()
                               if hasattr(self._audio, '_vol_slider') else 100),
                    "ui_scale": self._ui_scale,
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
            main_sizes = raw.get("main_splitter_sizes")
            if main_sizes and len(main_sizes) == 2:
                self._main_splitter.setSizes(main_sizes)
            # Schedule waveform view restore (applied after waveform loads)
            wf = raw.get("waveform", {})
            if wf:
                self._pending_wf_zoom = max(1.0, float(wf.get("zoom", 1.0)))
                self._pending_wf_scroll = max(0.0, float(wf.get("scroll_ms", 0.0)))
                self._audio.set_waveform_height(
                    wf.get("height", WaveformWidget.HEIGHT_DEFAULT)
                )
            # Restore options
            opts = raw.get("options", {})
            if self._audio._speed_combo:
                idx = opts.get("speed_idx", 2)
                self._audio._speed_combo.setCurrentIndex(
                    max(0, min(idx, self._audio._speed_combo.count() - 1))
                )
            if hasattr(self._audio, '_vol_slider'):
                self._audio._vol_slider.setValue(opts.get("volume", 100))
            self._set_ui_scale(
                opts.get("ui_scale", 1.0),
                resize_window=False,
                mark_dirty=False,
            )
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

    def _jump_to_line(self, idx: int):
        if 0 <= idx < len(self._lines):
            ms = int(self._lines[idx].get('start', 0))
            self._list_panel.set_current_row(idx)
            self._audio._seek(ms)
            self._status.showMessage(f"移動: 行 {idx + 1}  {ms_to_str(ms)}")

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
                f"furi-lrc-gui — {Path(path).stem}.flrc (LRCから変換)"
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
            QMessageBox.warning(self, "FLRC書出", "歌詞データが空です。")
            return
        stem = Path(self._save_path).stem if self._save_path else "lyrics"
        if stem.lower().endswith('.flproj'):
            stem = stem[:-7]
        default_name = str(_DIR_FLRC / (stem + ".flrc"))
        path, _ = self._save_file_dialog(
            self, "FLRC歌詞として保存", default_name,
            "FLRC Files (*.flrc);;All Files (*)"
        )
        if not path:
            return
        if not path.lower().endswith('.flrc'):
            path += '.flrc'
        try:
            Path(path).write_text(
                json.dumps({"lines": self._lines}, ensure_ascii=False, indent=2),
                'utf-8'
            )
            self._status.showMessage(f"FLRC書出完了: {path}")
        except Exception as e:
            QMessageBox.critical(self, "FLRC書出エラー", str(e))

    # ─── converge last units ───

    def _converge_last_units(self):
        """
        収束: 各行の最後のユニットの終了時間 (e) が次の行の開始時間を超えている
        場合、その e を次の行の開始時間に揃える。

        条件:
          last_unit['s'] < next_line['start']   (最後拍の開始 < 次行開始)
          last_unit['e'] > next_line['start']   (最後拍の終了 > 次行開始)
        """
        if not self._lines:
            QMessageBox.information(self, "収束", "歌詞データが空です。")
            return

        # ── スキャンフェーズ ──
        candidates: list = []   # (line_idx, last_unit_ref, old_e, new_e)
        for i in range(len(self._lines) - 1):
            line = self._lines[i]
            next_start = self._lines[i + 1]['start']
            segs = line.get('jp', [])
            # Find the very last unit across all segments
            last_unit = None
            for seg in segs:
                units = seg.get('units', [])
                if units:
                    last_unit = units[-1]
            if last_unit is None:
                continue
            if last_unit['s'] < next_start and last_unit['e'] > next_start:
                candidates.append((i, last_unit, last_unit['e'], next_start))

        count = len(candidates)
        if count == 0:
            QMessageBox.information(
                self, "収束",
                "修正が必要な箇所は見つかりませんでした。"
            )
            return

        # ── 確認ダイアログ ──
        confirm = QMessageBox(self)
        confirm.setWindowTitle("収束")
        confirm.setText(
            f"{count} 箇所で最後のユニットの終了時間が次の行の開始時間を超えています。\n"
            "これらを次の行の開始時間に揃えますか？"
        )
        confirm.setStandardButtons(
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        confirm.setDefaultButton(QMessageBox.StandardButton.Ok)
        if confirm.exec() != QMessageBox.StandardButton.Ok:
            return

        # ── 適用 ──
        self._push_undo()
        details: list = []
        for line_idx, unit_ref, old_e, new_e in candidates:
            unit_ref['e'] = new_e
            line = self._lines[line_idx]
            char = unit_ref.get('k', '?')
            details.append(
                f"  行 {line_idx + 1}  「{char}」  {old_e} ms → {new_e} ms"
            )
        self._dirty = True
        self._refresh_all(preserve_sel=True)

        # ── 結果ダイアログ (保留 / 撤銷) ──
        result_dlg = QDialog(self)
        result_dlg.setWindowTitle("収束 — 結果")
        result_dlg.setModal(True)
        result_dlg.resize(480, 300)
        vlay = QVBoxLayout(result_dlg)
        vlay.setSpacing(8)

        summary = QLabel(f"✔ {count} 箇所を修正しました:")
        summary.setStyleSheet("font-weight:bold;")
        vlay.addWidget(summary)

        detail_edit = QTextEdit()
        detail_edit.setReadOnly(True)
        detail_edit.setPlainText("\n".join(details))
        detail_edit.setFont(QFont("Consolas", 9))
        vlay.addWidget(detail_edit, 1)

        btn_row = QHBoxLayout()
        btn_keep = QPushButton("保留（変更を確定）")
        btn_keep.setDefault(True)
        btn_keep.setStyleSheet(
            "QPushButton{background:#2e8b4a;color:white;border-radius:4px;}"
            "QPushButton:hover{background:#39a85c;}"
        )
        btn_undo = QPushButton("撤銷（変更を元に戻す）")
        btn_undo.setStyleSheet(
            "QPushButton{background:#c0392b;color:white;border-radius:4px;}"
            "QPushButton:hover{background:#d9534f;}"
        )
        btn_row.addWidget(btn_keep)
        btn_row.addWidget(btn_undo)
        vlay.addLayout(btn_row)

        def _keep():
            result_dlg.accept()
            self._status.showMessage(f"収束: {count} 箇所を修正しました")

        def _revert():
            self._undo()
            result_dlg.reject()
            self._status.showMessage("収束: 変更を元に戻しました")

        btn_keep.clicked.connect(_keep)
        btn_undo.clicked.connect(_revert)
        result_dlg.exec()

    # ─── replace dialog ───

    def _open_batch_translate_dialog(self):
        if not self._lines:
            QMessageBox.information(self, "一括翻訳編集", "歌詞データが空です。")
            return
        dlg = BatchTranslateDialog(self._lines, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            zh_list = dlg.result_zh()
            if zh_list is not None:
                self._push_undo()
                for i, zh in enumerate(zh_list):
                    if i < len(self._lines):
                        self._lines[i]['zh'] = zh
                self._refresh_all(preserve_sel=True)
                self._status.showMessage("一括翻訳編集を保存しました")

    def _open_replace_dialog(self):
        if self._replace_dialog is None or not self._replace_dialog.isVisible():
            self._replace_dialog = ReplaceDialog(
                get_lines_fn=lambda: self._lines,
                push_undo_fn=self._push_undo,
                refresh_fn=lambda: self._refresh_all(preserve_sel=True),
                parent=self,
            )
        self._replace_dialog.show()
        self._replace_dialog.raise_()
        self._replace_dialog.activateWindow()

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
    # Keep Qt rendering in logical pixels and let the OS/Qt choose native backing
    # scale. PassThrough avoids blurry fractional-DPI rounding on mixed displays.
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu-compositing")

    if (
        hasattr(QApplication, "setHighDpiScaleFactorRoundingPolicy")
        and hasattr(Qt, "HighDpiScaleFactorRoundingPolicy")
    ):
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )

    app = QApplication(sys.argv)
    app.setApplicationName("furi-lrc-gui")
    app.setStyle("Fusion")

    # ── Application / taskbar icon ──
    _icon_path = _BASE_DIR / "icon-i.ico"
    if not _icon_path.exists():
        _icon_path = _BASE_DIR / "icon-i.png"
    app.setWindowIcon(QIcon(str(_icon_path)))

    # ── Load UI font: NotoSansJP ──
    _font_path = str(_BASE_DIR / "fonts" / "NotoSansJP-Regular.ttf")
    _fid = QFontDatabase.addApplicationFont(_font_path)
    _families = QFontDatabase.applicationFontFamilies(_fid)
    if _families:
        global _NOTO_JP
        _NOTO_JP = _families[0]
        app.setFont(QFont(_NOTO_JP, 10))

    # ── Load UI font: Microsoft YaHei Bold ──
    _msyh_path = str(_BASE_DIR / "fonts" / "msyhbd.ttc")
    _msyh_fid = QFontDatabase.addApplicationFont(_msyh_path)
    _msyh_families = QFontDatabase.applicationFontFamilies(_msyh_fid)
    if _msyh_families:
        global _MSYH_BD
        _MSYH_BD = _msyh_families[0]

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
