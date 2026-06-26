#!/usr/bin/env python3
"""
furi-lrc-player — PotPlayer-style audio player for furi-lrc lyrics overlay.

Drives LyricsCanvas directly from internal QMediaPlayer; SMTC not required.
Requires: PyQt6, PyQt6-Qt6Multimedia
Optional: mutagen  (track tags + album art)
"""

import sys
import json
import enum
import random
import importlib.util
import dataclasses
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QListWidget, QListWidgetItem, QLabel, QPushButton,
    QSlider, QFileDialog, QMenu, QSizePolicy, QFrame, QScrollArea,
    QAbstractItemView, QToolButton, QDialog, QDialogButtonBox,
    QFormLayout, QLineEdit,
)
from PyQt6.QtCore import (
    Qt, QTimer, QUrl, QSize, pyqtSignal, QObject, QPoint, QPointF,
    QPropertyAnimation, QEasingCurve, QRectF, QMimeData, QModelIndex,
)
from PyQt6.QtGui import (
    QColor, QCursor, QPainter, QPen, QFont, QFontDatabase,
    QPixmap, QLinearGradient, QBrush, QAction, QKeySequence, QShortcut,
    QDragEnterEvent, QDropEvent, QIcon, QPolygonF,
)

try:
    from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
    HAS_MEDIA = True
except ImportError:
    HAS_MEDIA = False
    QMediaPlayer = None
    QAudioOutput = None

try:
    import mutagen
    import mutagen.mp3
    import mutagen.flac
    import mutagen.id3
    HAS_MUTAGEN = True
except ImportError:
    HAS_MUTAGEN = False

# ── Load overlay module (furi-lrc_rubi.py) ──────────────────────────────────
def _load_overlay():
    p = Path(__file__).parent / "furi-lrc_rubi.py"
    if not p.exists():
        return None
    spec = importlib.util.spec_from_file_location("_furi_lrc_rubi", p)
    mod  = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:
        print(f"[player] overlay module error: {e}", file=sys.stderr)
        return None

_OV = _load_overlay()

# ── Theme ───────────────────────────────────────────────────────────────────
_DARK = "#1e1e2e"
_PANEL = "#181825"
_SURFACE = "#313244"
_OVERLAY_BG = "#24273a"
_TEXT = "#cdd6f4"
_SUB  = "#a6adc8"
_BLUE = "#89b4fa"
_BLUE2 = "#7287fd"
_GREEN = "#a6e3a1"
_SEL  = "#45475a"
_BORDER = "#313244"

_STYLESHEET = f"""
QMainWindow, QWidget {{ background: {_DARK}; color: {_TEXT}; font-family: "Noto Sans JP", "Segoe UI", sans-serif; }}
QFrame[frameRole="panel"] {{ background: {_PANEL}; border: 1px solid {_BORDER}; border-radius: 6px; }}
QLabel {{ color: {_TEXT}; background: transparent; }}
QLabel[role="title"] {{ font-size: 16px; font-weight: bold; }}
QLabel[role="sub"]   {{ font-size: 11px; color: {_SUB}; }}
QLabel[role="time"]  {{ font-size: 11px; color: {_SUB}; font-family: Consolas, monospace; }}
QPushButton {{
    background: {_SURFACE}; color: {_TEXT}; border: none;
    border-radius: 5px; padding: 5px 10px; font-size: 13px;
}}
QPushButton:hover   {{ background: {_SEL}; }}
QPushButton:pressed {{ background: {_BORDER}; }}
QPushButton:checked {{ background: {_BLUE}; color: {_DARK}; }}
QPushButton:disabled {{ color: {_BORDER}; }}
QListWidget {{
    background: {_PANEL}; color: {_TEXT}; border: 1px solid {_BORDER};
    border-radius: 4px; outline: none;
}}
QListWidget::item {{ padding: 4px 8px; border-radius: 3px; }}
QListWidget::item:selected  {{ background: {_SURFACE}; }}
QListWidget::item:hover     {{ background: {_SEL}; }}
QScrollBar:vertical {{
    background: {_PANEL}; width: 6px; border-radius: 3px; margin: 0;
}}
QScrollBar::handle:vertical {{ background: {_SURFACE}; border-radius: 3px; min-height: 20px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QSplitter::handle {{ background: {_BORDER}; width: 1px; }}
QMenu {{
    background: {_PANEL}; border: 1px solid {_BORDER}; border-radius: 6px; padding: 4px 0;
}}
QMenu::item {{ padding: 6px 24px 6px 14px; border-radius: 3px; margin: 1px 5px; }}
QMenu::item:selected {{ background: {_SURFACE}; }}
QMenu::separator {{ height: 1px; background: {_BORDER}; margin: 4px 10px; }}
"""

# ── Helpers ──────────────────────────────────────────────────────────────────
def _ms_fmt(ms: int) -> str:
    ms = max(0, int(ms))
    s, ms2 = divmod(ms, 1000)
    m, s   = divmod(s, 60)
    return f"{m}:{s:02d}"


@dataclasses.dataclass
class Track:
    path:        str
    title:       str = ""
    artist:      str = ""
    album:       str = ""
    dur_ms:      int = 0      # filled by QMediaPlayer on first play
    art_data:    bytes = dataclasses.field(default_factory=bytes, repr=False)
    lyrics_path: str = ""     # manually assigned JSON lyrics

    def display_title(self) -> str:
        return self.title or Path(self.path).stem

    def display_artist(self) -> str:
        return self.artist or "Unknown Artist"


def _read_tags(path: str) -> Tuple[str, str, str, bytes]:
    """Return (title, artist, album, cover_bytes). Falls back to filename."""
    title = artist = album = ""
    art   = b""
    stem  = Path(path).stem
    if HAS_MUTAGEN:
        try:
            f = mutagen.File(path, easy=True)
            if f:
                title  = str(f.get("title",  [stem])[0])
                artist = str(f.get("artist", [""])[0])
                album  = str(f.get("album",  [""])[0])
            # album art (ID3)
            f2 = mutagen.File(path)
            if f2:
                for tag in f2.values():
                    if hasattr(tag, "data") and hasattr(tag, "mime"):
                        art = tag.data
                        break
        except Exception:
            pass
    return title or stem, artist, album, art


def _build_track(path: str) -> Track:
    title, artist, album, art = _read_tags(path)
    return Track(path=path, title=title, artist=artist, album=album, art_data=art)


def _lock_icon(locked: bool) -> QIcon:
    px = QPixmap(24, 24)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("white"), 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(6, 10, 12, 9, 2, 2)
    if locked:
        p.drawArc(8, 4, 8, 10, 0, 180 * 16)
    else:
        p.drawArc(11, 4, 8, 10, 35 * 16, 180 * 16)
    p.end()
    return QIcon(px)


