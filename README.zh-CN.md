# Universal Content Intake

[English](README.md)

Universal Content Intake 是一套运行在 Mac 上的资料下载工具：**给它一个链接，就能把里面的视频、文档、图片、文章或整个网页下载下来**，并整理好放进"下载"文件夹。

在这个基础上，它还提供一项可选的配套功能：**自动监控**。指定一批 YouTube 作者后，系统会自动发现他们正在起量的新视频，自动下载、翻译并压制中文字幕，附上视频信息和发布文案。用不用这项功能，由你在第一次运行时决定。

## 快速开始

```bash
git clone https://github.com/YIAN6557/universal-content-intake.git
cd universal-content-intake
bin/uci https://example.com/report.pdf
```

第一次运行时，系统会先问两个问题：

1. **是否启用自动监控？** 选"启用"，向导会带你一步步完成配置；选"不启用"，这套系统就是一个下载工具，以后随时可以再开启。
2. **你自己下载视频时，是否自动翻译成简体中文并压制字幕？** 可选"启用"、"不启用"或"每次询问"。这个问题和第一个无关；自动监控选出的视频始终会加中文字幕。

然后它会列出你的选择需要的准备步骤（例如检查 ffmpeg、yt-dlp 是否装好），照着做就能用。以后想改，运行 `bin/uci settings`。

## 下载：一个链接就够了

```bash
bin/uci https://example.com/some-page                          # 只给链接，系统自己判断类型
bin/uci "帮我下载这个链接里的PDF https://example.com/report"     # 说明了要什么，就直接下载
bin/uci "下载这个视频 https://www.youtube.com/watch?v=…"
```

- **说了要什么，就直接下载**：例如"下载这个链接里的 PDF""下载这个视频""保存这个网页"，系统按你说的类型选择下载方式，不做检测。也可以用 `--type video|pdf|document|image|images|article|webpage` 指定。
- **只给链接，就自动判断**：依次看是不是已知平台（YouTube、B 站等视频网站，Google 云端硬盘，图片网站），再看文件扩展名，最后看服务器返回的内容类型；普通网页会再判断是"文章"还是"整页保存"。
- **视频**：支持 yt-dlp 能处理的上千个网站，画质默认 1080p，可以用 `--quality 720p|1440p|4k|highest` 调整，不会放大低清源。
  - 翻译压制按你的设置进行：选"启用"就自动加中文字幕，选"不启用"就只下载原视频，选"每次询问"会在下载时问你。
  - 单次想例外，可以加 `--zh`（这次翻译压制）或 `--no-zh`（这次只下载原视频）。
- **文档**：PDF、Word、Excel、PPT、压缩包等直链文件，Google 云端硬盘分享链接，以及配置好的 rclone 云盘。
- **图片**：单张图片，或者整个相册、图集。
- **网页**：文章会提取正文，保存成 Markdown；整页保存会生成离线 HTML、Markdown 和 PDF 三份。

下载完成后，文件默认放在"下载"文件夹，用原标题命名，旁边附一份"信息"说明文件（来源、作者、原链接等）。多个文件（例如图集）会放进以标题命名的子文件夹。可以用 `--to 文件夹` 换位置。

### 下载引擎保持最新

各类下载能力来自 GitHub 上的开源下载引擎，配置向导会装两个后台定时任务（不管是否启用自动监控）：

- **yt-dlp 每周自动更新**：更新后真实试一次，新版本出问题就自动退回旧版本。
- **其他引擎每两周检查一次**：gallery-dl、gdown、trafilatura、SingleFile、rclone、aria2、deno，以及翻译压制用的 whisper.cpp。和各自 GitHub 上的最新正式版本对比，有新版本时发一条系统通知，**只提醒，不自动安装**。通知里的更新命令都是一条；whisper.cpp 的升级会先用测试录音自检，通过才替换。

```bash
bin/uci engines    # 现在就检查一次，列出每个引擎的版本和更新命令
bin/uci status     # 查看上一次检查的结果
```

## 配套功能：自动监控（可选）

```
作者白名单 ─► 定时发现新视频 ─► 跟踪早期播放数据 ─► 规则筛选 + 可选的 AI 内容判断 ─► 每日选片
                                                                                          │
交付文件夹 ◄─ 字幕压制 ◄─ 本机翻译 ◄─ 字幕获取 / 本机语音识别 ◄─ Mac 后台下载 ◄──────────┘
```

- **云端（Google Apps Script + 一张 Google 表格）**：
  - 在你设定的时段内每 10 分钟查看一次作者的新视频，并记录发布后 30/60/120 分钟的播放数据；
  - 和该作者自己的历史表现比较，找出正在起量的视频；
  - 按时长、标题规则过滤；接入 Gemini 后，还会按你写的内容方向判断是否合适；
  - 每天在设定时间选出 1–2 条，交给本机处理。
- **你的 Mac（后台程序）**：领取任务后下载视频，翻译并压制中文字幕，连同发布信息（标题、文案、话题标签、来源）一起放进交付文件夹。

开启和关闭：

