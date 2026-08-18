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
import base64
import gzip
import math
import re
import time
import zlib
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
    QSpinBox, QCheckBox, QSlider, QLabel, QGroupBox, QComboBox,
    QMessageBox, QPlainTextEdit,
)
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QPoint, QPointF, QRectF, QRect,
    QPropertyAnimation, QEasingCurve,
)
from PyQt6.QtGui import (
    QColor, QCursor, QPainter, QPen, QFontDatabase, QFont, QFontMetricsF,
    QPixmap, QRawFont, QFontInfo,
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

# ── Runtime root (works both in source and PyInstaller onedir bundle) ───────
def _app_root() -> Path:
    """User-data root: always the folder containing the exe / script."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _bundle_root() -> Path:
    """Read-only asset root: _MEIPASS when frozen, same as _app_root() otherwise."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).parent


# ── Config ───
FONTS_DIR   = _bundle_root() / "fonts"
CONFIG_PATH = _app_root() / "settings.json"
DEFAULT_CFG = dict(
    x=0, y=0, w=941, h=106,
    opacity=0.85, locked=True,
    font_jp=str(FONTS_DIR / "NotoSerifJP-SemiBold.ttf"),
    font_zh=str(FONTS_DIR / "msyhbd.ttc"),
    font_size_jp=33, font_size_rt=15, font_size_zh=22,
    spacing_rt=-10, spacing_zh=0,
    color_sung="#ffe205", color_unsung="#0effd7", color_zh="#26d7ff",
    lyrics_path="", hide_on_pause=False, unlock_zone=80,
    shadow_enabled=True,
    shadow_color="#000000",
    shadow_opacity=0.25,
    shadow_blur=1.75,
    shadow_dx=0.75,
    shadow_dy=0.75,
    align_h="right",   # "left" | "center" | "right"
    align_v="bottom",  # "top"  | "center" | "bottom"
)


def _fit_file_dialog(dlg: QFileDialog, width: int = 640, height: int = 420) -> None:
    screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
    if not screen:
        dlg.resize(width, height)
        return
    avail = screen.availableGeometry()
    w = min(width, max(520, int(avail.width() * 0.62)))
    h = min(height, max(340, int(avail.height() * 0.58)))
    dlg.resize(w, h)
    frame = dlg.frameGeometry()
    frame.moveCenter(avail.center())
    dlg.move(frame.topLeft())


def _make_file_dialog_address_editable(dlg: QFileDialog) -> None:
    combo = dlg.findChild(QComboBox, "lookInCombo")
    if combo is None:
        return
    combo.setEditable(True)
    edit = combo.lineEdit()
    if edit is None:
        return
    edit.setClearButtonEnabled(True)

    def navigate_from_combo():
        text = edit.text().strip().strip('"')
        if not text:
            return
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = Path(dlg.directory().absolutePath()) / path
        if path.is_dir():
            dlg.setDirectory(str(path))
        elif path.parent.is_dir():
            dlg.setDirectory(str(path.parent))
            dlg.selectFile(path.name)

    edit.returnPressed.connect(navigate_from_combo)


def _prepare_file_dialog(dlg: QFileDialog, width: int = 640, height: int = 420) -> None:
    dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dlg.setViewMode(QFileDialog.ViewMode.Detail)
    dlg.setSizeGripEnabled(True)
    _make_file_dialog_address_editable(dlg)
    _fit_file_dialog(dlg, width, height)


def _migrate_shadow(cfg: dict) -> dict:
    """Convert legacy text_shadow CSS string into structured fields."""
    if "text_shadow" not in cfg:
        return cfg
    s = cfg.pop("text_shadow", "")
    if not s or s.lower() == "none":
        cfg.setdefault("shadow_enabled", False)
        return cfg
    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    dx = float(nums[0]) if len(nums) > 0 else 2.0
    dy = float(nums[1]) if len(nums) > 1 else 2.0
    blur = float(nums[2]) if len(nums) > 2 else 8.0
    rgba_m = re.search(
        r"rgba?\s*\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)", s
    )
    if rgba_m:
        r, g, b = int(float(rgba_m.group(1))), int(float(rgba_m.group(2))), int(float(rgba_m.group(3)))
        a = float(rgba_m.group(4)) if rgba_m.group(4) else 1.0
        color = QColor(r, g, b).name()
    else:
        hex_m = re.search(r"#[0-9a-fA-F]{3,8}", s)
        color = hex_m.group() if hex_m else "#000000"
        a = 1.0
    cfg.setdefault("shadow_enabled", True)
    cfg.setdefault("shadow_color",   color)
    cfg.setdefault("shadow_opacity", round(a, 2))
    cfg.setdefault("shadow_blur",    int(blur))
    cfg.setdefault("shadow_dx",      int(dx))
    cfg.setdefault("shadow_dy",      int(dy))
    return cfg


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = {**DEFAULT_CFG, **json.loads(CONFIG_PATH.read_text("utf-8"))}
            cfg = _migrate_shadow(cfg)
            for key in ("font_jp", "font_zh"):
                val = cfg[key]
                p = Path(val) if Path(val).is_absolute() else _app_root() / val
                if p.suffix.lower() not in {".ttf", ".otf", ".ttc", ".woff", ".woff2"} or not p.exists():
                    cfg[key] = DEFAULT_CFG[key]
            return cfg
        except Exception:
            pass
    return DEFAULT_CFG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


_STYLE_CONFIG_PREFIX = "FURI-LRC1."
_STYLE_CONFIG_KEYS = (
    "font_jp", "font_zh", "font_size_jp", "font_size_rt", "font_size_zh",
    "spacing_rt", "spacing_zh", "color_sung", "color_unsung", "color_zh",
    "opacity", "hide_on_pause", "unlock_zone", "shadow_enabled",
    "shadow_color", "shadow_opacity", "shadow_blur", "shadow_dx",
    "shadow_dy", "align_h", "align_v",
)
_STYLE_INT_RANGES = {
    "font_size_jp": (8, 96), "font_size_rt": (6, 72),
    "font_size_zh": (8, 96), "spacing_rt": (-30, 60),
    "spacing_zh": (-60, 60), "unlock_zone": (20, 200),
}
_STYLE_FLOAT_RANGES = {
    "opacity": (0.1, 1.0), "shadow_opacity": (0.0, 1.0),
    "shadow_blur": (0.0, 40.0), "shadow_dx": (-20.0, 20.0),
    "shadow_dy": (-20.0, 20.0),
}
_STYLE_COLOR_KEYS = {"color_sung", "color_unsung", "color_zh", "shadow_color"}


def _encode_style_config(cfg: dict) -> str:
    """Serialize style controls into a compact, URL-safe configuration code."""
    style = {key: cfg[key] for key in _STYLE_CONFIG_KEYS}
    payload = json.dumps(
        {"version": 1, "style": style},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    encoded = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return _STYLE_CONFIG_PREFIX + encoded


def _decode_style_config(code: str) -> dict:
    """Decode and strictly validate a style code without mutating live state."""
    code = code.strip()
    if not code.startswith(_STYLE_CONFIG_PREFIX):
        raise ValueError("設定コードの形式またはバージョンに対応していません。")
    encoded = code[len(_STYLE_CONFIG_PREFIX):]
    if not encoded or len(encoded) > 65536 or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise ValueError("設定コードが無効です。")
    try:
        padding = "=" * (-len(encoded) % 4)
        compressed = base64.b64decode(encoded + padding, altchars=b"-_", validate=True)
        if len(compressed) > 65536:
            raise ValueError
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = inflater.decompress(compressed, 262145)
        if len(raw) > 262144 or inflater.unconsumed_tail:
            raise ValueError
        raw += inflater.flush(262145 - len(raw))
        if len(raw) > 262144 or not inflater.eof or inflater.unused_data:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, zlib.error, json.JSONDecodeError) as exc:
        raise ValueError("設定コードが破損しているか、内容が不完全です。") from exc

    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("設定コードの形式またはバージョンに対応していません。")
    style = payload.get("style")
    if not isinstance(style, dict) or set(style) != set(_STYLE_CONFIG_KEYS):
        raise ValueError("設定コードに必要なスタイル項目がありません。")

    for key in ("font_jp", "font_zh"):
        if not isinstance(style[key], str) or not style[key] or len(style[key]) > 2048:
            raise ValueError("設定コードのフォント設定が無効です。")
    for key, (minimum, maximum) in _STYLE_INT_RANGES.items():
        value = style[key]
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"設定コードの {key} が無効です。")
    for key, (minimum, maximum) in _STYLE_FLOAT_RANGES.items():
        value = style[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"設定コードの {key} が無効です。")
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"設定コードの {key} が無効です。")
    for key in ("hide_on_pause", "shadow_enabled"):
        if not isinstance(style[key], bool):
            raise ValueError(f"設定コードの {key} が無効です。")
    for key in _STYLE_COLOR_KEYS:
        value = style[key]
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise ValueError("設定コードの色設定が無効です。")
    if style["align_h"] not in {"left", "center", "right"}:
        raise ValueError("設定コードの水平位置が無効です。")
    if style["align_v"] not in {"top", "center", "bottom"}:
        raise ValueError("設定コードの垂直位置が無効です。")
    return style


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
    # Sweep-reveal geometry — usually equal to (x, w), but for ruby morae
    # this is rescaled to span the segment's full on-screen width (the wider
    # of base/furigana), so the karaoke wipe reaches the right edge of
    # whichever of the two (base or furigana) is wider, with no dead gap
    # before the next segment starts. See _get_sweep_x().
    reveal_x: float = 0.0
    reveal_w: float = 0.0


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