def _player_icon(kind: str, color: str = _TEXT) -> QIcon:
    px = QPixmap(24, 24)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(color)
    pen = QPen(c, 2.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(c)

    def tri(points):
        p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))

    def arrow_head(x, y, left=True):
        if left:
            tri([(x, y), (x + 4, y - 3), (x + 4, y + 3)])
        else:
            tri([(x, y), (x - 4, y - 3), (x - 4, y + 3)])

    if kind == "play":
        tri([(8, 5), (8, 19), (18, 12)])
    elif kind == "pause":
        p.drawRoundedRect(QRectF(7, 5, 3.5, 14), 1, 1)
        p.drawRoundedRect(QRectF(13.5, 5, 3.5, 14), 1, 1)
    elif kind == "stop":
        p.drawRoundedRect(QRectF(7, 7, 10, 10), 1.5, 1.5)
    elif kind == "prev":
        p.drawLine(6, 6, 6, 18)
        tri([(8, 12), (14, 6), (14, 18)])
        tri([(14, 12), (20, 6), (20, 18)])
    elif kind == "next":
        p.drawLine(18, 6, 18, 18)
        tri([(16, 12), (10, 6), (10, 18)])
        tri([(10, 12), (4, 6), (4, 18)])
    elif kind == "seek_back":
        tri([(8, 12), (14, 7), (14, 17)])
        tri([(14, 12), (20, 7), (20, 17)])
    elif kind == "seek_forward":
        tri([(16, 12), (10, 7), (10, 17)])
        tri([(10, 12), (4, 7), (4, 17)])
    elif kind == "volume":
        tri([(4, 10), (8, 10), (13, 6), (13, 18), (8, 14), (4, 14)])
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(14, 8, 5, 8, -45 * 16, 90 * 16)
        p.drawArc(16, 6, 7, 12, -45 * 16, 90 * 16)
    elif kind == "loop_seq":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(5, 12, 18, 12)
        arrow_head(19, 12, left=False)
    elif kind == "loop_all":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(5, 6, 14, 9, 20 * 16, 210 * 16)
        p.drawArc(5, 9, 14, 9, 200 * 16, 210 * 16)
        arrow_head(18, 7, left=False)
        arrow_head(6, 17, left=True)
    elif kind == "loop_one":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(5, 6, 14, 9, 20 * 16, 210 * 16)
        p.drawArc(5, 9, 14, 9, 200 * 16, 210 * 16)
        arrow_head(18, 7, left=False)
        arrow_head(6, 17, left=True)
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(QRectF(9, 8, 6, 8), Qt.AlignmentFlag.AlignCenter, "1")
    elif kind == "shuffle":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(5, 8, 9, 8)
        p.drawLine(9, 8, 15, 16)
        p.drawLine(15, 16, 18, 16)
        arrow_head(19, 16, left=False)
        p.drawLine(5, 16, 9, 16)
        p.drawLine(9, 16, 15, 8)
        p.drawLine(15, 8, 18, 8)
        arrow_head(19, 8, left=False)

    p.end()
    return QIcon(px)


# ── Loop mode ────────────────────────────────────────────────────────────────
class LoopMode(enum.IntEnum):
    SEQUENTIAL = 0
    LOOP_ALL   = 1
    LOOP_ONE   = 2
    SHUFFLE    = 3


_LOOP_ICON_NAMES = ["loop_seq", "loop_all", "loop_one", "shuffle"]
_LOOP_TIPS   = ["順番再生", "全曲ループ", "1曲ループ", "シャッフル"]


# ── Seek bar ─────────────────────────────────────────────────────────────────
class SeekBar(QWidget):
    seeked = pyqtSignal(int)   # ms

    def __init__(self):
        super().__init__()
        self.setFixedHeight(20)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMouseTracking(True)
        self._dur  = 0
        self._pos  = 0
        self._drag = False
        self._hover_x = -1

    def set_duration(self, ms: int):
        self._dur = max(1, ms)
        self.update()

    def set_position(self, ms: int):
        if not self._drag:
            self._pos = ms
            self.update()

    def _x_to_ms(self, x: int) -> int:
        w = max(1, self.width() - 16)
        return max(0, min(self._dur, int(x / w * self._dur)))

    def _ms_to_x(self, ms: int) -> float:
        return ms / self._dur * (self.width() - 16) + 8 if self._dur else 8.0

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        cy = H / 2

        # Groove
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(_SURFACE))
        p.drawRoundedRect(8, int(cy) - 2, W - 16, 4, 2, 2)

        # Fill
        fx = self._ms_to_x(self._pos)
        if fx > 8:
            p.setBrush(QColor(_BLUE))
            p.drawRoundedRect(8, int(cy) - 2, int(fx) - 8, 4, 2, 2)

        # Hover preview
        if self._hover_x >= 0 and not self._drag:
            p.setPen(QPen(QColor(255, 255, 255, 60), 1, Qt.PenStyle.DashLine))
            p.drawLine(self._hover_x, 0, self._hover_x, H)

        # Handle
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("white"))
        hx = fx
        p.drawEllipse(QPointF(hx, cy), 6, 6)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._dur:
            self._drag = True
            self._pos  = self._x_to_ms(int(e.position().x()))
            self.update()

    def mouseMoveEvent(self, e):
        self._hover_x = int(e.position().x())
        if self._drag and self._dur:
            self._pos = self._x_to_ms(self._hover_x)
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._drag:
            self._drag = False
            ms = self._x_to_ms(int(e.position().x()))
            self._pos  = ms
            self.seeked.emit(ms)
            self.update()

    def leaveEvent(self, _):
        self._hover_x = -1
        self.update()


# ── Volume knob (compact slider) ─────────────────────────────────────────────
class VolumeBar(QWidget):
    changed = pyqtSignal(float)   # 0.0 – 1.0

    def __init__(self):
        super().__init__()
        self.setFixedSize(90, 20)
        self.setMouseTracking(True)
        self._val  = 1.0
        self._drag = False

    def set_volume(self, v: float):
        self._val = max(0.0, min(1.0, v))
        self.update()

    def _x_to_vol(self, x: int) -> float:
        return max(0.0, min(1.0, (x - 8) / (self.width() - 16)))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        cy = H / 2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(_SURFACE))
        p.drawRoundedRect(8, int(cy) - 2, W - 16, 4, 2, 2)
        fill_w = int(self._val * (W - 16))
        if fill_w > 0:
            p.setBrush(QColor(_GREEN))
            p.drawRoundedRect(8, int(cy) - 2, fill_w, 4, 2, 2)
        hx = 8 + self._val * (W - 16)
        p.setBrush(QColor("white"))
        p.drawEllipse(QPointF(hx, cy), 5, 5)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = True
            self._val = self._x_to_vol(int(e.position().x()))
            self.changed.emit(self._val)
            self.update()

    def mouseMoveEvent(self, e):
        if self._drag:
            self._val = self._x_to_vol(int(e.position().x()))
            self.changed.emit(self._val)
            self.update()

    def mouseReleaseEvent(self, _):
        self._drag = False

    def wheelEvent(self, e):
        self._val = max(0.0, min(1.0, self._val + e.angleDelta().y() / 1200.0))
        self.changed.emit(self._val)
        self.update()


