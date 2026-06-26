#!/usr/bin/env python3
"""furi-lrc — karaoke lyrics overlay for Windows 11
PyQt6 + QWebEngineView · bilingual JP/ZH · furigana · mora-level karaoke sweep
"""

import sys
import json
import time
import ctypes
import asyncio
import threading
import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QMenu, QDialog,
    QFormLayout, QLineEdit, QDialogButtonBox, QTabWidget,
    QDoubleSpinBox, QColorDialog, QPushButton, QFileDialog,
    QSpinBox, QCheckBox,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject, QPoint, QPropertyAnimation, QEasingCurve, QUrl
from PyQt6.QtGui import QColor, QCursor, QPainter, QPen, QFontDatabase, QFont

# ── winrt SMTC (optional) ───
WINRT_AVAILABLE = False
try:
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )
    WINRT_AVAILABLE = True
except ImportError:
    pass

# ── Config ───
FONTS_DIR   = Path(__file__).parent / "fonts"
CONFIG_PATH = Path(__file__).parent / "settings.json"
DEFAULT_CFG = dict(
    x=50, y=50, w=580, h=320,
    opacity=0.88, locked=False,
    font_jp=str(FONTS_DIR / "NotoSerifJP-SemiBold.ttf"),
    font_zh=str(FONTS_DIR / "msyhbd.ttc"),
    font_size_jp=20, font_size_zh=14, spacing_zh=-8,
    color_sung="#4fc3f7", color_unsung="#888888", color_zh="#aaaaaa",
    lyrics_path="", hide_on_pause=True, unlock_zone=48,
    text_shadow="2px 2px 8px rgba(0,0,0,0.85)",
)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = {**DEFAULT_CFG, **json.loads(CONFIG_PATH.read_text("utf-8"))}
            # If font values are not file paths (e.g. old config stored family names), reset to default
            for key in ("font_jp", "font_zh"):
                val = cfg[key]
                p = Path(val) if Path(val).is_absolute() else Path(__file__).parent / val
                if not p.suffix.lower() in {".ttf", ".otf", ".ttc", ".woff", ".woff2"}:
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
                playing = int(pb.playback_status) == 3  # MediaPlaybackStatus.Playing = 3

                pos = tl.position
                sec = pos.total_seconds() if hasattr(pos, "total_seconds") else float(pos) / 1e7

                # Interpolate: SMTC position is a snapshot, add elapsed time since snapshot
                if playing:
                    lu = tl.last_updated_time
                    now_utc = datetime.datetime.now(datetime.timezone.utc)
                    try:
                        if isinstance(lu, datetime.datetime):
                            elapsed = (now_utc - lu).total_seconds()
                        else:
                            # Windows FILETIME (100-ns ticks since 1601-01-01)
                            ft_epoch = datetime.datetime(1601, 1, 1, tzinfo=datetime.timezone.utc)
                            lu_utc = ft_epoch + datetime.timedelta(microseconds=int(lu) / 10)
                            elapsed = (now_utc - lu_utc).total_seconds()
                        sec = max(0.0, sec + elapsed)
                    except Exception:
                        pass

                self.time_updated.emit(sec, playing)
            except Exception:
                pass
            await asyncio.sleep(0.2)


# ── HTML/CSS/JS page ───
_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
:root {
  --sung:        #4fc3f7;
  --unsung:      #888;
  --zh:          #aaa;
  --font-jp:     "FuriLrcJP", serif;
  --font-zh:     "FuriLrcZH", sans-serif;
  --fsize-jp:    20px;
  --fsize-zh:    14px;
  --spacing-zh:  -8px;
  --text-shadow: 2px 2px 8px rgba(0,0,0,0.85);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  background: transparent;
  width: 100%; height: 100%;
  overflow: hidden;
}
#wrap {
  width: 100%; height: 100%;
  position: relative;
  overflow: hidden;
}
.line {
  position: absolute;
  left: 0; right: 0;
  top: 50%;
  transform: translateY(-50%);
  text-align: center;
  padding: 0 18px;
  line-height: 2.8;
  opacity: 0;
  transition: opacity 0.35s ease;
}
.line.active { opacity: 1; }

