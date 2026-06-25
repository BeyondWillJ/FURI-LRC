> 想要用Python开发一个Windows 11桌面歌词应用，这个应用能作为无边框悬浮窗口固定在桌面角落，始终置顶且可锁定位置，支持实时同步显示歌词并高亮当前行，能同时呈现日语原文与中文翻译的双语内容，允许用户分别自定义日语部分和汉语翻译的字体，同时通过HTML渲染实现标准的日语振假名显示效果，整体界面简洁流畅、支持音乐停止自动隐藏或半透明，并能兼容本地音乐文件或系统媒体会话获取播放进度。
>
> 请问我应该用什么工具箱以及什么思路能够实现这个目标？可以实现吗？
>
> 完全可以实现,而且这套需求几乎是为 PyQt6 + QWebEngineView 量身定做的——你之前做过 PyQt6 GUI 和 SudachiPy/fugashi 振假名工具,刚好能复用。下面我按"选型 → 分层架构 → 逐个难点"来讲思路。

## 一、技术选型(工具箱)

核心就一套:**PyQt6(或 PySide6)+ Qt WebEngine**。

- **窗口外壳**:`PyQt6`(`QWidget` 做无边框置顶悬浮窗)
- **歌词渲染**:`PyQt6-WebEngine`(`QWebEngineView`,底层是 Chromium)——这是关键,下面会解释为什么不能用原生 `QLabel`
- **系统播放进度**:`winrt-runtime` + `winrt-Windows.Media.Control`(读取 SMTC)
- **本地播放/进度**:如果你自己放歌,用 `python-vlc` 或 `QtMultimedia` 的 `QMediaPlayer`,进度自己就有
- **日文分词加振假名**:你现成的 `fugashi`/`SudachiPy` 管线,离线把 LRC 预处理成带 `<ruby>` 的 HTML
- **歌词解析**:LRC 时间轴自己写正则即可

一句话提醒:如果你打算把这个应用公开分发,`PyQt6` 是 GPL,`PySide6` 是 LGPL,两者 API 几乎一致(`QtWebEngineWidgets` 都有),换过去成本很低。

## 二、为什么必须用 WebEngine

你这几条需求里,**振假名 + 分语言字体**这两点直接决定了渲染方案:

Qt 原生的 `QLabel` 只支持一个 HTML 子集(富文本),**不支持 `<ruby>` 标签**,所以振假名要么自己用 `QPainter` 手画(在汉字上方按比例缩放绘制假名、处理换行对齐,工作量极大),要么上 WebEngine。既然你本来就想用 HTML 渲染,那答案很清楚:用 `QWebEngineView`,直接写标准 ruby:

```html
<ruby>到底<rt>とうてい</rt></ruby>
```

Chromium 对 ruby 的排版(假名居中、行高自动撑开)是原生且漂亮的,这一项基本白送。

分语言字体在 CSS 里也是一行的事,而在原生控件里很麻烦:

```css
.jp { font-family: "Noto Sans JP", sans-serif; }
.zh { font-family: "Source Han Sans SC", sans-serif; }
.active { color: #4fc3f7; font-weight: bold; }
```

用户自定义字体时,你只要改这两条 `font-family`(或用 CSS 变量),不用碰 Python 逻辑。

代价是 WebEngine 会带一整个 Chromium,打包体积大、首次启动稍慢、内存占用高。但换来的是完美的 ruby、CSS 动画(平滑高亮/滚动)和分语言字体,对这个应用非常值。

## 三、分层架构思路

强烈建议把"数据/同步层"和"UI 层"彻底分开,中间用 Qt 信号通信:

- **同步层**:一个后台线程(或 async 任务),负责拿到"当前播放位置"这一个数字,定时(比如每 200–300ms)`emit` 一个信号
- **UI 层**:主线程的 `QWebEngineView`,收到位置后算出当前应高亮第几行,通过 `runJavaScript` 切换 CSS class

这样无论进度来自本地播放器还是系统媒体会话,UI 层完全不用变——它只认"当前秒数"这一个输入。

## 四、逐个难点

**1. 无边框 / 置顶 / 锁定 / 半透明**

窗口 flags 这样设:

```python
self.setWindowFlags(
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool          # 不在任务栏显示
)
self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)  # 圆角/透明背景
self.setWindowOpacity(0.85)       # 半透明
```

