# H.264 Timestamp Repair

一个面向 Docker/Linux/NAS 的通用服务，用于检测 MP4 中缺失的 H.264 合成时间戳和无意义的整片空章节，并在严格验证后进行无损封装修复。视频和音频不会重新编码。

## 2.2 的工作方式

- 使用 Linux 文件事件监听新增、写入、移动、重命名和删除，不再按固定间隔反复遍历整个媒体库。
- 变化路径进入 SQLite 持久队列；最后一次变化静默 60 秒并满足最小文件年龄后处理。
- 每天在本地时间 04:00 做一次仅含目录和元数据的校准，用来捕捉停机期间或文件系统漏报的变化。
- 已确认文件仅在路径、设备、inode、大小、纳秒 mtime/ctime、分析版本和检测配置全部一致时命中缓存。
- MP4Box 的临时文件固定写入 `/work/job-*`，不会受容器小容量 `/tmp` 限制。
- Web 界面匿名且只读；默认隐藏完整媒体路径，不提供删除、配置修改或手动修复功能。
- 时间戳异常和章节异常分别判定；两者同时存在时合并为一次修复，避免重复读写大文件。
- 仅当文件恰好只有一个无标题章节、从 0 秒开始且结束时间等于片长时，才会自动移除该章节。
- 可选的 Emby 后处理模块会在修复后按完整路径精确匹配条目，请求重新读取媒体信息和替换章节缩略图。
- 条目尚未入库或服务器暂时不可用时，通知保存在 SQLite 独立队列中并自动退避重试，不影响文件修复状态。

> Web 端口没有登录验证。请只在可信局域网中开放，绝对不要直接暴露到互联网。

## 快速开始

```bash
mkdir -p h264-timestamp-repair/config h264-timestamp-repair/work
cd h264-timestamp-repair
curl -O https://raw.githubusercontent.com/kangtong/h264-timestamp-repair/main/docker-compose.hub.yml
```

创建 `.env`：

```dotenv
MEDIA_HOST_PATH=/srv/media
DOCKER_IMAGE=kangtong1993/h264-timestamp-repair:2.2.1
TZ=Asia/Shanghai
AUTO_REPAIR=false
REPAIR_EMPTY_FULL_CHAPTERS=true
MIN_FILE_AGE_SECONDS=3600
FILE_SETTLE_SECONDS=60
RECONCILE_LOCAL_TIME=04:00
WEB_UI_HOST_PORT=8080
```

可选的 Emby 自动刷新示例。API 密钥建议保存为 `config/emby-api-key`，权限设为仅管理员可读；不要提交到 Git：

```dotenv
EMBY_URL=http://media-server:8096/emby
EMBY_MEDIA_ROOT=/path-seen-by-emby
EMBY_API_KEY_FILE=/config/emby-api-key
EMBY_REFRESH_RETRY_SECONDS=60
EMBY_REFRESH_MAX_RETRY_SECONDS=21600
```

`MEDIA_ROOT` 是本容器看到的路径，`EMBY_MEDIA_ROOT` 是 Emby 看到的同一目录。两边的相对路径必须一致；程序只在完整路径唯一匹配时发送刷新请求。

先以只扫描模式启动：

```bash
docker compose -f docker-compose.hub.yml up -d
docker compose -f docker-compose.hub.yml logs -f
```

确认检测结果后，将 `AUTO_REPAIR=true` 并重新创建容器。自动修复只处理确认候选；修复文件经过时间戳、章节、流属性、抽样解码和哈希验证后才原子替换原文件。

Web 地址为 `http://NAS地址:8080/`。状态、文件列表和最近事件均直接从 SQLite 读取；2.0 不生成或导出 CSV。

