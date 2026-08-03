# H.264 Timestamp Repair

一个面向 Docker/Linux/NAS 的通用服务，用于检测 MP4 中缺失的 H.264 合成时间戳，并在严格验证后进行无损封装修复。视频不会重新编码。

## 2.0 的工作方式

- 使用 Linux 文件事件监听新增、写入、移动、重命名和删除，不再按固定间隔反复遍历整个媒体库。
- 变化路径进入 SQLite 持久队列；最后一次变化静默 60 秒并满足最小文件年龄后处理。
- 每天在本地时间 04:00 做一次仅含目录和元数据的校准，用来捕捉停机期间或文件系统漏报的变化。
- 已确认文件仅在路径、设备、inode、大小、纳秒 mtime/ctime、分析版本和检测配置全部一致时命中缓存。
- MP4Box 的临时文件固定写入 `/work/job-*`，不会受容器小容量 `/tmp` 限制。
- Web 界面匿名且只读；默认隐藏完整媒体路径，不提供删除、配置修改或手动修复功能。

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
DOCKER_IMAGE=kangtong1993/h264-timestamp-repair:2.0.0
TZ=Asia/Shanghai
AUTO_REPAIR=false
MIN_FILE_AGE_SECONDS=3600
FILE_SETTLE_SECONDS=60
RECONCILE_LOCAL_TIME=04:00
WEB_UI_HOST_PORT=8080
```

先以只扫描模式启动：

```bash
docker compose -f docker-compose.hub.yml up -d
docker compose -f docker-compose.hub.yml logs -f
```

确认检测结果后，将 `AUTO_REPAIR=true` 并重新创建容器。自动修复只处理确认候选；修复文件经过时间戳、流属性、抽样解码和哈希验证后才原子替换原文件。

Web 地址为 `http://NAS地址:8080/`。状态、文件列表和最近事件均直接从 SQLite 读取；2.0 不生成或导出 CSV。

## 配置

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `MEDIA_HOST_PATH` | 必填 | Docker 主机上的媒体目录 |
| `DOCKER_IMAGE` | `kangtong1993/h264-timestamp-repair:2.0.0` | 容器镜像 |
| `TZ` | `Asia/Shanghai` | 容器本地时区 |
| `NAME_CONTAINS` | 空 | 可选文件名过滤；空表示所有 MP4 |
| `AUTO_REPAIR` | `false` | 是否自动修复确认候选 |
| `MIN_FILE_AGE_SECONDS` | `3600` | 文件修改时间至少多旧才处理 |
| `FILE_SETTLE_SECONDS` | `60` | 最后一次文件事件后的静默时间 |
| `RECONCILE_LOCAL_TIME` | `04:00` | 每日元数据校准时间，格式 HH:MM |
| `SAMPLE_SECONDS` | `8` | 开头、中间、结尾各抽样秒数 |
| `MINIMUM_PACKETS` | `60` | 自动判定所需的最少可比较视频包 |
| `RETRY_FAILED_AFTER_SECONDS` | `86400` | 失败后的重试间隔 |
| `EVENT_HISTORY_LIMIT` | `5000` | SQLite 最近事件保留条数 |
| `KEEP_TEMP_ON_FAILURE` | `false` | 是否在 `/work` 保留失败现场 |
| `WEB_UI_ENABLED` | `true` | 是否启用匿名只读 Web 界面 |
| `WEB_UI_BIND_ADDRESS` | `0.0.0.0` | Docker 主机端口绑定地址 |
| `WEB_UI_HOST_PORT` | `8080` | Docker 主机 Web 端口 |
| `WEB_SHOW_FULL_PATHS` | `false` | Web/API 是否显示完整媒体路径 |

1.x 的 `SCAN_INTERVAL_SECONDS`、`STABLE_SCANS`、`WEB_USERNAME` 和 `WEB_PASSWORD` 在 2.0 中会被忽略并记录迁移提示。

## 持久数据与升级

- `config/state.sqlite3`：文件身份、分析结果、待处理队列和最近事件。
- `config/heartbeat.json`：健康检查与当前任务状态。
- `work/job-*`：修复过程中的临时文件；成功后自动清理。

首次从 1.x 启动时，服务会创建新的 2.0 数据库，复用元数据仍一致的有效缓存，将失败或变化文件重新排队，并在完整性检查通过后清理旧的膨胀数据库、历史、CSV 和旧密码文件。

## 安全边界

- 仅自动修复单一主 H.264 视频流、固定/明确帧率及可安全复制字幕的 MP4。
- 原文件在完整修复、属性比较、抽样解码、哈希和安装后验证全部成功前不会被替换。
- 文件系统监听不可用时服务进入降级状态，但仍由每日元数据校准发现变化。
- 硬链接和无法可靠识别的移动不会跨路径共享修复结果，优先保证安全。

## 架构

镜像发布为 `linux/amd64` 和 `linux/arm64`。项目与任何特定媒体服务器、NAS 品牌或内容类型无关。