# ── Album art display ────────────────────────────────────────────────────────
class ArtLabel(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedSize(160, 160)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._px: Optional[QPixmap] = None

    def set_art(self, data: bytes):
        if data:
            px = QPixmap()
            if px.loadFromData(data):
                self._px = px.scaled(160, 160,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self.update()
                return
        self._px = None
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(_SURFACE))
        p.drawRoundedRect(r, 8, 8)
        if self._px:
            x = (r.width()  - self._px.width())  // 2
            y = (r.height() - self._px.height()) // 2
            p.drawPixmap(x, y, self._px)
        else:
            p.setFont(QFont("Segoe UI Emoji", 48))
            p.setPen(QColor(_SUB))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "♪")


# ── Now-playing info panel ────────────────────────────────────────────────────
class NowPlayingPanel(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumWidth(200)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 20, 16, 16)
        lay.setSpacing(10)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._art = ArtLabel()
        lay.addWidget(self._art, alignment=Qt.AlignmentFlag.AlignHCenter)

        lay.addSpacing(6)

        self._title  = QLabel("—")
        self._title.setProperty("role", "title")
        self._title.setWordWrap(True)
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._artist = QLabel("")
        self._artist.setProperty("role", "sub")
        self._artist.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self._album  = QLabel("")
        self._album.setProperty("role", "sub")
        self._album.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        lay.addWidget(self._title)
        lay.addWidget(self._artist)
        lay.addWidget(self._album)
        lay.addStretch()

    def update_track(self, track: Optional[Track]):
        if track is None:
            self._title.setText("—")
            self._artist.setText("")
            self._album.setText("")
            self._art.set_art(b"")
        else:
            self._title.setText(track.display_title())
            self._artist.setText(track.display_artist())
            self._album.setText(track.album)
            self._art.set_art(track.art_data)


# ── Playlist panel ────────────────────────────────────────────────────────────
class PlaylistPanel(QWidget):
    play_index    = pyqtSignal(int)
    add_files     = pyqtSignal(list)   # list[str]
    remove_sel    = pyqtSignal()
    clear_all     = pyqtSignal()
    save_req      = pyqtSignal()
    load_req      = pyqtSignal()
    assign_lyrics = pyqtSignal(int)    # row index

    def __init__(self):
        super().__init__()
        self.setMinimumWidth(220)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        # Header
        hdr = QHBoxLayout()
        lbl = QLabel("プレイリスト")
        lbl.setStyleSheet(f"font-weight: bold; color: {_BLUE}; padding: 4px 8px;")
        hdr.addWidget(lbl)
        hdr.addStretch()

        btn_load = QPushButton("開く")
        btn_load.setFixedWidth(44)
        btn_load.setToolTip("プレイリストを開く (.flpl)")
        btn_load.clicked.connect(self.load_req)
        hdr.addWidget(btn_load)

        btn_save = QPushButton("保存")
        btn_save.setFixedWidth(44)
        btn_save.setToolTip("プレイリストを保存 (Ctrl+S)")
        btn_save.clicked.connect(self.save_req)
        hdr.addWidget(btn_save)

        lay.addLayout(hdr)

        # List
        self._list = QListWidget()
        self._list.setAlternatingRowColors(False)
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.itemDoubleClicked.connect(
            lambda item: self.play_index.emit(self._list.row(item))
        )
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._ctx_menu)
        lay.addWidget(self._list)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        def _mk(text, tip, fn):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedHeight(28)
            b.clicked.connect(fn)
            btn_row.addWidget(b)
            return b

        _mk("＋ 追加", "音声ファイルを追加", self._add_files)
        _mk("－ 削除", "選択を削除", self.remove_sel)
        _mk("✕ クリア", "全クリア", self.clear_all)
        lay.addLayout(btn_row)

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "音声ファイルを追加",
            "",
            "Audio Files (*.mp3 *.flac *.aac *.m4a *.wav *.ogg *.opus *.wma *.ape *.aiff)"
        )
        if paths:
            self.add_files.emit(paths)

    def _ctx_menu(self, pos):
        row = self._list.currentRow()
        menu = QMenu(self)
        a1 = menu.addAction("再生")
        a1.triggered.connect(lambda: self.play_index.emit(row))
        menu.addSeparator()
        a3 = menu.addAction("歌詞を指定…")
        a3.triggered.connect(lambda: self.assign_lyrics.emit(row))
        menu.addSeparator()
        a2 = menu.addAction("削除")
        a2.triggered.connect(self.remove_sel)
        menu.exec(self._list.mapToGlobal(pos))

    def rebuild(self, tracks: List[Track], current: int):
        self._list.blockSignals(True)
        self._list.clear()
        for i, t in enumerate(tracks):
            dur = f"  {_ms_fmt(t.dur_ms)}" if t.dur_ms else ""
            text = f"{i+1:02d}  {t.display_title()}{dur}"
            item = QListWidgetItem(text)
            item.setForeground(QColor(_BLUE if i == current else _TEXT))
            self._list.addItem(item)
        if 0 <= current < self._list.count():
            self._list.setCurrentRow(current)
            self._list.scrollToItem(self._list.currentItem())
        self._list.blockSignals(False)

    def highlight(self, idx: int):
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item:
                item.setForeground(QColor(_BLUE if i == idx else _TEXT))
        if 0 <= idx < self._list.count():
            self._list.setCurrentRow(idx)

    def selected_rows(self) -> List[int]:
        return [self._list.row(item) for item in self._list.selectedItems()]

    def reordered_indices(self) -> List[int]:
        """Return current item data order (after internal drag-drop reorder)."""
        # QListWidget internal DnD already reordered items; we read their text
        # to rebuild the correct track list in PlayerWindow.
        return list(range(self._list.count()))


