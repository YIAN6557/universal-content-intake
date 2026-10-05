# 首次配置指南

在项目目录里运行：

```bash
bin/uci-setup
```

向导会列出下面 14 个步骤，标出哪些已完成（✓），并给出下一步的具体操作。每做完一步就重新运行 `bin/uci-setup`，它会自动核对。进度保存在 `~/.config/universal-content-intake/`，中断后可以接着做。

- **【Agent】**：命令行就能完成，你自己、向导或编程 Agent（如 Claude Code）都可以执行。
- **【本人】**：必须你亲自操作，比如登录、同意授权、粘贴密钥、系统弹窗。

`bin/uci-setup status --json` 会输出机器可读的进度，给 Agent 用。

---

## 第 1 步：本机环境【Agent，Xcode 工具需本人确认】

```bash
bin/uci-setup doctor
```

逐项检查 macOS 15+、Python 3.11+、certifi、Xcode 命令行工具、ffmpeg/ffprobe、yt-dlp、deno、Node.js 和 clasp，缺什么就给出安装命令。常用的安装命令：

```bash
xcode-select --install                                  # 本人：在弹窗里点"安装"
brew install ffmpeg deno node                           # 有 Homebrew 时
python3 -m pip install --user -U certifi "yt-dlp[default]"
npm install -g @google/clasp                            # 提示权限不足时改用：npm install -g --prefix ~/.local @google/clasp
```

后台程序的 PATH 很短，所以系统会主动去这些位置找工具：Homebrew、`pip --user`、`~/bin`、`~/.deno/bin`。工具装在别处时，可以用环境变量 `UCI_YTDLP_PATH`、`UCI_FFMPEG_PATH`、`UCI_FFPROBE_PATH`、`UCI_DENO_PATH`、`UCI_CLASP_PATH` 指定路径。

## 第 2 步：模型与本地工具【Agent】

```bash
bin/uci-setup build-tools
```

- 下载 Whisper small 多语言模型（约 490 MB）和 Silero VAD 模型，并逐个校验 SHA-256。
- 从 v1.9.4 源码编译 whisper.cpp（需要 cmake，没有的话：`brew install cmake` 或 `python3 -m pip install --user cmake`）。
- 编译 Apple 翻译小工具和 PDF 信息小工具（需要 Xcode 命令行工具）。

可以用 `--skip-models`、`--skip-whisper`、`--skip-swift` 跳过其中某项。

## 第 3 步：Apple 翻译语言包【本人】

打开"系统设置 → 通用 → 语言与地区 → 翻译语言"，下载**英语**和**中文（简体）**。向导会调用翻译小工具核对是否就绪。

## 第 4 步：登录 clasp，开启 Apps Script API【本人】

1. 在终端运行 `clasp login`。浏览器会打开，选择用来存放数据的 Google 账号，点"允许"。
2. 打开 https://script.google.com/home/usersettings ，把"Google Apps Script API"切换为**开启**。

两件事都只需要做一次。

## 第 5 步：创建 Google 表格并推送云端代码【Agent】

```bash
bin/uci-setup cloud create
```

在你的 Google Drive 里新建表格"Universal Content Intake"，绑定一个 Apps Script 项目，然后推送 `cloud/apps-script/` 里的代码。`appsscript.json` 里已经声明了 YouTube 高级服务和 Web App 设置，**不需要申请 YouTube API Key**。

项目绑定信息写在 `.clasp.json`（已被 git 忽略）。以后云端代码有更新，运行 `bin/uci-setup cloud push`，再运行 `bin/uci-setup cloud deploy`。

## 第 6 步：部署 Queue API【Agent】

```bash
bin/uci-setup cloud deploy
```

把云端代码部署为 Web App，并把 `/exec` 地址写进 `~/.config/universal-content-intake/config.yaml`。以后再次部署会沿用同一个部署，地址不变。

Web App 设置为"以部署者身份执行、任何人可访问"。本机发来的每个请求都必须带上用共享密钥计算的签名，没有密钥的请求会被拒绝。有些 Google Workspace 组织会禁止"任何人"访问，这时请改用个人 Google 账号。

## 第 7 步：生成共享密钥【Agent】

```bash
bin/uci-setup secret create
```

在本机生成一个随机密钥，存进 macOS 钥匙串（服务名 `UCI Queue API HMAC`），不会显示在屏幕上。

## 第 8 步：粘贴密钥、运行 uciSetup 并授权【本人】

向导会给出编辑器地址（也可以运行 `bin/uci-setup cloud open` 查看）。

**A. 粘贴密钥**

1. 在终端运行 `bin/uci-setup secret copy`，密钥会被复制到剪贴板。
2. 在编辑器左侧点齿轮"项目设置"，拉到页面底部"脚本属性"，点"添加脚本属性"。
3. 属性填 `UCI_QUEUE_HMAC_SECRET`，值处粘贴，然后点"保存脚本属性"。

**B. 运行初始化**

1. 回到"编辑器"，打开 `setup.gs`，在顶部函数下拉框里选 `uciSetup`，点"运行"。
2. 点"审核权限"，选你的账号。
3. 如果出现"Google 尚未验证此应用"，点"高级"，再点"转至 Universal Content Intake（不安全）"，然后点"允许"。这是你自己的脚本，只在你的账号里运行。
4. 执行日志出现 `Universal Content Intake setup complete` 就是成功了。

`uciSetup` 会建好所有工作表和配置行，并安装每 10 分钟运行一次的定时器。它可以重复运行，不会重复建表。新安装默认**关闭每日选片**，到第 14 步验收通过后才会打开。