@dataclasses.dataclass
class _ShadowCfg:
    dx:      float
    dy:      float
    blur:    float
    color:   QColor
    offsets: List[Tuple[float, float, float]]  # (ox, oy, alpha_scale)


def _parse_shadow(cfg: dict) -> Optional["_ShadowCfg"]:
    if not cfg.get("shadow_enabled", True):
        return None
    base = QColor(cfg.get("shadow_color", "#000000"))
    opacity = max(0.0, min(1.0, float(cfg.get("shadow_opacity", 0.85))))
    dx   = float(cfg.get("shadow_dx", 2))
    dy   = float(cfg.get("shadow_dy", 2))
    blur = max(0.0, float(cfg.get("shadow_blur", 8)))

    # Build offset list using float arithmetic so sub-pixel blur values are felt.
    # Use ~3 spread passes regardless of blur magnitude; pass radius scales linearly.
    offsets: List[Tuple[float, float, float]] = [(dx, dy, 1.0)]
    if blur > 0.0:
        n_passes = 3
        for i in range(1, n_passes + 1):
            r = blur * i / n_passes
            alpha_scale = max(0.05, 1.0 - r / (blur + 1.0))
            for ox, oy in [
                (dx - r,       dy      ),
                (dx + r,       dy      ),
                (dx,           dy - r  ),
                (dx,           dy + r  ),
                (dx - r * 0.7, dy - r * 0.7),
                (dx + r * 0.7, dy + r * 0.7),
                (dx - r * 0.7, dy + r * 0.7),
                (dx + r * 0.7, dy - r * 0.7),
            ]:
                offsets.append((ox, oy, alpha_scale))

    c = QColor(base)
    c.setAlphaF(opacity)
    return _ShadowCfg(dx=dx, dy=dy, blur=blur, color=c, offsets=offsets)


