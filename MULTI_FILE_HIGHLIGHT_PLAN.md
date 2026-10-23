# 瞬游光影 · 代码梳理与多文件高光剪辑方案

> 目标：在现有"单文件 → 一条高光成片"的架构上，扩展为"多个录像文件 → 一条高光成片"。
> 本文档分两部分：第一部分梳理现有代码与完整链路；第二部分给出多文件改造的详细设计与改动清单。

---

## 一、项目概览

**瞬游光影**是一个本地运行的 FPS 游戏高光剪辑 Agent：用户上传录像 → 本地 FFmpeg 粗筛候选 → 候选压缩后交给 Qwen3-Omni-Flash 理解 → 按分数选出多段高光 → FFmpeg 拼接成一条 30~120 秒的成片（可选 Whisper 字幕）。

| 层 | 技术 | 说明 |
|---|---|---|
| 前端 | React + Vite（单页 [App.jsx](frontend/src/App.jsx)） | 上传、配置、进度、结果预览、历史任务 |
| 后端 | FastAPI（[main.py](backend/app/main.py)） | REST API、分块上传、任务状态 |
| 数据 | SQLite（[database.py](backend/app/database.py)） | `uploads` / `jobs` 两张表 |
| 任务 | ThreadPoolExecutor 单并发（[jobs.py](backend/app/jobs.py)） | 同一时间只跑一个剪辑任务 |
| 剪辑 | FFmpeg（[video.py](backend/app/services/video.py)） | 信号扫描、候选压缩、成片渲染 |
| AI 分析 | Qwen3-Omni-Flash（[qwen.py](backend/app/services/qwen.py)） | 候选视频理解 + 彩六二次审核 |
| 字幕 | 本地 Whisper（[subtitles.py](backend/app/services/subtitles.py)） | 智能模式按需烧录字幕 |

