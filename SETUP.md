# 配置指南

这套系统分两层：

- **下载功能（主体）**：`bin/uci <链接>`，给一个链接就能下载视频、文档、图片、文章、网页。
- **自动监控（配套功能，可选）**：指定一批 YouTube 作者，系统自动发现起量的新视频，下载、翻译并压制中文字幕。

## 第一次运行时的两个问题

第一次运行任何 `bin/uci` 命令时，系统会先问两个问题：

1. **是否启用自动监控？** 选"启用"，向导会带你一步步完成下面第三部分的配置；选"不启用"，这套系统就是一个下载工具。
2. **你自己下载视频时，是否自动翻译成简体中文并压制字幕？** 三个选项：启用、不启用、每次询问。这个问题和第一个无关，只影响你给链接下载的视频；自动监控选出的视频始终会加中文字幕。

以后随时可以修改：

```bash
bin/uci settings                                          # 查看当前选择
bin/uci settings --monitoring on|off --subtitles on|off|ask
```

关闭自动监控会停掉本机后台程序，并暂停云端（不再发现、记录和选片，不消耗 YouTube 配额），作者名单和设置都保留；重新开启时一条命令恢复。

如果是 Claude Code 这类 Agent 在帮你操作，它没法在终端里弹出提问，会先问你，再用 `bin/uci settings --monitoring … --subtitles …` 记下你的选择。

## 配置向导

```bash
bin/uci setup
```

向导只列出你的选择需要的步骤，标出哪些已完成（✓），并给出下一步的具体操作。每做完一步就重新运行 `bin/uci setup`，它会自动核对。进度保存在 `~/.config/universal-content-intake/`，中断后可以接着做。

- **【Agent】**：命令行就能完成，你自己、向导或编程 Agent（如 Claude Code）都可以执行。
- **【本人】**：必须你亲自操作，比如登录、同意授权、粘贴密钥、系统弹窗。

`bin/uci setup status --json` 会输出机器可读的进度，给 Agent 用。

| 你的选择 | 需要完成的部分 |
|---|---|
| 不启用自动监控，翻译压制选"不启用" | 一 |
| 不启用自动监控，翻译压制选"启用"或"每次询问" | 一、二 |
| 启用自动监控（无论翻译压制怎么选） | 一、二、三 |

---

# 一、下载功能（所有人都需要）

## 本机环境【Agent】

```bash
bin/uci setup doctor
```

逐项检查 macOS 15+（或 Windows 10/11）、Python 3.11+、certifi、ffmpeg/ffprobe、yt-dlp、deno；在 Mac 上启用翻译压制时还检查 Xcode 命令行工具，启用自动监控时还检查 Node.js 和 clasp。用不到的项目会标注"开启某功能时才需要"。下载图片、整页保存网页、云盘链接还分别用到 gallery-dl、SingleFile 加 Chrome、rclone，缺什么都会给出安装命令。常用的安装命令：

Mac：

```bash
xcode-select --install                                  # 翻译压制需要；本人在弹窗里点"安装"
brew install ffmpeg deno node                           # 有 Homebrew 时
python3 -m pip install --user -U certifi "yt-dlp[default]"
npm install -g @google/clasp                            # 自动监控需要；提示权限不足时改用：npm install -g --prefix ~/.local @google/clasp
```

Windows（`winget` 是 Windows 10/11 自带的安装工具；装完后关掉终端重新打开，新装的命令才能用）：

```bat
winget install --id Gyan.FFmpeg -e
winget install --id DenoLand.Deno -e
winget install --id Google.Chrome -e                    :: 整页保存网页用
winget install --id OpenJS.NodeJS.LTS -e                :: 整页保存网页和自动监控用
py -m pip install --user -U certifi trafilatura "yt-dlp[default]" gallery-dl
npm install -g @google/clasp                            :: 自动监控需要
```

后台程序的 PATH 很短，所以系统会主动去这些位置找工具：Homebrew、winget、Scoop、`pip --user`、npm 全局目录、`~/bin`、`~/.deno/bin`。工具装在别处时，可以用环境变量 `UCI_YTDLP_PATH`、`UCI_FFMPEG_PATH`、`UCI_FFPROBE_PATH`、`UCI_DENO_PATH`、`UCI_CLASP_PATH` 指定路径。

## 下载引擎的定期维护（不启用自动监控时）【Agent】

```bash
bin/uci setup launchagent install
```