@dataclasses.dataclass(frozen=True)
class _FontFace:
    family: str
    style: str = ""
    weight: Optional[int] = None


_FONT_EXACT_FACE_CACHE: dict = {}


def _infer_weight_from_style(style: str) -> Optional[int]:
    normalized = re.sub(r"[\s_-]+", "", style or "").lower()
    if not normalized:
        return None
    if "thin" in normalized:
        return 100
    if "extralight" in normalized or "ultralight" in normalized:
        return 200
    if "light" in normalized:
        return 300
    if "regular" in normalized or "normal" in normalized or "book" in normalized:
        return 400
    if "medium" in normalized:
        return 500
    if "semibold" in normalized or "demibold" in normalized:
        return 600
    if "extrabold" in normalized or "ultrabold" in normalized:
        return 800
    if "black" in normalized or "heavy" in normalized:
        return 900
    if "bold" in normalized:
        return 700
    return None


def _resolve_font_face(abs_path: str) -> Optional[_FontFace]:
    """Register a font file once and return the exact family/style face."""
    if abs_path in _FONT_EXACT_FACE_CACHE:
        return _FONT_EXACT_FACE_CACHE[abs_path]

    fid = QFontDatabase.addApplicationFont(abs_path)
    if fid < 0:
        _FONT_EXACT_FACE_CACHE[abs_path] = None
        return None
    families = QFontDatabase.applicationFontFamilies(fid)
    if not families:
        _FONT_EXACT_FACE_CACHE[abs_path] = None
        return None

    family = families[0]
    style = ""
    weight = None
    try:
        raw = QRawFont(abs_path, 16.0)
        raw_family = raw.familyName()
        raw_style = raw.styleName()
        raw_weight = raw.weight()
        if raw_family and raw_family in families:
            family = raw_family
        if raw_style:
            style = raw_style
        inferred_weight = _infer_weight_from_style(raw_style)
        if raw_weight > 0:
            weight = int(raw_weight)
            if raw_weight == 400 and inferred_weight not in (None, 400):
                weight = inferred_weight
        elif inferred_weight:
            weight = inferred_weight
    except Exception:
        pass

    # QRawFont returns no usable metadata for TrueType Collections (.ttc):
    # an empty style and a default weight of 400, which silently drops a bold
    # face's real weight (e.g. msyhbd.ttc reads back as weight 400 instead of
    # 700).  A family-only QFont request at weight 400 then binds to whatever
    # face the global font database happens to offer, so switching another
    # font — which re-registers fonts and perturbs that state — can flip this
    # one to a thinner weight.  Recover style/weight from the font database,
    # the only reliable source for these files, to make the request explicit.
    if not style:
        db_styles = QFontDatabase.styles(family)
        if db_styles:
            style = db_styles[0]
            if weight in (None, 400):
                db_weight = QFontDatabase.weight(family, style)
                weight = db_weight if db_weight > 0 else (
                    _infer_weight_from_style(style) or weight)

    if weight is None and style:
        weight = _infer_weight_from_style(style)

    face = _FontFace(family=family, style=style, weight=weight)
    _FONT_EXACT_FACE_CACHE[abs_path] = face
    return face


def _lock_font_to_requested_face(f: QFont) -> QFont:
    f.setStyleStrategy(
        QFont.StyleStrategy.PreferMatch |
        QFont.StyleStrategy.NoFontMerging |
        QFont.StyleStrategy.ForceOutline
    )
    return f


def _weight_enum_value(weight: QFont.Weight) -> int:
    return int(weight.value if hasattr(weight, "value") else weight)