/* The glyph run is wrapped in an inline-block so it shrinks to the text width
   and stays horizontally centered.  The two .jp layers stack pixel-perfectly
   inside it: .base (gray, in flow, defines the box and is measured) and
   .fill (highlight, absolutely positioned on top, revealed by a clip mask). */
.jp-wrap {
  position: relative;
  display: inline-block;
  text-align: left;
  white-space: nowrap;   /* one visual line → the horizontal sweep is exact */
  max-width: 100%;
}
.jp {
  font-family: var(--font-jp);
  font-size: var(--fsize-jp);
  font-weight: 600;
  white-space: nowrap;
}
.jp.base {
  color: var(--unsung);
  text-shadow: var(--text-shadow);
  transform: translateZ(0);   /* cache as a texture: the blurred shadow rasters once, not per frame */
}
.jp.fill {
  position: absolute;
  left: 0; top: 0;
  color: var(--sung);
  clip-path: inset(0 100% 0 0);   /* nothing shown until the rAF loop drives it */
  will-change: clip-path;
  pointer-events: none;
}

.zh {
  font-family: var(--font-zh);
  font-size: var(--fsize-zh);
  color: var(--zh);
  text-shadow: var(--text-shadow);
  display: block;
  margin-top: var(--spacing-zh);
  padding-bottom: 0.25em;
}
ruby { ruby-align: center; }
rt   { font-size: 0.5em; ruby-align: center; }

#placeholder {
  position: absolute;
  left: 0; right: 0;
  top: 50%;
  transform: translateY(-50%);
  font-family: var(--font-jp);
  font-size: var(--fsize-jp);
  color: var(--unsung);
  text-align: center;
  opacity: 0.4;
}
#hud {
  position: fixed;
  left: 4px; top: 2px;
  font: 11px/1.4 monospace;
  color: #0f0;
  text-shadow: 0 0 2px #000, 0 0 2px #000;
  white-space: pre;
  pointer-events: none;
  display: none;
}
#hud.on { display: block; }
</style>
</head>
<body>
<div id="wrap">
  <div id="placeholder">歌詞ファイルをドロップまたは右クリックで開く</div>
</div>
<div id="hud"></div>
<script>
"use strict";

let lyrics   = [];
let lineSegs = [];     // per line: flat, ordered list of timed+measurable units
let lineGeo  = [];     // per line: cached pixel geometry (lazy, invalidated on resize/font change)
let baseMs   = 0;      // playback position (ms) anchored at baseWall
let baseWall = 0;      // performance.now() when baseMs was anchored
let playing  = false;
let activeI  = -1;
let _dbg     = false;  // furiDebug(true) to show an fps/clock HUD

/* ── Public API (unchanged signatures — Python side needs no edits) ── */

function loadLyrics(data) {
  lyrics  = data || [];
  activeI = -1;
  lineGeo = [];
  buildDOM();
}

function syncTime(sec, isPlaying) {
  /* The external SMTC position is coarse and arrives ~5x/sec; snapping the
     clock to it every time makes the sweep jitter.  Instead the clock runs
     free at real wall-clock speed and only absorbs a fraction of the error
     each update — so it stays locked without ever visibly jumping.  A large
     error (seek) or a play/resume still hard-resyncs. */
  const target    = sec * 1000;
  const predicted = playing ? (baseMs + (performance.now() - baseWall)) : baseMs;

  if (!isPlaying) {
    playing  = false;
    baseMs   = target;
    baseWall = performance.now();
    return;
  }

  const wasPlaying = playing;
  playing = true;
  const err = target - predicted;

  if (!wasPlaying || Math.abs(err) > 700) {
    baseMs   = target;                  // resume or seek → hard resync
  } else {
    baseMs   = predicted + err * 0.15;  // small drift → ease 15% toward truth
  }
  baseWall = performance.now();
}

function setTheme(sung, unsung, zh, fontJP, fontZH, fsizeJP, fsizeZH, spacingZH, shadow) {
  const r = document.documentElement.style;
  r.setProperty('--sung',        sung);
  r.setProperty('--unsung',      unsung);
  r.setProperty('--zh',          zh);
  r.setProperty('--font-jp',     '"' + fontJP + '", serif');
  r.setProperty('--font-zh',     '"' + fontZH + '", sans-serif');
  r.setProperty('--fsize-jp',    fsizeJP + 'px');
  r.setProperty('--fsize-zh',    fsizeZH + 'px');
  r.setProperty('--spacing-zh',  spacingZH + 'px');
  r.setProperty('--text-shadow', shadow || 'none');
  lineGeo = [];   // font size changed → glyph widths changed → re-measure
}

