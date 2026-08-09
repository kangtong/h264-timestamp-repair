# 视频完整性修复

[English documentation](https://github.com/kangtong/h264-timestamp-repair/blob/main/README.en.md)

<p align="center"><img src="https://raw.githubusercontent.com/kangtong/h264-timestamp-repair/main/app/web/icon.png" width="128" alt="Video Integrity Repair icon"></p>

一个面向 Docker 和 Linux 的通用服务，用于检测并无损修复 H.264 B 帧时间轴异常及无效章节元数据。视频和音频均不重新编码。

## 支持的问题

- MP4：B 帧显示时间戳缺失或顺序错误。
- MKV：封装时间码错误地使用解码顺序，导致播放时重复、跳帧或不连贯。
- MP4：唯一、无标题且覆盖完整片长的无效空章节。
- 修复后可选通知 Emby 重新读取媒体信息和章节缩略图。

MP4 和 MKV 使用同一个时间轴完整性核心：最终依据是解码后画面的 PTS 是否按显示顺序严格递增。容器差异只存在于快速预筛选和重新封装适配器。

## 3.1 功能

- 监听新增、写入、移动、重命名和删除事件，不反复运行全库 FFprobe。
- 每日仅校准目录和文件身份；未变化文件直接命中 SQLite 缓存。
- 中英文 Web 界面，首次跟随浏览器语言并记住手动选择。
- 支持手动扫描变化、单文件复检、单个或批量修复及失败重试。
- 显示分析、抽取、时间戳重建、封装、验证、复制和覆盖进度。
- 手动任务、扫描队列和媒体库刷新队列均持久化到 SQLite。
- 修复完成并通过时间轴、码流哈希、流属性和抽样解码校验后才原子替换原文件。
- Web API 提供稳定的状态、原因和任务阶段代码，并返回中文或英文标签。

### 3.1.1

- 修正长视频中“空白且覆盖完整时长”的无效章节识别：允许音视频流尾部存在很小的合理时长差，同时设置严格上限，避免误判正常章节。
- 启动后只定向复查曾修复时间轴且丢弃过数据轨的 MP4，不触发媒体库全量重扫。
- 删除无效章节后继续自动通知媒体服务器替换并重新生成章节图像。

### 3.1.0

- 公共项目只保留通用 Docker 部署，不包含任何设备或平台专用配置。
- 新增完整英文文档、中英文容器元数据及 Docker Hub 说明自动同步。
- 新增浏览器语言识别、语言切换和本地持久化。
- 新增 `/api/i18n`、API `lang` 参数和稳定的 `current_stage_code`。

> Web 界面没有登录验证。请只在可信局域网开放，不要直接暴露到互联网。

## 快速开始

```bash
mkdir -p video-integrity-repair/config video-integrity-repair/work
cd video-integrity-repair
curl -O https://raw.githubusercontent.com/kangtong/h264-timestamp-repair/main/docker-compose.hub.yml
```

创建 `.env`：

```dotenv
MEDIA_HOST_PATH=/srv/media
DOCKER_IMAGE=kangtong1993/h264-timestamp-repair:3.1.1
TZ=Asia/Shanghai
AUTO_REPAIR=false
REPAIR_MKV_TIMESTAMPS=true
REPAIR_EMPTY_FULL_CHAPTERS=true
MIN_FILE_AGE_SECONDS=3600
FILE_SETTLE_SECONDS=60
RECONCILE_LOCAL_TIME=04:00
WEB_UI_HOST_PORT=8080
```

启动：

```bash
docker compose -f docker-compose.hub.yml up -d
```

建议先使用 `AUTO_REPAIR=false` 查看检测结果；确认后设为 `true` 并重新创建容器。Web 地址为 `http://服务器地址:8080/`。

## Emby 自动刷新

建议把 API 密钥保存为 `config/emby-api-key`，不要提交到 Git：

```dotenv
EMBY_URL=http://media-server:8096/emby
EMBY_MEDIA_ROOT=/path-seen-by-emby
EMBY_API_KEY_FILE=/config/emby-api-key
EMBY_REFRESH_RETRY_SECONDS=60
EMBY_REFRESH_MAX_RETRY_SECONDS=21600
```

`MEDIA_ROOT` 是本容器看到的路径，`EMBY_MEDIA_ROOT` 是 Emby 看到的对应根路径。只有完整路径唯一匹配时才会请求刷新；刷新失败会独立重试，不改变文件修复结果。

## 主要配置

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `MEDIA_HOST_PATH` | 必填 | Docker 主机上的媒体目录 |
| `AUTO_REPAIR` | `false` | 自动修复所有确认候选 |
| `REPAIR_MKV_TIMESTAMPS` | `true` | 启用 MKV 时间戳检测与修复 |
| `REPAIR_EMPTY_FULL_CHAPTERS` | `true` | 检测并移除无效整段空章节 |
| `MIN_FILE_AGE_SECONDS` | `3600` | 文件至少多旧才允许处理 |
| `FILE_SETTLE_SECONDS` | `60` | 最后一次文件事件后的等待时间 |
| `RECONCILE_LOCAL_TIME` | `04:00` | 每日文件身份校准时间 |
| `SAMPLE_SECONDS` | `8` | 片头、中段、片尾抽样秒数 |
| `MINIMUM_PACKETS` | `60` | 自动判定所需最少有效样本 |
| `KEEP_TEMP_ON_FAILURE` | `false` | 失败时是否保留工作文件 |
| `WEB_UI_HOST_PORT` | `8080` | 主机 Web 端口 |
| `WEB_SHOW_FULL_PATHS` | `false` | 是否在 Web/API 显示完整路径 |

## Web API

- `GET /api/i18n?lang=zh-CN|en`：返回完整语言词典。
- `GET /api/status`、`/api/files`、`/api/history` 和 `/api/tasks`：接受可选 `lang` 参数。
- 不传 `lang` 时默认 `zh-CN`，保持旧调用兼容。
- 响应保留原有代码字段，同时返回 `locale` 和本地化标签。
- 状态心跳同时返回 `current_stage_code` 与 `current_stage_label`。

## 持久数据与升级

- `config/state.sqlite3`：文件身份、分析结果、扫描队列、手动任务和媒体库刷新队列。
- `config/heartbeat.json`：健康检查和当前任务状态。
- `work/job-*`：修复过程临时文件，成功后自动清理。

从旧版本升级时数据库会自动迁移。现有未变化 MP4 和 MKV 继续使用缓存。升级前仍建议备份 `config/state.sqlite3`。

## 安全边界

- 时间轴自动修复只适用于单主视频流、H.264、包含 B 帧且帧率固定或明确的文件。
- 样本不足、可变帧率、结论冲突或无法安全复制的流不会自动修复。
- 修复前后承载画面的 H.264 VCL NAL 必须逐个完全一致；SPS、PPS、SEI 等参数单元也按类型校验。
- 手动修复按钮不能绕过检测、兼容性和完整性验证。
- 项目及测试数据使用通用名称，不包含用户媒体路径、内容名称、密钥或设备信息。

镜像发布为 `linux/amd64` 和 `linux/arm64`，不依赖特定存储设备、媒体服务器或内容类型。