def _weight_from_number(weight: Optional[int],
                        default: QFont.Weight = QFont.Weight.Normal) -> QFont.Weight:
    if weight is None or weight <= 0:
        return default
    if weight >= 900:
        return QFont.Weight.Black
    if weight >= 800:
        return QFont.Weight.ExtraBold
    if weight >= 700:
        return QFont.Weight.Bold
    if weight >= 600:
        return QFont.Weight.DemiBold
    if weight >= 500:
        return QFont.Weight.Medium
    if weight <= 200:
        return QFont.Weight.ExtraLight
    if weight <= 300:
        return QFont.Weight.Light
    return QFont.Weight.Normal


def _font_matches_face(font: QFont, face: _FontFace, target_weight: QFont.Weight) -> bool:
    info = QFontInfo(font)
    target_weight_value = _weight_enum_value(target_weight)
    if face.weight and target_weight_value >= 600 and info.weight() < 600:
        return False
    if face.style and face.style.lower() not in info.styleName().lower():
        return False
    return True


def _finalize_font_candidate(f: QFont, face: _FontFace, size: int,
                             target_weight: QFont.Weight) -> QFont:
    try:
        f.setFamilies([face.family])
    except Exception:
        pass
    f.setFamily(face.family)
    if face.style:
        f.setStyleName(face.style)
    f.setPixelSize(size)
    if _weight_enum_value(target_weight) >= 700:
        f.setBold(True)
    f.setWeight(target_weight)
    if face.style:
        f.setStyleName(face.style)
    return _lock_font_to_requested_face(f)


def _font_from_face(face: _FontFace, size: int, default_weight: QFont.Weight) -> QFont:
    target_weight = _weight_from_number(face.weight, default_weight)
    candidates: List[QFont] = []
    if face.style:
        candidates.append(QFontDatabase.font(face.family, face.style, size))
    direct = QFont(face.family)
    if face.style:
        direct.setStyleName(face.style)
    candidates.append(direct)

    last = candidates[-1]
    for f in candidates:
        f = _finalize_font_candidate(f, face, size, target_weight)
        if _font_matches_face(f, face, target_weight):
            return f
        last = f
    return last


def _resolve_face_from_path(path_str: str) -> Optional[_FontFace]:
    if not path_str:
        return None
    p = Path(path_str)
    if not p.is_absolute():
        p = _app_root() / p
    if not p.exists():
        return None
    return _resolve_font_face(str(p))