/* ── DOM builder: two identical layers per line + a flat timing list ── */

function buildDOM() {
  const wrap = document.getElementById('wrap');
  wrap.innerHTML = '';
  lineSegs = [];

  lyrics.forEach((line, li) => {
    const d = document.createElement('div');
    d.className = 'line';
    d.id = 'L' + li;

    const jw = document.createElement('div');
    jw.className = 'jp-wrap';
    jw.id = 'W' + li;

    const base = buildJP(li, line.jp, true);    // measured, unsung
    const fill = buildJP(li, line.jp, false);   // highlight, clipped
    base.classList.add('base');
    fill.classList.add('fill');
    fill.id = 'F' + li;
    jw.appendChild(base);
    jw.appendChild(fill);
    d.appendChild(jw);

    if (line.zh) {
      const zh = document.createElement('div');
      zh.className = 'zh';
      zh.textContent = line.zh;
      d.appendChild(zh);
    }
    wrap.appendChild(d);

    /* Flat, reading-order list of the units we can both time and measure.
       A ruby segment is treated as ONE geometry box (the base kanji) spanning
       the time range of all its furigana morae, so the kanji fills smoothly. */
    const flat = [];
    line.jp.forEach((seg, si) => {
      if (seg.ruby) {
        if (seg.units.length) {
          flat.push({
            id: 'B' + li + '_' + si,
            s:  seg.units[0].s,
            e:  seg.units[seg.units.length - 1].e,
          });
        }
      } else {
        seg.units.forEach((u, ui) => {
          flat.push({ id: 'U' + li + '_' + si + '_' + ui, s: u.s, e: u.e });
        });
      }
    });
    lineSegs[li] = flat;

    if (flat.some(f => typeof f.s !== 'number' || typeof f.e !== 'number')) {
      console.warn('[furi-lrc] line ' + li + ' has morae missing numeric s/e — no sweep possible for it.');
    }
  });
}

function buildJP(li, segs, isBase) {
  const jp = document.createElement('div');
  jp.className = 'jp';
  segs.forEach((seg, si) => {
    if (seg.ruby) {
      const ruby = document.createElement('ruby');
      const b = document.createElement('span');
      b.className = 'unit';
      if (isBase) b.id = 'B' + li + '_' + si;   // only the base layer is measured
      b.textContent = seg.base;
      ruby.appendChild(b);
      const rt = document.createElement('rt');
      seg.units.forEach((u) => {
        const s = document.createElement('span');
        s.textContent = u.k;
        rt.appendChild(s);
      });
      ruby.appendChild(rt);
      jp.appendChild(ruby);
    } else {
      seg.units.forEach((u, ui) => {
        const s = document.createElement('span');
        s.className = 'unit';
        if (isBase) s.id = 'U' + li + '_' + si + '_' + ui;
        s.textContent = u.k;
        jp.appendChild(s);
      });
    }
  });
  return jp;
}

/* ── Pixel geometry (measured once per line, cached) ── */

function measureLine(li) {
  const wrap = document.getElementById('W' + li);
  const flat = lineSegs[li] || [];
  const items = [];
  for (const seg of flat) {
    const el = document.getElementById(seg.id);
    if (!el) continue;
    items.push({ s: seg.s, e: seg.e, left: el.offsetLeft, width: el.offsetWidth });
  }
  return { items, totalW: wrap ? wrap.clientWidth : 0 };
}

function geoFor(li) {
  if (!lineGeo[li]) lineGeo[li] = measureLine(li);
  return lineGeo[li];
}

/* Map playback time → x pixel of the highlight edge.
   Within a unit the edge interpolates linearly in time across that glyph's
   width; between units it rests at the right edge of the last sung glyph. */
function sweepX(geo, t) {
  const it = geo.items;
  if (!it.length) return 0;
  if (t <= it[0].s) return 0;
  const last = it[it.length - 1];
  if (t >= last.e) return geo.totalW;
  for (let k = 0; k < it.length; k++) {
    const seg = it[k];
    if (t < seg.s) return k > 0 ? (it[k - 1].left + it[k - 1].width) : 0;
    if (t < seg.e) {
      const f = (t - seg.s) / Math.max(1, seg.e - seg.s);
      return seg.left + f * seg.width;
    }
  }
  return geo.totalW;
}

