import { useCallback, useEffect, useState } from "react";
import type { Bridge, InterviewCreate, InterviewInfo } from "../bridge/useBridge";
import { costLabel, engineLabel, modelsLabel, usageTitle } from "../usage";
import Dropdown from "../components/Dropdown";

interface Props {
  bridge: Bridge;
  /** 打开某一场的面试页（/interviews/<id>） */
  onOpen: (id: string) => void;
  onReplay: (id: string) => void;
}

interface Position {
  id: number;
  name: string;
}

interface ResumeRow {
  id: number;
  name: string;
}

interface EngineInfo {
  available: boolean;
  label: string;
  reason: string;
}

type Mode = "real" | "simulate" | null;

function fmtTime(ts?: number | null): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function fmtClock(seconds?: number | null): string {
  if (!seconds) return "00:00";
  const total = Math.max(0, Math.round(seconds));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

const STATUS_LABEL: Record<string, string> = {
  running: "进行中",
  finished: "已结束",
  auto_interrupted: "系统自动中断",
  interrupted: "已中断",
};

export default function InterviewList({ bridge, onOpen, onReplay }: Props) {
  const [items, setItems] = useState<InterviewInfo[]>([]);
  const [positions, setPositions] = useState<Position[]>([]);
  const [resumes, setResumes] = useState<ResumeRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [openMode, setOpenMode] = useState<Mode>(null);
  const [positionId, setPositionId] = useState("");
  const [resumeId, setResumeId] = useState("");
  const [engines, setEngines] = useState<Record<string, EngineInfo>>({});
  const [resumePreview, setResumePreview] = useState("");
  const [starting, setStarting] = useState(false);
  const [pendingEnd, setPendingEnd] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState(false);
  const [endingId, setEndingId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState(false);
  const [outputDevice, setOutputDevice] = useState("");
  const [inputDevice, setInputDevice] = useState("");
  const [monitoring, setMonitoring] = useState<"speaker" | "mic" | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const listResp = await fetch("/api/interviews?limit=100");
      if (listResp.ok) {
        const data = (await listResp.json()) as { items: InterviewInfo[] };
        setItems(data.items ?? []);
      }
    } catch (err) {
      setError(`加载失败：${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    void bridge.loadDevices();
  }, [load, bridge.loadDevices]);

  // 列表每 10 秒 + 窗口重新聚焦时刷新一次：
  // 别的窗口/脚本新建的面试也能及时出现在列表里，不会只在弹窗里看得到。
  useEffect(() => {
    const tick = () => void load();
    const timer = window.setInterval(tick, 10000);
    window.addEventListener("focus", tick);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", tick);
    };
  }, [load]);

  // 设备默认值：环回优先匹配系统默认输出的名字，麦克风用默认输入
  useEffect(() => {
    const devices = bridge.devices;
    if (!devices) return;
    const base = (devices.default_output || "").split(" (")[0].trim().toLowerCase();
    const preferred =
      devices.outputs.find((item) => base && item.name.toLowerCase().includes(base)) ??
      devices.outputs[0];
    setOutputDevice((prev) => prev || preferred?.name || "");
    setInputDevice((prev) => prev || devices.default_input || devices.inputs[0]?.name || "");
  }, [bridge.devices]);

  const closeStart = useCallback(async () => {
    if (monitoring) {
      await bridge.stopMonitor();
      setMonitoring(null);
    }
    setOpenMode(null);
    setError("");
  }, [bridge, monitoring]);

  const toggleMonitor = useCallback(
    async (channel: "speaker" | "mic") => {
      if (monitoring === channel) {
        await bridge.stopMonitor();
        setMonitoring(null);
        return;
      }
      setError("");
      try {
        await bridge.startMonitor(channel, { output_device: outputDevice, input_device: inputDevice });
        setMonitoring(channel);
      } catch (err) {
        setError(`检测失败：${(err as Error).message}`);
        setMonitoring(null);
      }
    },
    [bridge, inputDevice, monitoring, outputDevice],
  );

  const pickResume = useCallback(async (id: string) => {
    setResumeId(id);
    setResumePreview("");
    if (!id) return;
    try {
      const resp = await fetch(`/api/resumes/${id}`);
      if (resp.ok) {
        const data = (await resp.json()) as { content?: string };
        setResumePreview(data.content ?? "");
      }
    } catch {
      /* 忽略 */
    }
  }, []);

  const openStart = useCallback(
    async (mode: Mode) => {
      setOpenMode(mode);
      setError("");
      try {
        const [posResp, resResp, engResp] = await Promise.all([
          fetch("/api/positions"),
          fetch("/api/resumes"),
          fetch("/api/engines"),
        ]);
        let posItems: Position[] = [];
        let resItems: ResumeRow[] = [];
        if (posResp.ok) {
          posItems = ((await posResp.json()) as { items: Position[] }).items ?? [];
          setPositions(posItems);
        }
        if (resResp.ok) {
          resItems = ((await resResp.json()) as { items: ResumeRow[] }).items ?? [];
          setResumes(resItems);
        }
        if (engResp.ok) {
          const data = (await engResp.json()) as {
            engines?: Record<string, EngineInfo>;
          };
          setEngines(data.engines ?? {});
        }
        // 默认带出上次用过、且现在仍然存在的岗位/简历
        const lastPosition = localStorage.getItem("interview-last-position") ?? "";
        if (lastPosition && posItems.some((item) => String(item.id) === lastPosition)) {
          setPositionId(lastPosition);
        }
        const lastResume = localStorage.getItem("interview-last-resume") ?? "";
        if (lastResume && resItems.some((item) => String(item.id) === lastResume)) {
          void pickResume(lastResume);
        }
      } catch {
        /* 忽略，选择框留空 */
      }
    },
    [pickResume],
  );

  const endInterview = useCallback(
    async (id: string) => {
      setPendingEnd(null);
      setEndingId(id);
      setError("");
      try {
        await bridge.stopInterview(id);
        setNotice("本场面试已结束，正在打开详情页");
        onReplay(id);
      } catch (err) {
        setError(`结束失败：${(err as Error).message}`);
      } finally {
        setEndingId(null);
      }
    },
    [bridge, onReplay],
  );

  /** 第一次点击变成「确认结束？」，4 秒内再点一次才真的结束。 */
  const requestEnd = useCallback(
    (id: string) => {
      if (pendingEnd === id) {
        void endInterview(id);
        return;
      }
      setPendingEnd(id);
      window.setTimeout(() => setPendingEnd((prev) => (prev === id ? null : prev)), 4000);
    },
    [endInterview, pendingEnd],
  );

  const start = useCallback(async () => {
    if (!openMode) return;
    const position = positions.find((item) => String(item.id) === positionId);
    if (!position) {
      setError("请先选择面试岗位");
      return;
    }
    if (!resumePreview.trim()) {
      setError("请先选择个人简历");
      return;
    }
    setStarting(true);
    setError("");
    try {
      if (monitoring) {
        await bridge.stopMonitor();
        setMonitoring(null);
      }
      const payload: InterviewCreate = {
        role: position.name,
        resume: resumePreview,
        position_id: position.id,
        position_name: position.name,
        level: "mid",
        // 默认用免费本地；需要更准的英文识别，面试中再手动切云端
        engine: engines.local?.available ? "local" : "auto",
        answer_level: "standard",
        output_device: outputDevice,
        input_device: inputDevice,
        mode: openMode,
        background: "",
      };
      const data = await bridge.createInterview(payload);
      // 记住这次选的岗位/简历，下次打开弹窗自动带出来
      localStorage.setItem("interview-last-position", positionId);
      localStorage.setItem("interview-last-resume", resumeId);
      setOpenMode(null);
      const createdId = data.interview?.id;
      if (!createdId) {
        setError("面试已创建，但没有拿到面试 ID，请刷新列表");
        await load();
        return;
      }
      onOpen(createdId);
    } catch (err) {
      setError(`启动失败：${(err as Error).message}`);
    } finally {
      setStarting(false);
    }
  }, [bridge, engines, inputDevice, load, monitoring, onOpen, openMode, outputDevice, positionId, positions, resumeId, resumePreview]);

  const openById = useCallback(
    async (id: string) => {
      setError("");
      try {
        await bridge.resumeInterview(id);
        onOpen(id);
      } catch (err) {
        setError(`继续失败：${(err as Error).message}`);
        await load();
      }
    },
    [bridge, load, onOpen],
  );

  // 进行中的不能删（要先结束），其余都可以勾选
  const selectableIds = items.filter((item) => item.status !== "running").map((item) => item.id);
  const allSelected = selectableIds.length > 0 && selectableIds.every((id) => selected.has(id));

  const toggleSelect = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleAll = useCallback(
    (checked: boolean) => {
      setSelected(checked ? new Set(selectableIds) : new Set());
    },
    [selectableIds],
  );

  const removeSelected = useCallback(async () => {
    const ids = [...selected];
    if (!ids.length) return;
    if (!pendingDelete) {
      setPendingDelete(true);
      window.setTimeout(() => setPendingDelete(false), 4000);
      return;
    }
    setPendingDelete(false);
    setDeleting(true);
    setError("");
    try {
      const resp = await fetch("/api/interviews/batch-delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids }),
      });
      if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
      const data = (await resp.json()) as {
        deleted: number;
        skipped?: { id: string; reason: string }[];
      };
      const skipped = data.skipped?.length ?? 0;
      setNotice(`已删除 ${data.deleted} 场${skipped ? `，跳过 ${skipped} 场（进行中或不存在）` : ""}`);
      setSelected(new Set());
      await load();
    } catch (err) {
      setError(`批量删除失败：${(err as Error).message}`);
    } finally {
      setDeleting(false);
    }
  }, [load, pendingDelete, selected]);

  return (
    <div className="prompt-page interview-page">
      <header className="prompt-head">
        <div>
          <h1>面试列表</h1>
        </div>
      </header>

      {error && <div className="alert">{error}</div>}
      {notice && <div className="prompt-toast">{notice}</div>}

      <div className={`interview-body ${items.length === 0 && !loading ? "is-empty" : ""}`}>
        <div className="interview-hero">
          <div className="start-actions">
            <button className="btn primary btn-hero" onClick={() => void openStart("real")}>
              🎙 开始面试
            </button>
          </div>
        </div>

        {items.length > 0 && (
          <div className="list-toolbar">
            <label className="check-all">
              <input
                type="checkbox"
                checked={allSelected}
                onChange={(event) => toggleAll(event.target.checked)}
              />
              全选
            </label>
            <span className="muted">
              共 {items.length} 场{selected.size > 0 ? ` · 已选 ${selected.size}` : ""}
            </span>
            <span className="spacer" />
            <button
              className="btn danger"
              disabled={deleting || selected.size === 0}
              onClick={() => void removeSelected()}
            >
              {deleting
                ? "删除中…"
                : pendingDelete
                  ? `确认删除（${selected.size}）？`
                  : `删除选中${selected.size ? `（${selected.size}）` : ""}`}
            </button>
          </div>
        )}

        <div className="log-list">
        {items.map((item) => (
          <div
            key={item.id}
            className={`log-row interview-row ${selected.has(item.id) ? "picked" : ""}`}
          >
            <label
              className="row-check"
              title={item.status === "running" ? "进行中的面试不能删除" : "选择这场面试"}
            >
              <input
                type="checkbox"
                checked={selected.has(item.id)}
                disabled={item.status === "running"}
                onChange={() => toggleSelect(item.id)}
              />
            </label>
            <div className="interview-info">
              <span className="interview-role">{item.role || "未命名面试"}</span>
              <span className="interview-id" title="面试ID">
                ID {item.id}
              </span>
              <span className={`mode-chip ${item.mode === "simulate" ? "simulate" : "real"}`}>
                {item.mode === "simulate" ? "🤖 仿真面试" : "🎙 真实面试"}
              </span>
              <span className={`status-chip ${item.status}`}>
                {STATUS_LABEL[item.status] ?? item.status}
              </span>
              <span className="interview-meta">
                {fmtTime(item.started_at)} · {fmtClock(item.elapsed)} · {item.turns ?? 0} 题 /{" "}
                {item.utterances ?? 0} 句
                {item.score != null ? ` · ${item.score} 分` : ""}
              </span>
              <span className="interview-usage" title={usageTitle(item)}>
                {engineLabel(item.engines && item.engines.length ? item.engines.join("/") : item.engine)} ·{" "}
                {modelsLabel(item.usage)} · {costLabel(item.usage)}
              </span>
            </div>
            <div className="interview-actions">
              {["running", "interrupted"].includes(item.status) && (
                <button className="link-btn" onClick={() => void openById(item.id)}>
                  继续
                </button>
              )}
              {["running", "interrupted"].includes(item.status) && (
                <button
                  className={`link-btn ${pendingEnd === item.id ? "confirm-end" : ""}`}
                  disabled={endingId === item.id}
                  onClick={() => requestEnd(item.id)}
                >
                  {endingId === item.id ? "结束中…" : pendingEnd === item.id ? "确认结束？" : "结束"}
                </button>
              )}
              <button className="link-btn" onClick={() => onReplay(item.id)}>
                详情
              </button>
            </div>
          </div>
        ))}
        {items.length === 0 && !loading && (
          <div className="empty-state">
            <p>还没有面试记录</p>
            <p className="muted">点上面的「开始面试」开一场。</p>
          </div>
        )}
        </div>
      </div>

      {openMode && (
        <div className="report-overlay" onClick={() => void closeStart()}>
          <div className="report-card start-modal" onClick={(event) => event.stopPropagation()}>
            <header className="report-head">
              <h2>开始面试</h2>
              <button className="btn ghost" onClick={() => void closeStart()}>
                关闭
              </button>
            </header>

            {error && <div className="alert">{error}</div>}

            <label className="field">
              <span>面试岗位 *</span>
              <Dropdown
                value={positionId}
                options={[
                  { value: "", label: "请选择岗位…" },
                  ...positions.map((item) => ({ value: String(item.id), label: item.name })),
                ]}
                onChange={(value) => setPositionId(value)}
                placeholder="请选择岗位…"
              />
            </label>

            <label className="field">
              <span>个人简历 *</span>
              <Dropdown
                value={resumeId}
                options={[
                  { value: "", label: "请选择简历…" },
                  ...resumes.map((item) => ({ value: String(item.id), label: item.name })),
                ]}
                onChange={(value) => void pickResume(value)}
                placeholder="请选择简历…"
              />
            </label>

            <div className="device-grid">
              <label className="field">
                <span>扬声器环回（面试官的声音）*</span>
                <Dropdown
                  value={outputDevice}
                  options={(bridge.devices?.outputs ?? []).map((item) => ({
                    value: item.name,
                    label: item.name,
                  }))}
                  onChange={(value) => setOutputDevice(value)}
                  placeholder={
                    (bridge.devices?.outputs ?? []).length === 0
                      ? "没有可用环回设备"
                      : "选择扬声器环回"
                  }
                />
              </label>

              <label className="field">
                <span>麦克风（你的声音）*</span>
                <Dropdown
                  value={inputDevice}
                  options={(bridge.devices?.inputs ?? []).map((item) => ({
                    value: item.name,
                    label: item.name,
                  }))}
                  onChange={(value) => setInputDevice(value)}
                  placeholder={
                    (bridge.devices?.inputs ?? []).length === 0 ? "没有可用麦克风" : "选择麦克风"
                  }
                />
              </label>
            </div>

            <div className="device-test">
              <button
                className={`btn ghost ${monitoring === "speaker" ? "on" : ""}`}
                onClick={() => void toggleMonitor("speaker")}
              >
                {monitoring === "speaker" ? "停止检测" : "测扬声器"}
              </button>
              <button
                className={`btn ghost ${monitoring === "mic" ? "on" : ""}`}
                onClick={() => void toggleMonitor("mic")}
              >
                {monitoring === "mic" ? "停止检测" : "测麦克风"}
              </button>
              {monitoring && (
                <div className="probe-meter">
                  <div className="probe-track">
                    <div
                      className="probe-fill"
                      style={{
                        width: `${Math.min(
                          100,
                          Math.round(
                            (monitoring === "speaker"
                              ? bridge.monitorLevels.interviewer
                              : bridge.monitorLevels.candidate) * 500,
                          ),
                        )}%`,
                      }}
                    />
                  </div>
                  <span className="muted">
                    {(monitoring === "speaker"
                      ? bridge.monitorLevels.interviewer
                      : bridge.monitorLevels.candidate) >= 0.02
                      ? "有声音"
                      : "静音"}
                  </span>
                </div>
              )}
            </div>

            <p className="field-hint">
              面试官的声音是从「扬声器环回」抓的：会议软件输出到哪个扬声器，这里就要选哪个（选错会一句都听不到）。
              开始前可以点「测扬声器」让会议出声、点「测麦克风」说句话，确认有声音再开始。
            </p>

            <div className="setup-foot">
              <span className="muted">
                采集扬声器和麦克风，过程中可随时打开「仿真输入」
              </span>
              <button
                className="btn primary btn-hero"
                disabled={
                  starting ||
                  !positionId ||
                  !resumeId ||
                  (!outputDevice || !inputDevice)
                }
                onClick={() => void start()}
              >
                {starting ? "启动中…" : "开始"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
