<div align="center">
  <img src="https://furi-lrc.beyondwj.cc/assets/furi-lrc-icon.png" width="112" alt="FURI-LRC 图标">
  <h1>FURI-LRC</h1>
  <p><strong>为日语歌曲而生的逐字歌词编辑器、播放器与桌面悬浮歌词</strong></p>
  <p>逐字时间轴 · 假名注音 · 中文翻译 · 音频波形 · 透明悬浮窗</p>

  <p>
    <a href="https://github.com/BeyondWillJ/FURI-LRC/releases/latest"><img src="https://img.shields.io/github/v/release/BeyondWillJ/FURI-LRC?style=flat-square&display_name=tag&sort=semver" alt="Latest release"></a>
    <a href="https://github.com/BeyondWillJ/FURI-LRC/releases"><img src="https://img.shields.io/github/downloads/BeyondWillJ/FURI-LRC/total?style=flat-square" alt="Release downloads"></a>
    <a href="https://github.com/BeyondWillJ/FURI-LRC/stargazers"><img src="https://img.shields.io/github/stars/BeyondWillJ/FURI-LRC?style=flat-square" alt="GitHub stars"></a>
    <a href="https://github.com/BeyondWillJ/FURI-LRC/forks"><img src="https://img.shields.io/github/forks/BeyondWillJ/FURI-LRC?style=flat-square" alt="GitHub forks"></a><br>
    <a href="https://github.com/BeyondWillJ/FURI-LRC/issues"><img src="https://img.shields.io/github/issues/BeyondWillJ/FURI-LRC?style=flat-square" alt="GitHub issues"></a>
    <a href="https://github.com/BeyondWillJ/FURI-LRC/commits/main"><img src="https://img.shields.io/github/last-commit/BeyondWillJ/FURI-LRC?style=flat-square" alt="Last commit"></a>
    <a href="https://furi-lrc.beyondwj.cc/"><img src="https://img.shields.io/badge/website-FURI--LRC-8B5CF6?style=flat-square&logo=cloudflare&logoColor=white" alt="FURI-LRC website"></a>
  </p>

  <p>
    <img src="https://img.shields.io/badge/Platform-Windows_11-0078D4?logo=windows11&logoColor=white" alt="Windows 11">
    <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
    <img src="https://img.shields.io/badge/GUI-PyQt6-41CD52?logo=qt&logoColor=white" alt="PyQt6">
  </p>
</div>

<img src="https://furi-lrc.beyondwj.cc/assets/screenshots/player-main.png" width="100%" alt="FURI-LRC Player 主界面">

FURI-LRC 是一个面向日语歌曲的带假名注音歌词制作与播放工具。它把歌词制作、逐字对轴、音乐播放和桌面展示整合在一起，包含三个主要程序：

- `furi-lrc-gui.py`：歌词编辑器，用于制作带逐字时间轴、假名注音和中文翻译的歌词 JSON。
- `furi-lrc-player.py`：内置音频播放器，支持播放列表、音频播放和歌词悬浮窗同步显示。
- `furi-lrc_rubi.py`：独立透明歌词悬浮窗，可读取歌词 JSON，并可通过 Windows 媒体控制接口同步当前播放进度。

项目主要在 Windows 11 + Python + PyQt6 环境下使用。

## 功能亮点

- 制作 furi-lrc 歌词 JSON
- 从标准 `.lrc` 导入歌词时间轴
- 导出普通 `.lrc` 或歌词 JSON
- 支持日文假名注音标记：`{漢字|かんじ}`
- 支持逐字/逐假名时间轴编辑
- 支持音频波形显示和歌词行时间拖拽
- 支持透明置顶歌词悬浮窗
- 支持播放器播放列表 `.flpl`
- 支持自动保存最近播放列表和悬浮窗设置

## 界面预览

<table>
  <tr>
    <td width="50%" align="center">
      <img src="https://furi-lrc.beyondwj.cc/assets/screenshots/gui-editor.png" alt="FURI-LRC GUI 编辑器"><br>
      <sub><b>歌词编辑器</b> — 波形、歌词行与注音编辑集中在一个工作区</sub>
    </td>
    <td width="50%" align="center">
      <img src="https://furi-lrc.beyondwj.cc/assets/screenshots/overlay-desktop.png" alt="桌面悬浮歌词"><br>
      <sub><b>桌面悬浮歌词</b> — 同步显示日文、假名与中文翻译</sub>
    </td>
  </tr>
  <tr>
    <td width="50%" align="center">
      <img src="https://furi-lrc.beyondwj.cc/assets/screenshots/gui-units.png" alt="逐假名时间编辑"><br>
      <sub><b>精细时间轴</b> — 支持逐字、逐假名调整时间</sub>
    </td>
    <td width="50%" align="center">
      <img src="https://furi-lrc.beyondwj.cc/assets/screenshots/player-playlist.png" alt="播放器播放列表"><br>
      <sub><b>音乐播放器</b> — 播放列表与歌词悬浮窗联动</sub>
    </td>
  </tr>
</table>

## 环境要求

- Windows 11 推荐
- Python 3.10+
- PyQt6