数据目录（[settings.py](backend/app/settings.py#L9-L16)）：`data/uploads`（原片）、`data/outputs`（成片）、`data/temp`（临时）、`data/db`（数据库）、`data/config`（千问凭证）。

---

## 二、核心链路：单文件 → 一条高光

### 2.1 上传阶段（前端 → 后端）

1. **初始化**：`POST /api/uploads`（[main.py:94-105](backend/app/main.py#L94-L105)）校验后缀（mp4/mov/mkv）、大小（≤20GB），创建 `uploads` 记录（state=`uploading`），返回 `upload_id` + 分块大小（32MB）。
2. **分块写入**：`PUT /api/uploads/{id}/chunk`（[main.py:108-133](backend/app/main.py#L108-L133)）按 `Upload-Offset` 断点续传。
3. **完成校验**：`POST /api/uploads/{id}/complete`（[main.py:136-159](backend/app/main.py#L136-L159)）把 `.part` 转正，调用 `probe_video()`（ffprobe）读取时长/分辨率/音轨，写入 `metadata_json`，state → `ready`。
   - 校验规则（[video.py:113-122](backend/app/services/video.py#L113-L122)）：≤2小时、≤1080P、横屏。

### 2.2 创建剪辑任务

`POST /api/jobs`（[main.py:215-240](backend/app/main.py#L215-L240)）：

- 校验上传记录为 `ready`；确认没有其他 queued/running 任务（全局单并发）。
- 校验千问凭证已配置（必须配置才能剪辑）。
- 创建 `jobs` 记录，`metadata_json` 直接复用上传文件的 metadata。
- 调用 `JobManager.submit()`，参数：`source=单个文件路径`、`metadata=单个字典`、`options`。

请求体（[schemas.py:155-157](backend/app/schemas.py#L155-L157)）：

```python
class JobCreateRequest(BaseModel):
    upload_id: str          # ← 单文件假设的第一处
    options: JobOptions
```

`JobOptions`（[schemas.py:116-152](backend/app/schemas.py#L116-L152)）包含：游戏、模式、高光类型、成片时长（30/60/90/120s）、识别强度（严格/均衡/宽松）、字幕模式与样式。

### 2.3 流水线 `run_pipeline()`（[pipeline.py:210-501](backend/app/services/pipeline.py#L210-L501)）

这是核心函数，接收**单个** `source: Path` 和**单个** `metadata: dict`，共 6 个阶段：

```
① local_analysis  本地扫描
② candidate_video 准备候选窗口
③ qwen_analysis   千问并行理解（+彩六二次审核）
④ 资格筛选 + 构建成片计划（_build_montage_plan）
⑤ render          渲染成片（render_montage）
⑥ subtitles       智能字幕（transcribe_and_burn）
```

**① 本地扫描** — `rank_local_candidates()`（[video.py:293-351](backend/app/services/video.py#L293-L351)）：
- 并行两路：画面扫描（fps=1 抽帧 → 运动量/亮度/边缘）+ 声音扫描（8kHz 单声道 RMS）。
- 加权评分（运动 0.38 / 音量 0.34 / 闪变 0.16 / 边缘 0.12）→ 平滑 → 按 `candidate_count_for_duration()`（时长越长候选越多，6~24 个）选出局部峰值秒，强制最小间隔 16~22 秒。
- 返回 `[{center, local_score}]` 列表。

**② 候选窗口** — `candidate_window()`（[video.py:388-395](backend/app/services/video.py#L388-L395)）：以 center 为中心取约 30 秒上下文窗口（`qwen_candidate_seconds=30`），不跨文件边界。

**③ 千问分析**（[pipeline.py:291-362](backend/app/services/pipeline.py#L291-L362)）：
- 分析预算：`analysis_budget = max(12, ceil(目标时长/12) * 2)`，宽松 +2，截断候选列表。
- 每个候选：`render_analysis_proxy()` 把源视频对应窗口压成 ≤7MB 的 720p/15fps 代理视频（[video.py:398-455](backend/app/services/video.py#L398-L455)）。
- `analyze_candidate()`（[qwen.py:709-749](backend/app/services/qwen.py#L709-L749)）：代理视频转 Base64 + 提示词（[qwen.py:485-579](backend/app/services/qwen.py#L485-L579)）发给 Qwen，要求输出 JSON：`score / confidence / category / evidence / clip_start / clip_end / recommended_duration / speech_value / player_death_*` 等。
- 归一化（[qwen.py:582-706](backend/app/services/qwen.py#L582-L706)）：钳位分数、本人死亡自动修剪 clip_end、彩六证据链一致性校验。
- 彩六候选分数 ≥50 触发 `verify_candidate()` 二次独立审核。
- 2 路并行（`qwen_parallel_workers=2`），进度按完成数更新。

**④ 筛选与成片计划**：
- `_qualified_candidates()`（[pipeline.py:94-116](backend/app/services/pipeline.py#L94-L116)）：按灵敏度门槛过滤（严格 70 分/0.72 置信度，均衡 60/0.58，宽松 50/0.42）。
- `_build_montage_plan()`（[pipeline.py:148-207](backend/app/services/pipeline.py#L148-L207)）：
  - 按分数降序贪心选择；
  - **事件去重**：`abs(event_absolute - existing_event) < max(10s, 时长×0.55)` 跳过（单时间轴假设）；`event_absolute = context_start + event_time` 由 `_candidate_bounds()`（[pipeline.py:119-145](backend/app/services/pipeline.py#L119-L145)）基于 `source_duration` 计算；
  - 累计时长不超过目标时长（转场重叠扣除）；
  - 全军覆没时保底取最高分一段；
  - 输出 segment：`{start, duration, score, title, category, ...}`，**按 start 排序**。

**⑤ 渲染** — `render_montage()`（[video.py:514-655](backend/app/services/video.py#L514-L655)），三档：

| 情况 | 实现 | 说明 |
|---|---|---|
| 单段 | `render_clip(source, ...)`（[L526-534](backend/app/services/video.py#L526-L534)） | 直接 `-ss/-t` 截取 |
| 多段 + 转场（≥0.1s） | xfade filter graph（[L539-614](backend/app/services/video.py#L539-L614)） | **每个 segment 独立打开一个输入**：`-ss {start} -t {duration} -i {source}`，逐级 `xfade=custom:斜纹` + `acrossfade`，一次编码出完整时间线 |
| 多段无转场 | 逐段 `render_clip` → concat demuxer（[L616-655](backend/app/services/video.py#L616-L655)） | `-c copy` 快速合并 |

> **关键观察**：xfade 路径的输入循环本来就是「每段一个输入」，只是目前全部指向同一个 `source`。这是多文件改造最自然的切入点。

**⑥ 字幕**：`_should_generate_subtitles()`（[pipeline.py:63-79](backend/app/services/pipeline.py#L63-L79)）按智能/关闭/始终三模式决定；对**合成后的 raw_clip** 整体做 Whisper 转写 + PIL 逐帧烧字（不涉及源文件，天然与多文件无关）。

### 2.4 结果与状态

- 结果 JSON 写入 `jobs.result_json`：输出路径、下载/播放 URL、最佳段信息、`segments`（每段 start/duration/分数/分类）、转场统计、字幕信息（[pipeline.py:442-470](backend/app/services/pipeline.py#L442-L470)）。
- 前端 750ms 轮询 `GET /api/jobs/{id}` 刷新进度条与阶段（[App.jsx:261-284](frontend/src/App.jsx#L261-L284)）。
- 成片通过 `GET /api/jobs/{id}/video`（流媒体）/ `download`（下载）访问（[main.py:329-338](backend/app/main.py#L329-L338)）。
- 取消：CancellationToken 安全终止 FFmpeg 子进程；重试：`POST /api/jobs/{id}/retry` 复用已上传文件。

### 2.5 数据模型（[database.py:31-63](backend/app/database.py#L31-L63)）

```
uploads: id, filename, stored_path, size, received_bytes, content_type,
         state(uploading/ready/invalid), metadata_json, created_at, updated_at

jobs:    id, upload_id(单一外键 → uploads, ON DELETE CASCADE), state, stage,
         progress, message, options_json, metadata_json(单文件元数据),
         result_json, error, created_at, updated_at
```

---

## 三、多文件改造涉及的"单文件假设"清单

逐层排查，所有假设单源的代码点：

| # | 位置 | 现状 | 问题 |
|---|---|---|---|
| 1 | `jobs.upload_id`（DB 列，外键） | 单一外键 | 无法关联多个上传 |
| 2 | `jobs.metadata_json` | 单文件 metadata | 多文件需列表 |
| 3 | `JobCreateRequest.upload_id`（[schemas.py:155](backend/app/schemas.py#L155)） | 单个 str | 需 `upload_ids` 列表 |
| 4 | `main.create_job()` / `retry_job()` | 取单个 upload、单 source/metadata 提交 | 需遍历校验多个 |
| 5 | `JobManager.submit()`（[jobs.py:31-55](backend/app/jobs.py#L31-L55)） | `source: Path, metadata: dict` | 需 `sources: list[(Path, dict)]` |
| 6 | `run_pipeline()` 签名与全流程 | `source`/`metadata["duration"]` 单值，贯穿扫描、窗口、代理、渲染 | 需按文件循环 + 候选携带文件索引 |
| 7 | `_candidate_bounds()` | `source_duration` 单时间轴 | 每个候选用自己文件的时长 |
| 8 | `_build_montage_plan()` | 事件去重与排序基于单一全局时间轴 | 去重按 (文件, 事件点)；排序按 (文件顺序, start) |
| 9 | `render_montage()` / `render_clip()` 调用点 | 所有 segment 取自同一 `source` | segment 需携带 `source`，输入循环按段取源 |
| 10 | `_job_status()`（[main.py:58-75](backend/app/main.py#L58-L75)） | filename/source_size 单值 | 展示多文件名 |
| 11 | `delete_job()`（[main.py:341-356](backend/app/main.py#L341-L356)） | 删除单个 upload | 删除全部关联 upload |
| 12 | 前端 `App.jsx` | 单文件选择、单 `upload_id` 提交、单预览 | 多选、多卡片、批量上传、组合进度 |
| 13 | `GET /api/estimate` | 单 duration 查询 | 前端传总时长即可，后端可不改 |
| 14 | 分辨率/帧率/采样率一致性 | xfade/acrossfade 隐含假设同源同参数 | 多源需在滤镜中归一化 |

---

## 四、多文件高光方案设计

### 4.1 目标与场景

- **多场比赛集锦**：昨晚打了 3 局，各录一个文件 → 跨文件选最高分片段合成一条集锦。
- **分段录制的同一局**： OBS 每 20 分钟分段 → 文件间近似连续。

两种场景统一按「每个文件独立扫描分析、全局选片合成」处理。分段录制时文件边界处（约 ±15 秒）的候选会被各自文件的窗口约束截断，第一版可接受；若后续需要，可增加"连续时间轴模式"（把时间轴偏移累加后统一去重），见 4.7。

### 4.2 总体思路

**沿用现有管线，把"单文件"泛化为"文件列表"：**

```
多文件上传 (N 个 upload_id)
        │
        ▼
对每个文件独立执行：本地扫描 → 候选窗口（各自时长约束）
        │   候选对象新增: file_index / source / file_metadata
        ▼
统一压缩代理 + 千问并行分析（预算按文件数放大）
        │
        ▼
全局资格筛选（与现在完全一致）
        │
        ▼
全局构建 montage_plan（去重改为按文件维度，时长预算全局共享）
        │
        ▼
render_montage: 每个 segment 用自己的 source 打开输入
        │
        ▼
字幕（对合成后的成片处理，无需改动）
```

### 4.3 数据层改动（database.py）

`jobs` 表新增 JSON 列，保留旧列兼容：

```sql
-- user_version 1 → 2 迁移
ALTER TABLE jobs ADD COLUMN upload_ids_json TEXT;   -- ["id1","id2",...]
ALTER TABLE jobs ADD COLUMN files_metadata_json TEXT; -- [{...meta1},{...meta2}]
```

兼容策略：
- 新任务：`upload_ids_json` 为权威，`upload_id` 列写入**第一个**文件 id（满足 NOT NULL 外键，且旧代码/旧数据不炸）。
- 旧任务：`upload_ids_json` 为 NULL 时回退读取 `upload_id`，行为与现在完全一致。
- `_decode()` 增加对两个新 JSON 列的解码。
- `create_job()` / `update_job()` 白名单增加新字段。

> 不采用 join 表的原因：现有 `delete_upload`/`ON DELETE CASCADE` 语义要重做，且 SQLite + JSON 列在本项目规模下足够，改动面最小。

### 4.4 API / Schema 改动（schemas.py, main.py）

```python
class JobCreateRequest(BaseModel):
    upload_id: str | None = None          # 兼容旧客户端
    upload_ids: list[str] = Field(default_factory=list, min_length=1, max_length=8)

    # 校验：优先取 upload_ids；为空时回退 upload_id
```

- `create_job()`：
  - 解析出 `upload_ids`（去重、上限 8 个）；
  - 逐个校验 `state == "ready"` 且 metadata 存在，任一不满足返回 409 并指出具体文件名；
  - `metadata_json` 写入合并元数据列表；
  - `jobs.submit(sources=[(Path, meta), ...], ...)`。
- `retry_job()`：读取 `upload_ids_json`（或旧 `upload_id`），逐个校验存在且 ready。
- `_job_status()`：
  - `filename` → 拼接展示（`"a.mp4 +2"`）或新增 `filenames: list[str]`；
  - `source_size` → 总大小；
  - `metadata` → 新增 `files: list[dict]`（旧字段保留为第一个文件的）。
- `delete_job(delete_source=True)`：删除所有关联 upload 文件与记录。
- `GET /api/estimate`：不改（前端传多文件总时长）。

### 4.5 任务层改动（jobs.py）

```python
def submit(self, *, job_id, sources: list[tuple[Path, dict]], options, ...):
    # sources: [(path, metadata), ...]  传给 run_pipeline
```

### 4.6 流水线改动（pipeline.py）—— 核心

`run_pipeline()` 签名：

```python
def run_pipeline(*, database, job_id, sources: list[tuple[Path, dict]],
                 options, qwen_api_key, qwen_api_host, cancel_token=None):
```

**① 本地扫描（按文件循环，进度按文件均分）**：

```python
all_candidates = []
total_duration = sum(meta["duration"] for _, meta in sources)
for file_index, (source, meta) in enumerate(sources):
    cands = rank_local_candidates(source, meta["duration"], sensitivity, ...)
    for c in cands:
        all_candidates.append({**c, "file_index": file_index, "source": source})
```

**② 候选窗口**：`candidate_window(candidate["center"], sources[file_index][1]["duration"], ...)` —— 用各自文件的时长，天然不越界。

**③ 千问预算分配**：预算全局计算后按文件时长占比分配，且保证每文件至少 2~3 个名额（防止短视频文件颗粒无收）：

```python
budget = max(12, ceil(target_duration / 12) * 2) + 2 * (len(sources) - 1)  # 多文件略放大
per_file = 按时长比例分配，min 2
```

代理渲染 `render_analysis_proxy(source, ...)` 用候选自己的 `source`，无改动。

**④ `_candidate_bounds(item, source_duration)`**：调用处传 `sources[item["file_index"]][1]["duration"]`，函数本身不改。

**⑤ `_build_montage_plan()` 两处调整**：

```python
# 事件去重：单时间轴 → 按文件隔离
if any(item_file == sel_file and abs(event - sel_event) < gap ...)
# （不同文件之间不去重 —— 两局各有一个三杀都应该保留）

# 排序：单时间轴 → 文件顺序优先
sorted(plan, key=lambda s: (s["file_index"], s["start"]))
```

segment 结构新增 `file_index`、`filename`（展示用）。

**⑥ 渲染**：调用 `render_montage(raw_clip, montage_plan, ...)` 时不再传 source（见 4.8），段内自带来源。

**⑦ 字幕判断**：`has_audio` 改为 `any(meta.get("has_audio") for _, meta in sources)`；其余不动（转写对象是合成后的 raw_clip）。

**⑧ 结果 JSON**：新增 `file_count`、每段带 `filename`；`start/duration` 语义变为"文件内相对时间"，前端时间轴展示时按需累加偏移。

### 4.7 渲染层改动（video.py）

`render_montage()` / `render_clip()` 增加 segment 级 source 支持：

```python
def render_montage(output, segments, transition_duration=0.0, token=None):
    # segments[i] 需含: source, start, duration
```

**xfade 路径（改动极小）** —— 输入循环把写死的 `source` 换成段自己的：

```python
# 现在（video.py L546-555）：
for segment in segments:
    command.extend(["-ss", ..., "-t", ..., "-i", str(source)])

# 改后：
for segment in segments:
    command.extend(["-ss", ..., "-t", ..., "-i", str(segment["source"])])
```

**归一化滤镜（新增，必做）** —— xfade 要求两侧同分辨率/同 SAR/同时基，acrossfade 要求同采样率/声道。多源文件参数可能不一致，每个输入统一处理：

```python
# 视频链（MIN_W/MIN_H 取所有文件的最小宽高，保证不放大）
f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS,"
f"scale={min_w}:{min_h}:force_original_aspect_ratio=decrease,"
f"pad={min_w}:{min_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]"

# 音频链
f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]"
```

（探测各文件 metadata 已有 width/height/fps，无需额外 ffprobe。）

**concat 路径 / 单段路径**：`render_clip(segment["source"], ...)`，逐段指定来源。

**连续时间轴模式（可选增强，第二期）**：为分段录制场景，候选合并时对相邻文件边界 ±窗口半径的重叠事件做跨文件去重；渲染逻辑不变。

### 4.8 前端改动（App.jsx）

| 点 | 改动 |
|---|---|
| 文件选择 | `<input multiple>` + dropzone 支持多文件拖入；校验每个文件（后缀/大小） |
| 状态 | `file` → `files: []`，每文件一张卡片（名称/大小/预览缩略/可单独移除） |
| 上传 | `uploadInChunks` 顺序上传每个文件，收集 `upload_id` 列表；总进度 = 已传字节总和 / 总字节 |
| 提交 | `POST /api/jobs { upload_ids: [...] }` |
| 历史列表 | 文件名展示 `a.mp4 等3个文件` |
| 成本估算 | 前端把各文件本地取到的时长求和后请求 `/api/estimate`（上传前可用 `video.duration` 粗估，上传后用服务端 metadata） |
| 结果页 | segments 时间轴按文件分段着色/标注文件名（可选） |

### 4.9 约束与边界

- **文件数量上限**：建议 ≤8 个（API 校验），避免任务过长与代理视频数量失控。
- **单文件限制不变**：≤20GB / ≤2h / ≤1080P / 横屏，逐文件校验。
- **总时长**：第一版不做硬限制（成本由估算接口透出），可在 settings 加 `max_total_duration_hours`（建议默认 4h）。
- **单并发不变**：仍是全局一个任务。
- **上传存储**：多文件共用现有 uploads 机制，无新存储。
- **取消/重试**：机制不变，重试需所有源文件仍在（`retry_job` 已按此校验）。

### 4.10 分阶段实施清单

**Phase 1 — 后端多源管线（核心，可独立验证）**
1. `database.py`：schema 迁移（user_version 2）+ 新列读写。
2. `schemas.py`：`JobCreateRequest.upload_ids`（兼容 `upload_id`）、`JobStatus` 多文件字段。
3. `main.py`：`create_job` / `retry_job` / `_job_status` / `delete_job` 多文件化。
4. `jobs.py`：`submit(sources=[...])`。
5. `pipeline.py`：多文件扫描循环、候选带 `file_index/source`、预算分配、`_build_montage_plan` 按文件去重排序、`has_audio` 聚合、结果 JSON 扩展。
6. `video.py`：`render_montage`/`render_clip` 段级 source + 分辨率/采样率归一化滤镜。

**Phase 2 — 前端多文件 UI**
7. 多文件选择/拖入/卡片列表/单个移除。
8. 顺序批量上传 + 组合进度。
9. 提交 `upload_ids`、历史与结果展示适配。

**Phase 3 — 体验增强（可选）**
10. 结果页 segments 标注来源文件与文件内时间。
11. 成本估算按文件明细。
12. 连续时间轴模式（分段录制场景的跨文件边界去重）。

### 4.11 风险点

| 风险 | 缓解 |
|---|---|
| 多源分辨率/帧率不一致导致 xfade 失败 | 4.7 的归一化滤镜为必做项；用不同参数文件组合实测 |
| 代理视频数量 = 候选数（预算放大后）增多，千问耗时/费用上升 | 预算公式按文件数仅线性小幅放大；估算接口透出总额 |
| `upload_id` 外键兼容层出错（新数据写列表、旧数据读单值） | 读取端统一走 `_resolve_upload_ids(record)` helper，单点收敛 |
| 大量 `-i` 输入的 filter_complex 命令行长度（段数 ≤ 120s/6s = 20 段，安全） | 现有单源 xfade 已是同等输入数，无新增风险 |
| 用户中途删除某个已上传文件再创建任务 | `create_job` 逐个校验 ready，缺失即 409 并报文件名 |

---

## 五、总结

现有架构对多文件非常友好：**候选分析天然逐段进行、montage 渲染本就按段开输入、字幕作用于合成结果**。真正的"单文件假设"集中在三处——数据模型（`upload_id`/`metadata_json`）、管线签名（`source`/`metadata` 单值）、渲染取源（xfade 输入循环写死同一 source）。按本方案改造后，"3 局录像 → 一条 60 秒集锦"与"单文件 → 一条高光"将走完全相同的代码路径。