装两个后台定时任务（不装自动监控的处理程序）。Mac 上是 LaunchAgent，Windows 上是“任务计划程序”里“Universal Content Intake”文件夹下的 `ytdlp-update` 和 `engine-check`：

- `local.universal-content-intake.ytdlp-update`：每周自动更新 yt-dlp，更新后真实试一次，失败就退回旧版本。
- `local.universal-content-intake.engine-check`：每两周检查 gallery-dl、gdown、trafilatura、SingleFile、rclone、aria2、deno 和 whisper.cpp 在 GitHub 上有没有新版本。有的话发系统通知，**只提醒，不自动安装**；`bin/uci engines` 会给出每个引擎的一条更新命令。

想马上检查一次：`bin/uci engines`，会列出每个引擎的本机版本、最新版本和更新命令。上一次检查的结果也会显示在 `bin/uci status` 里。

## 验收（不启用自动监控时）【Agent】

```bash
bin/uci setup verify
```

下载一个公开的测试 PDF；选了"启用"或"每次询问"翻译压制时，再下载一条 67 秒的 NASA 视频并压制中文字幕。结果放在"下载"文件夹里的 `uci-setup 试跑`，看过后可以删掉。不想试视频可以加 `--no-smoke`。

之后直接用：

```bash
bin/uci <链接>
bin/uci "下载这个链接里的PDF https://…"
bin/uci <视频链接> --zh        # 这次翻译压制（不管设置是什么）
bin/uci <视频链接> --no-zh     # 这次只下载原视频
```

# 二、翻译压制工具（翻译压制选"启用"或"每次询问"，或启用了自动监控时需要）

## 模型与本地工具【Agent】

```bash
bin/uci setup build-tools
```

- 下载 Whisper small 多语言模型（约 490 MB）和 Silero VAD 模型，并逐个校验 SHA-256。
- Mac：从 GitHub 上最新的正式版本编译 whisper.cpp（需要 cmake，没有的话：`brew install cmake` 或 `python3 -m pip install --user cmake`）。编译完先用 whisper.cpp 自带的测试录音自检，通过才安装；连不上 GitHub 或最新版本自检失败时，改用已验证过的版本。
- Windows：不用编译，直接下载 whisper.cpp 官方发布的 Windows 版（`whisper-bin-x64.zip`），同样先用测试录音自检，通过才安装。
- 以后 whisper.cpp 出了新版本（每两周的检查会提醒），升级只要一条命令：`bin/uci setup build-tools --skip-models --skip-swift --update-whisper`。新版本自检不通过就保留旧版本。
- Mac：编译 Apple 翻译小工具和 PDF 信息小工具（需要 Xcode 命令行工具）。

可以用 `--skip-models`、`--skip-whisper`、`--skip-swift` 跳过其中某项。

## 翻译：Apple 翻译语言包（Mac 默认）或在线模型（Windows 必需）【本人】

**Mac 用 Apple 翻译时**：打开"系统设置 → 通用 → 语言与地区 → 翻译语言"，下载**英语**和**中文（简体）**。向导会调用翻译小工具核对是否就绪。

**Windows，或 Mac 想用在线模型时**：选一个模型，按提示申请 API Key，再粘贴进来。

```bash
bin/uci setup translation use deepseek   # 默认推荐；想用通义千问就写 qwen
bin/uci setup translation key            # 本人粘贴 Key（屏幕上不显示）；先试翻一句，成功才保存
```

- **DeepSeek**：在 https://platform.deepseek.com 注册、充值（10 元能用很久），在"API keys"里创建 Key。翻一条 10 分钟的视频不到 1 毛钱。
- **通义千问（阿里云百炼）**：在 https://bailian.console.aliyun.com 登录（需要实名认证），开通"模型服务"，创建 API Key。新用户有免费额度。
- Key 存在钥匙串或 Windows 凭据管理器里，不写进任何文件。字幕文字会发送给所选的服务做翻译。
- 余额不足、Key 失效或模型改名时，视频会在翻译这一步停下并说明原因；处理好后重新运行即可。模型改名时可以加 `--model <新名字>`。

# 三、自动监控（启用时需要）

## 第 1 步：登录 clasp，开启 Apps Script API【本人】

1. 在终端运行 `clasp login`。浏览器会打开，选择用来存放数据的 Google 账号，点"允许"。
2. 打开 https://script.google.com/home/usersettings ，把"Google Apps Script API"切换为**开启**。

两件事都只需要做一次。

## 第 2 步：创建 Google 表格并推送云端代码【Agent】

