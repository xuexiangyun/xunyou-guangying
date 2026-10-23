import { useEffect, useMemo, useRef, useState } from "react";

const HIGHLIGHTS = [
  ["kill", "关键击杀"],
  ["multi_kill", "连续击杀"],
  ["clutch", "逆风翻盘"],
  ["escape", "极限生存"],
  ["teamfight", "团战与配合"],
  ["victory", "目标完成与胜利"],
  ["reaction", "激烈语音反应"],
  ["skill", "高水平操作"],
  ["precision", "精准远距离击杀"],
  ["utility", "技能道具配合"],
  ["rescue", "关键救援"],
  ["boss", "Boss或精英敌人"],
  ["funny", "搞笑时刻"],
  ["unexpected", "意外事件"],
];

const GAMES = [
  ["auto", "自动识别"],
  ["rainbow_six_siege", "彩虹六号：围攻"],
  ["valorant", "无畏契约"],
  ["counter_strike_2", "CS2"],
  ["counter_strike_global_offensive", "CS:GO"],
  ["call_of_duty", "使命召唤"],
  ["battlefield", "战地系列"],
  ["delta_force", "三角洲行动"],
  ["apex_legends", "Apex英雄"],
  ["pubg", "绝地求生 PUBG"],
  ["overwatch_2", "守望先锋2"],
  ["escape_from_tarkov", "逃离塔科夫"],
  ["the_finals", "THE FINALS"],
  ["crossfire", "穿越火线"],
  ["halo_infinite", "光环：无限"],
  ["destiny_2", "命运2"],
  ["other_fps", "其他FPS"],
];

const GAME_MODES = [
  ["auto", "自动判断"],
  ["round_based", "回合制竞技"],
  ["battle_royale", "大逃杀"],
  ["team_respawn", "团队重生"],
  ["extraction", "战术撤离"],
  ["objective", "占点/推车/任务目标"],
  ["hostage", "人质模式"],
  ["bomb", "炸弹模式（下包与拆弹）"],
  ["campaign", "单人战役"],
];

const DURATIONS = [
  ["seconds_30", "30秒"],
  ["seconds_60", "60秒"],
  ["seconds_90", "90秒"],
  ["seconds_120", "2分钟"],
];

const SENSITIVITIES = [
  ["strict", "严格"],
  ["balanced", "均衡"],
  ["broad", "宽松"],
];

const SUBTITLE_MODES = [
  ["smart", "智能"],
  ["off", "关闭"],
  ["always", "始终开启"],
];

const SUBTITLE_STYLES = [
  ["gaming", "游戏高亮"],
  ["minimal", "极简"],
  ["neon", "霓虹"],
];

const HIGHLIGHT_LABELS = Object.fromEntries(HIGHLIGHTS);
const HIGHLIGHT_KEYS = HIGHLIGHTS.map(([key]) => key);
const GAME_LABELS = Object.fromEntries(GAMES);
const RAINBOW_SIX_EVENT_LABELS = {
  ace: "ACE",
  four_k: "4K",
  triple_kill: "三杀",
  double_kill: "双杀",
  single_kill: "关键单杀",
  clutch: "残局",
  plant: "下包",
  defuse: "拆弹",
  plant_denial: "阻止下包",
  defuse_denial: "阻止拆弹",
  opening_pick: "首杀突破",
  refrag: "补枪",
  wallbang: "穿墙击杀",
  vertical_play: "垂直打法",
  utility_play: "道具配合",
  teamplay: "团队配合",
  escape: "极限生存",
  funny: "节目效果",
};

const STAGES = [
  ["upload", "上传录像"],
  ["local_analysis", "本地筛选"],
  ["candidate_video", "准备候选"],
  ["qwen_analysis", "千问判断"],
  ["render", "生成成片"],
  ["subtitles", "识别字幕"],
  ["completed", "完成"],
];