安装依赖：

```powershell
pip install -r requirements.txt
pip install PyQt6-Qt6Multimedia mutagen
```

说明：

- `PyQt6-Qt6Multimedia` 用于音频播放。
- `mutagen` 用于读取音频标签和封面，播放器可选依赖。
- `numpy`、`librosa`、`soundfile` 用于编辑器波形显示；缺失时波形功能可能不可用。
- `winsdk` 用于独立悬浮窗读取 Windows 系统媒体播放状态；使用内置播放器时不依赖它同步。

## 快速开始

### 1. 启动歌词编辑器

```powershell
python furi-lrc-gui.py
```

也可以直接打开一个项目文件：

```powershell
python furi-lrc-gui.py yoru-ni-kakeru.flproj
```

编辑器用于创建和调整歌词。可以打开音频文件、导入 LRC、编辑日文歌词、添加假名注音、调整每行或每个假名的时间。

### 2. 启动播放器

```powershell
python furi-lrc-player.py
```

播放器支持拖入音频文件或 `.flpl` 播放列表。播放音频时会打开/同步歌词悬浮窗。歌词 JSON 可以手动指定，也会尝试按音频文件名或标题自动匹配。

### 3. 单独启动歌词悬浮窗

```powershell
python furi-lrc_rubi.py
```

右键悬浮窗可以打开歌词 JSON、锁定位置、调整样式或退出。独立模式依赖 Windows 媒体控制接口同步当前系统播放器。

## 歌词格式

歌词 JSON 的顶层通常包含 `lines`：

```json
{
  "lines": [
    {
      "start": 0,
      "end": 5000,
      "jp": [
        {
          "base": "東京",
          "ruby": true,
          "units": [
            { "k": "と", "s": 0, "e": 300 },
            { "k": "う", "s": 300, "e": 600 },
            { "k": "きょ", "s": 600, "e": 900 },
            { "k": "う", "s": 900, "e": 1200 }
          ]
        }
      ],
      "zh": "中文翻译"
    }
  ]
}
```

在编辑器的日文输入框中，可以使用这种标记添加假名注音：

```text
{東京|とうきょう}へ行く
```

普通文本会作为无注音片段保存；带 `{base|reading}` 的片段会保存为 ruby 片段。

## 常用快捷键

### 编辑器

| 快捷键 | 功能 |
| --- | --- |
| `Ctrl+O` | 打开项目/歌词 |
| `Ctrl+S` | 保存 |
| `Ctrl+Shift+S` | 另存为 |
| `Ctrl+Z` / `Ctrl+Y` | 撤销 / 重做 |
| `Space` | 播放 / 暂停，文本输入框聚焦时不拦截 |
| `Ctrl+Space` | 播放 / 暂停 |
| `J` / `L` | 后退 / 前进 500 ms |
| `,` / `.` | 后退 / 前进 100 ms |
| `T` | 将当前行开始时间设为当前播放位置 |
| `Enter` | 打拍模式下标记当前行 |
| `Backspace` | 打拍模式下撤回一步 |
| `Escape` | 退出打拍模式 |
| `Delete` | 删除选中行 |
| `Ctrl+Enter` | 在当前播放位置添加行 |
| `Ctrl+R` | 给选中文本添加注音标记 |

### 播放器

| 快捷键 | 功能 |
| --- | --- |
| `Space` | 播放 / 暂停 |
| `Ctrl+Left` / `Ctrl+Right` | 上一首 / 下一首 |
| `J` / `L` | 后退 / 前进 5 秒 |
| `,` / `.` | 后退 / 前进 1 秒 |
| `Ctrl+L` | 打开播放列表 |
| `Ctrl+S` | 保存播放列表 |
| `Ctrl+Shift+S` | 播放列表另存为 |
| `Delete` | 从播放列表移除选中项 |

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `furi-lrc-gui.py` | 歌词编辑器 |
| `furi-lrc-player.py` | 音频播放器和歌词悬浮窗整合入口 |
| `furi-lrc_rubi.py` | 独立歌词悬浮窗 |
| `requirements.txt` | Python 依赖 |
| `fonts/` | 内置字体资源 |
| `player_data/settings.json` | Player 悬浮窗位置、样式等本地设置 |
| `*.flproj` | 编辑器项目文件 |
| `*.flpl` | 播放器播放列表 |
| `*.json` | furi-lrc 歌词文件 |
| `*.lrc` | 标准 LRC 歌词文件 |

## 注意事项

- 项目中的示例音频、播放列表、设置文件可能包含本机绝对路径；换机器后需要重新选择音频或歌词文件。
- `player_data/settings.json` 和 `player_data/_last_playlist.flpl` 是 Player 运行时状态文件，会随使用变化。
- 独立悬浮窗的系统媒体同步依赖 `winsdk` 和 Windows 当前媒体会话；如果同步不稳定，可以优先使用 `furi-lrc-player.py` 内置播放器。
- 如果界面文字显示异常，请确认终端和编辑器使用 UTF-8，并优先在 Windows Terminal、PowerShell 7 或 IDE 终端中运行。

