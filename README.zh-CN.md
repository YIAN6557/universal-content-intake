# Universal Content Intake

[English](README.md)

Universal Content Intake 是一套运行在 Mac 上的内容采集与整理工具，可以分两层使用：

1. **自动化流程**：指定一批 YouTube 作者后，系统会自动监控、自动发现新发布的视频，自动下载，自动翻译并把简体中文字幕压制进画面，最后附上视频的完整信息和一份发布文案。
2. **单独的下载工具**：给它一个链接就能完成下载，支持视频、PDF 和各类文档、图片和图集、文章、整页网页。

两层可以一起用，也可以只用第二层。

## 第一层：自动化流程

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
- **你的 Mac（后台程序）**：
  - 领取任务后下载视频，画质**默认 1080p**，可以改成 720p、1440p、4K 或最高画质，不会放大低清源；
  - 字幕优先用视频自带的，没有就用本机 Whisper 识别，再用 Apple 翻译在本机译成简体中文，压制进画面；
  - 成片和一份发布信息一起放进交付文件夹。发布信息包括标题、文案、话题标签、作者、原链接、发布时间等。

## 第二层：下载工具

```bash
bin/uci-get "帮我下载这个链接里的PDF https://example.com/report"
bin/uci-get "下载这个视频 https://www.youtube.com/watch?v=…"
bin/uci-get https://example.com/some-page        # 只给链接，系统自己判断类型
```

- **说明了要什么，就直接下载**：例如"下载这个链接里的 PDF""下载这个视频""保存这个网页"，系统按你说的类型选择下载方式，不做检测。也可以用 `--type video|pdf|document|image|images|article|webpage` 指定。
- **只给链接，就自动判断**：依次看是不是已知平台（YouTube、B 站等视频网站，Google 云端硬盘，图片网站），再看文件扩展名，最后看服务器返回的内容类型；普通网页会再判断是"文章"还是"整页保存"。
- **视频**：支持 yt-dlp 能处理的上千个网站，画质默认 1080p，可以用 `--quality` 调整；加 `--zh` 会同时翻译并压制中文字幕。
- **文档**：PDF、Word、Excel、PPT、压缩包等直链文件，Google 云端硬盘分享链接，以及配置好的 rclone 云盘。
- **图片**：单张图片，或者整个相册、图集。
- **网页**：文章会提取正文，保存成 Markdown；整页保存会生成离线 HTML、Markdown 和 PDF 三份。

下载完成后，文件默认放在"下载"文件夹，用原标题命名，旁边附一份"信息"说明文件。多个文件（例如图集）会放进以标题命名的子文件夹。可以用 `--to 文件夹` 换位置。

## 运行要求

- macOS 15 或更新，建议 Apple 芯片。
- Python 3.11+、Xcode 命令行工具、ffmpeg、yt-dlp、deno。
- **只用下载工具**：完成配置向导的前 3 步（本机环境、模型与工具、翻译语言包）就可以用，不需要 Google 账号。按内容类型可能还需要 gallery-dl（图片）、SingleFile 和 Chrome（整页保存）、rclone（云盘），`bin/uci-setup doctor` 会列出缺什么、怎么装。
- **使用自动化流程**：另外需要一个 Google 账号和 Node.js（用来安装 clasp）。不需要申请 YouTube API Key：云端用你自己的 Google 授权访问 YouTube，每天免费配额 10,000 单位，常规用量一两千。
- **推荐**：一个免费的 Gemini API Key，约 2 分钟就能申请。用来按内容方向筛选视频、撰写发布标题和文案；不配置也能运行，但选片会更杂，文案也更简单。

## 安装与配置

```bash
git clone https://github.com/YIAN6557/universal-content-intake.git
cd universal-content-intake
bin/uci-setup
```

`bin/uci-setup` 是分步配置向导，每次运行都会检查进度，并告诉你下一步做什么：

- 每一步都标明是【Agent】可以代做，还是【本人】需要亲自完成（例如登录 Google、确认授权、粘贴密钥）；
- 照着做，直到它显示"系统可用"；
- 详细说明见 [SETUP.md](SETUP.md)；
- 如果你用 Claude Code 等编程 Agent，让它阅读 [SKILL.md](SKILL.md)，它可以陪你一步步完成配置。

日常使用：

```bash
bin/uci-status            # 云端状态、当天批次、队列、本机后台程序
bin/uci-setup config show # 时间安排、每日数量、筛选规则、内容方向
bin/uci-get <链接>         # 单独下载
```

## 配置在哪里

| 位置 | 内容 |
|---|---|
| Google 表格 → `Config`（用 `bin/uci-setup config set key=value` 修改） | 时区、发现时段、补扫时间、选片时间、每天数量、第 2 条最晚开始处理的时间、播放数据门槛、时长上限、标题过滤规则、Gemini 判断和内容方向 |
| Google 表格 → `Creators`（用 `bin/uci-setup creators …` 修改） | 作者白名单 |
| `~/.config/universal-content-intake/config.yaml` | 本机设置：云端接口地址、交付文件夹、下载文件夹、工作文件夹、画质（默认 1080p）、是否允许使用 Chrome 登录状态（默认关） |
| macOS 钥匙串 | 本机与云端的共享密钥，可选的 Gemini / Anthropic Key（写发布文案用） |
| Apps Script → 脚本属性 | 同一个共享密钥，可选的 `UCI_GEMINI_API_KEY` |

你的作者名单和内容方向只保存在你自己的 Google 表格和本机配置里，不会进入本仓库。

## 隐私与安全

- 本机与云端之间的每个请求都经过 HMAC-SHA256 签名，密钥只保存在你的钥匙串和脚本属性中，向导不会显示它。
- 下载默认匿名进行。只有你开启 `video.allow_browser_cookies`，遇到需要登录才能观看的视频时，才会使用 Chrome 的登录状态。
- 语音识别和翻译都在本机完成。可选的 Gemini 判断会把视频标题、简介和统计数据发送给 Google；可选的发布文案生成还会发送翻译后的字幕片段。

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