/* ── Clock ── */

function nowMs() {
  return playing ? (baseMs + (performance.now() - baseWall)) : baseMs;
}

function findLine(ms) {
  let idx = -1;
  for (let i = 0; i < lyrics.length; i++) {
    if (lyrics[i].start <= ms) idx = i; else break;
  }
  return idx;
}

/* ── rAF loop: pick the active line, then drive its clip mask every frame ── */

let _fpsT = 0, _fpsN = 0, _fps = 0, _lastFrame = 0, _worstGap = 0;

function furiDebug(on) {
  _dbg = !!on;
  document.getElementById('hud').classList.toggle('on', _dbg);
}

function sweep(ts) {
  if (_dbg) {
    if (_lastFrame) _worstGap = Math.max(_worstGap, ts - _lastFrame);
    _lastFrame = ts;
    _fpsN++;
    if (ts - _fpsT >= 500) { _fps = Math.round(_fpsN * 1000 / (ts - _fpsT)); _fpsT = ts; _fpsN = 0; }
  }

  if (lyrics.length > 0) {
    const ms = nowMs();
    const li = findLine(ms);

    if (li !== activeI) {
      if (activeI >= 0) {
        const prev = document.getElementById('L' + activeI);
        if (prev) prev.classList.remove('active');
      }
      if (li >= 0) {
        const cur = document.getElementById('L' + li);
        if (cur) cur.classList.add('active');
      }
      activeI = li;
    }

    if (li >= 0) {
      const fill = document.getElementById('F' + li);
      if (fill) {
        const geo = geoFor(li);
        const x   = sweepX(geo, ms);
        const r   = Math.max(0, geo.totalW - x);
        fill.style.clipPath = 'inset(0 ' + r.toFixed(1) + 'px 0 0)';
      }
    }

    if (_dbg) {
      document.getElementById('hud').textContent =
        'fps ' + _fps + '  worstGap ' + _worstGap.toFixed(0) + 'ms\n' +
        'clk ' + (ms / 1000).toFixed(2) + 's  line ' + activeI + '  play ' + playing;
      _worstGap = 0;
    }
  }
  requestAnimationFrame(sweep);
}

/* Cached pixel widths are only valid for the current layout/font, so drop the
   cache when either changes; the next frame re-measures lazily. */
window.addEventListener('resize', () => { lineGeo = []; });
if (document.fonts && document.fonts.ready) {
  document.fonts.ready.then(() => { lineGeo = []; });
}

