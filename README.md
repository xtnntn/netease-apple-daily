# netease-apple-daily

网易云音乐与 Apple Music 的个人双向同步工具：

- **日推 → Apple Music**：每天把网易云「每日推荐」生成为 Apple Music 资料库里的日期歌单。
- **Apple Music → 网易云**：把 Apple 的喜爱歌曲和最近播放回写为网易云红心与听歌打卡。

支持可选的 AI 辅助匹配，处理日文、罗马字、英文译名等跨语言差异，并避免误选 Karaoke、Live 等版本。

基于 [FlyRenxing/netease-apple-daily](https://github.com/FlyRenxing/netease-apple-daily) 继续开发，沿用 MIT 许可证。

> 仅供个人使用，请遵守网易云与 Apple 的服务条款。回写会修改网易云账号数据，请先用 `--dry-run` 预览。

## 依赖

- Python 3.10+（仅标准库）
- Node.js，用于在本机运行 [api-enhanced](https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced)
- `curl`、`jq`（登录脚本）
- Apple Music 订阅和网易云账号

## 快速开始

```bash
git clone https://github.com/xtnntn/netease-apple-daily.git
cd netease-apple-daily
cp config.env.example config.env && chmod 600 config.env
# 编辑 config.env：配置 Apple token 与 NCM_API_START_CMD

./login.sh qr                           # 网易云扫码登录
./run.sh                                # 日推 → Apple Music
./run_feedback.sh --dry-run --limit 5   # 回写预览
./run_feedback.sh                       # 确认无误后真实回写
```

## 配置

### 网易云 API

用 Node.js 安装 `@neteasecloudmusicapienhanced/api@4.41.1`，并让它只监听本机：`HOST=127.0.0.1`、`PORT=3000`。在 `config.env` 中指定启动脚本：

```bash
NCM_API_START_CMD=/absolute/path/to/start-ncm-api.sh
```

脚本需要在后台启动服务并立即返回；服务已运行时直接退出。程序会在 API 不可用时自动调用它。

### Apple Music

浏览器登录 [music.apple.com](https://music.apple.com)，从 Cookie 复制 `media-user-token`，二选一写入：

```bash
MUSIC_USER_TOKEN=你的media-user-token
# 或
AM_CONFIG=./secrets/apple-music-config.yaml   # 文件内容：media-user-token: "..."
```

Developer token 默认从 music.apple.com 自动获取。

### 常用配置项

| 变量 | 默认 | 说明 |
|------|------|------|
| `STOREFRONT` | `cn` | Apple 商店区域 |
| `PLAYLIST_PREFIX` | `网易云日推` | 歌单名为 `{前缀}-{日期}` |
| `PLAYLIST_FOLDER_NAME` | `网易云每日推荐` | 歌单所在文件夹，可留空 |
| `SKIP_IF_EXISTS` | `1` | 当天歌单已存在则跳过 |
| `FEEDBACK_DRY_RUN` | `0` | 设为 `1` 时只匹配不写入 |
| `FEEDBACK_MAX_LIKES_PER_RUN` | `40` | 每轮最多红心数 |
| `FEEDBACK_MAX_SCROBBLE_PER_RUN` | `25` | 每轮最多打卡数 |

完整选项见 [`config.env.example`](config.env.example)。相对路径以项目根目录为准；含空格的值需加引号。

## AI 辅助匹配（可选）

规则能确定的歌曲直接匹配，有歧义时交给模型从搜索结果中选择，必要时再补搜一次。模型只能选择 Apple 搜索结果中的歌曲；没有把握时会保留为未匹配，不会退回低分候选。版本保护始终生效，不会把正常人声版匹配成 Karaoke、伴奏或 Live 版。

```bash
AI_MATCHING_ENABLED=1
AI_MATCHING_PROVIDER=openai          # 或 anthropic
AI_MATCHING_BASE_URL=https://your-model-service.example/v1
AI_MATCHING_MODEL=your-model-id
AI_MATCHING_API_KEY_FILE=./secrets/ai-api-key   # chmod 600
```

只会把歌名、艺人、专辑、时长和 ISRC 发送给模型，不会发送任何账号凭据。启用前可以先用历史清单只读预览：

```bash
python3 scripts/preview_matching.py data/manifest-YYYY-MM-DD.json --output data/ai-preview.json
```

## 回写说明

- 喜爱歌曲 → 网易云红心，按已有红心去重。
- 最近播放 → 听歌打卡。Apple 不提供播放时间，只打卡新出现的歌曲；首次运行只记录快照。
- 超过限额或失败的歌曲会留到下次处理；结果不确定的写入不会自动重试。
- 状态保存在 `data/feedback-state.json`。

```bash
./run_feedback.sh --likes-only      # 只红心
./run_feedback.sh --scrobble-only   # 只打卡
```

## 定时任务

```cron
30 7 * * * /path/to/netease-apple-daily/run.sh
0 20 * * * /path/to/netease-apple-daily/run_feedback.sh
```

运行日志写入 `logs/`，每天的匹配结果写入 `data/manifest-YYYY-MM-DD.json`。Hermes Agent 集成见 [`extras/hermes/`](extras/hermes/README.md)。

## 故障排查

| 现象 | 处理 |
|------|------|
| 缺少或失效 cookie | `./login.sh qr` |
| Apple 401 / 403 | 更新 `media-user-token` |
| 连不上网易云 API | 检查 `NCM_API_START_CMD` 和 api-enhanced 日志 |
| 歌单不在文件夹内 | 已建歌单无法通过 API 移动，请在 App 内手动拖入 |
| 大量未匹配 | 查看当天 manifest，或启用 AI 辅助匹配 |

## 开发

```bash
python3 -m unittest discover -s tests -v
shellcheck run.sh run_feedback.sh login.sh
```

## License

[MIT](LICENSE)