```bash
bin/uci settings --monitoring on     # 开启；还没配置过的话，按提示运行 bin/uci setup 完成配置
bin/uci settings --monitoring off    # 关闭：停掉本机后台程序，并暂停云端；作者名单和设置都保留
bin/uci status                       # 查看自动监控的运行情况，以及下载引擎是否最新
```

配置需要一个 Google 账号，向导会一步步带你完成，大约 30–60 分钟。**不需要申请 YouTube API Key**：云端用你自己的 Google 授权访问 YouTube，每天免费配额 10,000 单位，常规用量一两千。推荐再申请一个免费的 Gemini API Key（约 2 分钟），用来按内容方向筛选视频、撰写发布文案。

## 运行要求

| 功能 | 需要 |
|---|---|
| 下载（所有人） | macOS 15 或更新（建议 Apple 芯片）、Python 3.11+、ffmpeg、yt-dlp、deno；图片、整页网页、云盘链接分别还要 gallery-dl、SingleFile 加 Chrome、rclone |
| 翻译压制（选"启用"或"每次询问"时） | Xcode 命令行工具；配置向导会下载约 490 MB 的 Whisper 语音识别模型、编译翻译小工具；Apple 翻译语言包（英→简中） |
| 自动监控（启用时） | 以上全部，加一个 Google 账号和 Node.js（用来安装 clasp）；推荐一个免费的 Gemini API Key |

缺什么，`bin/uci setup doctor` 会列出来并给出安装命令；用不到的功能所需的工具会标注出来，不用装。

## 配置与向导

```bash
bin/uci settings          # 查看或修改两个选择
bin/uci setup             # 配置向导：只列出你的选择需要的步骤，并告诉你下一步做什么
bin/uci setup verify      # 验收：试下载一次，确认可用
```

向导的每一步都标明【Agent】可以代做，还是【本人】需要亲自完成（例如登录 Google、确认授权、粘贴密钥）。详细说明见 [SETUP.md](SETUP.md)。如果你用 Claude Code 等编程 Agent，让它阅读 [SKILL.md](SKILL.md)，它会先问你那两个问题，再陪你一步步完成配置。

原来的 `bin/uci-get`、`bin/uci-setup`、`bin/uci-status` 仍然可用，分别等同于 `bin/uci get`、`bin/uci setup`、`bin/uci status`。

## 配置保存在哪里

| 位置 | 内容 |
|---|---|
| `~/.config/universal-content-intake/config.yaml` | 两个选择（`features.monitoring`、`features.video_subtitles`），以及下载文件夹、交付文件夹、工作文件夹、画质（默认 1080p）、是否允许使用 Chrome 登录状态（默认关）、云端接口地址 |
| Google 表格 → `Config`（自动监控；用 `bin/uci setup config set key=value` 修改） | 时区、发现时段、补扫和选片时间、每天数量、第 2 条最晚开始处理的时间、播放数据门槛、时长上限、标题过滤规则、Gemini 判断和内容方向 |
| Google 表格 → `Creators`（自动监控；用 `bin/uci setup creators …` 修改） | 作者白名单 |
| macOS 钥匙串 | 本机与云端的共享密钥，可选的 Gemini / Anthropic Key（写发布文案用） |
| Apps Script → 脚本属性 | 同一个共享密钥，可选的 `UCI_GEMINI_API_KEY` |

你的作者名单和内容方向只保存在你自己的 Google 表格和本机配置里，不会进入本仓库。

## 隐私与安全

- 下载默认匿名进行。只有你开启 `video.allow_browser_cookies`，遇到需要登录才能观看的视频时，才会使用 Chrome 的登录状态。
- 语音识别和翻译都在本机完成。可选的 Gemini 判断会把视频标题、简介和统计数据发送给 Google；可选的发布文案生成还会发送翻译后的字幕片段。
- 自动监控时，本机与云端之间的每个请求都经过 HMAC-SHA256 签名，密钥只保存在你的钥匙串和脚本属性中，向导不会显示它。

## 合规使用

请在拥有相应权利或授权的前提下下载、处理和发布内容，并遵守各平台的服务条款以及当地法律法规。本软件按"现状"提供，作者不对具体使用方式承担责任。

## 开发

```bash
python3 -m unittest discover -s tests   # Python 测试
node --test tests/*.js                  # Apps Script 测试（在 Node 虚拟机里运行）
```

各阶段的命令行工具和模块分工见 [docs/REFERENCE.md](docs/REFERENCE.md)。

## 许可证

Universal Content Intake 采用双轨授权：

- **个人非商用**：可按照 [Universal Content Intake Personal Use License 1.0](LICENSE) 免费使用；
- **商业使用**：必须事先取得权利人的书面商业授权，并支付适用的许可费用，申请流程见 [Commercial Licensing](COMMERCIAL_LICENSING.md)，联系邮箱为 `ohhhhhmya@gmail.com`。

以下都属于商业使用：

- 企业、机构、工作室、团队、雇员、承包商或客户项目使用；
- 收费服务、商业内容、营销、广告、品牌传播；
- 产品或工作流集成。

内部使用、试用、尚未盈利或没有单独向客户收费，不因此变成个人非商用。

第三方组件不因本仓库的许可声明而获得重新授权，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

维护者：[@YIAN6557](https://github.com/YIAN6557)