const STATE_LABELS = {
  queued: "排队中",
  running: "处理中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

const MAX_JOB_FILES = 8;

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const payload = await response.json();
      message = typeof payload.detail === "string" ? payload.detail : message;
    } catch {
      // Keep the generic message.
    }
    throw new Error(message);
  }
  if (response.status === 204) return null;
  return response.json();
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function pickOption(options, fallback) {
  const allowed = new Set(options.map(([key]) => key));
  return (value) => (allowed.has(value) ? value : fallback);
}

function pickHighlights(allKeys) {
  const allowed = new Set(allKeys);
  return (value) => {
    if (!Array.isArray(value)) return allKeys;
    const filtered = value.filter((item) => allowed.has(item));
    return filtered.length ? filtered : allKeys;
  };
}

function usePersistedState(key, fallback, sanitize) {
  const [value, setValue] = useState(() => {
    try {
      const raw = window.localStorage.getItem(key);
      if (raw === null) return fallback;
      const parsed = JSON.parse(raw);
      return sanitize ? sanitize(parsed) : parsed;
    } catch {
      return fallback;
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Storage can be unavailable in private mode; keeping defaults is fine.
    }
  }, [key, value]);
  return [value, setValue];
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return "—";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.floor(seconds % 60);
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}` : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", { hour12: false });
}

async function uploadInChunks(file, setProgress) {
  const initialized = await fetchJson("/api/uploads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      filename: file.name,
      size: file.size,
      content_type: file.type || "application/octet-stream",
    }),
  });
  let offset = initialized.received_bytes;
  while (offset < file.size) {
    const chunk = file.slice(offset, Math.min(offset + initialized.chunk_size, file.size));
    const status = await fetchJson(`/api/uploads/${initialized.upload_id}/chunk`, {
      method: "PUT",
      headers: {
        "Content-Type": "application/octet-stream",
        "Upload-Offset": String(offset),
      },
      body: chunk,
    });
    offset = status.received_bytes;
    setProgress(offset / file.size);
  }
  return fetchJson(`/api/uploads/${initialized.upload_id}/complete`, { method: "POST" });
}

function fileKey(file) {
  return `${file.name}:${file.size}:${file.lastModified}`;
}

function ToggleGroup({ label, value, options, onChange }) {
  return (
    <div className="field-block">
      <div className="field-label">{label}</div>
      <div className="segmented">
        {options.map(([key, text]) => (
          <button
            type="button"
            key={key}
            className={value === key ? "active" : ""}
            onClick={() => onChange(key)}
          >
            {text}
          </button>
        ))}
      </div>
    </div>
  );
}

function SelectField({ label, value, options, onChange }) {
  return (
    <label className="select-field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map(([key, text]) => <option key={key} value={key}>{text}</option>)}
      </select>
    </label>
  );
}

function App() {
  const fileInput = useRef(null);
  const fileIdRef = useRef(0);
  const filesRef = useRef([]);
  const [files, setFiles] = useState([]);
  const [uploadLabel, setUploadLabel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiHost, setApiHost] = useState(() => window.localStorage.getItem("xunyou_qwen_api_host") || "");
  const [keyStatus, setKeyStatus] = useState("idle");
  const [qwenConfigured, setQwenConfigured] = useState(false);
  const [qwenStatusLoading, setQwenStatusLoading] = useState(true);
  const [showQwenSetup, setShowQwenSetup] = useState(false);
  const [game, setGame] = usePersistedState("xunyou_option_game", "auto", pickOption(GAMES, "auto"));
  const [gameMode, setGameMode] = usePersistedState("xunyou_option_game_mode", "auto", pickOption(GAME_MODES, "auto"));
  const [highlightTypes, setHighlightTypes] = usePersistedState(
    "xunyou_option_highlights",
    HIGHLIGHT_KEYS,
    pickHighlights(HIGHLIGHT_KEYS),
  );
  const [durationMode, setDurationMode] = usePersistedState("xunyou_option_duration", "seconds_60", pickOption(DURATIONS, "seconds_60"));
  const [subtitleMode, setSubtitleMode] = usePersistedState("xunyou_option_subtitle_mode", "smart", pickOption(SUBTITLE_MODES, "smart"));
  const [subtitleStyle, setSubtitleStyle] = usePersistedState("xunyou_option_subtitle_style", "gaming", pickOption(SUBTITLE_STYLES, "gaming"));
  const [sensitivity, setSensitivity] = usePersistedState("xunyou_option_sensitivity", "balanced", pickOption(SENSITIVITIES, "balanced"));
  const [busy, setBusy] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(0);
  const [job, setJob] = useState(null);
  const [history, setHistory] = useState([]);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [sliceTask, setSliceTask] = useState(null);
  const [sliceBusy, setSliceBusy] = useState(false);

  async function refreshHistory() {
    try {
      const jobs = await fetchJson("/api/jobs");
      setHistory(jobs || []);
      return jobs || [];
    } catch {
      return [];
    }
  }

  useEffect(() => {
    filesRef.current = files;
  }, [files]);

  useEffect(
    () => () => {
      filesRef.current.forEach((item) => URL.revokeObjectURL(item.previewUrl));
    },
    [],
  );

  useEffect(() => {
    const value = apiHost.trim();
    if (value) window.localStorage.setItem("xunyou_qwen_api_host", value);
    else window.localStorage.removeItem("xunyou_qwen_api_host");
  }, [apiHost]);

  useEffect(() => {
    fetchJson("/api/qwen/status")
      .then((status) => {
        setQwenConfigured(Boolean(status.configured));
        setShowQwenSetup(!status.configured);
        if (status.error) setError(status.error);
      })
      .catch((statusError) => {
        setShowQwenSetup(true);
        setError(statusError.message);
      })
      .finally(() => setQwenStatusLoading(false));
  }, []);

  useEffect(() => {
    refreshHistory().then((jobs) => {
      const latest = jobs[0];
      if (latest && ["completed", "failed", "cancelled"].includes(latest.state)) {
        setJob(latest);
        if (latest.state === "failed") setError(latest.error || "上一次剪辑没有完成。");
      }
    });
  }, []);

  useEffect(() => {
    if (!job?.job_id || ["completed", "failed", "cancelled"].includes(job.state)) return undefined;
    const timer = window.setInterval(async () => {
      try {
        const next = await fetchJson(`/api/jobs/${job.job_id}`);
        setJob(next);
        setHistory((prev) => {
          const index = prev.findIndex((item) => item.job_id === next.job_id);
          if (index < 0) return [next, ...prev];
          const copy = [...prev];
          copy[index] = next;
          return copy;
        });
        if (["completed", "failed", "cancelled"].includes(next.state)) {
          setBusy(false);
          if (next.state === "failed") setError(next.error || "剪辑失败，请查看提示。");
        }
      } catch (pollError) {
        setError(pollError.message);
        setBusy(false);
      }
    }, 750);
    return () => window.clearInterval(timer);
  }, [job?.job_id, job?.state]);

  const currentStageIndex = useMemo(() => {
    if (busy && !job) return 0;
    const index = STAGES.findIndex(([key]) => key === job?.stage);
    return index < 0 ? 0 : index;
  }, [busy, job]);

  function chooseFiles(candidates) {
    setError("");
    setJob(null);
    const accepted = [];
    const problems = [];
    for (const candidate of candidates || []) {
      if (!candidate) continue;
      const extension = candidate.name.split(".").pop()?.toLowerCase();
      if (!["mp4", "mov", "mkv"].includes(extension)) {
        problems.push(`${candidate.name}：仅支持 MP4、MOV 和 MKV 文件`);
        continue;
      }
      if (candidate.size > 20 * 1024 ** 3) {
        problems.push(`${candidate.name}：单个文件不能超过20GB`);
        continue;
      }
      accepted.push(candidate);
    }
    const existing = new Set(files.map((item) => fileKey(item.file)));
    const additions = accepted
      .filter((candidate) => !existing.has(fileKey(candidate)))
      .map((candidate) => ({
        id: (fileIdRef.current += 1),
        file: candidate,
        previewUrl: URL.createObjectURL(candidate),
        metadata: null,
      }));
    if (!additions.length) {
      if (problems.length) setError(problems.join("；"));
      return;
    }
    let combined = [...files, ...additions];
    if (combined.length > MAX_JOB_FILES) {
      combined.slice(MAX_JOB_FILES).forEach((item) => URL.revokeObjectURL(item.previewUrl));
      combined = combined.slice(0, MAX_JOB_FILES);
      problems.push(`一次最多支持${MAX_JOB_FILES}个录像文件，已忽略多余的文件。`);
    }
    setFiles(combined);
    if (problems.length) setError(problems.join("；"));
    setUploadProgress(0);
  }

  function removeFile(id) {
    const target = files.find((item) => item.id === id);
    if (target) URL.revokeObjectURL(target.previewUrl);
    setFiles((current) => current.filter((item) => item.id !== id));
  }

  function toggleHighlight(key) {
    setHighlightTypes((current) =>
      current.includes(key) ? current.filter((item) => item !== key) : [...current, key],
    );
  }

  async function configureQwen() {
    const key = apiKey.trim();
    const host = apiHost.trim();
    if (!key || !host) {
      setError("请同时填写百炼 API Key 和 API Host。");
      return;
    }
    setError("");
    setKeyStatus("validating");
    try {
      await fetchJson("/api/qwen/configure", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ qwen_api_key: key, qwen_api_host: host }),
      });
      setQwenConfigured(true);
      setShowQwenSetup(false);
      setApiKey("");
      setKeyStatus("valid");
    } catch (configureError) {
      setKeyStatus("invalid");
      setError(configureError.message);
    }
  }

  async function startEditing() {
    if (!files.length || busy) return;
    if (!highlightTypes.length) {
      setError("请至少选择一种高光类型。");
      return;
    }
    if (!qwenConfigured) {
      setShowQwenSetup(true);
      setError("请先完成一次千问配置，所有候选片段都会默认交给千问理解。");
      return;
    }
    setBusy(true);
    setError("");
    setJob(null);
    setUploadProgress(0);
    const totalBytes = files.reduce((sum, item) => sum + item.file.size, 0) || 1;
    const uploadIds = [];
    try {
      let sentBytes = 0;
      for (let index = 0; index < files.length; index += 1) {
        const item = files[index];
        setUploadLabel(`正在上传 ${index + 1}/${files.length}：${item.file.name}`);
        const upload = await uploadInChunks(item.file, (fraction) => {
          setUploadProgress((sentBytes + fraction * item.file.size) / totalBytes);
        });
        sentBytes += item.file.size;
        uploadIds.push(upload.upload_id);
        if (upload.metadata) {
          setFiles((current) =>
            current.map((entry) => (entry.id === item.id ? { ...entry, metadata: upload.metadata } : entry)),
          );
        }
      }
      setUploadLabel("");
      const created = await fetchJson("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          upload_ids: uploadIds,
          options: {
            game,
            game_mode: gameMode,
            highlight_types: highlightTypes,
            duration_mode: durationMode,
            subtitle_mode: subtitleMode,
            subtitle_style: subtitleStyle,
            sensitivity,
          },
        }),
      });
      setJob(created);
    } catch (startError) {
      setError(startError.message);
      setUploadLabel("");
      setBusy(false);
    }
  }

  async function retryEditing() {
    if (!job?.job_id || busy) return;
    if (!qwenConfigured) {
      setShowQwenSetup(true);
      setError("请先重新配置千问，再重试原任务。");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const retried = await fetchJson(`/api/jobs/${job.job_id}/retry`, {
        method: "POST",
      });
      setJob(retried);
    } catch (retryError) {
      setError(retryError.message);
      setBusy(false);
    }
  }

  async function startLocalSlices() {
    if (!files.length || busy || sliceBusy) return;
    setSliceBusy(true);
    setSliceTask(null);
    setError("");
    setUploadProgress(0);
    const totalBytes = files.reduce((sum, item) => sum + item.file.size, 0) || 1;
    const uploadIds = [];
    try {
      let sentBytes = 0;
      for (let index = 0; index < files.length; index += 1) {
        const item = files[index];
        setUploadLabel(`正在上传 ${index + 1}/${files.length}：${item.file.name}`);
        const upload = await uploadInChunks(item.file, (fraction) => {
          setUploadProgress((sentBytes + fraction * item.file.size) / totalBytes);
        });
        sentBytes += item.file.size;
        uploadIds.push(upload.upload_id);
      }
      setUploadLabel("正在生成本地切片…");
      let current = await fetchJson("/api/local-slices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ upload_ids: uploadIds, sensitivity, duration_mode: durationMode }),
      });
      setSliceTask(current);
      while (current.state === "queued" || current.state === "running") {
        await sleep(800);
        current = await fetchJson(`/api/local-slices/${current.task_id}`);
        setSliceTask(current);
      }
      if (current.state === "failed") setError(current.error || "生成本地切片失败。");
    } catch (sliceError) {
      setError(sliceError.message);
    } finally {
      setUploadLabel("");
      setSliceBusy(false);
    }
  }

  async function resetAll() {
    if (job?.job_id && ["completed", "failed", "cancelled"].includes(job.state)) {
      await fetchJson(`/api/jobs/${job.job_id}`, { method: "DELETE" }).catch(() => null);
    }
    files.forEach((item) => URL.revokeObjectURL(item.previewUrl));
    setFiles([]);
    setJob(null);
    setError("");
    setBusy(false);
    setUploadProgress(0);
    setUploadLabel("");
    setKeyStatus("idle");
    if (fileInput.current) fileInput.current.value = "";
    await refreshHistory();
  }

  async function cancelJob(jobId) {
    if (busy) return;
    try {
      await fetchJson(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    } catch (cancelError) {
      setError(cancelError.message);
    }
    await refreshHistory();
  }

  async function deleteHistoryJob(jobId) {
    try {
      await fetchJson(`/api/jobs/${jobId}`, { method: "DELETE" });
      if (job?.job_id === jobId) {
        setJob(null);
        setBusy(false);
        setError("");
      }
    } catch (deleteError) {
      setError(deleteError.message);
    }
    await refreshHistory();
  }

  async function cleanupAll() {
    try {
      await fetchJson("/api/maintenance/cleanup", { method: "POST" });
      setJob(null);
      setBusy(false);
      setError("");
    } catch (cleanupError) {
      setError(cleanupError.message);
    }
    await refreshHistory();
  }

  const displayProgress = job ? 0.12 + job.progress * 0.88 : uploadProgress * 0.12;
  const result = job?.result;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <img src="/r6-pistol-mark.png" alt="" />
          </span>
          <span>瞬游光影</span>
        </div>
        <div className="local-pill"><span /> 本机运行</div>
      </header>

      <main>
        <section className="hero">
          <img
            className="hero-helmet-decor"
            src="/tactical-helmet.jpeg"
            alt=""
            aria-hidden="true"
          />
          <div className="eyebrow">MULTI-GAME FPS HIGHLIGHT AGENT</div>
          <h1>两小时录像，<br /><em>只留最精彩的一刻。</em></h1>
          <div className="hero-metrics">
            <div><strong>1</strong><span>唯一成片</span></div>
            <div><strong>16:9</strong><span>横屏原比例</span></div>
            <div><strong>30–120s</strong><span>多段高光</span></div>
          </div>
        </section>

        <div className="workspace-grid">
          <section className="panel upload-panel">
            <div className="section-heading"><span>01</span><div><h2>选择游戏录像</h2><p>支持 MP4 / MOV / MKV · 最高1080P · 单个最长2小时 · 最多{MAX_JOB_FILES}个文件</p></div></div>
            <input
              ref={fileInput}
              type="file"
              multiple
              accept=".mp4,.mov,.mkv,video/mp4,video/quicktime"
              onChange={(event) => {
                chooseFiles(event.target.files);
                event.target.value = "";
              }}
              hidden
            />
            {files.length === 0 ? (
              <div
                className={`dropzone ${dragging ? "dragging" : ""}`}
                onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
                onDragLeave={() => setDragging(false)}
                onDrop={(event) => { event.preventDefault(); setDragging(false); chooseFiles(event.dataTransfer.files); }}
                onClick={() => fileInput.current?.click()}
                role="button"
                tabIndex={0}
                data-testid="upload-dropzone"
              >
                <div className="upload-icon">↥</div>
                <strong>拖入一个或多个录像，或点击选择</strong>
                <span>多段录像会合成一条高光 · 单文件上限 20GB · 分块上传</span>
              </div>
            ) : (
              <div className="file-list">
                {files.map((item) => (
                  <div className="file-item" key={item.id}>
                    <video src={item.previewUrl} muted preload="metadata" />
                    <div className="file-info">
                      <strong>{item.file.name}</strong>
                      <span>{formatBytes(item.file.size)}{item.metadata ? ` · ${item.metadata.width}×${item.metadata.height} · ${formatDuration(item.metadata.duration)}` : " · 上传后显示分辨率与时长"}</span>
                    </div>
                    {!busy && <button type="button" className="text-button" onClick={() => removeFile(item.id)}>移除</button>}
                  </div>
                ))}
                <div className="file-list-footer">
                  {!busy && files.length < MAX_JOB_FILES && (
                    <button type="button" className="text-button" onClick={() => fileInput.current?.click()}>+ 继续添加</button>
                  )}
                  {!busy && <button type="button" className="text-button" onClick={resetAll}>清空重选</button>}
                  {files.length > 1 && <small>{files.length} 个文件 · 共 {formatBytes(files.reduce((sum, item) => sum + item.file.size, 0))}</small>}
                </div>
              </div>
            )}

          </section>

          <section className="panel settings-panel">
            <div className="section-heading"><span>02</span><div><h2>定义你的高光</h2><p>适用于主流第一人称射击游戏</p></div></div>

            <div className="context-grid">
              <SelectField label="游戏名称" value={game} options={GAMES} onChange={setGame} />
              <SelectField label="游戏模式" value={gameMode} options={GAME_MODES} onChange={setGameMode} />
            </div>
            <div className="context-note">不知道具体类型时保持自动，千问会根据画面、HUD和声音判断。</div>

            <div className="field-block">
              <div className="field-label">高光类型 <small>可多选</small></div>
              <div className="chip-grid">
                {HIGHLIGHTS.map(([key, label]) => (
                  <button type="button" key={key} className={highlightTypes.includes(key) ? "selected" : ""} onClick={() => toggleHighlight(key)}>
                    <span>{highlightTypes.includes(key) ? "✓" : "+"}</span>{label}
                  </button>
                ))}
              </div>
            </div>

            <ToggleGroup label="成片时长" value={durationMode} onChange={setDurationMode} options={DURATIONS} />
            <div className="duration-note">这是最长时长：只保留证据完整的高光，不会用跑图、回放或结算画面强行填满。</div>
            <ToggleGroup label="识别强度" value={sensitivity} onChange={setSensitivity} options={SENSITIVITIES} />
            <div className="duration-note">严格需要至少70分和高置信度；均衡为60分；宽松为50分。三种模式都会先广泛搜索候选。</div>

            <ToggleGroup label="字幕模式" value={subtitleMode} onChange={setSubtitleMode} options={SUBTITLE_MODES} />
            <div className="duration-note subtitle-note">智能模式只为语音反应、交流和对话有价值的高光生成字幕，纯操作成片会自动跳过。</div>
            {subtitleMode !== "off" && <ToggleGroup label="字幕样式" value={subtitleStyle} onChange={setSubtitleStyle} options={SUBTITLE_STYLES} />}

            <div className={`ai-status-row ${qwenConfigured ? "connected" : "missing"}`}>
              <div><span /> <strong>{qwenStatusLoading ? "正在检查千问…" : qwenConfigured ? "千问AI已连接" : "千问尚未配置"}</strong></div>
              {!qwenStatusLoading && <button type="button" onClick={() => { setShowQwenSetup((value) => !value); setKeyStatus("idle"); }}>{showQwenSetup ? "收起" : "重新配置"}</button>}
            </div>

            {showQwenSetup && (
              <div className="qwen-setup-card">
                <strong>首次设置Qwen3-Omni-Flash</strong>
                <small>验证后保存到本机私有配置，主页面不再显示Key。</small>
                <input
                  type="password"
                  value={apiKey}
                  onChange={(event) => { setApiKey(event.target.value); setKeyStatus("idle"); }}
                  placeholder="粘贴新的 sk- 开头API Key"
                  aria-label="百炼 API Key"
                  autoComplete="off"
                />
                <input
                  type="text"
                  value={apiHost}
                  onChange={(event) => { setApiHost(event.target.value); setKeyStatus("idle"); }}
                  placeholder="粘贴完整API Host"
                  aria-label="百炼 API Host"
                  autoComplete="off"
                />
                <button type="button" onClick={configureQwen} disabled={keyStatus === "validating"}>{keyStatus === "validating" ? "正在验证…" : "保存并连接千问"}</button>
                {keyStatus === "invalid" && <small className="setup-error">配置失败，请查看下方提示。</small>}
              </div>
            )}

            <button
              type="button"
              className="primary-button"
              disabled={!files.length || busy || !highlightTypes.length || !qwenConfigured || qwenStatusLoading}
              onClick={startEditing}
              data-testid="start-button"
            >
              {busy ? "正在处理中…" : "开始生成高光成片"}<span>→</span>
            </button>
            <button
              type="button"
              className="secondary-button"
              disabled={!files.length || busy || sliceBusy}
              onClick={startLocalSlices}
              data-testid="local-slice-button"
            >
              {sliceBusy ? "正在生成本地切片…" : "生成本地切片"}
            </button>
          </section>
        </div>

        {(busy || job) && (
          <section className="panel progress-panel" data-testid="progress-panel">
            <div className="progress-header"><div><span className="pulse" /><strong>{job?.message || uploadLabel || `正在快速写入本机：${Math.round(uploadProgress * 100)}%`}</strong></div><div className="progress-actions">{job && ["queued", "running"].includes(job.state) && <button type="button" className="cancel-button" onClick={() => cancelJob(job.job_id)}>取消任务</button>}<b>{Math.min(100, Math.round(displayProgress * 100))}%</b></div></div>
            <div className="progress-track"><i style={{ width: `${Math.max(2, displayProgress * 100)}%` }} /></div>
            <div className="stage-row">
              {STAGES.map(([key, label], index) => <div key={key} className={index <= currentStageIndex ? "reached" : ""}><span>{index < currentStageIndex || job?.state === "completed" ? "✓" : index + 1}</span><small>{label}</small></div>)}
            </div>
          </section>
        )}

        {sliceTask && (
          <section className="panel slice-panel" data-testid="slice-panel">
            <div className="progress-header">
              <div><span className="pulse" /><strong>{sliceTask.message}</strong></div>
              <div className="progress-actions"><b>{Math.min(100, Math.round((sliceTask.progress || 0) * 100))}%</b></div>
            </div>
            {sliceTask.state !== "completed" && (
              <div className="progress-track"><i style={{ width: `${Math.max(2, (sliceTask.progress || 0) * 100)}%` }} /></div>
            )}
            {sliceTask.state === "completed" && (
              <>
                <small className="slice-dir">切片保存在：{sliceTask.directory}</small>
                <div className="slice-list">
                  {sliceTask.slices.map((item) => (
                    <div className="slice-row" key={item.filename}>
                      <strong>{item.filename}</strong>
                      <span>{item.source} · 第 {Math.round(item.start)} 秒起 · {Math.round(item.duration)} 秒 · 本地分 {item.local_score}</span>
                    </div>
                  ))}
                  {!sliceTask.slices.length && <span>没有找到候选片段。</span>}
                </div>
              </>
            )}
          </section>
        )}

        {error && <div className="error-card" role="alert"><strong>处理遇到问题</strong><span>{error}</span>{job?.state === "failed" && /(Qwen|千问|百炼|API Key|API Host)/i.test(job.error || "") && <div className="error-actions"><button type="button" onClick={() => setShowQwenSetup(true)} disabled={busy}>重新配置千问</button><button type="button" onClick={retryEditing} disabled={busy}>重试原任务</button><small>原录像仍在本机，无需重新上传</small></div>}</div>}

        {result && (
          <section className="result-section" data-testid="result-section">
            <div className="result-copy"><span className="success-label">剪辑完成</span><h2>{result.title}</h2><p>{result.reason}</p><div className="result-tags"><span>{result.category_label || HIGHLIGHT_LABELS[result.category] || result.category}</span><span>{result.detected_game || GAME_LABELS[result.game] || "FPS游戏"}</span>{RAINBOW_SIX_EVENT_LABELS[result.rainbow_six?.event_type] && <span>{RAINBOW_SIX_EVENT_LABELS[result.rainbow_six.event_type]}</span>}{!["none", "unknown", undefined].includes(result.rainbow_six?.clutch_situation) && <span>{result.rainbow_six.clutch_situation} 残局</span>}<span>{Math.round(result.duration)}秒</span>{result.segment_count > 1 && <span>{result.segment_count}段高光</span>}{result.file_count > 1 && <span>{result.file_count}个录像合成</span>}{result.transition_count > 0 && <span>斜纹转场</span>}<span>千问精选</span>{result.subtitles?.burned && <span>已烧录字幕</span>}{result.subtitles?.mode === "smart" && !result.subtitles?.burned && <span>智能跳过字幕</span>}</div></div>
            <div className="result-video"><video src={result.video_url} controls preload="metadata" /><div className="result-actions"><a className="download-button" href={result.download_url}>下载 MP4</a><button type="button" onClick={resetAll}>清理并处理新视频</button></div><small>同时保存在：{result.output_path}</small></div>
          </section>
        )}

        {history.length > 0 && (
          <section className="panel history-panel">
            <div className="history-head">
              <div><h2>历史任务</h2><p>可下载已完成成片，或清理本机数据释放空间。</p></div>
              <button type="button" className="cleanup-button" disabled={history.some((item) => ["queued", "running"].includes(item.state))} onClick={cleanupAll}>清理全部历史</button>
            </div>
            <div className="history-list">
              {history.map((item) => (
                <div className="history-row" key={item.job_id}>
                  <div className="history-main">
                    <span className={`state-badge state-${item.state}`}>{STATE_LABELS[item.state] || item.state}</span>
                    <strong>{item.filename || "原片已清理"}</strong>
                    <small>{formatDateTime(item.created_at)}{item.source_size ? ` · ${formatBytes(item.source_size)}` : ""}{item.options ? ` · ${GAME_LABELS[item.options.game] || "FPS"}` : ""}</small>
                  </div>
                  <div className="history-actions">
                    {item.state === "completed" && <a className="history-download" href={`/api/jobs/${item.job_id}/download`}>下载</a>}
                    {["queued", "running"].includes(item.state) ? (
                      <button type="button" onClick={() => cancelJob(item.job_id)}>取消</button>
                    ) : (
                      <button type="button" onClick={() => deleteHistoryJob(item.job_id)}>清理</button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}
      </main>

      <footer><span>瞬游光影 · 通用FPS高光剪辑 Agent</span><span>完整原片留在本机 · 仅上传压缩候选片段</span></footer>
    </div>
  );
}

export default App;
