# 视频完整性修复

一个面向 Docker、Linux 和 NAS 的通用服务，用于检测并无损修复 H.264 B 帧时间轴异常，以及 MP4 中覆盖整段视频的无效空章节。视频和音频不会重新编码。

## 支持的问题

- MP4：B 帧显示时间戳缺失或错误。
- MKV：封装时间码错误地使用了解码顺序，导致播放时重复、跳帧或不连贯。
- MP4：唯一、无标题且覆盖完整片长的无效章节。
- 修复完成后可选通知 Emby 重新读取媒体信息和章节缩略图。

MP4 和 MKV 使用同一个时间轴完整性核心：最终依据是解码后的画面 PTS 是否按显示顺序严格递增。容器差异只存在于可选的快速预筛选和重新封装适配器。

## 3.0 功能

- 文件系统事件监听新增、写入、移动、重命名和删除，不反复运行全库 FFprobe。
- 每日只做目录和文件身份校准，未变化文件直接命中 SQLite 缓存。
- 中文多菜单 Web 界面：概览、问题文件、全部文件、任务中心和运行设置。
- 支持手动扫描变化、单文件复检、单个或批量修复及失败重试。
- 显示分析、抽取、时间戳重建、封装、验证、复制和覆盖进度。
- 手动任务、扫描队列和媒体库刷新队列均持久化到 SQLite。
- 修复完成并通过时间轴、码流哈希、流属性和抽样解码校验后才原子替换原文件。

### 3.0.3 自动修复覆盖改进

- MP4 中零数据包的空占位流不再触发完整性误报；真实音频、字幕及非空数据仍严格校验
- 对超大帧率分数进行受控化简，只有预计总时长漂移不超过 5 毫秒时才采用
- 帧率化简后仍要求时间轴、帧数、画面 NAL、非视频包和抽样解码全部通过才覆盖原文件

### 3.0.2 队列状态显示修正

- 已进入后台队列的旧问题会显示为“等待自动修复”或“等待自动复检”，不再误计入“无法自动处理”
- 问题分类只统计当前没有自动处理任务的真实待确认项目
- 最近记录会结合当前队列状态，避免把历史检测结果误认为当前状态

### 3.0.1 自动判定改进

- 明确达到异常阈值的文件继续自动修复，不因 Annex-B 参数单元的安全重排而误报。
- 低于阈值的单个孤立时间戳抖动按抽样噪声处理，减少不必要的人工确认。
- 真正的画面 NAL 变化、可变帧率、样本不足或跨区段结论冲突仍会停止覆盖并等待确认。

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
DOCKER_IMAGE=kangtong1993/h264-timestamp-repair:3.0.3
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

先使用 `AUTO_REPAIR=false` 查看检测结果。确认后将其设为 `true` 并重新创建容器。Web 地址为 `http://NAS地址:8080/`。

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
| `MINIMUM_PACKETS` | `60` | 自动判定需要的最少有效样本 |
| `KEEP_TEMP_ON_FAILURE` | `false` | 失败时是否保留工作文件 |
| `WEB_UI_HOST_PORT` | `8080` | 主机 Web 端口 |
| `WEB_SHOW_FULL_PATHS` | `false` | 是否在 Web/API 显示完整路径 |

## WatchCow 部署

项目另行提供 `docker-compose.watchcow.yml`。它根据 WatchCow 文档添加应用标签，普通 Docker 部署文件不包含特定 NAS 依赖。

```bash
docker compose -f docker-compose.watchcow.yml up -d
```

默认宿主机端口为 `18080`，入口对所有系统用户可见并使用新标签页打开。WatchCow 标签属于容器元数据；修改标签后需要重新创建容器，仅重启不会生效。

## 持久数据与升级

- `config/state.sqlite3`：文件身份、分析结果、扫描队列、手动任务和媒体库刷新队列。
- `config/heartbeat.json`：健康检查和当前任务状态。
- `work/job-*`：修复过程临时文件，成功后自动清理。

从 2.x 升级时数据库会自动迁移。现有未变化 MP4 继续使用缓存，新纳入范围的 MKV 会进行首次分析。升级前仍建议备份 `config/state.sqlite3`。

## 安全边界

- 时间轴自动修复仅适用于单主视频流、H.264、包含 B 帧且帧率固定或明确的文件。
- 样本不足、可变帧率、结论冲突或无法安全复制的流不会自动修复。
- 修复前后承载画面的 H.264 VCL NAL 必须逐个完全一致；SPS、PPS、SEI 等参数单元也按类型校验，允许封装器安全调整不同类型之间的排列位置。
- 手动修复按钮不能绕过检测、兼容性和完整性验证。
- 项目及测试数据使用通用名称，不包含用户媒体路径、内容名称、密钥或设备信息。

镜像发布为 `linux/amd64` 和 `linux/arm64`，项目不依赖特定媒体服务器、NAS 品牌或内容类型。