def _load_qt_font(path_str: str, size: int,
                  weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    face = _resolve_face_from_path(path_str)
    if face is None:
        # Fall back to a bundled typeface so the overlay never depends on a
        # system-installed font — it keeps working on a machine that has no
        # fonts of its own, as long as ./fonts ships with the app.
        for fallback in (DEFAULT_CFG.get("font_jp"), DEFAULT_CFG.get("font_zh")):
            face = _resolve_face_from_path(fallback)
            if face is not None:
                break
    if face is not None:
        return _font_from_face(face, size, weight)
    f = QFont()
    f.setPixelSize(size)
    f.setWeight(weight)
    return _lock_font_to_requested_face(f)


def _clone_qt_font_with_size(font: QFont, size: int) -> QFont:
    f = QFont(font)
    f.setPixelSize(size)
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
        # Keep independent snapshots so in-place updates to a config dict still
        # trigger font and pixmap rebuilds.
        prev = dict(self._cfg)
        self._cfg = dict(cfg)
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
            cfg.get("shadow_enabled") != prev.get("shadow_enabled") or
            cfg.get("shadow_color")   != prev.get("shadow_color")   or
            cfg.get("shadow_opacity") != prev.get("shadow_opacity") or
            cfg.get("shadow_blur")    != prev.get("shadow_blur")    or
            cfg.get("shadow_dx")      != prev.get("shadow_dx")      or
            cfg.get("shadow_dy")      != prev.get("shadow_dy")      or
            cfg.get("spacing_rt")   != prev.get("spacing_rt")   or
            cfg.get("spacing_zh")   != prev.get("spacing_zh")   or
            cfg.get("align_h")      != prev.get("align_h")      or
            cfg.get("align_v")      != prev.get("align_v")
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
        self._font_jp = _load_qt_font(jp_p, sz_jp)
        self._font_rt = _clone_qt_font_with_size(self._font_jp, sz_rt)
        self._font_zh = _load_qt_font(zh_p, sz_zh)
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
                # Distribute the segment's *full* on-screen width (Wc — whichever
                # of base/furigana is wider) across its morae, proportionally to
                # each mora's rendered furigana width, so the cumulative reveal
                # exactly spans [x, x + Wc] with no gap at either end.
                #
                # This must use Wc (not just Wb) because the base block is
                # centred inside Wc: when the base is wider than the furigana
                # (Wb > Wr) it sits flush against both edges (Wc == Wb, so this
                # reduces to the base's own width as before); when the furigana
                # is wider (Wr > Wb, the common case — e.g. two kanji read by
                # three-plus kana) the base is padded on *both* sides and Wc ==
                # Wr, so the reveal must reach all the way to x + Wr or the
                # widest (furigana) glyphs would sit unrevealed after the base
                # finishes, then jump instantly forward once the next segment's
                # morae start progressing.
                running = 0.0
                n_units = len(seg_morae)
                for idx, sm in enumerate(seg_morae):
                    if idx == n_units - 1:
                        rw = Wc - running
                    elif Wr > 0:
                        rw = (sm.w / Wr) * Wc
                    else:
                        rw = Wc / n_units
                    sm.reveal_x = x + running
                    sm.reveal_w = max(0.0, rw)
                    running += sm.reveal_w
                kanjis.append(_KanjiDraw(x=kanji_x, w=Wb, text=base, mora_refs=seg_morae))
                x += Wc
            else:
                for u in units:
                    uw = fm_jp.horizontalAdvance(u["k"])
                    m  = _MoraDraw(x=x, w=uw, text=u["k"],
                                   s_ms=float(u["s"]), e_ms=float(u["e"]), is_rt=False,
                                   reveal_x=x, reveal_w=uw)
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
        gap     = float(cfg.get("spacing_rt", 2))
        spacing = float(cfg.get("spacing_zh", -8))
        has_zh  = bool(layout.zh_text)

        align_h = cfg.get("align_h", "center")
        align_v = cfg.get("align_v", "center")

        # Block height is always computed as if all three layers (ruby / jp / zh)
        # exist, so jp_base_y stays fixed regardless of whether a particular line
        # actually has furigana or a translation.
        block_top    = fm_rt.ascent() + fm_rt.descent() + gap
        block_bottom = fm_jp.descent() + spacing + fm_zh.ascent() + fm_zh.descent()

        pad_v = 8.0
        if align_v == "top":
            jp_base_y = pad_v + block_top + fm_jp.ascent()
        elif align_v == "bottom":
            jp_base_y = H - pad_v - block_bottom
        else:  # center
            jp_base_y = (H - (block_top + fm_jp.ascent() + block_bottom)) / 2.0 + block_top + fm_jp.ascent()

        rt_base_y = jp_base_y - fm_jp.ascent() - gap - fm_rt.descent()
        zh_base_y = jp_base_y + fm_jp.descent() + spacing + fm_zh.ascent()

        pad   = 18.0
        avail = W - 2 * pad
        scale = min(1.0, avail / layout.total_w) if layout.total_w > 1 else 1.0
        if align_h == "left":
            x_off = pad
        elif align_h == "right":
            x_off = pad + avail - layout.total_w * scale
        else:  # center
            x_off = pad + (avail - layout.total_w * scale) / 2

        shadow       = _parse_shadow(cfg)
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
            for sox, soy, sa in shadow.offsets:
                sc = QColor(shadow.color)
                sc.setAlphaF(shadow.color.alphaF() * sa)
                p_u.setPen(sc)
                for m in layout.morae:
                    p_u.setFont(self._font_rt if m.is_rt else self._font_jp)
                    y = rt_base_y if m.is_rt else jp_base_y
                    p_u.drawText(QPointF(m.x + sox, y + soy), m.text)
                if layout.kanjis:
                    p_u.setFont(self._font_jp)
                    for k in layout.kanjis:
                        p_u.drawText(QPointF(k.x + sox, jp_base_y + soy), k.text)

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
            if align_h == "left":
                zh_x = pad
            elif align_h == "right":
                zh_x = W - pad - layout.zh_w
            else:
                zh_x = (W - layout.zh_w) / 2
            zh_y = (H / 2.0 * (1.0 - scale) + zh_base_y * scale
                    if scale < 1.0 else zh_base_y)
            if shadow:
                p_u.setFont(self._font_zh)
                for sox, soy, sa in shadow.offsets:
                    sc = QColor(shadow.color)
                    sc.setAlphaF(shadow.color.alphaF() * sa)
                    p_u.setPen(sc)
                    p_u.drawText(QPointF(zh_x + sox, zh_y + soy), layout.zh_text)
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
                right = m.reveal_x + m.reveal_w * p
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


# ── Snapping double spin box ──
class _SnapSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that snaps the current value to the nearest 0.25 multiple
    before each step, so clicking + or - always lands on a clean 0.25 boundary."""

    def stepBy(self, steps: int) -> None:
        step = self.singleStep()
        if step <= 0 or steps == 0:
            super().stepBy(steps)
            return
        current = self.value()
        # Snap to nearest multiple of step
        snapped = round(round(current / step) * step, self.decimals())
        # Then move N steps in the requested direction
        target = snapped + step * steps
        target = round(target, self.decimals())
        target = max(self.minimum(), min(self.maximum(), target))
        self.setValue(target)


# ── Settings dialog ────
class SettingsDialog(QDialog):
    def __init__(self, cfg: dict, parent=None, on_preview=None, on_center=None):
        super().__init__(parent)
        self.setWindowTitle("furi-lrc 設定")
        self.cfg = dict(cfg)
        self._on_preview = on_preview
        self._on_center = on_center
        self._applying_config = False
        if _MENU_FONT:
            self.setFont(_MENU_FONT)
        self._build()
        self._connect_preview()

    def _connect_preview(self):
        if not self._on_preview:
            return
        self.font_jp_w._edit.textChanged.connect(self._preview)
        self.font_zh_w._edit.textChanged.connect(self._preview)
        # font_size_* は _on_font_manual / _on_scale 内で _preview() を呼ぶ
        self.spacing_rt.valueChanged.connect(self._preview)
        self.spacing_zh.valueChanged.connect(self._preview)
        self.opacity.valueChanged.connect(self._preview)
        self.hide_pause.toggled.connect(self._preview)
        self.unlock_zone.valueChanged.connect(self._preview)
        self.shadow_enabled.toggled.connect(self._preview)
        self.shadow_opacity.valueChanged.connect(self._preview)
        self.shadow_blur.valueChanged.connect(self._preview)
        self.shadow_dx.valueChanged.connect(self._preview)
        self.shadow_dy.valueChanged.connect(self._preview)
        self.align_h.currentIndexChanged.connect(self._preview)
        self.align_v.currentIndexChanged.connect(self._preview)

    def _preview(self, *_):
        if self._on_preview and not self._applying_config:
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

        # 総合サイズ: ＋／－ボタンで全フォント・間距を等比率で拡縮する
        # 浮点精度を保つために内部変数で実数値を管理し、スピンボックスには丸めた値のみ表示する
        self._sz_jp  = float(self.cfg["font_size_jp"])
        self._sz_rt  = float(self.cfg.get("font_size_rt", max(8, self.cfg["font_size_jp"] // 2)))
        self._sz_zh  = float(self.cfg["font_size_zh"])
        self._sp_rt  = float(self.cfg.get("spacing_rt", 2))
        self._sp_zh  = float(self.cfg["spacing_zh"])
        _STEP = 1.05  # 1クリックで約5%拡縮

        def _scale_all(factor: float):
            self._sz_jp = max(8.0,  self._sz_jp * factor)
            self._sz_rt = max(6.0,  self._sz_rt * factor)
            self._sz_zh = max(8.0,  self._sz_zh * factor)
            self._sp_rt = self._sp_rt * factor
            self._sp_zh = self._sp_zh * factor
            for w, v, lo in [
                (self.font_size_jp, self._sz_jp, 8),
                (self.font_size_rt, self._sz_rt, 6),
                (self.font_size_zh, self._sz_zh, 8),
                (self.spacing_rt,   self._sp_rt, -30),
                (self.spacing_zh,   self._sp_zh, -60),
            ]:
                w.blockSignals(True)
                w.setValue(max(lo, round(v)))
                w.blockSignals(False)
            self._preview()

        def _sync_internal(*_):
            # 個別手動変更時は内部変数をスピンボックスの値に同期する
            self._sz_jp = float(self.font_size_jp.value())
            self._sz_rt = float(self.font_size_rt.value())
            self._sz_zh = float(self.font_size_zh.value())
            self._sp_rt = float(self.spacing_rt.value())
            self._sp_zh = float(self.spacing_zh.value())
            self._preview()

        self.font_size_jp.valueChanged.connect(_sync_internal)
        self.font_size_rt.valueChanged.connect(_sync_internal)
        self.font_size_zh.valueChanged.connect(_sync_internal)
        self.spacing_rt.valueChanged.connect(_sync_internal)
        self.spacing_zh.valueChanged.connect(_sync_internal)

        btn_scale_dec = QPushButton("－")
        btn_scale_inc = QPushButton("＋")
        for b in (btn_scale_dec, btn_scale_inc):
            b.setFixedSize(32, 24)
        btn_scale_dec.clicked.connect(lambda: _scale_all(1.0 / _STEP))
        btn_scale_inc.clicked.connect(lambda: _scale_all(_STEP))
        scale_row = QWidget()
        scale_lay = QHBoxLayout(scale_row)
        scale_lay.setContentsMargins(0, 0, 0, 0)
        scale_lay.setSpacing(4)
        scale_lay.addWidget(btn_scale_dec)
        scale_lay.addWidget(btn_scale_inc)
        scale_lay.addStretch()

        # 設定画面の表示名は日本語で統一すること。
        f1.addRow("総合サイズ",           scale_row)
        f1.addRow("振り仮名サイズ",       self.font_size_rt)
        f1.addRow("振り仮名と日本語の間隔", self.spacing_rt)
        f1.addRow("日本語フォント",        self.font_jp_w)
        f1.addRow("中国語フォント",        self.font_zh_w)
        f1.addRow("日本語フォントサイズ",  self.font_size_jp)
        f1.addRow("中国語フォントサイズ",  self.font_size_zh)
        f1.addRow("日中間距(px)",          self.spacing_zh)
        tabs.addTab(w1, "表示")
        w2  = QWidget()
        f2  = QFormLayout(w2)
        self.btn_sung   = self._color_btn(self.cfg["color_sung"])
        self.btn_unsung = self._color_btn(self.cfg["color_unsung"])
        self.btn_zh     = self._color_btn(self.cfg["color_zh"])
        self.opacity    = QDoubleSpinBox(); self.opacity.setRange(0.1, 1.0); self.opacity.setSingleStep(0.05); self.opacity.setValue(self.cfg["opacity"])

        # Shadow controls
        self.shadow_enabled = QCheckBox()
        self.shadow_enabled.setChecked(self.cfg.get("shadow_enabled", True))
        self.btn_shadow = self._color_btn(self.cfg.get("shadow_color", "#000000"))
        self.shadow_opacity = _SnapSpinBox()
        self.shadow_opacity.setRange(0.0, 1.0)
        self.shadow_opacity.setDecimals(2)
        self.shadow_opacity.setSingleStep(0.25)
        self.shadow_opacity.setValue(self.cfg.get("shadow_opacity", 0.85))
        self.shadow_blur = _SnapSpinBox()
        self.shadow_blur.setRange(0.0, 40.0)
        self.shadow_blur.setDecimals(2)
        self.shadow_blur.setSingleStep(0.25)
        self.shadow_blur.setSuffix(" px")
        self.shadow_blur.setValue(float(self.cfg.get("shadow_blur", 8)))
        self.shadow_dx = _SnapSpinBox()
        self.shadow_dx.setRange(-20.0, 20.0)
        self.shadow_dx.setDecimals(2)
        self.shadow_dx.setSingleStep(0.25)
        self.shadow_dx.setSuffix(" px")
        self.shadow_dx.setValue(self.cfg.get("shadow_dx", 2))
        self.shadow_dy = _SnapSpinBox()
        self.shadow_dy.setRange(-20.0, 20.0)
        self.shadow_dy.setDecimals(2)
        self.shadow_dy.setSingleStep(0.25)
        self.shadow_dy.setSuffix(" px")
        self.shadow_dy.setValue(self.cfg.get("shadow_dy", 2))

        f2.addRow("歌唱色",         self.btn_sung)
        f2.addRow("未歌唱色",       self.btn_unsung)
        f2.addRow("訳文色",         self.btn_zh)
        f2.addRow("透明度",         self.opacity)
        f2.addRow("シャドウ 有効",   self.shadow_enabled)
        f2.addRow("シャドウ 色",     self.btn_shadow)
        f2.addRow("シャドウ 不透明度", self.shadow_opacity)
        f2.addRow("シャドウ ぼかし", self.shadow_blur)
        f2.addRow("シャドウ X方向",  self.shadow_dx)
        f2.addRow("シャドウ Y方向",  self.shadow_dy)
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

        from PyQt6.QtWidgets import QComboBox
        w4 = QWidget()
        f4 = QFormLayout(w4)

        self.align_h = QComboBox()
        self.align_h.addItems(["左揃え", "中央揃え", "右揃え"])
        _ah_map  = {"left": 0, "center": 1, "right": 2}
        self.align_h.setCurrentIndex(_ah_map.get(self.cfg.get("align_h", "center"), 1))

        self.align_v = QComboBox()
        self.align_v.addItems(["上揃え", "中央揃え", "下揃え"])
        _av_map  = {"top": 0, "center": 1, "bottom": 2}
        self.align_v.setCurrentIndex(_av_map.get(self.cfg.get("align_v", "center"), 1))

        f4.addRow("水平位置", self.align_h)
        f4.addRow("垂直位置", self.align_v)
        self.btn_center_screen = QPushButton("画面中央に移動")
        self.btn_center_screen.setEnabled(self._on_center is not None)
        self.btn_center_screen.clicked.connect(self._center_on_current_screen)
        f4.addRow("ウィンドウ位置", self.btn_center_screen)
        tabs.addTab(w4, "レイアウト")

        w5 = QWidget()
        config_layout = QVBoxLayout(w5)
        config_layout.setSpacing(8)
        config_note = QLabel("現在の歌詞スタイルを設定コードとして共有できます。")
        config_note.setWordWrap(True)
        config_layout.addWidget(config_note)
        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        btn_export = QPushButton("エクスポート")
        btn_import = QPushButton("インポート")
        btn_export.setMinimumHeight(32)
        btn_import.setMinimumHeight(32)
        btn_export.clicked.connect(self._export_config_code)
        btn_import.clicked.connect(self._import_config_code)
        button_row.addWidget(btn_export, 1)
        button_row.addWidget(btn_import, 1)
        config_layout.addLayout(button_row)
        config_layout.addStretch()
        tabs.addTab(w5, "設定データ")

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _center_on_current_screen(self):
        if not self._on_center:
            return
        geometry = self._on_center()
        if isinstance(geometry, dict):
            self.cfg.update(geometry)

    def _export_config_code(self):
        code = _encode_style_config(self.result_cfg())
        QApplication.clipboard().setText(code)
        QMessageBox.information(
            self, "設定コードをエクスポート", "設定コードをクリップボードにコピーしました。"
        )

    def _import_config_code(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("設定コードをインポート")
        dlg.resize(520, 260)
        layout = QVBoxLayout(dlg)
        note = QLabel("設定コードを貼り付けてください。")
        editor = QPlainTextEdit()
        editor.setPlaceholderText(_STYLE_CONFIG_PREFIX + "…")
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("インポート")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("キャンセル")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(note)
        layout.addWidget(editor)
        layout.addWidget(buttons)
        editor.setFocus()
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            style = _decode_style_config(editor.toPlainText())
        except ValueError as exc:
            QMessageBox.warning(
                self, "インポートに失敗しました", f"{exc}\n現在のスタイルは変更されていません。"
            )
            return

        self._apply_style_config(style)
        self._preview()
        QMessageBox.information(self, "インポート完了", "歌詞スタイルを復元しました。")

    def _apply_style_config(self, style: dict):
        """Populate every style control after the complete code was validated."""
        self._applying_config = True
        try:
            self.font_jp_w._edit.setText(style["font_jp"])
            self.font_zh_w._edit.setText(style["font_zh"])
            self.font_size_jp.setValue(style["font_size_jp"])
            self.font_size_rt.setValue(style["font_size_rt"])
            self.font_size_zh.setValue(style["font_size_zh"])
            self.spacing_rt.setValue(style["spacing_rt"])
            self.spacing_zh.setValue(style["spacing_zh"])
            for button, key in (
                (self.btn_sung, "color_sung"),
                (self.btn_unsung, "color_unsung"),
                (self.btn_zh, "color_zh"),
                (self.btn_shadow, "shadow_color"),
            ):
                button._color = style[key]
                button.setStyleSheet(f"background:{button._color};")
            self.opacity.setValue(style["opacity"])
            self.hide_pause.setChecked(style["hide_on_pause"])
            self.unlock_zone.setValue(style["unlock_zone"])
            self.shadow_enabled.setChecked(style["shadow_enabled"])
            self.shadow_opacity.setValue(style["shadow_opacity"])
            self.shadow_blur.setValue(style["shadow_blur"])
            self.shadow_dx.setValue(style["shadow_dx"])
            self.shadow_dy.setValue(style["shadow_dy"])
            self.align_h.setCurrentIndex({"left": 0, "center": 1, "right": 2}[style["align_h"]])
            self.align_v.setCurrentIndex({"top": 0, "center": 1, "bottom": 2}[style["align_v"]])
        finally:
            self._applying_config = False

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
            dlg = QFileDialog(
                self, "フォントファイルを選択",
                str(FONTS_DIR),
                "Font Files (*.ttf *.otf *.ttc *.woff2 *.woff)"
            )
            dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
            _prepare_file_dialog(dlg, 620, 400)
            if dlg.exec():
                files = dlg.selectedFiles()
                if files:
                    edit.setText(files[0])
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
            "hide_on_pause":   self.hide_pause.isChecked(),
            "unlock_zone":     self.unlock_zone.value(),
            "shadow_enabled":  self.shadow_enabled.isChecked(),
            "shadow_color":    self.btn_shadow._color,
            "shadow_opacity":  self.shadow_opacity.value(),
            "shadow_blur":     self.shadow_blur.value(),
            "shadow_dx":       self.shadow_dx.value(),
            "shadow_dy":       self.shadow_dy.value(),
            "align_h":         ["left", "center", "right"][self.align_h.currentIndex()],
            "align_v":         ["top",  "center", "bottom"][self.align_v.currentIndex()],
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


def _center_window_on_current_screen(window: QWidget) -> dict:
    """Show a lyrics window and fit it at the center of the pointer's screen."""
    screen = (
        QApplication.screenAt(QCursor.pos())
        or window.screen()
        or QApplication.primaryScreen()
    )
    if not screen:
        window.show()
        return dict(x=window.x(), y=window.y(), w=window.width(), h=window.height())

    avail = screen.availableGeometry()
    width = min(max(1, window.width()), avail.width())
    height = min(max(1, window.height()), avail.height())
    x = avail.x() + (avail.width() - width) // 2
    y = avail.y() + (avail.height() - height) // 2
    window.setGeometry(x, y, width, height)
    window.show()
    window.raise_()
    return dict(x=x, y=y, w=width, h=height)


def _screen_relative_defaults() -> dict:
    screen = QApplication.primaryScreen()
    if not screen:
        return {}
    g = screen.availableGeometry()
    w = min(DEFAULT_CFG["w"], g.width())
    h = min(DEFAULT_CFG["h"], g.height())
    return dict(x=g.x() + (g.width() - w) // 2, y=g.y() + (g.height() - h) // 2,
                w=w, h=h)


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
        _prepare_file_dialog(dlg, 560, 380)
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

        def on_center():
            geometry = _center_window_on_current_screen(self)
            self.cfg.update(geometry)
            save_config(self.cfg)
            return geometry

        dlg = SettingsDialog(
            self.cfg, self, on_preview=on_preview, on_center=on_center
        )
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
