# Universal Content Intake

[English](README.md)

Universal Content Intake 盯住一批你指定的 YouTube 作者，挑出正在起量的新视频，把它们做成烧录了简体中文字幕的成片，并配一份中文发布信息（标题、文案、话题标签和全部原始信息）。最后的发布由你自己完成。

系统分两半：

- **云端（Google Apps Script + 一张 Google 表格）**：在你设定的发现时段里每 10 分钟读取作者的上传列表，在发布后 30/60/120 分钟记录播放量。播放表现超过该作者自身基线就标记为 HOT，再经过你的内容规则过滤（可选：用 Gemini 按你的内容方向做语义判断）。到了选片时间，把当天最好的 1–2 条放进队列。
- **你的 Mac（后台程序 LaunchAgent）**：领取队列任务，用 yt-dlp 下载（最高 1080p）。字幕优先用 YouTube 自带的，没有就用本地 Whisper 识别；再用 Apple 翻译在本机翻成中文，用 ffmpeg 烧录进视频。成片和发布信息一起放进你的交付文件夹。

## 运行要求

- macOS 15 或更新（需要 Apple 翻译、PDFKit、LaunchAgent），建议 Apple 芯片。
- Python 3.11+、Node.js（给 clasp 用）、Xcode 命令行工具。
- ffmpeg/ffprobe、yt-dlp、deno。
- 一个 Google 账号。**不需要申请 YouTube API Key**：云端通过 Apps Script 的 YouTube 高级服务、用你自己的 Google 授权访问接口，每天免费配额 10,000 单位，常规用量只有几千。
- 可选：Gemini API Key（语义判断）；在钥匙串里放 Anthropic 或 Gemini 的 Key，可以让大模型写发布标题和文案。

## 安装

```bash
git clone https://github.com/YIAN6557/universal-content-intake.git
cd universal-content-intake
bin/uci-setup
```

`bin/uci-setup` 是分步向导：每次运行都会检查哪些已经完成，并告诉你下一步做什么。每一步都标明是【Agent】可以代做，还是【本人】必须亲自做（比如登录 Google、同意授权、粘贴密钥）。照着做，直到它显示"系统可用"。每一步的详细说明见 [SETUP.md](SETUP.md)。如果你用 Claude Code 之类的编程 Agent，让它读 [SKILL.md](SKILL.md)，它就能带着你一步步装好。

日常使用：

```bash
bin/uci-status            # 云端状态、今天的批次、队列、本机后台程序
bin/uci-setup config show # 时间安排、上限、内容规则
```

## 配置在哪里

| 位置 | 内容 |
|---|---|
| Google 表格 → `Config`（用 `bin/uci-setup config set key=value` 修改） | 时区、发现时段、补扫、选片时间、每日上限、第 2 条截止时间、播放量/点赞门槛、`content_max_duration_minutes`（时长上限）、`content_title_filters`（标题过滤规则）、Gemini 语义判断和你的内容方向 |
| Google 表格 → `Creators`（用 `bin/uci-setup creators …` 修改） | 作者白名单 |
| `~/.config/universal-content-intake/config.yaml` | 本机设置：Queue API 地址、交付文件夹、工作文件夹、画质上限、是否允许读取 Chrome 登录状态（默认关） |
| macOS 钥匙串 | Queue API 共享密钥（`UCI Queue API HMAC`），可选的 `UCI Gemini API` / `UCI Anthropic API`（写发布文案用） |
| Apps Script → 脚本属性 | 同一个 Queue API 共享密钥，可选的 `UCI_GEMINI_API_KEY` |

你的作者名单和内容方向只保存在你自己的 Google 表格和 `~/.config` 里，不会进入本仓库。

## 隐私与安全

- Mac 与云端之间的每个请求都用 HMAC-SHA256 签名。密钥只存在你的钥匙串和脚本属性里，向导不会把它显示出来。
- 默认匿名下载。只有你打开 `video.allow_browser_cookies`，遇到需要登录的视频时才会读取 Chrome 的登录状态。
- 语音识别和翻译都在本机完成。可选的 Gemini 语义判断会把视频标题、简介和统计数据发给 Google；可选的发布文案生成还会把翻译后的字幕片段发给 Anthropic 或 Google。

## 合规使用

本工具会下载他人的视频并重新加字幕。**你需要自行确保**拥有下载、修改和再发布相关内容的权利或授权，并遵守 YouTube 服务条款和你所在地的法律。软件按"现状"提供，作者不对使用方式承担责任。

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