- **固定到角落**:启动时按屏幕分辨率 `move()` 到右下角,位置存进配置文件
- **锁定**:加一个 `locked` 标志;`mousePressEvent`/`mouseMoveEvent` 里实现拖动,锁定时直接 `return` 不响应拖动
- **进阶可选**:锁定状态下让窗口"鼠标穿透"(点击穿到桌面),Windows 上用 `ctypes` 给窗口加 `WS_EX_TRANSPARENT` 扩展样式即可,体验更像桌面挂件

**2. 实时进度——两条路**

这是整个应用最核心、也最容易踩坑的地方。

*本地文件*:你自己用 `QMediaPlayer`/`python-vlc` 播放,`position()` 直接给毫秒进度,最简单可靠,同步精度最高。

*系统媒体会话(SMTC)*:好处是能蹭 Spotify、网易云、浏览器等任何应用的播放进度,坏处是有坑。现在的正确做法是装模块化的 winrt 包(2023 年 9 月后从 `winsdk` 改回 `winrt` 命名空间发布,每个 Windows SDK 命名空间一个独立包),需要 `winrt-runtime` 加上 `winrt-Windows.Media.Control`。导入和取值是异步的:

```python
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager
)

sessions = await MediaManager.request_async()
session = sessions.get_current_session()
timeline = session.get_timeline_properties()
playback = session.get_playback_info()
```

`timeline_properties` 能拿到 position(当前位置)和 max_seek_time(总时长)。

**关键坑**:SMTC 的 `position` 不是实时走秒的,它是一个"快照 + 时间戳",你 poll 一次拿到的是上次更新时刻的位置。所以必须自己插值,否则歌词会一卡一卡地跳:

```python
# 播放中时,真实位置 ≈ 快照位置 + (现在 - 快照更新时刻)
if status == "Playing":
    estimated = timeline.position + (now_utc - timeline.last_updated_time)
else:
    estimated = timeline.position
```

也就是:每隔几百毫秒 poll 一次 SMTC 校准,两次之间用本地时钟自己往前推。这样歌词才会平滑滚动。

**3. asyncio 与 Qt 事件循环的桥接**

winrt 是 async 的,Qt 是自己的事件循环,硬凑会很痛。最省事的做法:**把 SMTC 轮询放进一个独立线程,线程内跑自己的 asyncio loop**,拿到位置后用 `pyqtSignal` 发回主线程。不用引入 `qasync` 这种深度集成方案,因为你只是定时取一个数,后台线程轮询完全够用。

**4. 高亮当前行同步**

LRC 解析成 `[(秒数, 行号)]` 列表。同步层每次给个秒数,UI 层二分查找定位当前行,然后:

```python
self.web.page().runJavaScript(f"highlightLine({line_idx})")
```

JS 端给当前行加 `.active` class、移除旧的,再 `scrollIntoView({behavior:'smooth'})`。高亮变色和滚动都交给 CSS transition,丝滑感就来自这里,Python 一侧不用做任何动画。

**5. 双语 + 振假名的数据准备**

建议**离线预处理**:用你现成的 fugashi/SudachiPy 工具把每句日文 LRC 转成带 `<ruby>` 的 HTML 片段,中文翻译作为另一行,生成一个结构化文件(比如每行 `{time, jp_html, zh_text}` 的 JSON)。运行时直接读 JSON 拼进模板,既快又稳,避免实时分词拖慢同步。

**6. 停止自动隐藏 / 半透明**

同步层本来就在监控 `playback_status`。检测到 Paused/Stopped 时,用 `QPropertyAnimation` 对 `windowOpacity` 做淡出(或直接 `hide()`);恢复 Playing 再淡入。一个状态信号驱动即可。

## 五、需要提前知道的几个坑

- **WebEngine 打包**:用 PyInstaller 打包时,QtWebEngine 的 Chromium 资源和 `.pak` 文件经常漏带导致空白窗口,需要特别配置 hook
- **SMTC 拿不到某些应用**:部分播放器不上报 timeline(只报标题状态),这时 `position` 会一直是 0,需要做降级处理
- **多会话**:同时开多个播放器时 `get_current_session()` 可能不是你想要的那个,必要时用 `source_app_user_model_id` 过滤
- **WebEngine 透明背景**:要让网页背景透明,除了窗口的 `WA_TranslucentBackground`,还要给 page 设置透明背景色,并在 HTML/CSS 里 `background: transparent`

整体路线很成熟,没有"卡死做不出来"的环节。我的建议是先用本地 `QMediaPlayer` 把 UI、振假名渲染、高亮同步跑通(进度来源最可控),再把进度源切换成 SMTC,这样能把"渲染"和"系统集成"两个难点解耦,调试起来轻松很多。