# ── Control bar ───────────────────────────────────────────────────────────────
class ControlBar(QWidget):
    play_pause  = pyqtSignal()
    stop        = pyqtSignal()
    prev_track  = pyqtSignal()
    next_track  = pyqtSignal()
    seek_rel    = pyqtSignal(int)    # ms delta
    loop_changed = pyqtSignal(int)  # LoopMode value
    vol_changed  = pyqtSignal(float)
    seeked       = pyqtSignal(int)  # from seek bar

    def __init__(self):
        super().__init__()
        self.setFixedHeight(84)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 0, 12, 8)
        outer.setSpacing(4)

        # Seek bar row
        seek_row = QHBoxLayout()
        self._time_cur = QLabel("0:00")
        self._time_cur.setProperty("role", "time")
        self._time_cur.setFixedWidth(36)
        self._seekbar = SeekBar()
        self._seekbar.seeked.connect(self.seeked)
        self._time_tot = QLabel("0:00")
        self._time_tot.setProperty("role", "time")
        self._time_tot.setFixedWidth(36)
        self._time_tot.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self._time_cur)
        seek_row.addWidget(self._seekbar)
        seek_row.addWidget(self._time_tot)
        outer.addLayout(seek_row)

        # Transport row
        trans = QHBoxLayout()
        trans.setSpacing(6)
        self._icon_play = _player_icon("play")
        self._icon_pause = _player_icon("pause")
        self._loop_icons = [_player_icon(name) for name in _LOOP_ICON_NAMES]

        def _btn(text, tip, fn, checkable=False, w=36, icon=None):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedSize(w, 32)
            b.setIconSize(QSize(18, 18))
            if icon is not None:
                b.setIcon(icon)
            b.setCheckable(checkable)
            b.clicked.connect(fn)
            trans.addWidget(b)
            return b

        _btn("", "前の曲 (Ctrl+←)", self.prev_track,
             icon=_player_icon("prev"))
        _btn("", "-5秒", lambda: self.seek_rel.emit(-5000),
             icon=_player_icon("seek_back"))
        self._btn_play = _btn("", "再生/一時停止 (Space)", self.play_pause, w=48,
                              icon=self._icon_play)
        _btn("", "+5秒", lambda: self.seek_rel.emit(5000),
             icon=_player_icon("seek_forward"))
        _btn("", "次の曲 (Ctrl+→)", self.next_track,
             icon=_player_icon("next"))
        _btn("", "停止", self.stop,
             icon=_player_icon("stop"))

        trans.addSpacing(16)

        # Loop mode button (cycles through modes)
        self._loop_mode = LoopMode.LOOP_ALL
        self._btn_loop = QPushButton("")
        self._btn_loop.setFixedSize(36, 32)
        self._btn_loop.setIconSize(QSize(18, 18))
        self._btn_loop.setIcon(self._loop_icons[self._loop_mode])
        self._btn_loop.setToolTip(_LOOP_TIPS[self._loop_mode])
        self._btn_loop.clicked.connect(self._cycle_loop)
        trans.addWidget(self._btn_loop)

        trans.addSpacing(8)

        # Volume
        vol_btn = QPushButton("")
        vol_btn.setFixedSize(28, 32)
        vol_btn.setIconSize(QSize(18, 18))
        vol_btn.setIcon(_player_icon("volume"))
        vol_btn.setToolTip("音量")
        trans.addWidget(vol_btn)
        self._vol = VolumeBar()
        self._vol.changed.connect(self.vol_changed)
        trans.addWidget(self._vol)

        trans.addStretch()

        outer.addLayout(trans)

    def _cycle_loop(self):
        self._loop_mode = LoopMode((int(self._loop_mode) + 1) % len(LoopMode))
        self._btn_loop.setIcon(self._loop_icons[self._loop_mode])
        self._btn_loop.setToolTip(_LOOP_TIPS[self._loop_mode])
        self.loop_changed.emit(int(self._loop_mode))

    def set_loop_mode(self, mode: LoopMode):
        self._loop_mode = mode
        self._btn_loop.setIcon(self._loop_icons[mode])
        self._btn_loop.setToolTip(_LOOP_TIPS[mode])

    def set_playing(self, playing: bool):
        self._btn_play.setIcon(self._icon_pause if playing else self._icon_play)
        self._btn_play.setToolTip("一時停止 (Space)" if playing else "再生 (Space)")

    def set_position(self, ms: int, dur: int):
        self._time_cur.setText(_ms_fmt(ms))
        if dur:
            self._time_tot.setText(_ms_fmt(dur))
        self._seekbar.set_position(ms)

    def set_duration(self, ms: int):
        self._seekbar.set_duration(ms)
        self._time_tot.setText(_ms_fmt(ms))

    def set_volume(self, v: float):
        self._vol.set_volume(v)

    def get_loop_mode(self) -> LoopMode:
        return self._loop_mode


# ── Transparent mouse-capture layer for LyricOverlay ─────────────────────────
class _OverlayMousePane(QWidget):
    """Full-size transparent child that sits above LyricsCanvas and routes
    all mouse/context/drag events back to LyricOverlay."""

    def __init__(self, parent: "LyricOverlay"):
        super().__init__(parent)
        self._win = parent
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

    def enterEvent(self, e):
        if not self._win._locked:
            self._win._hovering = True
            self._win.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._win._hovering = False
        self._win.update()
        super().leaveEvent(e)

    def paintEvent(self, _):
        w = self._win
        p = QPainter(self)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 1))
        p.drawRect(self.rect())
        if not (w._hovering and not w._locked):
            return
        pen = QPen(QColor(255, 255, 255, 38))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawRect(0, 0, self.width() - 1, self.height() - 1)

    def mousePressEvent(self, e):   self._win.mousePressEvent(e)
    def mouseMoveEvent(self, e):    self._win.mouseMoveEvent(e)
    def mouseReleaseEvent(self, e): self._win.mouseReleaseEvent(e)
    def contextMenuEvent(self, e):  self._win.contextMenuEvent(e)
    def dragEnterEvent(self, e):    self._win.dragEnterEvent(e)
    def dropEvent(self, e):         self._win.dropEvent(e)


