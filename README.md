# netease-apple-daily

网易云与 Apple Music 的个人同步项目，支持 AI 辅助跨语言歌曲匹配。

基于 [FlyRenxing/netease-apple-daily](https://github.com/FlyRenxing/netease-apple-daily) 继续开发，保留原项目历史与 MIT 许可证。本仓库加入可选 AI 匹配、歌曲版本保护、只读匹配预览，以及凭据、请求重试和反馈状态处理的可靠性修复。

双向同步（可选）：

1. **正向**：网易云「每日推荐」→ Apple Music 云端资料库日期播放列表  
2. **回写**：Apple Music **喜爱歌曲** + **最近播放** → 网易云 **红心** / **听歌打卡**，帮助网易云更好推荐  

可选把日推列表放入资料库文件夹（如 `网易云每日推荐`）。

**不下载音频、不写本地 melib。** 只调用：

1. [NeteaseCloudMusicApi-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)（Docker：`moefurina/ncm-api`）
2. [Apple Music API](https://developer.apple.com/documentation/applemusicapi)（catalog / library / recent / 资料库歌单）

> 仅供个人学习 / 备份用途。请遵守网易云与 Apple 服务条款，勿滥用接口或传播付费内容。回写会改动网易云账号数据，请先 `--dry-run`。

---

## 架构

```text
NetEase /recommend/songs  ──►  match  ──►  Apple Music library playlist
        (cookie)              catalog           (media-user-token)

Apple 喜爱歌曲 / 最近播放  ──►  match  ──►  NetEase /like + /scrobble
        (media-user-token)      cloudsearch           (cookie)
```

| 组件 | 说明 |
|------|------|
| `docker-compose.yml` | 本机 `ncm-api`（默认 `127.0.0.1:3000`） |
| `login.sh` | 网易云扫码登录 → `data/cookie.txt` |
| `daily_recommend.py` | 日推 → Apple 资料库播放列表 |
| `apple_to_netease_feedback.py` | Apple 喜爱/最近播放 → 网易云红心/打卡 |
| `run.sh` / `run_feedback.sh` | 正向 / 回写入口 |
| `scripts/cron_summary.py` | 给 cron / 机器人用的短摘要输出 |

---

## 依赖

- Docker 或 Node.js（运行 api-enhanced；下文提供无 Docker 配置）
- Python 3.10+（仅标准库）
- `curl`、`jq`（登录脚本）
- 有效的 **Apple Music 订阅**
- 网易云账号（日推需登录）

---

## 快速开始

```bash
git clone https://github.com/xtnntn/netease-apple-daily.git
cd netease-apple-daily

cp config.env.example config.env
chmod 600 config.env
# 编辑 config.env：至少配置 MUSIC_USER_TOKEN 或 AM_CONFIG

docker compose up -d

# 网易云扫码登录
./login.sh qr
# 或分步（适合 bot）：./login.sh qr-init  → 扫码 → ./login.sh qr-poll

# 试跑：日推 → Apple
./run.sh

# 试跑：Apple → 网易云（务必先 dry-run）
./run_feedback.sh --dry-run --limit 5
# 确认匹配无误后再真实写入
./run_feedback.sh
```

成功后在 iPhone / Mac **音乐** App → 资料库 → 播放列表（或你配置的文件夹）中查看日推列表；回写结果见网易云「我喜欢的音乐」与听歌排行。

### 无 Docker 部署

也可使用 Node.js 单独运行 api-enhanced，例如在用户目录安装 `@neteasecloudmusicapienhanced/api@4.41.1`。设置 `HOST=127.0.0.1`、`PORT=3000`，并关闭 `ENABLE_GENERAL_UNBLOCK`。在 `config.env` 配置：

```bash
NCM_API_START_CMD=/absolute/path/to/start-ncm-api.sh
```

启动脚本应幂等：服务已运行时直接退出，否则将服务转入后台并及时返回。Python 入口统一负责健康检查和启动，优先使用此命令，未配置时才尝试 Docker Compose；启动执行和就绪等待均有超时。命令来自可信的 `config.env`，请保持文件权限为 `600`。启动子进程不会继承 Apple token。

Compose 镜像固定为 `4.41.1` 的多平台 digest，来源为 [Docker Hub 标签元数据](https://hub.docker.com/v2/repositories/moefurina/ncm-api/tags/4.41.1)（2026-10-15 核实）；升级时应重新验证登录、搜索和反馈接口。

---

## Apple Music 凭证

| 凭证 | 获取方式 |
|------|----------|
| **Music User Token** | 浏览器打开 [music.apple.com](https://music.apple.com) 登录 → F12 → Application → Cookies → `media-user-token` |
| **Developer JWT** | 默认每次从 music.apple.com 前端 JS 提取（`iss=AMPWebPlay`）；也可自备正式 MusicKit developer token |

写入方式二选一：

```bash
# config.env
MUSIC_USER_TOKEN=你的media-user-token
```

或：

```yaml
# secrets/apple-music-config.yaml（示例，勿提交）
media-user-token: "..."
```

```bash
# config.env
AM_CONFIG=./secrets/apple-music-config.yaml
```

---

## 配置说明（`config.env`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `NCM_API_BASE` | `http://127.0.0.1:3000` | api-enhanced 地址，建议仅监听本机 |
| `NCM_API_START_CMD` | 空 | API 未就绪时执行的后台启动脚本；未配置时使用 Docker |
| `COOKIE_FILE` | `./data/cookie.txt` | 网易云 cookie |
| `MUSIC_USER_TOKEN` / `AM_CONFIG` | — | Apple 用户 token |
| `STOREFRONT` | `cn` | Apple 商店区域 |
| `PLAYLIST_PREFIX` | `网易云日推` | 列表名 = `{PREFIX}-{日期}` |
| `PLAYLIST_FOLDER_NAME` | `网易云每日推荐` | 目标文件夹（可空） |
| `PLAYLIST_FOLDER_ID` | — | 文件夹 ID（优先于名称） |
| `SKIP_IF_EXISTS` | `1` | 当日同名列表存在则跳过 |
| `SKIP_UNMATCHED` | `1` | 个别曲未匹配仍继续 |
| `AM_MIN_INTERVAL` | `0.75` | Apple API 请求最小间隔（秒），防 429 |
| `AM_429_RETRIES` | `6` | 遇 429 重试次数（指数退避） |
| `MATCH_EARLY_SCORE` | `90` | 匹配分达此值则不再搜其它关键词 |
| `FEEDBACK_SYNC_LIKES` | `1` | 喜爱 → 红心 |
| `FEEDBACK_SYNC_SCROBBLE` | `1` | 最近播放增量 → scrobble |
| `FEEDBACK_DRY_RUN` | `0` | `1` 则只匹配不写 |
| `FEEDBACK_FAVORITES_PLAYLIST_NAME` | `喜爱歌曲` | Apple 喜爱歌单名（或设 ID） |
| `FEEDBACK_RECENT_LIMIT` | `50` | 读取最近播放条数 |
| `FEEDBACK_MAX_LIKES_PER_RUN` | `40` | 每轮最多红心数 |
| `FEEDBACK_MAX_SCROBBLE_PER_RUN` | `25` | 每轮最多打卡数 |
| `FEEDBACK_MIN_SCORE` | `55` | 反查匹配最低分 |
| `FEEDBACK_SCROBBLE_SEED_ONLY_FIRST` | `1` | 首次只建最近播放快照、不打卡 |

相对路径均相对于**项目根目录**。配置同时由 Bash 和 Python 读取：含空格的值需加引号，不使用行尾注释、`export` 前缀或 `$VAR` 展开。Python 直接入口优先使用已设置的环境变量；shell 包装脚本会 source 此配置。

---

## AI 辅助匹配（可选）

日推支持“明确规则匹配 → AI 判断候选 → 必要时补搜一次”。AI 可识别日文、罗马字、英文译名和艺人/角色署名差异，例如 `旅路 / 藤井風` 与 `Tabiji`、`悩みたいだけ / 家入レオ` 与 `Nayamitaidake / 家入莉奥`。AI 默认关闭，当前仅接入 **网易云 → Apple Music**；反馈回写继续使用原有评分，但同样执行版本保护。

无论 AI 是否启用，正常人声版均不会选明确标记的 Karaoke/Instrumental/伴奏版；Live、Remix、Solo、Game Version 等版本也必须相容。Remastered 不作为硬性排除条件。规则清晰且高分的结果直接使用，其余候选由 AI 选择或拒绝；AI 不能指定搜索结果以外的歌曲 ID。需要补搜时，最多生成两条搜索词，再判断一次，不循环搜索。中国区没有的录音不会自动改用其他地区曲库。

配置示例（使用你实际拥有的服务和模型）：

```bash
AI_MATCHING_ENABLED=1
AI_MATCHING_PROVIDER=openai
AI_MATCHING_BASE_URL=https://your-model-service.example/v1
AI_MATCHING_MODEL=your-exact-model-id
AI_MATCHING_API_KEY_FILE=./secrets/ai-api-key
AI_MATCHING_TIMEOUT=30
AI_MATCHING_MAX_CALLS=40
```

将独立的模型 API Key 写入上述文件，并设置 `chmod 600 secrets/ai-api-key`。也支持 `AI_MATCHING_PROVIDER=anthropic`，直连时 Base URL 使用 `https://api.anthropic.com/v1`。服务地址要求 HTTPS（本机开发允许 loopback HTTP），不跟随重定向。无需安装 SDK，使用 Python 标准库。启用后 MODEL 必填；默认关闭时不读取密钥、不调用模型。

只发送歌名、艺人、专辑、时长及可用的 ISRC，不发送网易云 cookie 或 Apple token。每个进程最多执行配置数量的模型请求，超时和错误不会自动重试。`AI_MATCHING_TIMEOUT` 是网络等待超时（1–60 秒），不是整次调用的总耗时上限；服务持续缓慢传输时可能超过该值。AI 返回无匹配、非法结果、预算耗尽或调用失败时，疑难歌曲保留为未匹配；不会悄悄回退到低把握候选。结果清单中的 `match_method` 为 `rules` 或 `ai`，AI 选择另存 `ai_reason` 供人工检查（这是模型解释，不是核实过的事实）；`score` 仍是原来的规则分，AI 认可的跨语言匹配可以低于 55 分。

首次启用建议先读取历史清单做预览：

```bash
# 只重查过去未匹配的歌曲；输出文件必须尚不存在
python3 scripts/preview_matching.py data/manifest-2026-10-16.json \
  --unmatched-only --output data/ai-preview-2026-10-16.json

# 也可检查全部歌曲，包含之前选错版本的项目
python3 scripts/preview_matching.py data/manifest-2026-10-16.json --limit 30
```

预览仅读取当前地区 Apple 曲库；不创建歌单，不运行网易云反馈，不修改输入清单或正式状态。预览仍会消耗模型 API 用量。首版仅在一次进程内缓存完全相同的判断，不持久化模型判定；代码中的模拟测试通过不等于真实模型准确率已验证。已经创建的日期歌单不会自动修正，`SKIP_IF_EXISTS` 规则继续生效。

## 回写说明（Apple → 网易云）

| 源（Apple Music API） | 目标（网易云） | 说明 |
|----------------------|----------------|------|
| 资料库歌单「喜爱歌曲」 | `/like` | 与已有红心 / `feedback-state.json` 去重 |
| `/v1/me/recent/played/tracks` | `/scrobble` | **增量**：相对上次快照的新曲；**无播放时间戳** |

限制与策略：

- Apple 最近播放 **没有时间戳 / 次数**，不能做精确 scrobble；只能「窗口里新出现的曲」打卡。  
- **首次 scrobble 默认只写快照**，避免把历史窗口一次灌进网易云；需要可加 `--force-scrobble-seed`（不推荐）。  
- 曲目匹配靠 **歌名 + 艺人 + 时长**（ISRC 在网易云搜索不可靠），误匹配可能红心错歌 → 先 `--dry-run`。  
- 状态文件：`data/feedback-state.json`；清单：`data/feedback-manifest-YYYY-MM-DD.json`。
- 超过本轮限额或明确失败的打卡保留为待处理，即使歌曲离开最近播放窗口也能继续。未匹配歌曲冷却 7 天后重新搜索，不反复占用本轮扫描预算。
- 写请求超时可能已经生效：红心会通过当前 likelist 对账；打卡无可靠对账接口，会在 `pending_scrobbles` 中保留为 `unknown` 并返回非零退出码，不自动重复打卡。请先核实网易云结果并备份状态文件：若确认已成功，将对应 Apple ID 加入 `scrobbled_apple_ids` 并移除该待处理项；只有确认未生效时才把该项 `status` 改回 `pending`。不要直接删除整个状态文件。
- 状态通过临时文件原子替换，并在关键进度处保存。损坏状态会报错并保留原文件；旧格式保守迁移。升级不会自动重放旧版本已经误标记为处理完成的歌曲。

```bash
./run_feedback.sh --dry-run              # 全量预览（受 MAX_* 限制）
./run_feedback.sh --likes-only           # 只红心
./run_feedback.sh --scrobble-only        # 只打卡
./run_feedback.sh --limit 10             # 本轮每类最多 10 首
```

---

## 定时任务

### systemd / crontab

```cron
30 7 * * * /path/to/netease-apple-daily/run.sh
# 回写每 24 小时（示例 20:00，与日推 07:30 错开）
0 20 * * * /path/to/netease-apple-daily/run_feedback.sh
```

或摘要模式（适合 bot 投递）：

```cron
30 7 * * * /usr/bin/python3 /path/to/netease-apple-daily/scripts/cron_summary.py
0 20 * * * /usr/bin/python3 /path/to/netease-apple-daily/scripts/feedback_summary.py
```

### Hermes Agent（可选）

见 [`extras/hermes/`](extras/hermes/README.md)。

---

## 登录脚本

```bash
./login.sh qr              # 终端一体化扫码
./login.sh qr-init         # 只生成二维码（data/login-qr.png）
./login.sh qr-poll [N]     # 轮询扫码结果
./login.sh status          # 检查登录态
# 从 stdin 读取，避免 cookie 出现在命令历史或进程参数：
# 以下交互读取示例使用 Bash：
bash -c 'read -rs -p "NetEase cookie: " ncm_cookie; printf "\n"; printf "%s" "$ncm_cookie" | ./login.sh cookie -'
```

Cookie 失效后重新扫码即可。旧的 `cookie '<value>'` 形式仍兼容，推荐使用上面的 stdin 方式。Cookie 通过 POST 请求体传递；登录 cookie 和临时响应头以私有权限写入，轮询退出时删除临时响应头。

旧版本可能已把 cookie 写入 api-enhanced 的 URL 日志。升级会阻止新请求继续产生此类日志，已有日志需由使用者自行决定清理或轮换。

---

## 目录结构

```text
.
├── daily_recommend.py              # 日推 → Apple
├── apple_to_netease_feedback.py    # Apple → 网易云反馈
├── login.sh                        # 网易云登录
├── run.sh / run_feedback.sh        # 入口
├── docker-compose.yml              # ncm-api
├── config.env.example
├── scripts/cron_summary.py
├── extras/hermes/                  # 可选 Hermes skill / cron 说明
├── data/                           # cookie、manifest、feedback-state（gitignore）
└── logs/
```

- 日推：`data/manifest-YYYY-MM-DD.json`  
- 回写：`data/feedback-manifest-YYYY-MM-DD.json`、`data/feedback-state.json`  

---

## 故障排查

| 现象 | 处理 |
|------|------|
| 缺少 cookie | `./login.sh qr` |
| storefront / 401 / 403 | 更新 `media-user-token` |
| 连不上 ncm-api | 检查 `NCM_API_START_CMD` 启动脚本，或运行 `docker compose up -d`；API 应只监听 `127.0.0.1` |
| 列表不在文件夹内 | 确认 `PLAYLIST_FOLDER_NAME`；**已建列表 API 无法移动**，请 App 内手动拖一次 |
| 大量未匹配 | 版权差异 / 改名；见当日 manifest |
| 日志里大量 `429` | 已内置限速与重试；可调大 `AM_MIN_INTERVAL`（如 `1.2`） |
| 找不到喜爱歌单 | 设置 `FEEDBACK_FAVORITES_PLAYLIST_ID` 或改 `FEEDBACK_FAVORITES_PLAYLIST_NAME` |
| scrobble 一直 seed_only | 正常：首跑只建快照；再跑一次才会打新增曲 |

---

## 本地验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile daily_recommend.py apple_to_netease_feedback.py
bash -n run.sh && bash -n run_feedback.sh && bash -n login.sh
shellcheck run.sh run_feedback.sh login.sh
```

测试使用临时目录与模拟 API，不需要真实账号。包装脚本在 `logs/cron.log` 记录开始、结束和原始退出码；失败返回非零。Linux 使用 `flock` 防止重入，无 `flock` 的环境会显示告警。`--dry-run` 可写独立的预览日志和清单，但不会推进正式反馈状态。

## 致谢

- [NeteaseCloudMusicApiEnhanced/api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)
- [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi)
- Apple Music API / MusicKit

## License

[MIT](LICENSE)