需要的话,我可以先帮你搭一个最小可运行骨架(无边框置顶窗 + WebEngine 显示一段带 ruby 的双语歌词 + 假进度驱动高亮),你看着它再往上加 SMTC?

> 我希望高亮能够逐字（如果标了假名甚至要逐假名），该怎么做？歌词文件应该如何设计？有现成的歌词格式吗？

逐字/逐假名是 karaoke(卡拉OK)级别的同步,比逐行难一个量级,核心在于**计时单元**和**渲染方式**两件事。先让我确认一下现有格式的现状再给你完整设计。先说结论:**现成格式都能做到"逐字",但没有一个能干净地处理你的"逐假名 + 振假名 + 中日双语"组合**。所以正确的策略是——理解现有格式的计时模型,然后为自己的渲染器设计一个 JSON。下面分四部分讲。

## 一、现有格式怎么做逐字,以及它们的短板

主要有三类带"子行级"计时的格式:

**Enhanced LRC(增强 LRC / A2 扩展)**。在普通 LRC 行首时间戳之外,给每个词前面再加一个 `<mm:ss.xx>` 内联时间戳:`[00:12.50]<00:12.50>When <00:12.90>the <00:13.20>night <00:13.60>has`。酷狗、QQ音乐、网易云、AIMP 等播放器都支持它。短板有两个:它只有**起始时间没有时长**(靠下一个时间戳隐式结束),而且它是**按词/字**切分,没有任何振假名或第二语言的概念。

**TTML**。XML 结构,能做到词级甚至音节级的时间戳,每个 `<span>` 带自己的 begin/end,精确到毫秒,这就是 iOS 16 起 Apple Music"逐字"模式的底层。它的优势是可以包含多语言层,支持双语显示——这点对你有用。短板是它本身没有"ruby/振假名"的标准语义(你得自己塞进 span 的属性里),而且 XML 解析比 JSON 啰嗦。

**ASS(SubStation Alpha)**。动漫卡拉OK字幕的老牌格式,用 `\k`/`\kf` 标签给每个音节标时长,`\kf` 还能做经典的"颜色扫过"填充效果。词级卡拉OK计时早年基本是动漫字幕组的专属,工具是 Aegisub。Aegisub 确实**原生支持振假名卡拉OK**(给汉字和读音分别打 `\k`)。短板:ASS 是为字幕渲染设计的,塞进 HTML 渲染器里不顺手,而且双语翻译轨没有干净的表达方式。

> 顺带一提:网易云的 YRC、QQ 的 QRC、酷狗的 KRC 都是逐字格式,如果你能拿到日文歌的这些数据,可以当**数据来源**抓取,但它们同样只有基础文本的逐字时间,没有假名。

所以:**Enhanced LRC 适合做你的"导入源 / 最简版";真要逐假名 + 双语,自己设计 JSON 最省事。**

## 二、核心概念:计时单元 + 两种高亮模式

想清楚两件事,后面全是体力活。

**第一,计时单元(unit)选什么。** 关键洞察:**日语演唱对齐的是「拍(mora)」,不是显示的字形。** 一个「桜(さくら)」唱的时候是 3 拍(さ・く・ら),所以**时间数据要挂在读音的每个 mora 上**,而不是挂在汉字字形上。注意 mora ≠ 假名:

- 「きょ」「しゃ」这种拗音是**一个 mora 但两个假名**(基础假名 + 小写ゃゅょ)
- 「っ」(促音)、「ー」(长音)、「ん」(拨音)通常各算一拍

你的 SudachiPy/fugashi 管线给的是读音串,再加一步**把读音切成 mora**(规则:遇到小写ゃゅょぁぃぅぇぉ 就和前一个假名合并成一拍,其余每个假名/っ/ー/ん各一拍)。这一步你以前做振假名对齐时应该很熟。

中文翻译则简单——每个汉字就是一个 unit。但要注意:**翻译不是被"唱"出来的**,它没有天然的逐字时间。所以中文译文通常的处理是整行随当前行点亮(或淡入),而不是逐字扫;如果非要逐字,就按日文行的总时长在汉字数上均分。我建议中文整行高亮,只让日文逐假名,视觉上更干净。

**第二,高亮模式选哪种。**

- **离散跳变**:每个 unit 只存起始时间,当前时刻 t 把所有 `start <= t` 的 unit 标成"已唱"。实现最简单,但一格一格地跳,廉价感重。
- **扫色填充(推荐)**:每个 unit 存 `start + end`,在这段时间内让颜色从左到右"扫"过这个字。这就是 ASS 的 `\kf` 和 Apple 的效果。Apple 的做法是把绝大部分"智能"离线烘焙进时间戳,运行时播放引擎只需要知道当前时间,逐字进度动画用一个归一化的进度遮罩(clip-path)实现。这正是你该抄的思路。