# ── Lyrics overlay (overlay driven from player, no SMTC) ─────────────────────
class LyricOverlay(QWidget):
    """Floating lyrics window driven by PlayerWindow instead of SMTC."""

    def __init__(self):
        super().__init__()
        if _OV is None:
            return

        self._locked   = False
        self._hovering = False
        self._drag_pos = QPoint()
        self._resize_dir = (0, 0)
        self._resize_start_geo  = None
        self._resize_start_pos  = None
        self._unlock_visible    = False
        self._zone_preview      = False
        self._preview_zone_size = 48

        cfg = _OV.load_config()
        self.cfg = cfg

        self._state_save_timer = QTimer(self)
        self._state_save_timer.setSingleShot(True)
        self._state_save_timer.setInterval(600)
        self._state_save_timer.timeout.connect(self.save_geometry)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(cfg.get("opacity", 0.88))
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

        from PyQt6.QtWidgets import QVBoxLayout as _VBL
        lay = _VBL(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.canvas = _OV.LyricsCanvas(self)
        self.canvas.apply_cfg(cfg)
        lay.addWidget(self.canvas)

        # Transparent pane above canvas — captures mouse/drag, draws hover border
        self._mouse_pane = _OverlayMousePane(self)
        self._mouse_pane.setGeometry(0, 0, self.width(), self.height())

        if cfg.get("lyrics_path"):
            self._load_lyrics(cfg["lyrics_path"])

        x, y, w, h = _OV._fit_to_screen(cfg["x"], cfg["y"], cfg["w"], cfg["h"])
        self.setGeometry(x, y, w, h)

        # Lock / unlock button (shown when cursor enters the unlock zone)
        self._zone_btn = QPushButton(self)
        self._zone_btn.setFixedSize(30, 30)
        self._zone_btn.setStyleSheet(
            "QPushButton{"
            "  background:rgba(0,0,0,150); color:white; border:none;"
            "  border-radius:6px; font-size:15px;"
            "}"
            "QPushButton:hover{ background:rgba(60,60,60,200); }"
        )
        self._zone_btn.clicked.connect(self._toggle_lock)
        self._zone_btn.setIconSize(QSize(20, 20))
        self._zone_btn.hide()
        self._zone_btn.raise_()
        self._update_zone_btn_icon()
        self._reposition_zone_btn()

        # Lock-zone cursor timer
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(80)
        self._cursor_timer.timeout.connect(self._check_unlock_zone)
        self._cursor_timer.start()

        self._locked = cfg.get("locked", False)
        if self._locked:
            self._set_clickthrough(True)

    def _load_lyrics(self, path: str):
        p = Path(path)
        if not p.exists():
            return
        try:
            raw   = __import__("json").loads(p.read_text("utf-8"))
            lines = raw.get("lines", raw) if isinstance(raw, dict) else raw
            self.cfg["lyrics_path"] = str(p)
            self.canvas.load_lyrics(lines)
            self._queue_state_save()
        except Exception as e:
            print(f"[overlay] lyrics load error: {e}", file=sys.stderr)

    def auto_load_lyrics(self, audio_path: str, title: str):
        """Search for a matching JSON lyrics file near the audio file."""
        ap   = Path(audio_path)
        stem = ap.stem.lower()

        def _try(directory: Path):
            # Exact stem match
            exact = directory / f"{ap.stem}.json"
            if exact.exists():
                self._load_lyrics(str(exact))
                return True
            # Fuzzy match
            for p in directory.glob("*.json"):
                ps = p.stem.lower()
                if stem in ps or ps in stem or title.lower() in ps:
                    self._load_lyrics(str(p))
                    return True
            return False

        if _try(ap.parent):
            return
        _try(ap.parent / "lyrics")

    def _set_clickthrough(self, on: bool):
        import ctypes
        hwnd  = int(self.winId())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        if on:
            style |=  0x00080020
        else:
            style &= ~0x00000020
        ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)

    def _update_zone_btn_icon(self):
        self._zone_btn.setText("")
        self._zone_btn.setIcon(_lock_icon(self._locked))
        self._zone_btn.setToolTip("ロック解除" if self._locked else "位置をロック")

    def _reposition_zone_btn(self):
        zone   = self.cfg.get("unlock_zone", 48)
        bw, bh = self._zone_btn.width(), self._zone_btn.height()
        x = self.width()  - zone + (zone - bw) // 2
        y = (zone - bh) // 2
        self._zone_btn.move(x, y)

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

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_mouse_pane"):
            self._mouse_pane.setGeometry(0, 0, self.width(), self.height())
            self._mouse_pane.raise_()
        if hasattr(self, "_zone_btn"):
            self._reposition_zone_btn()
            self._zone_btn.raise_()
        self._queue_state_save()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._queue_state_save()

    _RESIZE_M = 8

    def _get_resize_dir(self, pos):
        x, y, w, h, m = pos.x(), pos.y(), self.width(), self.height(), self._RESIZE_M
        dx = -1 if x < m else (1 if x > w - m else 0)
        dy = -1 if y < m else (1 if y > h - m else 0)
        return dx, dy

    def _cursor_for_dir(self, dx, dy):
        if dx == 0 and dy == 0: return Qt.CursorShape.OpenHandCursor
        if dx != 0 and dy == 0: return Qt.CursorShape.SizeHorCursor
        if dx == 0 and dy != 0: return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.SizeBDiagCursor if dx * dy > 0 else Qt.CursorShape.SizeFDiagCursor

    def _set_overlay_cursor(self, cursor):
        if hasattr(self, "_mouse_pane"):
            self._mouse_pane.setCursor(cursor)
        self.setCursor(cursor)

    def mousePressEvent(self, e):
        if self._locked or e.button() != Qt.MouseButton.LeftButton:
            return
        dx, dy = self._get_resize_dir(e.position().toPoint())
        if dx or dy:
            self._resize_dir = (dx, dy)
            self._resize_start_geo = self.geometry()
            self._resize_start_pos = e.globalPosition().toPoint()
            self._drag_pos = QPoint()
        else:
            self._drag_pos   = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._resize_dir = (0, 0)
            self._set_overlay_cursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, e):
        if self._locked:
            return
        if e.buttons() & Qt.MouseButton.LeftButton:
            if self._resize_dir != (0, 0) and self._resize_start_geo:
                dx, dy = self._resize_dir
                geo    = self._resize_start_geo
                ddx    = int(e.globalPosition().x()) - self._resize_start_pos.x()
                ddy    = int(e.globalPosition().y()) - self._resize_start_pos.y()
                nx, ny, nw, nh = geo.x(), geo.y(), geo.width(), geo.height()
                if dx == -1: nw -= ddx; nx += ddx
                elif dx ==  1: nw += ddx
                if dy == -1: nh -= ddy; ny += ddy
                elif dy ==  1: nh += ddy
                screen = self.screen() or QApplication.primaryScreen()
                avail  = screen.availableGeometry()
                nw = max(200, min(nw, avail.width()))
                nh = max(80,  min(nh, avail.height()))
                nx = max(avail.x(), min(nx, avail.right()  - nw))
                ny = max(avail.y(), min(ny, avail.bottom() - nh))
                self.setGeometry(nx, ny, nw, nh)
            elif not self._drag_pos.isNull():
                self.move(e.globalPosition().toPoint() - self._drag_pos)
        else:
            self._set_overlay_cursor(self._cursor_for_dir(*self._get_resize_dir(e.position().toPoint())))

    def mouseReleaseEvent(self, e):
        self._drag_pos = QPoint()
        self._resize_dir = (0, 0)
        self._resize_start_geo = None
        self._resize_start_pos = None
        if not self._locked:
            self._set_overlay_cursor(self._cursor_for_dir(*self._get_resize_dir(e.position().toPoint())))

    def show_settings(self):
        if not (_OV and hasattr(self, "canvas")):
            return
        from PyQt6.QtWidgets import QDialog
        original_cfg = dict(self.cfg)

        def on_preview(preview_cfg: dict):
            self.setWindowOpacity(preview_cfg["opacity"])
            self.canvas.apply_cfg(preview_cfg)

        dlg = _OV.SettingsDialog(self.cfg, self, on_preview=on_preview)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.cfg = dlg.result_cfg()
            self.setWindowOpacity(self.cfg["opacity"])
            self.canvas.apply_cfg(self.cfg)
            self.save_geometry()
        else:
            self.setWindowOpacity(original_cfg["opacity"])
            self.canvas.apply_cfg(original_cfg)

    def contextMenuEvent(self, e):
        menu = QMenu(self)
        a = menu.addAction("歌詞ファイルを開く…")
        a.triggered.connect(self._open_lyrics)
        menu.addSeparator()
        a = menu.addAction("設定…")
        a.triggered.connect(self.show_settings)
        menu.addSeparator()
        a = menu.addAction("ロック解除" if self._locked else "位置をロック")
        a.triggered.connect(self._toggle_lock)
        menu.addSeparator()
        a = menu.addAction("閉じる")
        a.triggered.connect(self.hide)
        menu.exec(e.globalPos())

    def _open_lyrics(self):
        p, _ = QFileDialog.getOpenFileName(self, "歌詞 JSON を選択", "", "JSON (*.json)")
        if p:
            self._load_lyrics(p)

    def _toggle_lock(self):
        self._locked = not self._locked
        self.cfg["locked"] = self._locked
        self._update_zone_btn_icon()
        self._zone_btn.hide()
        self._unlock_visible = False
        if self._locked:
            self._hovering = False
            self._drag_pos = QPoint()
            self._resize_dir = (0, 0)
            self._resize_start_geo = None
            self._resize_start_pos = None
            self._set_overlay_cursor(Qt.CursorShape.ArrowCursor)
            self._set_clickthrough(True)
        else:
            self._set_clickthrough(False)
        self.save_geometry()
        self.update()

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

    def save_geometry(self):
        if _OV:
            self.cfg.update(x=self.x(), y=self.y(), w=self.width(), h=self.height(),
                            locked=self._locked)
            _OV.save_config(self.cfg)

    def _queue_state_save(self):
        if hasattr(self, "_state_save_timer"):
            self._state_save_timer.start()

    def closeEvent(self, e):
        self.save_geometry()
        super().closeEvent(e)


