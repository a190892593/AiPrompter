import { useCallback, useEffect, useRef, useState } from "react";
import { type Bridge, type InterviewInfo, type QuestionCard } from "../bridge/useBridge";
import { detailPath, navigate } from "../router";
import CallTrace from "../components/CallTrace";
import AnswerColumns from "../components/AnswerColumns";
import IntentStatsBar from "../components/IntentStats";
import JudgeLogList from "../components/JudgeLogList";
import Dropdown from "../components/Dropdown";

function MonitorBar({ label, value }: { label: string; value: number }) {
  const width = Math.min(100, Math.round(value * 400));
  const level = value >= 0.02 ? "有声音" : value > 0 ? "微弱" : "静音";
  return (
    <div className="monitor-bar">
      <span className="monitor-label">{label}</span>
      <div className="meter-track">
        <div className="meter-fill" style={{ width: `${width}%` }} />
      </div>
      <span className={`monitor-state ${value >= 0.02 ? "ok" : ""}`}>
        {level} {value.toFixed(3)}
      </span>
    </div>
  );
}

function formatDateTime(ts: number): string {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 秒 → 00:12:34（不足一小时显示 12:34） */
function formatDuration(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  const pad = (value: number) => String(value).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** 从开始时间戳算到现在多少秒（服务端返回的是秒级时间戳） */
function elapsedFrom(startedAt?: number | null): number {
  if (!startedAt) return 0;
  return Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
}

/** 按 URL 里的面试 ID 取记录摘要（列表接口只给摘要，这里用详情文档自己拼）。 */
interface InterviewLookup {
  info: InterviewInfo | null;
  notFound: boolean;
  offline: boolean;
}

async function fetchInterviewInfo(id: string): Promise<InterviewLookup> {
  try {
    const resp = await fetch(`/api/interviews/${encodeURIComponent(id)}`);
    if (resp.status === 404) return { info: null, notFound: true, offline: false };
    if (!resp.ok) return { info: null, notFound: false, offline: true };
    const doc = (await resp.json()) as Record<string, unknown>;
    const config = (doc.config as Record<string, unknown>) ?? {};
    const devices = (doc.devices as Record<string, unknown>) ?? {};
    const questions = (doc.questions as unknown[]) ?? [];
    const utterances = (doc.utterances as unknown[]) ?? [];
    const started = Number(doc.started_at ?? 0);
    const ended = doc.ended_at ? Number(doc.ended_at) : null;
    return {
      notFound: false,
      offline: false,
      info: {
        id,
        role: String(doc.position_name ?? config.role ?? ""),
        mode: String(doc.mode ?? config.mode ?? "real"),
        status: String(doc.status ?? ""),
        status_label: String(doc.status_label ?? ""),
        started_at: started,
        ended_at: ended,
        elapsed: ended
          ? Math.max(0, Math.round(ended - started))
          : started
            ? Math.max(0, Math.round(Date.now() / 1000 - started))
            : 0,
        turns: questions.length,
        utterances: utterances.length,
        end_reason: String(doc.end_reason ?? ""),
        position_id: doc.position_id ? Number(doc.position_id) : null,
        devices: {
          interviewer: String(devices.interviewer ?? ""),
          candidate: String(devices.candidate ?? ""),
        },
        engine: String(doc.engine ?? ""),
        engines: (doc.engines as string[] | undefined) ?? undefined,
        usage: (doc.usage as InterviewInfo["usage"]) ?? undefined,
        has_report: Boolean(doc.report),
      },
    };
  } catch {
    return { info: null, notFound: false, offline: true };
  }
}

function LevelMeter({ label, value, active }: { label: string; value: number; active: boolean }) {
  const width = Math.min(100, Math.round(value * 320));
  return (
    <div className={`meter ${active ? "meter-on" : ""}`}>
      <span className="meter-label">{label}</span>
      <div className="meter-track">
        <div className="meter-fill" style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

function AnswerCard({
  card,
  onMistake,
  onSuggest,
  onRegenerate,
  selected,
  onToggleSelect,
}: {
  card: QuestionCard;
  onMistake: () => void;
  onSuggest: (raw: string, current: string) => Promise<string[]>;
  onRegenerate: (question: string, raw: string) => Promise<void>;
  selected: boolean;
  onToggleSelect: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(card.summary);
  // 提词器里三版答案随时切换；还没拿到变体时先显示标准版
  const [ideas, setIdeas] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const askSuggest = useCallback(async () => {
    setBusy(true);
    try {
      setIdeas(await onSuggest(card.raw, card.summary));
    } finally {
      setBusy(false);
    }
  }, [card.raw, card.summary, onSuggest]);

  const pick = useCallback(
    async (question: string) => {
      setBusy(true);
      try {
        await onRegenerate(question, card.raw);
        setIdeas([]);
        setEditing(false);
      } finally {
        setBusy(false);
      }
    },
    [card.raw, onRegenerate],
  );

  const applyEdit = useCallback(async () => {
    if (!draft.trim()) return;
    setBusy(true);
    try {
      await onRegenerate(draft.trim(), card.raw);
      setEditing(false);
    } finally {
      setBusy(false);
    }
  }, [card.raw, draft, onRegenerate]);

  return (
    <article className="answer-card">
      <div className="answer-head">
        <span className={`badge ${card.confidence >= 0.8 ? "badge-hot" : ""}`}>
          识别置信度 {Math.round(card.confidence * 100)}%
        </span>
        {card.followup && (
          <span
            className="badge badge-followup"
            title={`追问链第 ${card.depth ?? 2} 层：承接上一问继续深挖`}
          >
            追问{card.depth && card.depth > 1 ? ` · L${card.depth}` : ""}
          </span>
        )}
        {card.rewriting && (
          <span className="badge badge-rewriting" title="第一次没归纳清楚，正在后台重写问题，稍后会自动更新">
            问题整理中…
          </span>
        )}
        {card.rejected && (
          <span className="badge badge-rejected" title="异步复核未通过，这张卡片已撤销，不会生成回答">
            已撤销
          </span>
        )}
        {card.challenge && (
          <span
            className="badge badge-challenge"
            title={`反问链第 ${card.depth ?? 2} 层：先正面回应质疑并给证据，再展开细节`}
          >
            反问{card.depth && card.depth > 1 ? ` · L${card.depth}` : ""}
          </span>
        )}
        {card.focus.map((item) => (
          <span className="chip chip-inline" key={item}>
            {item}
          </span>
        ))}
        {card.degraded && <span className="badge badge-muted">模板模式</span>}
        {!card.done && <span className="badge badge-live">生成中</span>}
        {card.spans && card.spans > 1 && <span className="badge">合并 {card.spans} 句</span>}
        {(card.total_ms || card.duration_ms) ? (
          <span className="badge badge-time">
            {(((card.total_ms || card.duration_ms) as number) / 1000).toFixed(1)}s
          </span>
        ) : null}
        <span className="answer-head-spacer" />
        <label className="pick pick-strong" title="勾选后可批量存入面试题库">
          <input type="checkbox" checked={selected} onChange={onToggleSelect} />
          <span>存入题库</span>
        </label>
      </div>

      {editing ? (
        <div className="edit-box">
          <input
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void applyEdit();
            }}
            placeholder="改成你判断的真正问题…"
          />
          <button className="btn primary" onClick={() => void applyEdit()} disabled={busy}>
            重新生成
          </button>
          <button className="btn ghost" onClick={() => setEditing(false)}>
            取消
          </button>
        </div>
      ) : (
        <div className="answer-title-row">
          <div className="answer-title-left">
            <h2 className="answer-question">{card.summary}</h2>
            <span className="answer-time">{formatDateTime(card.ts)}</span>
          </div>
          <div className="answer-actions-inline">
            <button className="link-btn" onClick={() => void askSuggest()} disabled={busy}>
              {busy ? "思考中…" : "答案不对？"}
            </button>
            <button
              className="link-btn"
              onClick={() => {
                setDraft(card.summary);
                setEditing(true);
              }}
            >
              改问题
            </button>
            <button className="link-btn" onClick={onMistake}>
              意图识别错了？
            </button>
          </div>
        </div>
      )}

      {card.parentQuestion && (
        <p className="answer-chain" title="这条是追问/反问，继承自上一问">
          ↳ 承接：{card.parentQuestion}
        </p>
      )}

      <p className="answer-raw">面试官原话：{card.raw}</p>

      {card.rejected && (
        <div className="alert rejected-tip">
          复核未通过，这张卡片已撤销（不会生成回答）：{card.verifyReason || "问题不成立"}
        </div>
      )}

      {!card.rejected && (
        <AnswerColumns
          answers={card.answers}
          fallback={card.answer}
          modes={card.modes}
          pending={!card.done}
        />
      )}

      {ideas.length > 0 && (
        <div className="ideas">
          <p className="ideas-title">面试官可能是想问：</p>
          {ideas.map((idea) => (
            <button key={idea} className="idea-item" onClick={() => void pick(idea)} disabled={busy}>
              {idea}
            </button>
          ))}
        </div>
      )}

      {card.done && (
        <details className="card-calls">
          <summary>模型调用（意图识别 + 三版答案）</summary>
          <CallTrace trace={card.id} />
        </details>
      )}

      {card.sources && card.sources.length > 0 && (
        <div className="answer-sources">
          <span className="sources-label">来源</span>
          {card.sources.slice(0, 6).map((src, index) => (
            <a
              key={`${src.url ?? index}`}
              className="source-chip"
              href={src.url ?? "#"}
              target="_blank"
              rel="noreferrer"
              title={src.title ?? src.url}
            >
              {index + 1}. {(src.title ?? src.url ?? "").slice(0, 24)}
            </a>
          ))}
        </div>
      )}

    </article>
  );
}

export default function Teleprompter({
  bridge,
  interviewId,
  onEnded,
}: {
  bridge: Bridge;
  /** URL 里的面试 ID：/interviews/<id>，刷新后仍停在这一场 */
  interviewId?: string;
  onEnded: () => void;
}) {
  const [outputDevice, setOutputDevice] = useState("");
  const [inputDevice, setInputDevice] = useState("");
  const [engine, setEngine] = useState("auto");  const [enginesInfo, setEnginesInfo] = useState<
    Record<string, { available: boolean; label: string; reason: string }>
  >({});
  const [selAsk, setSelAsk] = useState<{ text: string; x: number; y: number } | null>(null);
  const [judgeElapsed, setJudgeElapsed] = useState(0);
  const [resuming, setResuming] = useState(false);
  const [askThinking, setAskThinking] = useState(false);
  const [askWeb, setAskWeb] = useState(false);
  const [role, setRole] = useState("后端开发工程师");
  const [level, setLevel] = useState("mid");
  const [background, setBackground] = useState("");
  const [resumeText, setResumeText] = useState(() => localStorage.getItem("interview-resume") || "");
  const [resumes, setResumes] = useState<{ id: number; name: string }[]>([]);
  const [resumeId, setResumeId] = useState("");
  const [runningInterview, setRunningInterview] = useState<InterviewInfo | null>(null);
  const [missing, setMissing] = useState(false);
  const [offline, setOffline] = useState(false);
  const onEndedRef = useRef(onEnded);

  // 用 ref 扛住父组件传下来的回调，避免它换身份时把下面的加载 effect 重新触发
  useEffect(() => {
    onEndedRef.current = onEnded;
  }, [onEnded]);
  const [elapsed, setElapsed] = useState(0);
  const [busy, setBusy] = useState(false);
  const [monitoring, setMonitoring] = useState<"speaker" | "mic" | null>(null);
  const [switching, setSwitching] = useState(false);
  const [switchingEngine, setSwitchingEngine] = useState(false);
  const [simMode, setSimMode] = useState<"bank" | "resume">("resume");
  const [switchingSimMode, setSwitchingSimMode] = useState(false);
  const [fatal, setFatal] = useState("");
  const [askText, setAskText] = useState("");
  const [askBusy, setAskBusy] = useState(false);
  const [audible, setAudible] = useState(true);
  const [positions, setPositions] = useState<{ id: number; name: string }[]>([]);
  const [positionId, setPositionId] = useState("");
  const askAreaRef = useRef<HTMLTextAreaElement | null>(null);
  const [split, setSplit] = useState<number>(() => {
    const saved = Number(localStorage.getItem("live-split"));
    return saved >= 25 && saved <= 75 ? saved : 52;
  });
  const gridRef = useRef<HTMLDivElement | null>(null);
  const draggingRef = useRef(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bankCategory, setBankCategory] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");

  // 顶部提示改成小 toast：3 秒自动消失，不占版面
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 3200);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const promptRef = useRef<HTMLDivElement>(null);

  // 仿真输入开关：会话进行中可随时开启/关闭，其他规则与真实面试完全一致
  const simulated = Boolean(
    bridge.active &&
      (runningInterview?.sim_enabled ??
        bridge.interview?.sim_enabled ??
        runningInterview?.mode === "simulate"),
  );
  const [switchingSim, setSwitchingSim] = useState(false);
  // 本场是真实还是仿真：优先用面试记录里的 mode，其次用会话快照
  const stopLabel = "结束并评分";

  // 服务端说这场结束了（手动或超时自动中断），提词器就别再提示「继续」
  useEffect(() => {
    const info = bridge.interview;
    if (!info) return;
    if (info.status === "running") return;
    // 页面可能开的是另一场：别拿别的场次的状态清掉当前这一场的「继续」入口
    if (runningInterview && info.id !== runningInterview.id) return;
    setRunningInterview(null);
    setElapsed(0);
  }, [bridge.interview, runningInterview]);

  // 面试计时：从记录里的 started_at 算起，刷新页面也不会归零
  useEffect(() => {
    if (!bridge.active) {
      setElapsed(0);
      return;
    }
    const startTs = runningInterview?.started_at ?? bridge.interview?.started_at;
    if (!startTs) return;
    const tick = () => setElapsed(elapsedFrom(startTs));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [bridge.active, bridge.interview?.started_at, runningInterview?.started_at]);

  const startDrag = useCallback((event: React.MouseEvent) => {
    event.preventDefault();
    draggingRef.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  useEffect(() => {
    const move = (event: MouseEvent) => {
      const grid = gridRef.current;
      if (!draggingRef.current || !grid) return;
      const rect = grid.getBoundingClientRect();
      if (rect.width <= 0) return;
      const percent = ((event.clientX - rect.left) / rect.width) * 100;
      setSplit(Math.min(75, Math.max(25, percent)));
    };
    const stop = () => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", stop);
    return () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", stop);
    };
  }, []);

  useEffect(() => {
    localStorage.setItem("live-split", String(Math.round(split)));
  }, [split]);

  useEffect(() => {
    void bridge.loadDevices();
    void (async () => {
      if (interviewId) {
        const found = await fetchInterviewInfo(interviewId);
        setOffline(found.offline);
        setMissing(found.notFound);
        const info = found.info;
        if (!info) return;
        // 已结束的场次：直接进详情页（对话 + 提词 + 报告都在那儿）
        if (info.status === "finished" || info.status === "auto_interrupted") {
          navigate(detailPath(interviewId));
          return;
        }
        // running/中断（可继续）：先恢复已保存的对话和提词
        setRunningInterview(info);
        if (info.position_id != null) setPositionId(String(info.position_id));
        if (info.devices?.interviewer) setOutputDevice(info.devices.interviewer);
        if (info.devices?.candidate) setInputDevice(info.devices.candidate);
        await bridge.hydrate(interviewId);
        return;
      }
      const item = await bridge.fetchRunning();
      setRunningInterview(item);
      if (item?.position_id != null) setPositionId(String(item.position_id));
      // 只有「没结束的面试」才把历史对话/提词恢复出来，结束的让它留在历史里
      if (item) await bridge.hydrate(item.id);
    })();
    fetch("/api/engines")      .then((resp) => (resp.ok ? resp.json() : null))
      .then((data: { engines?: Record<string, { available: boolean; label: string; reason: string }> } | null) => {
        if (!data?.engines) return;
        setEnginesInfo(data.engines);
        setEngine((prev) => {
          if (prev !== "auto") return prev;
          if (data.engines?.volc?.available) return "volc";
          if (data.engines?.local?.available) return "local";
          if (data.engines?.cloud?.available) return "cloud";
          return "mock";
        });
      })
      .catch(() => {
        /* 拿不到就保持 auto */
      });
    fetch("/api/resumes")
      .then((resp) => (resp.ok ? resp.json() : { items: [] }))
      .then((data: { items?: { id: number; name: string }[] }) => setResumes(data.items ?? []))
      .catch(() => {
        /* 没有简历库也能手填 */
      });
    fetch("/api/positions")
      .then((resp) => (resp.ok ? resp.json() : { items: [] }))
      .then((data: { items?: { id: number; name: string }[] }) => setPositions(data.items ?? []))
      .catch(() => {
        /* 没岗位库也能用，保持自定义 */
      });
  }, [bridge.fetchRunning, bridge.hydrate, bridge.loadDevices, interviewId]);

  // 非主动停止（服务端超时/另一窗口结束）时，也直接进详情页
  const prevActiveRef = useRef(bridge.active);
  useEffect(() => {
    const wasActive = prevActiveRef.current;
    prevActiveRef.current = bridge.active;
    if (wasActive && !bridge.active && interviewId) {
      navigate(detailPath(interviewId));
    }
  }, [bridge.active, interviewId]);

  // 服务从断开到重连时，如果页面停在「连不上」，自动重载一次
  const prevConnectedRef = useRef(bridge.connected);
  useEffect(() => {
    const reconnected = !prevConnectedRef.current && bridge.connected;
    prevConnectedRef.current = bridge.connected;
    if (reconnected && offline && interviewId) window.location.reload();
  }, [bridge.connected, interviewId, offline]);

  // 没在面试时每 10 秒跟服务端对一次：别的窗口／进程把这场结束了，横幅要及时消失
  useEffect(() => {
    if (bridge.active) return;
    const tick = async () => {
      const item = await bridge.fetchRunning();
      if (interviewId) {
        // URL 已经指定了某一场：只关心它，别的场次在进行也不打扰本页
        if (item?.id === interviewId) setRunningInterview(item);
        return;
      }
      setRunningInterview(item);
    };
    const timer = window.setInterval(() => void tick(), 10000);
    const onFocus = () => void tick();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [bridge.active, bridge.fetchRunning, interviewId]);

  const askFromText = useCallback(
    async (text: string) => {
      const content = text.trim();
      if (!content) return;
      if (!bridge.active) {
        setNotice("先开始面试或仿真，再手动提词");
        return;
      }
      try {
        await bridge.generateAnswer(content, content);
        setNotice("已发送到提词器，正在生成回答");
      } catch (error) {
        setNotice(`提词失败：${(error as Error).message}`);
      }
    },
    [bridge],
  );

  // 停止面试后不再自动跳走：留在本页看这场的结果（报告在下面，返回列表由用户自己点）

  // 意图识别中实时计时，直观看到这一判花了多久
  useEffect(() => {
    if (!bridge.judging) {
      setJudgeElapsed(0);
      return;
    }
    const started = Date.now();
    const timer = window.setInterval(() => setJudgeElapsed(Math.round((Date.now() - started) / 100) / 10), 100);
    return () => window.clearInterval(timer);
  }, [bridge.judging]);

  // 输入框随内容行数自动变高（最高 110px，再多就滚动）
  useEffect(() => {
    const node = askAreaRef.current;
    if (!node) return;
    node.style.height = "auto";
    node.style.height = `${Math.min(node.scrollHeight, 110)}px`;
  }, [askText]);

  // 选中任意一段文字后，浮出"发送到提词器"按钮
  useEffect(() => {
    const onUp = () => {
      window.setTimeout(() => {
        const selection = window.getSelection();
        const text = selection?.toString().trim() ?? "";
        if (!text || text.length < 2 || !selection || selection.rangeCount === 0) {
          setSelAsk(null);
          return;
        }
        const rect = selection.getRangeAt(0).getBoundingClientRect();
        setSelAsk({ text, x: rect.left + rect.width / 2, y: rect.top });
      }, 0);
    };
    document.addEventListener("mouseup", onUp);
    return () => document.removeEventListener("mouseup", onUp);
  }, []);

  const pickResume = useCallback(async (id: string) => {
    setResumeId(id);
    if (!id) return;
    try {
      const resp = await fetch(`/api/resumes/${id}`);
      if (!resp.ok) return;
      const data = (await resp.json()) as { content?: string };
      setResumeText(data.content ?? "");
    } catch {
      /* 忽略 */
    }
  }, []);

  const pickPosition = useCallback(async (id: string) => {
    setPositionId(id);
    if (!id) return;
    try {
      const resp = await fetch(`/api/positions/${id}/context`);
      if (!resp.ok) return;
      const data = (await resp.json()) as { name: string; background: string };
      setRole(data.name);
      setBackground(data.background);
    } catch {
      /* 忽略 */
    }
  }, []);

  // 有新问题进来（识别到的 / 手工提问的）一定滚到最底部，最新内容永远在下面
  useEffect(() => {
    const node = promptRef.current;
    if (node) node.scrollTo({ top: node.scrollHeight, behavior: "smooth" });
  }, [bridge.questions.length]);

  // 回答流式增长时，只有用户还在底部附近才跟随，避免打断往回翻
  useEffect(() => {
    const node = promptRef.current;
    if (!node) return;
    const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 160;
    if (nearBottom) node.scrollTop = node.scrollHeight;
  }, [bridge.questions]);

  useEffect(() => {
    if (!bridge.active) {
      setElapsed(0);
    }
  }, [bridge.active]);

  useEffect(() => {
    const devices = bridge.devices;
    if (!devices) return;
    const outputs = devices.outputs ?? [];
    // default_output 是普通输出名（不带 [Loopback]），这里优先找同名环回设备
    const preferredOutput =
      outputs.find((item) => item.name === devices.default_output) ||
      outputs.find((item) => devices.default_output && item.name.includes(devices.default_output)) ||
      outputs[0];
    setOutputDevice((prev) => prev || preferredOutput?.name || "");
    setInputDevice((prev) => prev || devices.default_input || devices.inputs[0]?.name || "");
  }, [bridge.devices]);

  // 会话实际在用的设备（刷新后从面试记录里带回来）
  useEffect(() => {
    if (bridge.devicesInUse.interviewer) setOutputDevice(bridge.devicesInUse.interviewer);
    if (bridge.devicesInUse.candidate) setInputDevice(bridge.devicesInUse.candidate);
  }, [bridge.devicesInUse.interviewer, bridge.devicesInUse.candidate]);

  // 引擎下拉显示服务端实际在用的引擎（local / volc / cloud）
  useEffect(() => {
    if (bridge.engine) setEngine(bridge.engine);
  }, [bridge.engine]);

  // 仿真提问模式跟随服务端（题库 / 简历+JD）
  useEffect(() => {
    const mode = runningInterview?.sim_mode || bridge.interview?.sim_mode;
    if (mode === "bank" || mode === "resume") setSimMode(mode);
  }, [runningInterview?.sim_mode, bridge.interview?.sim_mode]);

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [bridge.utterances]);


  const ensurePosition = useCallback((): boolean => {
    if (positionId) return true;
    setFatal("请先在上方选择面试岗位，再开始面试（岗位在左侧菜单「岗位管理」里维护）");
    return false;
  }, [positionId]);

  const start = useCallback(async () => {
    if (!ensurePosition()) return;
    if (!resumeText.trim()) {
      setFatal("请先填写个人简历：它是新建面试的必填项，回答会参考里面的经历");
      return;
    }
    setFatal("");
    setBusy(true);
    try {
      if (monitoring) {
        await bridge.stopMonitor();
        setMonitoring(null);
      }
      localStorage.setItem("interview-resume", resumeText);
      const data = await bridge.createInterview({
        role,
        resume: resumeText,
        position_id: positionId ? Number(positionId) : null,
        position_name: positions.find((item) => String(item.id) === positionId)?.name || role,
        level,
        engine,
        answer_level: "standard",
        output_device: outputDevice,
        input_device: inputDevice,
        mode: "real",
        background,
      });
      setRunningInterview((data.interview as InterviewInfo) ?? null);
      setNotice("面试已开始，计时中。结束请点「停止面试」");
    } catch (error) {
      setFatal((error as Error).message || "启动失败");
    } finally {
      setBusy(false);
    }
  }, [
    background,
    bridge,
    engine,
    ensurePosition,
    inputDevice,
    level,
    monitoring,
    outputDevice,
    positionId,
    positions,
    resumeText,
    role,
  ]);

  const stopInterview = useCallback(async () => {
    setFatal("");
    setBusy(true);
    const stopId = runningInterview?.id ?? interviewId ?? bridge.interview?.id;
    try {
      await bridge.stopInterview(runningInterview?.id);
      setRunningInterview(null);
      setElapsed(0);
      setNotice("本场面试已结束，正在打开详情页");
      // 结束后直接进这场的详情页（完整对话 + 三版提词 + 评分报告）
      if (stopId) navigate(detailPath(stopId));
    } catch (error) {
      setFatal((error as Error).message || "停止失败");
    } finally {
      setBusy(false);
    }
  }, [bridge, interviewId, runningInterview]);

  const resumeInterview = useCallback(async () => {
    if (!runningInterview) return;
    setFatal("");
    setResuming(true);
    try {
      await bridge.resumeInterview(runningInterview.id);
      setNotice("已回到本场面试，之前的对话和提词都在");
    } catch (error) {
      setNotice(`继续失败：${(error as Error).message}`);
    } finally {
      setResuming(false);
    }
  }, [bridge, runningInterview]);

  const toggleMonitor = useCallback(
    async (channel: "speaker" | "mic") => {
      if (monitoring === channel) {
        await bridge.stopMonitor();
        setMonitoring(null);
        return;
      }
      setFatal("");
      try {
        await bridge.startMonitor(channel, { output_device: outputDevice, input_device: inputDevice });
        setMonitoring(channel);
      } catch (error) {
        setFatal(`检测失败：${(error as Error).message}`);
        setMonitoring(null);
      }
    },
    [bridge, inputDevice, monitoring, outputDevice],
  );

  // 面试进行中也能换设备：服务端只重启采集线程，不重开会话
  const changeDevice = useCallback(
    async (channel: "speaker" | "mic", name: string) => {
      if (channel === "speaker") setOutputDevice(name);
      else setInputDevice(name);
      if (!bridge.active) return;
      setSwitching(true);
      setFatal("");
      try {
        await bridge.switchDevices({
          // 只传这次改的通道，另一个通道让服务端沿用当前设备
          output_device: channel === "speaker" ? name : "",
          input_device: channel === "mic" ? name : "",
        });
        setNotice(channel === "speaker" ? "已切换扬声器环回" : "已切换麦克风");
      } catch (error) {
        setFatal(`切换设备失败：${(error as Error).message}`);
        // 失败就退回服务端当前在用的设备
        if (channel === "speaker") setOutputDevice(bridge.devicesInUse.interviewer);
        else setInputDevice(bridge.devicesInUse.candidate);
      } finally {
        setSwitching(false);
      }
    },
    [bridge, inputDevice, outputDevice],
  );

  // 面试中切换识别引擎：本地免费 ↔ 云端（火山/Whisper）
  const changeEngine = useCallback(
    async (next: string) => {
      const prev = engine;
      setEngine(next);
      if (!bridge.active) return;
      setSwitchingEngine(true);
      setFatal("");
      try {
        const data = await bridge.switchEngine(next);
        const actual = String((data as { engine?: string }).engine ?? next);
        setEngine(actual);
        const label =
          actual === "local"
            ? "本地（免费）"
            : actual === "volc"
              ? "火山豆包（计费）"
              : actual === "deepseek"
                ? "DeepSeek 官方 ASR（计费）"
                : actual;
        setNotice(`已切换识别引擎：${label}`);
      } catch (error) {
        setEngine(prev);
        setFatal(`切换引擎失败：${(error as Error).message}`);
      } finally {
        setSwitchingEngine(false);
      }
    },
    [bridge, engine],
  );

  // 仿真提问模式：题库随机 / 简历+岗位职责生成
  const changeSimMode = useCallback(
    async (next: "bank" | "resume") => {
      const prev = simMode;
      setSimMode(next);
      setSwitchingSimMode(true);
      setFatal("");
      try {
        await bridge.setSimMode(next);
        setNotice(next === "bank" ? "提问已切换到：题库随机" : "提问已切换到：简历 + 追问");
      } catch (error) {
        setSimMode(prev);
        setFatal(`切换提问模式失败：${(error as Error).message}`);
      } finally {
        setSwitchingSimMode(false);
      }
    },
    [bridge, simMode],
  );

  // 仿真输入总开关：开=自动出题+TTS 输入+回答回放；关=真实会议采集
  const changeSim = useCallback(
    async (enabled: boolean) => {
      setFatal("");
      setSwitchingSim(true);
      try {
        const data = await bridge.setSim({ enabled, audible, mode: simMode });
        const output = bridge.devicesInUse.interviewer || "当前扬声器环回";
        setNotice(
          enabled
            ? `仿真输入已开启：自动出题${data.count ? ` ${data.count} 道` : ""}，${
                audible ? `声音播到「${output}」` : "当前静音播放"
              }`
            : "仿真输入已关闭：恢复真实会议声音和麦克风采集",
        );
      } catch (error) {
        setFatal(`切换仿真输入失败：${(error as Error).message}`);
      } finally {
        setSwitchingSim(false);
      }
    },
    [audible, bridge, simMode],
  );

  const toggleSelect = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const saveSelected = useCallback(async () => {
    const cards = bridge.questions.filter((card) => selected.has(card.id) && card.answer.trim());
    if (!cards.length) {
      setNotice("选中的条目还没有生成回答，先等一下再存");
      return;
    }
    setSaving(true);
    setNotice("");
    try {
      const existing = new Set<string>();
      const listResp = await fetch("/api/bank");
      if (listResp.ok) {
        const data = (await listResp.json()) as { items: { question: string }[] };
        data.items.forEach((item) => existing.add(item.question.trim()));
      }

      let added = 0;
      let skipped = 0;
      for (const card of cards) {
        const question = card.summary.trim();
        if (existing.has(question)) {
          skipped += 1;
          continue;
        }
        const resp = await fetch("/api/bank", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            category:
              positions.find((item) => String(item.id) === (bankCategory || positionId))?.name ||
              role.trim() ||
              "提词器收藏",
            question,
            answer: (card.answers?.[card.level] ?? card.answer) || "",
          }),
        });
        if (resp.ok) added += 1;
        else skipped += 1;
      }
      setSelected(new Set());
      setNotice(`已存入题库 ${added} 条${skipped ? `，跳过 ${skipped} 条（重复或无回答）` : ""}`);
    } catch (error) {
      setNotice(`存入失败：${(error as Error).message}`);
    } finally {
      setSaving(false);
    }
  }, [bankCategory, bridge.questions, positionId, positions, role, selected]);

  const sendAsk = useCallback(async () => {
    const question = askText.trim();
    if (!question) return;
    setFatal("");
    setAskBusy(true);
    try {
      await bridge.generateAnswer(question, question);
      setAskText("");
    } catch (error) {
      setFatal((error as Error).message || "生成失败");
    } finally {
      setAskBusy(false);
    }
  }, [askText, bridge]);

  // 同一时间只允许一场面试：URL 指向的这一场没在跑，别把另一场的实时状态画进来
  const liveMismatch = Boolean(
    interviewId && bridge.active && bridge.interview?.id && bridge.interview.id !== interviewId,
  );

  if (interviewId && offline) {
    return (
      <div className="console">
        <div className="alert">
          本地服务暂时连不上（Failed to fetch）。这通常是服务正在重启；恢复后页面会自动重载。
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="btn primary" onClick={() => window.location.reload()}>
            立即重试
          </button>
          <button className="btn ghost" onClick={onEnded}>
            返回面试列表
          </button>
        </div>
      </div>
    );
  }

  if (interviewId && liveMismatch) {
    const liveId = bridge.interview?.id ?? "";
    return (
      <div className="console">
        <div className="alert">
          当前进行中的是另一场面试（{bridge.interview?.role || liveId}），本页对应的这场没有在采集。
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button
            className="btn primary"
            onClick={() => window.location.assign(`/interviews/${liveId}`)}
          >
            打开进行中的面试
          </button>
          <button className="btn ghost" onClick={onEnded}>
            返回面试列表
          </button>
        </div>
      </div>
    );
  }

  if (interviewId && missing) {
    return (
      <div className="console">
        <div className="alert">
          找不到这场面试（ID: {interviewId}）。可能已被删除，或链接不完整。
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="btn primary" onClick={() => window.location.reload()}>
            刷新重试
          </button>
          <button className="btn ghost" onClick={onEnded}>
            返回面试列表
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="console">

      {(fatal || bridge.error) && <div className="alert">{fatal || bridge.error}</div>}
      {notice && <div className="prompt-toast">{notice}</div>}
      {bridge.note && <div className="prompt-toast">{bridge.note}</div>}
      {selAsk && (
        <button
          className="sel-ask"
          style={{ left: selAsk.x, top: selAsk.y }}
          onMouseDown={(event) => event.preventDefault()}
          onClick={() => {
            void askFromText(selAsk.text);
            setSelAsk(null);
            window.getSelection()?.removeAllRanges();
          }}
        >
          → 提词器
        </button>
      )}

      {monitoring && (
        <div className="test-strip">
          {monitoring === "speaker" ? (
            <MonitorBar label="扬声器输出" value={bridge.monitorLevels.interviewer} />
          ) : (
            <MonitorBar label="麦克风输入" value={bridge.monitorLevels.candidate} />
          )}
          <span className="test-tip">
            实时检测中，再点一次停止。测扬声器时让会议/音乐出声；测麦克风时对着话筒说话
          </span>
        </div>
      )}

      {!bridge.active && runningInterview && (
        <div
          className={`resume-bar ${
            runningInterview.status === "interrupted" ? "interview-paused" : "interview-running"
          }`}
        >
          <span className="resume-text">
            {runningInterview.status === "interrupted" ? (
              <>
                上次{runningInterview.mode === "simulate" ? "仿真面试" : "真实面试"}中断了（没手动停止，服务重启过）：<b>{runningInterview.role || "未命名岗位"}</b> · 已进行{" "}
                {formatDuration(runningInterview.elapsed ?? 0)}
              </>
            ) : (
              <>
                有一场{runningInterview.mode === "simulate" ? "仿真面试" : "真实面试"}还没结束：<b>{runningInterview.role || "未命名岗位"}</b> · 已进行{" "}
                {formatDuration(elapsedFrom(runningInterview.started_at))}
              </>
            )}
          </span>
          <button className="btn primary" disabled={resuming} onClick={() => void resumeInterview()}>
            {resuming ? "恢复中…" : "继续这场面试"}
          </button>
          <button className="link-btn" disabled={busy} onClick={() => void stopInterview()}>
            结束并评分
          </button>
        </div>
      )}

      {!bridge.active && !runningInterview && !interviewId && (
        <section className="interview-setup">
          <div className="setup-head">
            <h3>新建面试</h3>
            <span className="setup-hint">
              岗位和个人简历是必填项。创建后立即开始计时，只有点「停止面试」才算本场结束。
            </span>
          </div>

          <div className="setup-grid">
            <label>
              面试岗位 <span className="req">*</span>
              <select
                value={positionId}
                onChange={(event) => void pickPosition(event.target.value)}
                title="岗位在左侧菜单「岗位管理」里维护"
              >
                <option value="">请选择岗位…</option>
                {positions.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>

            <label>
              面试级别
              <select value={level} onChange={(event) => setLevel(event.target.value)}>
                <option value="junior">初级</option>
                <option value="mid">中级</option>
                <option value="senior">高级</option>
              </select>
            </label>

          </div>

          <label className="setup-resume">
            个人简历 <span className="req">*</span>
            <select
              value={resumeId}
              onChange={(event) => void pickResume(event.target.value)}
              title="简历在左侧菜单「简历管理」里维护"
            >
              <option value="">请选择简历…</option>
              {resumes.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
            {resumeText.trim() ? (
              <pre className="resume-preview">{resumeText.trim().slice(0, 600)}</pre>
            ) : (
              <p className="muted">还没有选择简历。去「简历管理」新增一份，或先在下面手填。</p>
            )}
            {!resumeText.trim() && (
              <textarea
                rows={4}
                value={resumeText}
                onChange={(event) => setResumeText(event.target.value)}
                placeholder="临时填写简历要点（不保存到简历库）"
              />
            )}
          </label>

          <div className="setup-foot">
            <span className="muted">
              {resumeText.trim() ? `简历 ${resumeText.trim().length} 字` : "还没填简历"}
            </span>
            <button
              className="btn primary btn-hero"
              disabled={busy || !positionId || !resumeText.trim()}
              title={
                !positionId
                  ? "请先选择岗位"
                  : !resumeText.trim()
                    ? "请先填写个人简历"
                    : "创建本场面试并开始计时"
              }
              onClick={() => void start()}
            >
              {busy ? "创建中…" : "开始面试"}
            </button>
          </div>
        </section>
      )}

      <div className="live-grid" ref={gridRef}>
        <section className="panel" style={{ flexBasis: `${split}%` }}>
          <header className="panel-head">
            {interviewId && (
              <button className="link-btn back-link" onClick={onEnded} title="返回面试列表">
                ← 列表
              </button>
            )}
            <h3>对话流</h3>
            <span className={`dot ${bridge.active ? "dot-live" : ""}`} />
            <span className="panel-meta">
              {bridge.active
                ? bridge.engine
                : runningInterview
                  ? runningInterview.status === "interrupted"
                    ? "已中断（可继续）"
                    : "未开始"
                  : bridge.connected
                    ? "未开始"
                    : "服务未连接"}
              {bridge.dropped > 0 ? ` · 丢帧 ${bridge.dropped}` : ""}
            </span>
          </header>

          <div className="stream" ref={scrollRef}>
            {bridge.utterances.length === 0 && (
              <p className="empty">
                {bridge.active
                  ? "已开始采集，等待声音…"
                  : interviewId
                    ? "这场面试目前没有在采集，点上方「继续这场面试」恢复。"
                    : "先在上方「新建面试」里选岗位、填简历并开始，这里会实时显示双方说话内容"}
              </p>
            )}
            {bridge.utterances.map((item) => (
              <div key={item.id} className={`bubble-row ${item.channel}`}>
                <span className={`avatar ${item.channel}`}>
                  {item.channel === "interviewer" ? "官" : "候"}
                </span>
                <div className="bubble">
                  <span className="bubble-who">
                    {item.channel === "interviewer" ? "面试官" : "候选人"}
                    <span className="bubble-time">{formatDateTime(item.ts)}</span>
                  </span>
                  <p>{item.text}</p>
                  <div className="bubble-actions">
                    <button
                      className="link-btn"
                      title="用这句话生成参考回答；选中其中一段则只用选中的部分"
                      onClick={(event) => {
                        const selection = window.getSelection();
                        const selected = selection?.toString().trim() ?? "";
                        const inside =
                          selection &&
                          selection.anchorNode &&
                          event.currentTarget.closest(".bubble-row")?.contains(selection.anchorNode);
                        void askFromText(inside && selected ? selected : item.text);
                      }}
                    >
                      → 提词器
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>

      {(!interviewId || bridge.active) && (
      <div className="control-bar">
        <div className="io-row">
          <LevelMeter label="面试官" value={bridge.levels.interviewer} active={bridge.active} />
          <span className="cb-field">
            扬声器环回
            <Dropdown
              value={outputDevice}
              options={(bridge.devices?.outputs ?? []).map((item) => ({
                value: item.name,
                label: item.name,
              }))}
              onChange={(value) => void changeDevice("speaker", value)}
              disabled={switching}
              title={bridge.active ? "面试中也能切换，会短暂重启采集" : "选择扬声器环回设备"}
              placeholder={
                (bridge.devices?.outputs ?? []).length === 0 ? "没有可用输出设备" : "请选择扬声器环回"
              }
            />
          </span>

          <button
            className={`btn ghost ${monitoring === "speaker" ? "active-test" : ""}`}
            onClick={() => void toggleMonitor("speaker")}
            disabled={bridge.active || switching}
            title={bridge.active ? "面试进行中不能检测，请先结束本场" : "检测会临时占用设备，测完点停止"}
          >
            {monitoring === "speaker" ? "停止检测" : "检测扬声器"}
          </button>
        </div>

        <div className="io-row">
          <LevelMeter label="候选人" value={bridge.levels.candidate} active={bridge.active} />
          <span className="cb-field">
            麦克风
            <Dropdown
              value={inputDevice}
              options={(bridge.devices?.inputs ?? []).map((item) => ({
                value: item.name,
                label: item.name,
              }))}
              onChange={(value) => void changeDevice("mic", value)}
              disabled={switching}
              title={bridge.active ? "面试中也能切换，会短暂重启采集" : "选择麦克风设备"}
              placeholder={(bridge.devices?.inputs ?? []).length === 0 ? "没有可用麦克风" : "请选择麦克风"}
            />
          </span>

          <button
            className={`btn ghost ${monitoring === "mic" ? "active-test" : ""}`}
            onClick={() => void toggleMonitor("mic")}
            disabled={bridge.active || switching}
            title={bridge.active ? "面试进行中不能检测，请先结束本场" : "检测会临时占用设备，测完点停止"}
          >
            {monitoring === "mic" ? "停止检测" : "检测麦克风"}
          </button>
        </div>

        <span className="cb-field">
          引擎
          <Dropdown
            value={engine}
            options={[
              { value: "auto", label: "自动（优先本地）" },
              ...Object.entries(enginesInfo)
                .filter(([key, info]) => info.available && key !== "mock")
                .map(([key, info]) => ({
                  value: key,
                  label: info.label,
                  hint: key === "local" ? "免费" : "计费",
                })),
            ]}
            onChange={(value) => void changeEngine(value)}
            disabled={switchingEngine}
            title="面试中也能切换；本地免费，云端按量计费"
            minWidth={240}
          />
        </span>

        <label
          className={`sim-toggle ${simulated ? "on" : ""} ${!bridge.active ? "locked" : ""}`}
          title="开启后：自动出题 + 用 TTS 把面试官问题播进系统 + 把参考答案当作你的回答播回；关闭后：采集真实会议声音和你的麦克风。两种情况下判题和话术规则完全一致"
        >
          <span className="sim-toggle-label">仿真输入</span>
          <input
            type="checkbox"
            checked={simulated}
            disabled={!bridge.active || switchingSim}
            onChange={(event) => void changeSim(event.target.checked)}
          />
          <span className="sim-toggle-track" aria-hidden="true" />
          <span className="sim-toggle-state">{simulated ? "开" : "关"}</span>
        </label>

        {simulated && (
          <label>
            提问
            <select
              value={simMode}
              onChange={(event) => void changeSimMode(event.target.value as "bank" | "resume")}
              disabled={switchingSimMode}
              title="仿真提问来源：题库随机 / 简历 + 追问"
            >
              <option value="bank">题库随机</option>
              <option value="resume">简历 + 追问</option>
            </select>
          </label>
        )}

        <label
          className={`sim-toggle ${audible && simulated ? "on" : ""} ${!simulated ? "locked" : ""}`}
          title={
            simulated
              ? "打开后能听到面试官提问和候选人回答的声音"
              : "真实面试时不可开启：扬声器的声音会被麦克风收回去，产生回音和重复转写"
          }
        >
          <span className="sim-toggle-label">声音</span>
          <input
            type="checkbox"
            checked={audible && simulated}
            disabled={!simulated}
            onChange={(event) => {
              const next = event.target.checked;
              setAudible(next);
              void bridge.setAudible(next);
            }}
          />
          <span className="sim-toggle-track" aria-hidden="true" />
          <span className="sim-toggle-state">{audible && simulated ? "开" : "关"}</span>
        </label>

        <button
          className="btn glass"
          disabled={bridge.active && !simulated}
          title={
            bridge.active && !simulated
              ? "真实面试时不要试听，扬声器声音会被麦克风收回去"
              : "合成一句话并播放，检查仿真会不会出声"
          }
          onClick={async () => {
            setNotice("正在试听…");
            try {
              const resp = await fetch("/api/tts/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
              const data = (await resp.json()) as { ok?: boolean; synth_ok?: boolean; synth_bytes?: number; error?: string; played?: boolean };
              if (!data.synth_ok) setNotice(`合成失败：${data.error ?? "未知原因"}`);
              else if (!data.played) setNotice(`合成成功（${data.synth_bytes} 字节），但播放失败：检查系统默认输出设备`);
              else setNotice(`试听成功（${data.synth_bytes} 字节），如果没听到请检查系统音量/输出设备`);
            } catch (error) {
              setNotice(`试听失败：${(error as Error).message}`);
            }
          }}
        >
          试听
        </button>

        <span className="spacer" />

        {bridge.active ? (
          <div className="interview-timer">
            <span className="live-dot" />
            <span className="interview-timer-label">面试进行中</span>
            <span className="interview-timer-value">{formatDuration(elapsed)}</span>
            <button
              className="btn danger stop-score"
              onClick={() => void stopInterview()}
              disabled={busy}
              title="结束本场面试并生成评分报告"
            >
              {busy ? "结束中…" : stopLabel}
            </button>
          </div>
        ) : (
          <span className="muted control-hint">在上方「新建面试」里填写岗位和简历后开始</span>
        )}
      </div>
      )}
        </section>

        <div
          className="split-handle"
          onMouseDown={startDrag}
          title="拖动调整左右宽度"
        />

        <section className="panel teleprompter" style={{ flexBasis: `${100 - split}%` }}>
          <header className="panel-head">
            <h3>提词器</h3>
          </header>

          <IntentStatsBar stats={bridge.intentStats} />

          {(bridge.judging || bridge.judgeLog.length > 0) && (
            <div className="judge-strip">
              <div className="judge-now">
                {bridge.judging ? (
                  <>
                    <span className="judge-dot live" />
                    <span className="judge-label">意图识别中</span>
                    <span className={`judge-timer ${judgeElapsed >= 10 ? "slow" : ""}`}>
                      {judgeElapsed}s{judgeElapsed >= 10 ? "（网络较慢，20 秒会自动兜底）" : ""}
                    </span>
                    <span className="judge-raw">「{bridge.judging.raw.slice(0, 30)}」</span>
                  </>
                ) : (
                  (() => {
                    const last = bridge.judgeLog[0];
                    return (
                      <>
                        <span className={`judge-dot ${last.accepted ? "ok" : "skip"}`} />
                        <span className="judge-label">{last.accepted ? "已出卡片" : "不回答"}</span>
                        <span className="judge-kind">{last.kind}</span>
                        <span className="judge-conf">{Math.round(last.confidence * 100)}%</span>
                        <span className="judge-timer">{last.duration_ms}ms</span>
                        {last.timed_out && <span className="judge-timeout">超时兜底</span>}
                        <span className="judge-raw">
                          「{last.raw.slice(0, 24)}」
                          {last.skip_reason ? ` · ${last.skip_reason}` : ""}
                        </span>
                      </>
                    );
                  })()
                )}
              </div>

              <JudgeLogList entries={bridge.judgeLog} />
            </div>
          )}

          <div className="teleprompter-body" ref={promptRef}>
            {bridge.questions.length === 0 ? (
              <div className="empty-state">
                <p>{bridge.active ? "还没有识别到提问。" : "面试官一开始提问，这里会自动出「问题」和「参考回答」。"}</p>
                <p className="muted">先分别测试扬声器和麦克风，确认有声音后再开始面试。</p>
              </div>
            ) : (
              bridge.questions.map((card) => (
                <div
                  key={card.id}
                  className={`prompt-item ${card.id === bridge.activeId ? "active" : ""}`}
                >
                  <AnswerCard
                    card={card}
                    onSuggest={(rawText, current) => bridge.suggestQuestions(rawText, current)}
                    onRegenerate={async (nextQuestion, rawText) => {
                      await bridge.generateAnswer(nextQuestion, rawText);
                    }}
                    selected={selected.has(card.id)}
                    onToggleSelect={() => toggleSelect(card.id)}
                    onMistake={() =>
                      void bridge.reportMistake({
                        last_text: card.raw,
                        predicted_type: "question",
                        correct_type: "statement",
                        note: "用户标记：这不是需要回答的问题",
                      })
                    }
                  />
                </div>
              ))
            )}
          </div>
          {selected.size > 0 && (
            <div className="bulk-bar">
              <span className="bulk-count">已选 {selected.size} 条</span>
              <select
                className="bulk-select"
                value={bankCategory || positionId}
                onChange={(event) => setBankCategory(event.target.value)}
                title="存入哪个岗位的题库"
              >
                {positions.length === 0 && <option value="">（没有岗位，存到「提词器收藏」）</option>}
                {positions.map((item) => (
                  <option key={item.id} value={String(item.id)}>
                    {item.name}
                  </option>
                ))}
              </select>
              <button className="btn primary" onClick={() => void saveSelected()} disabled={saving}>
                {saving ? "保存中…" : "存入题库"}
              </button>
              <button className="link-btn" onClick={() => setSelected(new Set())}>
                取消选择
              </button>
            </div>
          )}

          {bridge.active && (
          <div className="composer">
            <textarea
              ref={askAreaRef}
              rows={1}
              value={askText}
              onChange={(event) => setAskText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void sendAsk();
                }
              }}
              placeholder="输入要问的问题，回车生成参考回答（Shift+Enter 换行）"
            />
            <div className="composer-bar">
              <button
                className={`mode-chip ${askThinking ? "on" : ""}`}
                onClick={() => setAskThinking((v) => !v)}
                title="调用 DeepSeek 原生 thinking：先思考再回答"
              >
                🧠 深度思考
              </button>
              <button
                className={`mode-chip ${askWeb ? "on" : ""}`}
                onClick={() => setAskWeb((v) => !v)}
                title="调用 DeepSeek 原生联网搜索，回答带引用"
              >
                🌐 智能搜索
              </button>
              <span className="composer-spacer" />
              <button
                className="send-round"
                onClick={() => void sendAsk()}
                disabled={askBusy || !askText.trim()}
                title="发送（Enter）"
              >
                {askBusy ? "…" : "↑"}
              </button>
            </div>
          </div>
          )}
          {bridge.active && (
            <p className="composer-hint">内容由 AI 生成，请仔细甄别</p>
          )}

        </section>
      </div>
    </div>
  );
}