```bash
bin/uci setup cloud create
```

在你的 Google Drive 里新建表格"Universal Content Intake"，绑定一个 Apps Script 项目，然后推送 `cloud/apps-script/` 里的代码。`appsscript.json` 里已经声明了 YouTube 高级服务和 Web App 设置，**不需要申请 YouTube API Key**。

项目绑定信息写在 `.clasp.json`（已被 git 忽略）。以后云端代码有更新，运行 `bin/uci setup cloud push`，再运行 `bin/uci setup cloud deploy`。

## 第 3 步：部署 Queue API【Agent】

```bash
bin/uci setup cloud deploy
```

把云端代码部署为 Web App，并把 `/exec` 地址写进 `~/.config/universal-content-intake/config.yaml`。以后再次部署会沿用同一个部署，地址不变。

Web App 设置为"以部署者身份执行、任何人可访问"。本机发来的每个请求都必须带上用共享密钥计算的签名，没有密钥的请求会被拒绝。有些 Google Workspace 组织会禁止"任何人"访问，这时请改用个人 Google 账号。

## 第 4 步：生成共享密钥【Agent】

```bash
bin/uci setup secret create
```

在本机生成一个随机密钥，存进 macOS 钥匙串或 Windows 凭据管理器（名称 `UCI Queue API HMAC`），不会显示在屏幕上。

## 第 5 步：粘贴密钥、运行 uciSetup 并授权【本人】

运行 `bin/uci setup cloud open`，它会在浏览器里直接打开正确的编辑器。开始之前请注意两点：

> ⚠ **确认项目名**：页面左上角的项目名必须是 **Universal Content Intake**。如果你的账号里还有别的 Apps Script 项目，不要在别的项目里操作，否则密钥和初始化都不会生效。
>
> ⚠ **不要在代码区打字**：全程只用左侧菜单、顶部按钮和设置页的输入框，不要点进中间的代码区域。如果执行日志报 `ReferenceError`、`SyntaxError` 这类错误（例如"xx is not defined"），说明代码被误改了：运行 `bin/uci setup cloud push` 恢复原样，再点一次"运行"。

**A. 粘贴密钥**

1. 在终端运行 `bin/uci setup secret copy`，密钥会被复制到剪贴板。
2. 在编辑器左侧点齿轮"项目设置"，拉到页面底部"脚本属性"，点"添加脚本属性"。
3. 属性填 `UCI_QUEUE_HMAC_SECRET`，值处粘贴，然后点"保存脚本属性"。

**B. 运行初始化**

1. 回到"编辑器"，打开 `setup.gs`，在顶部函数下拉框里选 `uciSetup`，点"运行"。
2. 点"审核权限"，选你的账号。
3. 如果出现"Google 尚未验证此应用"，点"高级"，再点"转至 Universal Content Intake（不安全）"，然后点"允许"。这是你自己的脚本，只在你的账号里运行。
4. 执行日志出现 `Universal Content Intake setup complete` 就是成功了。

`uciSetup` 会建好所有工作表和配置行，并安装每 10 分钟运行一次的定时器。它可以重复运行，不会重复建表。新安装默认**关闭每日选片**，到本部分最后一步（第 11 步）验收通过后才会打开。

完成后重新运行 `bin/uci setup`。如果核对失败，向导会说明原因：

- 签名校验没通过：密钥没粘贴，或粘贴的值与本机钥匙串（Windows 凭据管理器）里的不一致。
- 云端返回了网页：通常是还没完成授权。

## 第 6 步（可选，推荐）：Gemini 语义判断与发布文案【本人申请 Key，Agent 开启】

**Gemini Key 免费，约 2 分钟就能申请好**，不需要绑定付款方式，免费额度对这套系统足够。Gemini 在 Google 的服务器上调用，不经过你本机的网络。它用来做两件事：

- **语义判断**：按你写的内容方向，判断每条起量的视频是否合适；
- **发布文案**：为成片撰写中文标题、文案和话题标签。

申请和接入：

1. 打开 https://aistudio.google.com/apikey ，用同一个 Google 账号登录；第一次打开要先同意服务条款。
2. 点"Create API key"（创建 API 密钥），项目选默认的或新建一个，复制生成的 Key（以 `AIza` 开头）。
3. 运行 `bin/uci setup cloud open`，在同一个 **Universal Content Intake** 项目的"项目设置 → 脚本属性"里添加 `UCI_GEMINI_API_KEY`，值粘贴这个 Key，保存。
4. 开启语义判断：

   ```bash
   bin/uci setup config set semantic_judge_enabled=true semantic_gemini_model=gemini-3.5-flash-lite
   ```