# ── Main player window ────────────────────────────────────────────────────────
_PLAYLIST_EXT = ".flpl"
_AUDIO_FILTER = "Audio Files (*.mp3 *.flac *.aac *.m4a *.wav *.ogg *.opus *.wma *.ape *.aiff)"


class PlayerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("furi-lrc Player")
        self.resize(820, 520)
        self.setMinimumSize(560, 380)

        # ── State ──
        self._tracks:  List[Track]    = []
        self._current: int            = -1
        self._loop:    LoopMode       = LoopMode.LOOP_ALL
        self._shuffle_history: List[int] = []
        self._current_playlist_path: str = ""
        self._volume: float = 1.0
        self._pending_restore_pos_ms: Optional[int] = None

        # ── Media player ──
        self._player: Optional[QMediaPlayer] = None
        self._audio:  Optional[QAudioOutput] = None
        if HAS_MEDIA:
            self._audio  = QAudioOutput()
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._audio)
            self._audio.setVolume(self._volume)
            self._player.positionChanged.connect(self._on_position)
            self._player.durationChanged.connect(self._on_duration)
            self._player.playbackStateChanged.connect(self._on_state_change)
            self._player.mediaStatusChanged.connect(self._on_media_status)

        # ── Sync: use positionChanged (fires ~every 50-100 ms) instead of a
        #    separate 200 ms poll timer — lower latency, no extra thread needed ──
        if HAS_MEDIA and self._player:
            self._player.positionChanged.connect(self._push_sync_from_position)

        # ── Overlay ──
        self._overlay: Optional[LyricOverlay] = (
            LyricOverlay() if _OV else None
        )
        if self._overlay:
            self._overlay.show()

        # ── Build UI ──
        self._build_ui()
        self._build_menu()
        self._build_shortcuts()
        self.setStyleSheet(_STYLESHEET)

        # ── Geometry save timer ──
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.setInterval(600)
        self._geo_timer.timeout.connect(self._save_geometry)

        self._load_last_playlist()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        main_lay = QVBoxLayout(central)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # Splitter: NowPlaying | Playlist
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)

        self._now_playing = NowPlayingPanel()
        splitter.addWidget(self._now_playing)

        self._playlist = PlaylistPanel()
        self._playlist.play_index.connect(self._play_index)
        self._playlist.add_files.connect(self._add_tracks)
        self._playlist.remove_sel.connect(self._remove_selected)
        self._playlist.clear_all.connect(self._clear_all)
        self._playlist.save_req.connect(self._save_playlist_now)
        self._playlist.load_req.connect(self._load_playlist_dialog)
        self._playlist.assign_lyrics.connect(self._assign_lyrics_to_track)
        splitter.addWidget(self._playlist)

        splitter.setSizes([220, 580])
        main_lay.addWidget(splitter, 1)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {_BORDER};")
        main_lay.addWidget(sep)

        # Control bar
        self._ctrl = ControlBar()
        self._ctrl.play_pause.connect(self._toggle_play)
        self._ctrl.stop.connect(self._stop)
        self._ctrl.prev_track.connect(self.prev_track)
        self._ctrl.next_track.connect(self.next_track)
        self._ctrl.seek_rel.connect(self._seek_rel)
        self._ctrl.loop_changed.connect(self._set_loop)
        self._ctrl.vol_changed.connect(self._set_volume)
        self._ctrl.seeked.connect(self._seek)
        self._ctrl.set_loop_mode(self._loop)
        main_lay.addWidget(self._ctrl)

        self.setCentralWidget(central)

    def _build_menu(self):
        mb = self.menuBar()
        mb.setStyleSheet(f"""
            QMenuBar {{ background: {_PANEL}; color: {_TEXT}; padding: 2px 4px; border-bottom: 1px solid {_BORDER}; }}
            QMenuBar::item {{ padding: 4px 12px; border-radius: 4px; }}
            QMenuBar::item:selected {{ background: {_SURFACE}; }}
        """)

        def _act(menu, text, fn, shortcut=None, checkable=False):
            a = QAction(text, self)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            if checkable:
                a.setCheckable(True)
            a.triggered.connect(fn)
            menu.addAction(a)
            return a

        fm = mb.addMenu("ファイル")
        _act(fm, "音声ファイルを追加…", self._add_files_dialog,  "Ctrl+O")
        fm.addSeparator()
        _act(fm, "プレイリストを開く…",       self._load_playlist_dialog, "Ctrl+L")
        _act(fm, "プレイリストを保存",         self._save_playlist_now,    "Ctrl+S")
        _act(fm, "名前を付けて保存…",          self._save_playlist_as,     "Ctrl+Shift+S")
        fm.addSeparator()
        _act(fm, "終了", self.close, "Ctrl+Q")

        vm = mb.addMenu("表示")
        self._overlay_action = _act(vm, "歌詞オーバーレイ", self._toggle_overlay, checkable=True)
        self._overlay_action.setChecked(self._overlay is not None and self._overlay.isVisible())
        _act(vm, "歌詞ファイルを開く…", self._open_lyrics_for_current)
        vm.addSeparator()
        _act(vm, "歌詞オーバーレイ設定…", self._open_overlay_settings)

    def _build_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Space),      self).activated.connect(self._toggle_play)
        QShortcut(QKeySequence("Ctrl+Left"),            self).activated.connect(self.prev_track)
        QShortcut(QKeySequence("Ctrl+Right"),           self).activated.connect(self.next_track)
        QShortcut(QKeySequence("J"),                    self).activated.connect(lambda: self._seek_rel(-5000))
        QShortcut(QKeySequence("L"),                    self).activated.connect(lambda: self._seek_rel(5000))
        QShortcut(QKeySequence(","),                    self).activated.connect(lambda: self._seek_rel(-1000))
        QShortcut(QKeySequence("."),                    self).activated.connect(lambda: self._seek_rel(1000))
        QShortcut(QKeySequence(Qt.Key.Key_Delete),      self).activated.connect(self._remove_selected)

    # ── Playback control ──────────────────────────────────────────────────────

    def _play_index(self, idx: int):
        if not (0 <= idx < len(self._tracks)):
            return
        self._current = idx
        track = self._tracks[idx]
        if self._player:
            self._player.setSource(QUrl.fromLocalFile(track.path))
            self._player.play()
        self._on_track_started(track)

    def _toggle_play(self):
        if not self._player:
            return
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        elif self._player.playbackState() == QMediaPlayer.PlaybackState.PausedState:
            self._player.play()
        else:
            if self._current < 0 and self._tracks:
                self._play_index(0)
            elif 0 <= self._current < len(self._tracks):
                self._play_index(self._current)

    def _stop(self):
        if self._player:
            self._player.stop()
        if self._overlay:
            self._overlay.canvas.sync_time(0.0, False, force_hard=True)
        self._ctrl.set_playing(False)

    def prev_track(self):
        if not self._tracks:
            return
        idx = self._current - 1
        if idx < 0:
            idx = len(self._tracks) - 1 if self._loop != LoopMode.SEQUENTIAL else 0
        self._play_index(idx)

    def next_track(self):
        if not self._tracks:
            return
        if self._loop == LoopMode.SHUFFLE:
            self._play_index(self._next_shuffle())
        elif self._loop == LoopMode.LOOP_ONE:
            self._play_index(self._current)
        else:
            nxt = self._current + 1
            if nxt >= len(self._tracks):
                if self._loop == LoopMode.LOOP_ALL:
                    nxt = 0
                else:
                    self._stop()
                    return
            self._play_index(nxt)

    def _next_shuffle(self) -> int:
        n = len(self._tracks)
        if n <= 1:
            return 0
        candidates = [i for i in range(n) if i != self._current]
        if not candidates:
            return 0
        idx = random.choice(candidates)
        self._shuffle_history.append(self._current)
        return idx

    def _seek(self, ms: int):
        if self._player:
            self._player.setPosition(max(0, ms))

    def _seek_rel(self, delta: int):
        if self._player:
            self._player.setPosition(max(0, self._player.position() + delta))

    def _set_volume(self, v: float):
        self._volume = max(0.0, min(1.0, v))
        if self._audio:
            self._audio.setVolume(self._volume)

    def _set_loop(self, mode: int):
        self._loop = LoopMode(mode)
        self._ctrl.set_loop_mode(self._loop)

    # ── QMediaPlayer callbacks ────────────────────────────────────────────────

    def _on_position(self, ms: int):
        self._ctrl.set_position(ms, self._player.duration() if self._player else 0)

    def _on_duration(self, ms: int):
        self._ctrl.set_duration(ms)
        if 0 <= self._current < len(self._tracks):
            self._tracks[self._current].dur_ms = ms
            self._playlist.rebuild(self._tracks, self._current)
        self._apply_pending_restore_position()

    def _on_state_change(self, state):
        playing = (state == QMediaPlayer.PlaybackState.PlayingState)
        self._ctrl.set_playing(playing)
        if self._overlay:
            pos = self._player.position() / 1000.0 if self._player else 0.0
            self._overlay.canvas.sync_time(pos, playing, force_hard=True)

    def _on_media_status(self, status):
        if self._player and status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.next_track()

    def _apply_pending_restore_position(self):
        if self._player and self._pending_restore_pos_ms is not None:
            pos = max(0, int(self._pending_restore_pos_ms))
            dur = self._player.duration()
            if dur > 0:
                pos = min(pos, max(0, dur - 1000))
            self._player.setPosition(pos)
            self._ctrl.set_position(pos, dur)
            if self._overlay:
                playing = self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                self._overlay.canvas.sync_time(pos / 1000.0, playing, force_hard=True)
            self._pending_restore_pos_ms = None

    # ── Sync overlay canvas ───────────────────────────────────────────────────

    def _push_sync_from_position(self, ms: int):
        if not (self._overlay and self._player):
            return
        playing = (self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        self._overlay.canvas.sync_time(ms / 1000.0, playing)

    # ── Track management ──────────────────────────────────────────────────────

    def _on_track_started(self, track: Track):
        self._now_playing.update_track(track)
        self.setWindowTitle(f"furi-lrc Player — {track.display_title()}")
        self._playlist.highlight(self._current)
        if self._overlay:
            if track.lyrics_path and Path(track.lyrics_path).exists():
                self._overlay._load_lyrics(track.lyrics_path)
            else:
                self._overlay.auto_load_lyrics(track.path, track.display_title())

    def _prepare_current_track(self, pos_ms: int = 0, autoplay: bool = False):
        if not (self._player and 0 <= self._current < len(self._tracks)):
            return
        track = self._tracks[self._current]
        self._pending_restore_pos_ms = max(0, int(pos_ms))
        self._player.setSource(QUrl.fromLocalFile(track.path))
        self._on_track_started(track)
        if autoplay:
            self._player.play()
        else:
            self._player.pause()
        QTimer.singleShot(0, self._apply_pending_restore_position)

    def _assign_lyrics_to_track(self, row: int):
        if not (0 <= row < len(self._tracks)):
            return
        p, _ = QFileDialog.getOpenFileName(
            self, f"歌詞を指定 — {self._tracks[row].display_title()}", "", "JSON (*.json)"
        )
        if p:
            self._tracks[row].lyrics_path = p
            # if currently playing this track, reload lyrics immediately
            if row == self._current and self._overlay:
                self._overlay._load_lyrics(p)

    def _add_tracks(self, paths: List[str]):
        new = [_build_track(p) for p in paths]
        self._tracks.extend(new)
        self._playlist.rebuild(self._tracks, self._current)

    def _add_files_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "音声ファイルを追加", "", _AUDIO_FILTER)
        if paths:
            self._add_tracks(paths)

    def _remove_selected(self):
        rows = sorted(self._playlist.selected_rows(), reverse=True)
        for r in rows:
            if 0 <= r < len(self._tracks):
                self._tracks.pop(r)
                if r < self._current:
                    self._current -= 1
                elif r == self._current:
                    self._current = min(self._current, len(self._tracks) - 1)
        self._playlist.rebuild(self._tracks, self._current)

    def _clear_all(self):
        self._stop()
        self._tracks  = []
        self._current = -1
        self._playlist.rebuild([], -1)
        self._now_playing.update_track(None)
        self.setWindowTitle("furi-lrc Player")

    # ── Playlist save / load ──────────────────────────────────────────────────

    def _save_playlist_now(self):
        """Ctrl+S: save to current path, fall back to Save As dialog."""
        if self._current_playlist_path:
            self._save_playlist(self._current_playlist_path)
        else:
            self._save_playlist_as()

    def _save_playlist_as(self):
        """Ctrl+Shift+S: always prompt for a new path."""
        path, _ = QFileDialog.getSaveFileName(
            self, "名前を付けて保存", "", f"furi-lrc Playlist (*{_PLAYLIST_EXT})"
        )
        if path:
            if not path.endswith(_PLAYLIST_EXT):
                path += _PLAYLIST_EXT
            self._current_playlist_path = path
            self._save_playlist(path)

    def _load_playlist_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "プレイリストを開く", "", f"furi-lrc Playlist (*{_PLAYLIST_EXT})"
        )
        if path:
            self._load_playlist(path)

    def _save_playlist(self, path: str):
        position_ms = self._player.position() if self._player else 0
        playing = (
            self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
            if self._player else False
        )
        data = {
            "tracks":    [{"path": t.path, "lyrics": t.lyrics_path} for t in self._tracks],
            "current":   self._current,
            "loop_mode": int(self._loop),
            "position_ms": position_ms,
            "volume": self._volume,
            "playing": playing,
            "shuffle_history": self._shuffle_history,
        }
        try:
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except Exception as e:
            print(f"[player] playlist save error: {e}", file=sys.stderr)

    def _load_playlist(self, path: str):
        try:
            data = json.loads(Path(path).read_text("utf-8"))
        except Exception as e:
            print(f"[player] playlist load error: {e}", file=sys.stderr)
            return
        tracks = []
        for entry in data.get("tracks", []):
            # support both old format (str) and new format (dict)
            if isinstance(entry, str):
                p, lp = entry, ""
            else:
                p, lp = entry.get("path", ""), entry.get("lyrics", "")
            if Path(p).exists():
                t = _build_track(p)
                t.lyrics_path = lp if lp and Path(lp).exists() else ""
                tracks.append(t)
        self._tracks  = tracks
        self._current = min(data.get("current", 0), len(self._tracks) - 1)
        try:
            mode = LoopMode(data.get("loop_mode", int(LoopMode.LOOP_ALL)))
        except ValueError:
            mode = LoopMode.LOOP_ALL
        self._loop = mode
        self._ctrl.set_loop_mode(mode)
        self._volume = max(0.0, min(1.0, float(data.get("volume", self._volume))))
        if self._audio:
            self._audio.setVolume(self._volume)
        self._ctrl.set_volume(self._volume)
        self._shuffle_history = [
            int(i) for i in data.get("shuffle_history", [])
            if isinstance(i, int) and 0 <= i < len(self._tracks)
        ]
        self._playlist.rebuild(self._tracks, self._current)
        if 0 <= self._current < len(self._tracks):
            self._prepare_current_track(
                int(data.get("position_ms", 0)),
                False,
            )
        else:
            self._now_playing.update_track(None)
        # track the path for Ctrl+S quick-save (skip for _last_playlist auto-save)
        last = str(Path(__file__).parent / "_last_playlist.flpl")
        if path != last:
            self._current_playlist_path = path

    def _load_last_playlist(self):
        last = Path(__file__).parent / "_last_playlist.flpl"
        if last.exists():
            self._load_playlist(str(last))

    def _save_last_playlist(self):
        last = Path(__file__).parent / "_last_playlist.flpl"
        self._save_playlist(str(last))

    # ── Overlay toggle ────────────────────────────────────────────────────────

    def _toggle_overlay(self, checked: bool):
        if self._overlay:
            self._overlay.setVisible(checked)

    def _open_lyrics_for_current(self):
        if not self._overlay:
            return
        p, _ = QFileDialog.getOpenFileName(self, "歌詞 JSON を選択", "", "JSON (*.json)")
        if p:
            self._overlay._load_lyrics(p)

    def _open_overlay_settings(self):
        if self._overlay:
            self._overlay.show_settings()

    # ── Window events ─────────────────────────────────────────────────────────

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            urls = e.mimeData().urls()
            exts = {".mp3", ".flac", ".aac", ".m4a", ".wav", ".ogg",
                    ".opus", ".wma", ".ape", ".aiff", ".flpl"}
            if any(Path(u.toLocalFile()).suffix.lower() in exts for u in urls):
                e.acceptProposedAction()

    def dropEvent(self, e: QDropEvent):
        paths  = [u.toLocalFile() for u in e.mimeData().urls()]
        plists = [p for p in paths if p.lower().endswith(_PLAYLIST_EXT)]
        audio  = [p for p in paths if not p.lower().endswith(_PLAYLIST_EXT)]
        if plists:
            self._load_playlist(plists[0])
        if audio:
            self._add_tracks(audio)
        e.acceptProposedAction()

    def moveEvent(self, e):
        super().moveEvent(e)
        self._geo_timer.start()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._geo_timer.start()

    def _save_geometry(self):
        pass  # could persist window size if desired

    def closeEvent(self, e):
        self._save_last_playlist()
        if self._overlay:
            self._overlay.save_geometry()
            self._overlay.close()
        super().closeEvent(e)


# ── Entry ────────────────────────────────────────────────────────────────────
def main():
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName("furi-lrc-player")

    # Load bundled Noto Sans JP and apply as app-wide UI font
    _font_path = Path(__file__).parent / "fonts" / "NotoSansJP-Regular.ttf"
    if _font_path.exists():
        _fid = QFontDatabase.addApplicationFont(str(_font_path))
        if _fid >= 0:
            _families = QFontDatabase.applicationFontFamilies(_fid)
            if _families:
                app.setFont(QFont(_families[0], 10))

    if not HAS_MEDIA:
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.critical(
            None, "Missing dependency",
            "PyQt6-Qt6Multimedia が見つかりません。\n"
            "pip install PyQt6-Qt6Multimedia でインストールしてください。"
        )
        sys.exit(1)

    # Load menu font for overlay if available
    if _OV:
        _OV._load_menu_font()

    win = PlayerWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