## 配置

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `MEDIA_HOST_PATH` | 必填 | Docker 主机上的媒体目录 |
| `DOCKER_IMAGE` | `kangtong1993/h264-timestamp-repair:2.2.1` | 容器镜像 |
| `TZ` | `Asia/Shanghai` | 容器本地时区 |
| `NAME_CONTAINS` | 空 | 可选文件名过滤；空表示所有 MP4 |
| `AUTO_REPAIR` | `false` | 是否自动修复确认候选 |
| `REPAIR_EMPTY_FULL_CHAPTERS` | `true` | 是否检测并移除唯一、无标题且覆盖整片的异常章节 |
| `MIN_FILE_AGE_SECONDS` | `3600` | 文件修改时间至少多旧才处理 |
| `FILE_SETTLE_SECONDS` | `60` | 最后一次文件事件后的静默时间 |
| `RECONCILE_LOCAL_TIME` | `04:00` | 每日元数据校准时间，格式 HH:MM |
| `SAMPLE_SECONDS` | `8` | 开头、中间、结尾各抽样秒数 |
| `MINIMUM_PACKETS` | `60` | 自动判定所需的最少可比较视频包 |
| `RETRY_FAILED_AFTER_SECONDS` | `86400` | 失败后的重试间隔 |
| `EVENT_HISTORY_LIMIT` | `5000` | SQLite 最近事件保留条数 |
| `KEEP_TEMP_ON_FAILURE` | `false` | 是否在 `/work` 保留失败现场 |
| `EMBY_URL` | 空 | Emby API 基础地址；为空时不启用集成 |
| `EMBY_MEDIA_ROOT` | 空 | Emby 中与 `MEDIA_ROOT` 对应的媒体根路径 |
| `EMBY_API_KEY_FILE` | 空 | 推荐：容器内 API 密钥文件路径 |
| `EMBY_API_KEY` | 空 | 备选：直接传入 API 密钥；不建议用于长期部署 |
| `EMBY_REFRESH_RETRY_SECONDS` | `60` | 首次媒体库刷新重试间隔 |
| `EMBY_REFRESH_MAX_RETRY_SECONDS` | `21600` | 媒体库刷新最大重试间隔 |
| `WEB_UI_ENABLED` | `true` | 是否启用匿名只读 Web 界面 |
| `WEB_UI_BIND_ADDRESS` | `0.0.0.0` | Docker 主机端口绑定地址 |
| `WEB_UI_HOST_PORT` | `8080` | Docker 主机 Web 端口 |
| `WEB_SHOW_FULL_PATHS` | `false` | Web/API 是否显示完整媒体路径 |

1.x 的 `SCAN_INTERVAL_SECONDS`、`STABLE_SCANS`、`WEB_USERNAME` 和 `WEB_PASSWORD` 在 2.0 中会被忽略并记录迁移提示。

## 持久数据与升级

从 2.0 升级到 2.1 后，分析规则版本会变化，因此现有 MP4 会进行一次必要的重新分析，以发现以前未检测的章节问题。此后未变化文件仍会直接命中 SQLite 缓存，每日校准不会反复运行 FFprobe。

从 2.1 升级到 2.2 不改变媒体分析签名，因此不会再次触发全库重新分析；尚未完成的扫描队列会原样继续。

2.2.1 会在首次启用集成时，把升级前已经完成的异常章节修复一次性补入媒体刷新队列，并记录迁移标记防止以后重启时重复通知。

- `config/state.sqlite3`：文件身份、分析结果、待处理队列和最近事件。
- `media_refresh_queue`：位于同一 SQLite 数据库中的媒体服务器通知队列，成功请求刷新后自动删除。
- `config/heartbeat.json`：健康检查与当前任务状态。
- `work/job-*`：修复过程中的临时文件；成功后自动清理。

首次从 1.x 启动时，服务会创建新的 2.0 数据库，复用元数据仍一致的有效缓存，将失败或变化文件重新排队，并在完整性检查通过后清理旧的膨胀数据库、历史、CSV 和旧密码文件。

## 安全边界

- 时间戳修复仅用于单一主 H.264 视频流和固定/明确帧率；章节修复也适用于其他 MP4 视频编码，但仍要求单一主视频流及可安全复制的字幕。
- 有标题的章节、多个章节、局部章节或结束时间与片长不一致的章节不会被自动删除。
- 原文件在完整修复、属性比较、抽样解码、哈希和安装后验证全部成功前不会被替换。
- 文件系统监听不可用时服务进入降级状态，但仍由每日元数据校准发现变化。
- 硬链接和无法可靠识别的移动不会跨路径共享修复结果，优先保证安全。
- Emby 集成不会替换全部元数据或全部图片；刷新失败不会把已经验证成功的媒体修复标记为失败。

## 架构

镜像发布为 `linux/amd64` 和 `linux/arm64`。项目与任何特定媒体服务器、NAS 品牌或内容类型无关。