完成后重新运行 `bin/uci-setup`。如果核对失败，向导会说明原因：

- 签名校验没通过：密钥没粘贴，或粘贴的值与本机钥匙串里的不一致。
- 云端返回了网页：通常是还没完成授权。

## 第 9 步（可选）：Gemini 语义判断【本人申请 Key，Agent 开启】

1. 在 https://aistudio.google.com/apikey 创建 API Key（免费额度足够）。
2. 在编辑器"项目设置 → 脚本属性"里添加 `UCI_GEMINI_API_KEY`，值粘贴这个 Key。
3. 开启语义判断：

   ```bash
   bin/uci-setup config set semantic_judge_enabled=true semantic_gemini_model=gemini-3.5-flash-lite
   ```

不需要的话，运行 `bin/uci-setup skip gemini`，只用规则过滤。

如果想让大模型写发布标题和文案，把 Key 存进本机钥匙串（自己在终端运行，系统会提示你输入 Key）：

```bash
security add-generic-password -s "UCI Gemini API" -a api-key -w
```

也可以改用 Anthropic Key，服务名是 `UCI Anthropic API`，并且需要先安装 `anthropic` 包。两个都没有时，发布文案按规则生成。

## 第 10 步：第一批作者白名单【本人提供名单，Agent 解析写入】

```bash
bin/uci-setup creators resolve @handle1 https://www.youtube.com/@handle2 UCxxxxxxxxxxxxxxxxxxxxxx
```

向导会解析每个频道，抽查最近 20 条视频，列出这些信息：订阅数、中位时长、不超过时长上限的视频占比、每周更新条数，以及是否适合监控的建议。

- 不想要的频道：`bin/uci-setup creators drop @handle`
- 确认后写入云端：`bin/uci-setup creators apply`
- 以后管理名单：`bin/uci-setup creators list`、`enable <频道ID>`、`disable <频道ID>`

新频道写入后，云端会在 10–20 分钟内为它建立播放量基线。

## 第 11 步：内容方向与监控时间【本人决定，Agent 写入】

先查看当前设置：

```bash
bin/uci-setup config show
```

常改的项：

| 键 | 默认 | 含义 |
|---|---|---|
| `production_timezone` | Asia/Shanghai | 所有时间按这个时区计算 |
| `discovery_window_start` / `discovery_window_end` | 00:00 / 08:00 | 只收这个时段内发布的视频 |
| `final_sweep_time` | 08:10 | 补扫时间，不早于时段结束 |
| `daily_selection_time` | 10:00 | 选片时间，至少比时段结束晚 120 分钟（等 T+120 数据） |
| `rank2_start_cutoff` | 12:00 | 第 2 条必须在此之前开始处理，晚于选片时间 |
| `daily_selection_max` | 2 | 每天最多选几条（0–2） |
| `content_max_duration_minutes` | 20 | 超过这个时长直接排除 |
| `content_title_filters` | 空 | 标题过滤规则，可选 PODCAST、KEYNOTE、QA、LIVESTREAM、REVIEW、FINANCE、TUTORIAL、GAMING、NEWS_ROUNDUP、AD，或 ALL |
| `cold_start_checkpoint_30/60/120_ratio` | 见表格 | 新视频在 T+30/60/120 的播放量达到作者基线中位数的这个比例算 HOT |

修改设置：

```bash
bin/uci-setup config set discovery_window_start=06:00 discovery_window_end=12:00 final_sweep_time=12:10 daily_selection_time=14:00 rank2_start_cutoff=16:00
bin/uci-setup config set content_title_filters=PODCAST,LIVESTREAM,AD content_max_duration_minutes=15
bin/uci-setup config brief --file 内容方向.txt    # 用大白话写想要什么、不要什么（Gemini 语义判断用）
bin/uci-setup config set delivery_root="~/Movies/成片"   # 本机交付文件夹
```

不合理的时间组合会被云端拒绝，并说明原因。确认后记录：

```bash
bin/uci-setup confirm preferences
```

## 第 12 步：安装本机后台程序【Agent】

```bash
bin/uci-setup launchagent install
```

安装两个 LaunchAgent，都使用当前运行向导的 Python：

- `local.universal-content-intake.worker`：常驻的处理程序。
- `local.universal-content-intake.ytdlp-update`：每周自动更新 yt-dlp。

日志在 `~/Library/Logs/Universal Content Intake/`。卸载：`bin/uci-setup launchagent uninstall`。

## 第 13 步：macOS 权限【本人】

- 后台程序第一次写入"下载""桌面""文稿"里的文件夹时，macOS 会询问是否允许 Python 访问，点"允许"。
- 在"系统设置 → 通知"里允许"脚本编辑器"发送通知（用于完成和失败提醒）。

确认后记录：

```bash
bin/uci-setup confirm macos-permissions
```

## 第 14 步：验收【Agent】

```bash
bin/uci-setup verify
```

验收依次检查：

1. 本机能连上云端，签名请求通过。
2. 表格结构完整，定时触发器恰好 1 个。
3. 云端状态接口正常。
4. 白名单频道的基线建立情况。
5. 端到端试跑：下载一条 19 秒的公开视频，加字幕、翻译、烧录，交付到 `交付文件夹/uci-setup 试跑/`。看过后可以删掉这个文件夹；不想试跑可以加 `--no-smoke`。

全部通过后，向导会开启每日选片，并显示每天的时间表和 YouTube 配额估算。

之后日常查看运行情况：

```bash
bin/uci-status
```