requestAnimationFrame(sweep);
</script>
</body>
</html>
"""


def _resolve_font_uri(path_str: str) -> str:
    p = Path(path_str)
    if not p.is_absolute():
        p = Path(__file__).parent / p
    return p.as_uri()


def _build_html(cfg: dict) -> str:
    jp_url = _resolve_font_uri(cfg["font_jp"])
    zh_url = _resolve_font_uri(cfg["font_zh"])
    font_face = (
        f'@font-face {{\n'
        f'  font-family: "FuriLrcJP";\n'
        f'  src: url("{jp_url}") format("truetype");\n'
        f'}}\n'
        f'@font-face {{\n'
        f'  font-family: "FuriLrcZH";\n'
        f'  src: url("{zh_url}") format("truetype");\n'
        f'}}\n'
    )
    return _HTML.replace("<style>", "<style>\n" + font_face, 1)


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
        self.font_size_zh.valueChanged.connect(self._preview)
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

        # ── Tab: 表示 ──
        w1   = QWidget()
        f1   = QFormLayout(w1)
        self.font_jp_w    = self._font_row(self.cfg["font_jp"])
        self.font_zh_w    = self._font_row(self.cfg["font_zh"])
        self.font_size_jp = QSpinBox(); self.font_size_jp.setRange(8, 96); self.font_size_jp.setSuffix(" px"); self.font_size_jp.setValue(self.cfg["font_size_jp"])
        self.font_size_zh = QSpinBox(); self.font_size_zh.setRange(8, 96); self.font_size_zh.setSuffix(" px"); self.font_size_zh.setValue(self.cfg["font_size_zh"])
        self.spacing_zh   = QSpinBox(); self.spacing_zh.setRange(-60, 60); self.spacing_zh.setSuffix(" px"); self.spacing_zh.setValue(self.cfg["spacing_zh"])
        f1.addRow("日本語フォント",      self.font_jp_w)
        f1.addRow("中国語フォント",         self.font_zh_w)
        f1.addRow("日本語フォントサイズ", self.font_size_jp)
        f1.addRow("中国語フォントサイズ",   self.font_size_zh)
        f1.addRow("日中間距(px)",         self.spacing_zh)
        tabs.addTab(w1, "表示")

        # ── Tab: 色彩 ──
        w2   = QWidget()
        f2   = QFormLayout(w2)
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

        # ── Tab: 動作 ──
        w3   = QWidget()
        f3   = QFormLayout(w3)
        self.hide_pause = QCheckBox(); self.hide_pause.setChecked(self.cfg["hide_on_pause"])
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
            "font_jp":      self.font_jp_w._edit.text(),
            "font_zh":      self.font_zh_w._edit.text(),
            "font_size_jp": self.font_size_jp.value(),
            "font_size_zh": self.font_size_zh.value(),
            "spacing_zh":   self.spacing_zh.value(),
            "color_sung":   self.btn_sung._color,
            "color_unsung": self.btn_unsung._color,
            "color_zh":     self.btn_zh._color,
            "opacity":      self.opacity.value(),
            "hide_on_pause": self.hide_pause.isChecked(),
            "unlock_zone":  self.unlock_zone.value(),
            "text_shadow":  self.text_shadow.text(),
        }


# ── Transparent mouse overlay (QWebEngineView swallows OS mouse events) ───
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
        if not (w._hovering and not w._locked) and not w._zone_preview:
            return
        painter = QPainter(self)
        if w._hovering and not w._locked:
            pen = QPen(QColor(255, 255, 255, 38))
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawRect(0, 0, self.width() - 1, self.height() - 1)
        if w._zone_preview:
            zone = w._preview_zone_size
            x0 = self.width() - zone
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


# ── Main window ───
class LyricWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg          = load_config()
        self._locked         = self.cfg["locked"]
        self._drag_pos       = QPoint()
        self._is_playing     = False
        self._page_ready     = False
        self._pending        = None   # lyrics lines waiting for page load
        self._last_sec       = 0.0
        self._last_wall      = time.monotonic()
        self._fade_anim      = None
        self._unlock_visible = False  # whether the unlock hot-zone btn is showing
        self._hovering       = False
        self._resize_dir     = (0, 0)   # (dx, dy) each -1/0/1
        self._resize_start_geo = None
        self._resize_start_pos = None
        self._zone_preview       = False
        self._preview_zone_size  = self.cfg.get("unlock_zone", 48)

        self._geo_save_timer = QTimer(self)
        self._geo_save_timer.setSingleShot(True)
        self._geo_save_timer.setInterval(600)
        self._geo_save_timer.timeout.connect(self._save_geometry)

        self._setup_window()
        self._setup_zone_btn()
        self._setup_web()
        self._setup_smtc()
        self._setup_timer()
        self._setup_cursor_timer()

        if self.cfg["lyrics_path"]:
            self._load_lyrics(self.cfg["lyrics_path"])

    # ── Setup 

    def _setup_window(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool          # no taskbar entry
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(self.cfg["opacity"])
        self.setGeometry(self.cfg["x"], self.cfg["y"], self.cfg["w"], self.cfg["h"])
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
        zone = self.cfg.get("unlock_zone", 48)
        bw, bh = self._zone_btn.width(), self._zone_btn.height()
        x = self.width() - zone + (zone - bw) // 2
        y = (zone - bh) // 2
        self._zone_btn.move(x, y)

    def _set_clickthrough(self, enabled: bool):
        hwnd  = int(self.winId())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)   # GWL_EXSTYLE
        if enabled:
            style |= 0x00080020   # WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            style &= ~0x00000020  # clear WS_EX_TRANSPARENT; keep WS_EX_LAYERED
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
                self._set_clickthrough(False)   # let button receive the click
            self._zone_btn.raise_()
            self._zone_btn.show()
        elif not in_zone and self._unlock_visible:
            self._unlock_visible = False
            self._zone_btn.hide()
            if self._locked:
                self._set_clickthrough(True)

    def _setup_web(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.web = QWebEngineView(self)
        self.web.page().setBackgroundColor(QColor(0, 0, 0, 0))
        self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        layout.addWidget(self.web)

        # Overlay catches all mouse events — QWebEngineView's Chromium renderer
        # has its own native HWND that consumes OS mouse messages before Qt sees them,
        # so WA_TransparentForMouseEvents alone is not reliable.
        self._overlay = _MouseOverlay(self)
        self._overlay.setGeometry(0, 0, self.width(), self.height())
        self._zone_btn.raise_()   # keep zone button above overlay

        self.web.loadFinished.connect(self._on_page_ready)
        self.web.setHtml(_build_html(self.cfg), QUrl.fromLocalFile(str(Path(__file__).parent) + "/"))

    def _setup_smtc(self):
        self.smtc = SMTCWorker()
        self.smtc.time_updated.connect(self._on_time_updated)
        self.smtc.track_changed.connect(self._on_track_changed)
        self.smtc.start()

    def _setup_timer(self):
        # Re-send current time every 200 ms so JS interpolation stays calibrated
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._push_sync)
        self._timer.start()

    # ── Page / lyrics ──

    def _on_page_ready(self):
        self._page_ready = True
        self._apply_theme()
        if self._pending is not None:
            self._inject(self._pending)
            self._pending = None

    def _apply_theme(self):
        self._apply_theme_cfg(self.cfg)

    def _apply_theme_cfg(self, cfg: dict):
        c = cfg
        shadow = c.get("text_shadow", "").replace("'", "\\'") or "none"
        js = (
            f"setTheme('{c['color_sung']}','{c['color_unsung']}',"
            f"'{c['color_zh']}','FuriLrcJP','FuriLrcZH',"
            f"{c['font_size_jp']},{c['font_size_zh']},{c['spacing_zh']},'{shadow}');"
        )
        self.web.page().runJavaScript(js)

    def _load_lyrics(self, path: str):
        p = Path(path)
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text("utf-8"))
            lines = raw.get("lines", raw) if isinstance(raw, dict) else raw
            self.cfg["lyrics_path"] = str(p)
            if self._page_ready:
                self._inject(lines)
            else:
                self._pending = lines
        except Exception as e:
            print(f"[furi-lrc] load error: {e}", file=sys.stderr)

    def _inject(self, lines: list):
        js = f"loadLyrics({json.dumps(lines, ensure_ascii=False)});"
        self.web.page().runJavaScript(js)

    # ── SMTC callbacks ─

    def _on_time_updated(self, sec: float, playing: bool):
        was_playing = self._is_playing
        self._last_sec   = sec
        self._last_wall  = time.monotonic()
        self._is_playing = playing
        self._push_sync()

        if self.cfg["hide_on_pause"]:
            if playing and not was_playing:
                self._fade(show=True)
            elif not playing and was_playing:
                self._fade(show=False)

    def _on_track_changed(self, title: str):
        if not self.cfg["lyrics_path"]:
            return
        base = Path(self.cfg["lyrics_path"]).parent
        for p in base.glob("*.json"):
            if title.lower() in p.stem.lower():
                self._load_lyrics(str(p))
                return

    def _push_sync(self):
        if not self._page_ready:
            return
        if self._is_playing:
            sec = self._last_sec + (time.monotonic() - self._last_wall)
        else:
            sec = self._last_sec
        playing_js = "true" if self._is_playing else "false"
        self.web.page().runJavaScript(f"syncTime({sec:.3f},{playing_js});")

    # ── Animation ─

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

    # ── Resize helpers ─

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
        # diagonal: dx*dy>0 → \  dx*dy<0 → /
        return Qt.CursorShape.SizeBDiagCursor if dx * dy > 0 else Qt.CursorShape.SizeFDiagCursor

    def _do_resize(self, global_pos):
        dx, dy = self._resize_dir
        geo = self._resize_start_geo
        ddx = global_pos.x() - self._resize_start_pos.x()
        ddy = global_pos.y() - self._resize_start_pos.y()
        MIN_W, MIN_H = 200, 100
        nx, ny, nw, nh = geo.x(), geo.y(), geo.width(), geo.height()
        if dx == -1:
            nw -= ddx;  nx += ddx
        elif dx == 1:
            nw += ddx
        if dy == -1:
            nh -= ddy;  ny += ddy
        elif dy == 1:
            nh += ddy
        if nw < MIN_W:
            if dx == -1: nx = geo.right() - MIN_W + 1
            nw = MIN_W
        if nh < MIN_H:
            if dy == -1: ny = geo.bottom() - MIN_H + 1
            nh = MIN_H
        self.setGeometry(nx, ny, nw, nh)

    # ── Drag / resize (window not locked) ─

    def mousePressEvent(self, e):
        if self._locked or e.button() != Qt.MouseButton.LeftButton:
            return
        dx, dy = self._get_resize_dir(e.position().toPoint())
        if dx != 0 or dy != 0:
            self._resize_dir      = (dx, dy)
            self._resize_start_geo = self.geometry()
            self._resize_start_pos = e.globalPosition().toPoint()
            self._drag_pos = QPoint()
        else:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
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

    # ── Drag & drop JSON files ───

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

    # ── Context menu ───

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
        self._locked = not self._locked
        self.cfg["locked"] = self._locked
        self._unlock_visible = False
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
            self._apply_theme_cfg(preview_cfg)
            self._preview_zone_size = preview_cfg.get("unlock_zone", 48)
            self._zone_preview = True
            self._overlay.update()
            # reposition zone button live as zone size changes
            self.cfg["unlock_zone"] = self._preview_zone_size
            self._reposition_zone_btn()

        dlg = SettingsDialog(self.cfg, self, on_preview=on_preview)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_cfg = dlg.result_cfg()
            font_changed = (new_cfg["font_jp"] != self.cfg["font_jp"] or
                            new_cfg["font_zh"] != self.cfg["font_zh"])
            self.cfg = new_cfg
            self.setWindowOpacity(self.cfg["opacity"])
            if font_changed:
                self._page_ready = False
                self.web.setHtml(_build_html(self.cfg), QUrl.fromLocalFile(str(Path(__file__).parent) + "/"))   # triggers _on_page_ready
            else:
                self._apply_theme()
            save_config(self.cfg)
        else:
            self.cfg["unlock_zone"] = original_cfg.get("unlock_zone", 48)
            self.setWindowOpacity(original_cfg["opacity"])
            self._apply_theme_cfg(original_cfg)
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

    # ── Persist on close ────

    def closeEvent(self, e):
        self.cfg.update(x=self.x(), y=self.y(), w=self.width(), h=self.height(), locked=self._locked)
        save_config(self.cfg)
        self.smtc.stop()
        super().closeEvent(e)


# ── Entry ────
_MENU_FONT: QFont | None = None


def _load_menu_font() -> None:
    global _MENU_FONT
    path = str(FONTS_DIR / "NotoSansJP-Regular.ttf")
    fid  = QFontDatabase.addApplicationFont(path)
    families = QFontDatabase.applicationFontFamilies(fid)
    if families:
        _MENU_FONT = QFont(families[0])


def main():
    # Must be set before QApplication for WebEngine transparency to work on Windows
    import os
    # The real smoothness killer for a transparent, always-on-top tool window is
    # Chromium's *throttling*, not the sweep code: Windows native-occlusion
    # detection can flag the overlay as covered and slow/halt its renderer, and
    # the renderer-backgrounding heuristics do the same when it has no focus.
    # That caps rAF to a crawl → visible stutter no matter how cheap the frame is.
    # Disabling those three lets the overlay paint at a steady vsync rate.
    #
    # GPU rasterization stays on (software compositing makes every animation
    # stutter). If the transparent background ever breaks on your GPU, append
    # --disable-gpu-compositing to the env var below before launching.
    os.environ.setdefault(
        "QTWEBENGINE_CHROMIUM_FLAGS",
        "--enable-gpu-rasterization "
        "--disable-features=CalculateNativeWinOcclusion "
        "--disable-backgrounding-occluded-windows "
        "--disable-renderer-backgrounding"
    )

    app = QApplication(sys.argv)
    app.setApplicationName("furi-lrc")
    _load_menu_font()

    win = LyricWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()