## 三、推荐的文件格式设计

一个分层的 JSON,既能退化成"只逐字",也能升级到"逐假名扫色":

```json
{
  "meta": { "title": "...", "artist": "...", "offset": 0 },
  "lines": [
    {
      "start": 12340,
      "end": 15670,
      "jp": [
        {
          "base": "今日",
          "ruby": "きょう",
          "units": [
            { "k": "きょ", "s": 12340, "e": 12700 },
            { "k": "う",   "s": 12700, "e": 12950 }
          ]
        },
        {
          "base": "は",
          "ruby": null,
          "units": [ { "k": "は", "s": 12950, "e": 13200 } ]
        }
      ],
      "zh": "今天"
    }
  ]
}
```

设计要点:

- `jp` 是一个 **segment 列表**,每个 segment = 一个显示块(汉字或假名)+ 它的读音 + 读音切成的 mora `units`。`ruby` 为 `null` 表示本身就是假名、不显示振假名。

- 高亮逻辑全部由 

  ```
  units
  ```

   的时间驱动:

  - **振假名(rt)逐假名点亮**:rt 里的每个 mora 直接对应一个 unit,挨个扫。
  - **汉字基础字形按比例扫**:「桜」整个字形的填充比例 = 已唱 mora 数 / 该 segment 总 mora 数,さ→1/3、く→2/3、ら→3/3。这样汉字和它上方的假名是同步扫过的,就是动漫卡拉OK的观感。

- `zh` 用纯字符串,整行高亮即可(如前所述,翻译不参与逐字)。哪天想让中文也逐字,把它换成和 `jp` 一样的结构即可,格式向前兼容。

如果你只想先做"逐字不逐假名"的简化版,把 `units` 退化成一个 segment 一个时间区间就行,数据生产成本低很多。

## 四、HTML/CSS 怎么把扫色做出来

**别用 CSS `@keyframes` 按时长做动画**——因为音频会暂停、拖动进度、和系统时钟漂移,关键帧动画对不上。正确做法:**每一帧根据"当前播放时间"现算填充比例,写进一个 CSS 变量。**

扫色用 `background-clip: text` + 渐变,或者两层文字叠加 + `clip-path` 裁切。前者更轻:

```css
.unit {
  /* 未唱用灰,已唱/正在扫用高亮色,边界由 --p (0~1) 控制 */
  background: linear-gradient(90deg,
    #4fc3f7 0%, #4fc3f7 calc(var(--p) * 100%),
    #888    calc(var(--p) * 100%), #888 100%);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
```

驱动它的循环建议这样分工,避免 Qt↔JS 桥被刷爆:

- **Python 侧**只需每 ~200ms 把"校准后的当前时间 + 是否播放中"发给页面(上一轮说的 SMTC 插值)。
- **JS 侧**自己跑 `requestAnimationFrame`,在两次校准之间用本地时钟内插,60fps 地算每个正在唱的 unit 的 `--p = (now - s) / (e - s)`,直接写 style。

这样既精确同步,又有丝滑的 60 帧扫色,而且暂停/拖动时只要 Python 把新时间一推,JS 立刻跟上。

## 生产这些文件的现实路径

逐假名的时间戳是整个工程里**最贵**的资产。手工对时一首 4 分钟的歌可能要几十分钟。务实的流水线:

1. 拿到日文歌词 + 你的 fugashi/SudachiPy 加振假名、切 mora;
2. 行级时间用 Enhanced LRC 或强制对齐(forced alignment,如 Montreal Forced Aligner)先粗对到行/词;
3. mora 级时间在词级时间内按 mora 数初分,再用波形微调(Aegisub 的卡拉OK定时模式就是干这个的);
4. 导出成上面那个 JSON。

我的建议还是先把渲染管线跑通(用假时间数据驱动逐假名扫色),确认观感满意了,再回头啃"怎么高效产出精确时间戳"这个真正费时的环节。

要不要我先帮你写一个**最小渲染原型**:一段写死的双语 JSON(带 mora 时间)+ 上面那套 CSS/JS,用一个模拟的播放时钟驱动,让你直接看到「汉字按比例扫 + 振假名逐假名点亮 + 中文整行高亮」长什么样?你觉得效果对了我们再往里接 SMTC 和文件生产。