5. （可选）让本机也用它写发布文案。在终端运行下面的命令，按提示粘贴 Key：

   ```bash
   security add-generic-password -s "UCI Gemini API" -a api-key -w
   ```

   也可以改用 Anthropic Key：服务名是 `UCI Anthropic API`，需要先安装 `anthropic` 包。

**不想用也可以跳过**（`bin/uci setup skip gemini`），但要清楚跳过的代价：

- 选片只剩时长、标题规则和播放热度，你写的内容方向不起作用，选出来的视频会更杂；
- 发布标题只是原标题的直译，文案从字幕里摘句子，质量明显差一截。

以后随时可以按上面的步骤补上。

## 第 7 步：第一批作者白名单【本人提供名单，Agent 解析写入】

```bash
bin/uci setup creators resolve @handle1 https://www.youtube.com/@handle2 UCxxxxxxxxxxxxxxxxxxxxxx
```

向导会解析每个频道，抽查最近 20 条视频，列出这些信息：订阅数、中位时长、不超过时长上限的视频占比、每周更新条数，以及是否适合监控的建议。

- 不想要的频道：`bin/uci setup creators drop @handle`
- 确认后写入云端：`bin/uci setup creators apply`
- 以后管理名单：`bin/uci setup creators list`、`enable <频道ID>`、`disable <频道ID>`

新频道写入后，云端会在 10–20 分钟内为它建立播放量基线。

## 第 8 步：内容方向与监控时间【本人决定，Agent 写入】

先查看当前设置：

```bash
bin/uci setup config show
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
bin/uci setup config set discovery_window_start=06:00 discovery_window_end=12:00 final_sweep_time=12:10 daily_selection_time=14:00 rank2_start_cutoff=16:00
bin/uci setup config set content_title_filters=PODCAST,LIVESTREAM,AD content_max_duration_minutes=15
bin/uci setup config brief --file 内容方向.txt    # 用大白话写想要什么、不要什么（Gemini 语义判断用）
bin/uci setup config set delivery_root="~/Movies/成片"   # 本机交付文件夹
```

不合理的时间组合会被云端拒绝，并说明原因。确认后记录：

```bash
bin/uci setup confirm preferences
```

## 第 9 步：安装本机后台程序【Agent】

```bash
bin/uci setup launchagent install
```

安装三个后台程序，都使用当前运行向导的 Python。Mac 上是 LaunchAgent；Windows 上是“任务计划程序”里“Universal Content Intake”文件夹下的 `worker`、`ytdlp-update`、`engine-check`，处理程序在登录 Windows 时启动，出错退出后 30 秒自动重启：

- `local.universal-content-intake.worker`：常驻的处理程序。
- `local.universal-content-intake.ytdlp-update`：每周自动更新 yt-dlp。
- `local.universal-content-intake.engine-check`：每两周检查其他下载引擎有没有新版本，只发通知，不安装。

关闭自动监控时只停掉处理程序，后两个定时任务保留。

日志在 `~/Library/Logs/Universal Content Intake/`（Windows：`%LOCALAPPDATA%\Universal Content Intake\Logs\`）。卸载：`bin/uci setup launchagent uninstall`，会先停掉正在运行的处理程序和它启动的下载。

## 第 10 步：macOS 权限【本人】（只有 Mac 需要）

- 后台程序第一次写入"下载""桌面""文稿"里的文件夹时，macOS 会询问是否允许 Python 访问，点"允许"。
- 在"系统设置 → 通知"里允许"脚本编辑器"发送通知（用于完成和失败提醒）。

确认后记录：

```bash
bin/uci setup confirm macos-permissions
```

## 第 11 步：验收【Agent】

```bash
bin/uci setup verify
```

验收依次检查：

1. 本机能连上云端，签名请求通过。
2. 表格结构完整，定时触发器恰好 1 个。
3. 云端状态接口正常。
4. 白名单频道的基线建立情况。
5. 端到端试跑：下载一条 67 秒的 NASA 公开视频，加字幕、翻译、烧录，交付到 `交付文件夹/uci-setup 试跑/`。看过后可以删掉这个文件夹；不想试跑可以加 `--no-smoke`。

全部通过后，向导会开启每日选片，并显示每天的时间表和 YouTube 配额估算。

之后日常查看运行情况：

```bash
bin/uci status
```
