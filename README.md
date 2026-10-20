# netease-apple-daily

网易云音乐与 Apple Music 的个人双向同步工具：

- **日推 → Apple Music**：每天把网易云「每日推荐」生成为 Apple Music 资料库里的日期歌单，例如 `网易云日推-2026-10-16`。
- **Apple Music → 网易云**：把 Apple 的喜爱歌曲同步为网易云红心，把最近播放同步为网易云听歌打卡，帮助网易云更了解你的口味。

两边曲库对同一首歌的写法常常不同（日文、罗马字、英文译名、歌手别名）。可选的 AI 辅助匹配能处理这些差异，同时始终避免误选 Karaoke、伴奏、Live 等不同版本。

基于 [FlyRenxing/netease-apple-daily](https://github.com/FlyRenxing/netease-apple-daily) 继续开发，沿用 MIT 许可证。

> 仅供个人使用，请遵守网易云与 Apple 的服务条款。不下载任何音频。回写会修改网易云账号数据，首次使用请先用 `--dry-run` 预览。

## 目录

- [工作原理](#工作原理)
- [准备](#准备)
- [安装](#安装)
- [首次运行](#首次运行)
- [AI 辅助匹配](#ai-辅助匹配)
- [回写说明](#回写说明)
- [定时任务](#定时任务)
- [配置参考](#配置参考)
- [日常维护](#日常维护)
- [故障排查](#故障排查)
- [开发](#开发)

## 工作原理

```text
网易云日推 ──► 在 Apple 曲库搜索匹配 ──► Apple Music 资料库日期歌单
 (cookie)        (规则 + 可选 AI)             (media-user-token)

Apple 喜爱歌曲 ──► 在网易云搜索匹配 ──► 网易云红心
Apple 最近播放 ──►  (规则 + 可选 AI)  ──► 网易云听歌打卡
```

网易云的数据通过本机运行的 [api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)（Node.js）读取和写入，Apple Music 使用官方 [Apple Music API](https://developer.apple.com/documentation/applemusicapi)。所有凭据只保存在本机的私有文件中。

| 文件 | 作用 |
|------|------|
| `run.sh` | 日推 → Apple Music 入口 |
| `run_feedback.sh` | Apple Music → 网易云回写入口 |
| `login.sh` | 网易云扫码登录 |
| `daily_recommend.py` | 日推同步与 Apple 曲库匹配 |
| `apple_to_netease_feedback.py` | 红心与打卡回写 |
| `ai_matching.py` | AI 辅助匹配（OpenAI 兼容接口或 Anthropic） |
| `scripts/start-ncm-api.sh` | 后台启动 api-enhanced |
| `scripts/preview_matching.py` | 用历史清单只读预览 AI 匹配效果 |
| `scripts/cron_summary.py` / `feedback_summary.py` | 给机器人推送用的简短运行摘要 |

## 准备

- **系统**：Linux 或 macOS，需能长期运行（定时任务需要）。
- **Python 3.10+**：只用标准库，不需要 `pip install`。
- **Node.js**：运行 api-enhanced，建议使用当前的 LTS 版本。
- **命令行工具**：`curl`、`jq`；Linux 上还建议有 `flock`（防止任务重复运行，一般系统自带）。
- **账号**：有效的 Apple Music 订阅；一个网易云音乐账号。
- **可选**：一个大模型 API（OpenAI 兼容接口或 Anthropic），用于 AI 辅助匹配。

不需要 root 权限，下面的步骤都在普通用户目录下完成。

## 安装

### 1. 获取代码

```bash
git clone https://github.com/xtnntn/netease-apple-daily.git
cd netease-apple-daily
cp config.env.example config.env
chmod 600 config.env
```

后续命令都在项目目录中执行。`config.env`、`secrets/`、`data/` 和 `logs/` 已被 `.gitignore` 排除，不会提交到 Git。

### 2. 安装并启动网易云 API

把 api-enhanced 安装到单独的目录（示例为 `~/ncm-api`）：

```bash
mkdir -p ~/ncm-api
cd ~/ncm-api
npm init -y
npm install @neteasecloudmusicapienhanced/api@4.41.1
cd -
```

用项目自带的脚本在后台启动。它固定只监听 `127.0.0.1:3000`，并关闭解灰功能；服务已在运行时直接退出：

```bash
NCM_API_DIR=~/ncm-api ./scripts/start-ncm-api.sh
curl -s 'http://127.0.0.1:3000/banner?type=0' | head -c 80; echo
```

能看到一段 JSON 即表示启动成功。日志写在 `~/ncm-api/ncm-api.log`。

在 `config.env` 中配置启动命令，这样同步任务发现 API 没启动时会自动拉起：

```bash
NCM_API_START_CMD='NCM_API_DIR=/home/you/ncm-api /home/you/netease-apple-daily/scripts/start-ncm-api.sh'
```

请使用绝对路径。如果 Node 不在 cron 的默认 `PATH` 里（例如手动解压安装在 `~/.local/opt/node`），再加上 `NCM_NODE_BIN=/home/you/.local/opt/node/bin`。

> api-enhanced 的日志可能记录请求地址。本项目通过请求体传递 cookie，不会写进日志；旧版本留下的日志请自行清理。

### 3. 登录网易云

```bash
./login.sh qr
```

终端会生成二维码图片 `data/login-qr.png`（同时保存链接到 `data/login-qrurl.txt`），用网易云 App 扫码并确认。看到 `LOGIN_OK` 即完成，cookie 保存在 `data/cookie.txt`（权限 600）。

在服务器上不方便看图片时，可以分两步：

```bash
./login.sh qr-init        # 生成二维码，把 data/login-qr.png 下载到本地扫码
./login.sh qr-poll 90     # 扫码后轮询结果，最多 90 次
./login.sh status         # 随时检查登录状态
```

也可以从已登录的浏览器复制 cookie，通过标准输入写入（不会留在命令历史里）：

```bash
bash -c 'read -rs -p "NetEase cookie: " c; printf "\n"; printf "%s" "$c" | ./login.sh cookie -'
```

### 4. 配置 Apple Music

1. 在电脑浏览器打开 [music.apple.com](https://music.apple.com) 并登录。
2. 打开开发者工具（F12）→ Application（应用）→ Cookies → `https://music.apple.com`。
3. 复制 `media-user-token` 的值。

把它写入私有文件，并在 `config.env` 中指向它：

```bash
mkdir -p secrets && chmod 700 secrets
bash -c 'umask 077; read -rs -p "media-user-token: " t; printf "\n"; printf "media-user-token: \"%s\"\n" "$t" > secrets/apple-music-config.yaml'
```

```bash
# config.env
AM_CONFIG=./secrets/apple-music-config.yaml
STOREFRONT=cn
```

也可以直接在 `config.env` 中写 `MUSIC_USER_TOKEN=...`。Developer token 默认每次从 music.apple.com 网页自动获取，无需配置。

`media-user-token` 会不定期失效，失效后按上面的步骤重新复制即可。

## 首次运行

先手动跑一次日推：

```bash
./run.sh
```

成功后在 iPhone 或 Mac 的「音乐」App → 资料库 → 播放列表 → 文件夹「网易云每日推荐」中可以看到当天的歌单。每首歌的匹配结果保存在 `data/manifest-YYYY-MM-DD.json`，日志在 `logs/`。当天歌单已存在时会直接跳过，重复运行是安全的。

再预览回写，确认匹配无误：

```bash
./run_feedback.sh --dry-run --limit 5
```

`--dry-run` 只匹配不写入，也不修改状态文件。确认没问题后再真实运行：

```bash
./run_feedback.sh
```

## AI 辅助匹配

AI 是可选的，默认关闭。不启用时完全使用规则匹配。

### 规则与 AI 如何分工

| 情况 | 处理 |
|------|------|
| 歌名、歌手规范化后完全一致，版本相同，时长接近 | 直接使用规则结果，不调用 AI |
| 规则匹配到了，但写法不同或把握不高 | AI 审核：确认、改选其他候选或否决 |
| 规则没匹配上 | AI 从搜索结果中选择，必要时生成至多两条新搜索词补搜一次 |

几条始终生效的护栏：

- AI 只能选择曲库搜索结果中真实存在的歌曲，不能凭空指定。
- Karaoke、伴奏、Live、Remix 等版本不一致的候选，以及时长相差超过 15 秒的候选，不会交给 AI。
- AI 没有把握就放弃，不会退回低分候选。
- 每次运行的调用次数有上限，失败不自动重试。
- 只发送歌名、歌手、专辑、时长和 ISRC，不发送任何账号凭据。
- 模型给出的理由只记录在匹配清单中供人工检查，不写进日志，也不作为匹配依据。

### 启用

准备一个模型 API 的密钥，写入私有文件（不要放在命令参数或配置文件里）：

```bash
bash -c 'umask 077; read -rs -p "AI API key: " k; printf "\n"; printf "%s" "$k" > secrets/ai-api-key'
```

在 `config.env` 中配置：

```bash
AI_MATCHING_ENABLED=1
AI_MATCHING_PROVIDER=openai                 # OpenAI 兼容接口；Claude 用 anthropic
AI_MATCHING_BASE_URL=https://your-model-service.example/v1
AI_MATCHING_MODEL=your-model-id
AI_MATCHING_API_KEY_FILE=./secrets/ai-api-key
AI_MATCHING_TIMEOUT=60
AI_MATCHING_MAX_CALLS=60

FEEDBACK_AI_MATCHING=1                      # 红心也使用 AI 审核
```

- `AI_MATCHING_ENABLED=1` 让日推使用 AI。
- `FEEDBACK_AI_MATCHING=1` 让红心使用 AI 审核，需同时开启上一项。
- 听歌打卡始终只用规则匹配。

服务地址必须是 HTTPS（本机调试可用 `http://127.0.0.1`），请求不跟随重定向，也不使用系统代理。

### 启用前预览

可以用历史日推清单只读预览 AI 的效果，不会创建歌单或修改任何状态：

```bash
python3 scripts/preview_matching.py data/manifest-YYYY-MM-DD.json --output data/ai-preview.json
python3 scripts/preview_matching.py data/manifest-YYYY-MM-DD.json --unmatched-only --output data/ai-preview-missed.json
```

红心可以用 `FEEDBACK_AI_MATCHING=1 ./run_feedback.sh --dry-run --likes-only` 预览，结果在 `data/feedback-manifest-YYYY-MM-DD.json` 的 `like_items` 中，每首歌都标明了 `match_method`（`rules`、`ai_review` 或 `ai`）和 AI 结论。

AI 不可用时：日推中需要 AI 判断的歌曲保留为未匹配；红心中需要审核的歌曲留到下次运行，不会红心未经审核的结果。

## 回写说明

回写只向网易云添加数据，不会取消红心、删除或修改歌单；Apple Music 这边只读不写。

**喜爱歌曲 → 网易云红心**

- 读取 Apple 资料库的「喜爱歌曲」歌单，找到对应的网易云歌曲后红心。
- 网易云已红心或之前处理过的歌曲会跳过。每次最多红心 40 首，其余留到下次。
- 没匹配上的歌曲 7 天后重新搜索。
- 在 Apple 取消喜爱不会取消网易云红心。

**最近播放 → 网易云听歌打卡**

- 读取 Apple 最近播放的 50 首，只打卡和上次相比新出现的歌曲，每次最多 25 首。
- Apple 不提供播放时间和次数，所以同一首歌反复听只算一次，打卡时间是程序运行的时间，时长按 240 秒计（短于 4 分钟的按歌曲长度）。
- 首次运行只记录快照，不打卡，避免把历史播放一次性灌进网易云。

**失败与重试**

- 超过限额或明确失败的歌曲会留到下次处理。
- 网络中断等结果不确定的写入不会在本轮重复提交：红心会保留待处理记录，本轮遇到相同网易云歌曲 ID 的条目也会延后，其他歌曲继续同步；下次运行先对照网易云红心列表核对，再决定是否重试。打卡没有可靠的核对接口，会标记为 `unknown` 并让本次运行返回非零退出码。
- 单曲红心失败不会阻塞其他歌曲；部分失败仍返回非零退出码。认证失效或明确限流时停止后续写入。
- 状态保存在 `data/feedback-state.json`，写入前后都会原子保存。状态文件损坏时程序会报错停止，并保留原文件。

常用参数：

```bash
./run_feedback.sh --dry-run        # 只匹配不写入
./run_feedback.sh --likes-only     # 只同步红心
./run_feedback.sh --scrobble-only  # 只同步打卡
./run_feedback.sh --limit 10       # 本次每类最多写入 10 首
```

单独关闭某一项：在 `config.env` 中设置 `FEEDBACK_SYNC_LIKES=0` 或 `FEEDBACK_SYNC_SCROBBLE=0`。

## 定时任务

用 `crontab -e` 添加（时间为服务器本地时区）：

```cron
# 保持网易云 API 运行：开机启动，并每 10 分钟检查一次
@reboot NCM_API_DIR=$HOME/ncm-api $HOME/netease-apple-daily/scripts/start-ncm-api.sh
*/10 * * * * NCM_API_DIR=$HOME/ncm-api $HOME/netease-apple-daily/scripts/start-ncm-api.sh

# 每天 07:30 生成日推歌单
30 7 * * * $HOME/netease-apple-daily/run.sh

# 每天 20:00 回写红心和打卡
0 20 * * * $HOME/netease-apple-daily/run_feedback.sh
```

网易云日推每天早上 6 点左右更新，日推任务应安排在这之后。两个入口都会加锁防止重复运行，并在 `logs/cron.log` 记录开始、结束时间和退出码，失败时返回非零。

需要把运行结果推送到 Telegram 等机器人时，可以改用 `scripts/cron_summary.py` 和 `scripts/feedback_summary.py`，它们会运行同步并只输出一段简短摘要。Hermes Agent 的集成方法见 [`extras/hermes/`](extras/hermes/README.md)。

## 配置参考

所有配置都在 `config.env` 中，完整列表和说明见 [`config.env.example`](config.env.example)。相对路径以项目根目录为准；含空格的值需加引号，不要使用行尾注释、`export` 或 `$VAR` 展开。

| 变量 | 默认 | 说明 |
|------|------|------|
| `NCM_API_BASE` | `http://127.0.0.1:3000` | api-enhanced 地址 |
| `NCM_API_START_CMD` | 空 | API 不可用时执行的启动命令 |
| `COOKIE_FILE` | `./data/cookie.txt` | 网易云 cookie |
| `AM_CONFIG` / `MUSIC_USER_TOKEN` | — | Apple `media-user-token` |
| `STOREFRONT` | `cn` | Apple 商店区域 |
| `PLAYLIST_PREFIX` | `网易云日推` | 歌单名为 `{前缀}-{日期}` |
| `PLAYLIST_FOLDER_NAME` | `网易云每日推荐` | 歌单所在文件夹，不存在会自动创建；留空则放在根目录 |
| `SKIP_IF_EXISTS` | `1` | 当天歌单已存在则跳过 |
| `AM_MIN_INTERVAL` | `0.75` | Apple API 请求最小间隔（秒），遇到大量 429 时调大 |
| `AI_MATCHING_ENABLED` | `0` | 日推使用 AI 辅助匹配 |
| `FEEDBACK_AI_MATCHING` | `0` | 红心使用 AI 审核 |
| `FEEDBACK_SYNC_LIKES` / `FEEDBACK_SYNC_SCROBBLE` | `1` | 开关红心 / 打卡 |
| `FEEDBACK_DRY_RUN` | `0` | 设为 `1` 时只匹配不写入 |
| `FEEDBACK_FAVORITES_PLAYLIST_NAME` | `喜爱歌曲` | Apple 喜爱歌单名，也可用 `FEEDBACK_FAVORITES_PLAYLIST_ID` 指定 |
| `FEEDBACK_MAX_LIKES_PER_RUN` | `40` | 每次最多红心数 |
| `FEEDBACK_MAX_SCROBBLE_PER_RUN` | `25` | 每次最多打卡数 |

## 日常维护

| 事项 | 操作 |
|------|------|
| 查看运行结果 | `tail logs/cron.log`；详细日志在 `logs/daily-*.log` 和 `logs/feedback-*.log` |
| 查看匹配明细 | `data/manifest-YYYY-MM-DD.json`（日推）、`data/feedback-manifest-YYYY-MM-DD.json`（回写） |
| 网易云登录失效 | `./login.sh qr` 重新扫码 |
| Apple token 失效 | 重新从浏览器复制 `media-user-token`，覆盖 `secrets/apple-music-config.yaml` |
| 升级代码 | `git pull`；`config.env` 和数据不会被覆盖。之后对照 `config.env.example` 看是否有新配置项 |
| 升级 api-enhanced | 在 `~/ncm-api` 中安装新版本，重启服务后先用 `--dry-run` 验证登录、搜索和回写 |

请保持 `config.env`、`secrets/` 和 `data/` 为私有权限（600 / 700），不要提交到 Git 或分享给他人。

## 故障排查

| 现象 | 处理 |
|------|------|
| 提示缺少或失效的 cookie | `./login.sh qr` |
| Apple 返回 401 / 403 | 更新 `media-user-token` |
| 连不上网易云 API | 手动运行 `NCM_API_START_CMD` 中的命令，查看 `ncm-api.log`；确认 Node 在 `PATH` 中 |
| 获取 Apple developer token 很慢或失败 | 通常是网络问题，稍后重试 |
| 日志中大量 `429` | 已内置限速和重试；可把 `AM_MIN_INTERVAL` 调到 `1.2` |
| 歌单不在文件夹内 | 已创建的歌单无法通过 API 移动，请在 App 中手动拖入 |
| 大量歌曲未匹配 | 查看当天 manifest；启用 AI 辅助匹配；部分歌曲在当前地区没有版权 |
| 找不到喜爱歌单 | 设置 `FEEDBACK_FAVORITES_PLAYLIST_NAME` 或 `FEEDBACK_FAVORITES_PLAYLIST_ID` |
| 打卡一直显示 `seed_only` | 首次运行只记录快照，下次运行开始打卡 |
| 回写返回退出码 1 | 查看 `logs/feedback-*.log`，通常是个别歌曲网络失败，下次运行会自动核对处理 |

## 开发

```bash
python3 -m unittest discover -s tests -v
shellcheck run.sh run_feedback.sh login.sh scripts/start-ncm-api.sh
```

测试使用临时目录和模拟接口，不需要真实账号，也不会访问网络。

## 致谢

- [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)
- [FlyRenxing/netease-apple-daily](https://github.com/FlyRenxing/netease-apple-daily)

## License

[MIT](LICENSE)
