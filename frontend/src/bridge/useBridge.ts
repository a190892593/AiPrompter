/** 本地桥前端状态机：WebSocket 只收事件，音频全部由 Python 进程处理。 */

import { useCallback, useEffect, useRef, useState } from "react";

export interface DeviceInfo {
  id: string;
  name: string;
  rate: number;
  channels: number;
}

export interface DevicesPayload {
  outputs: DeviceInfo[];
  inputs: DeviceInfo[];
  default_output: string;
  default_input: string;
}

export interface Utterance {
  id: string;
  channel: "interviewer" | "candidate";
  text: string;
  final: boolean;
  ts: number;
}

export interface QuestionCard {
  id: string;
  summary: string;
  focus: string[];
  confidence: number;
  raw: string;
  answer: string;
  reasoning?: string;
  sources?: { title?: string; url?: string; cited_text?: string }[];
  duration_ms?: number;
  total_ms?: number;
  answers?: Record<string, string> | null;
  level: string;
  done: boolean;
  degraded: boolean;
  ts: number;
  spans?: number;
  reason?: string;
  followup?: boolean;
  challenge?: boolean;
  prevId?: string | null;
  parentId?: string | null;
  rootId?: string | null;
  depth?: number;
  parentQuestion?: string;
  rejected?: boolean;
  verifyReason?: string;
  rewriting?: boolean;
  modes?: Record<string, { thinking?: boolean; web?: boolean }>;
}

export interface JudgeEntry {
  raw: string;
  kind: string;
  confidence: number;
  question: string;
  focus: string[];
  spans: number;
  reason: string;
  accepted: boolean;
  skip_reason: string;
  trace: string;
  duration_ms: number;
  timed_out?: boolean;
  ts: number;
}

export interface Report {
  overall_score: number;
  verdict: string;
  dimensions: { name: string; score: number; comment: string }[];
  per_question: { index: number; score: number; comment: string }[];
  strengths: string[];
  gaps: string[];
  action_plan: string[];
  degraded?: boolean;
}

export interface StartConfig {
  role: string;
  level: string;
  background: string;
  engine: string;
  answer_level: string;
  output_device: string;
  input_device: string;
}

/** 一场面试的模型用量与估算费用。 */
export interface UsageInfo {
  models?: string[];
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  cost?: number | null;
  currency?: string;
  priced?: boolean;
}

/** 一场面试（一条记录）。 */
export interface InterviewInfo {
  id: string;
  role: string;
  status: "running" | "finished" | "auto_interrupted" | string;
  status_label?: string;
  started_at: number;
  ended_at?: number | null;
  elapsed?: number;
  mode?: string;
  sim_enabled?: boolean;
  sim_mode?: string;
  turns?: number;
  utterances?: number;
  score?: number | null;
  end_reason?: string;
  alive?: boolean;
  position_id?: number | null;
  devices?: { interviewer?: string; candidate?: string };
  engine?: string;
  engines?: string[];
  usage?: UsageInfo;
  has_report?: boolean;
}

export interface InterviewCreate {
  role: string;
  resume: string;
  position_id?: number | null;
  position_name?: string;
  level: string;
  engine: string;
  answer_level: string;
  output_device: string;
  input_device: string;
  mode: "real" | "simulate";
  background?: string;
}

export function useBridge() {
  const [connected, setConnected] = useState(false);
  const [active, setActive] = useState(false);
  const [engine, setEngine] = useState("");
  const [devices, setDevices] = useState<DevicesPayload | null>(null);
  const [devicesInUse, setDevicesInUse] = useState<{ interviewer: string; candidate: string }>({
    interviewer: "",
    candidate: "",
  });
  const [levels, setLevels] = useState({ interviewer: 0, candidate: 0 });
  const [note, setNote] = useState("");
  const [judging, setJudging] = useState<{ raw: string; ts: number } | null>(null);
  const [lastSaved, setLastSaved] = useState<{ id: string; at: string; turns: number; utterances: number } | null>(null);
  const [judgeLog, setJudgeLog] = useState<JudgeEntry[]>([]);
  const [intentStats, setIntentStats] = useState<Record<string, number>>({});
  const [dropped, setDropped] = useState(0);
  const [utterances, setUtterances] = useState<Utterance[]>([]);
  const [questions, setQuestions] = useState<QuestionCard[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const [monitorLevels, setMonitorLevels] = useState({ interviewer: 0, candidate: 0 });
  const [error, setError] = useState("");
  const [interview, setInterview] = useState<InterviewInfo | null>(null);

  const socketRef = useRef<WebSocket | null>(null);
  const seenRef = useRef<Set<string>>(new Set());

  /** 开新面试 / 继续旧面试时，清掉上一场残留在界面上的东西：
   *  对话、卡片、报告、意图识别记录，以及"这句转写见过了"的去重集合。
   *  不清的话，新面试会带着上一场的意图识别记录，重复的短句还会被误判成重复丢掉。 */
  const resetSessionScopedState = useCallback(() => {
    setUtterances([]);
    setQuestions([]);
    setReport(null);
    setJudging(null);
    setJudgeLog([]);
    setIntentStats({});
    seenRef.current = new Set();
  }, []);

  const handleEvent = useCallback((event: Record<string, unknown>) => {
    const type = event.type as string;

    if (type === "session") {
      setActive(Boolean(event.active));
      setEngine(String(event.engine ?? ""));
      if (event.interview) setInterview(event.interview as InterviewInfo);
      return;
    }
    if (type === "interview_stopped" || type === "interview_auto_stopped") {
      setActive(false);
      setLevels({ interviewer: 0, candidate: 0 });
      if (event.interview) setInterview(event.interview as InterviewInfo);
      else setInterview(null);
      if (type === "interview_auto_stopped") {
        setError(String(event.message ?? "本场面试已因超时自动中断"));
      }
      return;
    }
    if (type === "status") {
      const lv = (event.levels as { interviewer: number; candidate: number }) ?? { interviewer: 0, candidate: 0 };
      setLevels({ interviewer: lv.interviewer ?? 0, candidate: lv.candidate ?? 0 });
      setDropped(Number(event.dropped ?? 0));
      return;
    }
    if (type === "partial") {
      const channel = event.channel as "interviewer" | "candidate";
      const value = String(event.text ?? "").trim();
      setUtterances((prev) => {
        const id = `live-${channel}`;
        // ASR 线程晚到的结果会把已定稿的那句话再回放一遍。这时如果还建"实时"气泡，
        // 后续重复定稿已被服务端去重、不会再发 final，这个气泡就永远清不掉了。
        const lastFinal = [...prev].reverse().find((item) => item.channel === channel && item.final);
        if (lastFinal && value && (lastFinal.text === value || lastFinal.text.startsWith(value))) {
          return prev.filter((item) => item.id !== id);
        }
        const rest = prev.filter((item) => item.id !== id);
        return [...rest, { id, channel, text: value, final: false, ts: Date.now() }];
      });
      return;
    }
    if (type === "final") {
      const channel = event.channel as "interviewer" | "candidate";
      const text = String(event.text ?? "");
      const key = `${channel}|${text}`;
      if (seenRef.current.has(key)) return;
      seenRef.current.add(key);
      setUtterances((prev) => {
        const rest = prev.filter((item) => item.id !== `live-${channel}`);
        const duplicated = rest
          .slice(-3)
          .some((item) => item.channel === channel && item.final && item.text === text && Date.now() - item.ts < 3000);
        if (duplicated) return rest;
        return [...rest, { id: `u-${Date.now()}-${channel}`, channel, text, final: true, ts: Date.now() }];
      });
      return;
    }
    if (type === "final_update") {
      const channel = event.channel as "interviewer" | "candidate";
      const text = String(event.text ?? "");
      setUtterances((prev) => {
        const rest = prev.filter((item) => item.id !== `live-${channel}`);
        for (let i = rest.length - 1; i >= 0; i -= 1) {
          if (rest[i].channel === channel && rest[i].final) {
            const updated = [...rest];
            updated[i] = { ...updated[i], text };
            return updated;
          }
        }
        return [...rest, { id: `u-${Date.now()}-${channel}`, channel, text, final: true, ts: Date.now() }];
      });
      return;
    }
    if (type === "question_updated") {
      const id = String(event.id ?? "");
      const summary = String(event.summary ?? "");
      if (id && summary) {
        setQuestions((prev) =>
          prev.map((q) => (q.id === id ? { ...q, summary, rewriting: false } : q)),
        );
      }
      return;
    }
    if (type === "verify_result") {
      const id = String(event.id ?? "");
      const reason = String(event.reason ?? "");
      if (id) {
        setQuestions((prev) =>
          prev.map((q) => (q.id === id ? { ...q, verifyReason: reason } : q)),
        );
      }
      return;
    }
    if (type === "question_rejected") {
      const id = String(event.id ?? "");
      const reason = String(event.reason ?? "");
      if (id) {
        setQuestions((prev) =>
          prev.map((q) => (q.id === id ? { ...q, rejected: true, verifyReason: reason } : q)),
        );
      }
      return;
    }
    if (type === "question") {
      const card: QuestionCard = {
        id: String(event.id),
        summary: String(event.summary ?? ""),
        focus: (event.focus as string[]) ?? [],
        confidence: Number(event.confidence ?? 0),
        raw: String(event.raw ?? ""),
        answer: "",
        reasoning: "",
        answers: null,
        level: "standard",
        done: false,
        degraded: Boolean(event.degraded),
        ts: Date.now(),
        spans: Number(event.spans ?? 1),
        reason: String(event.reason ?? ""),
        followup: Boolean(event.followup),
        challenge: Boolean(event.challenge),
        prevId: (event.prev_id as string | null) ?? null,
        parentId: (event.parent_id as string | null) ?? null,
        rootId: (event.root_id as string | null) ?? null,
        depth: Number(event.depth ?? 1),
        parentQuestion: String(event.parent_question ?? ""),
        rewriting: Boolean(event.rewriting),
      };
      setQuestions((prev) => [...prev, card].slice(-30));
      setActiveId(card.id);
      return;
    }
    if (type === "answer_start") {
      const id = String(event.id);
      setActiveId(id);
      setQuestions((prev) =>
        prev.map((card) =>
            card.id === id
              ? { ...card, answer: "", reasoning: "", done: false, level: String(event.level ?? card.level) }
              : card,
          ),
      );
      return;
    }
    if (type === "answer_sources") {
      const id = String(event.id);
      const items = (event.items as { title?: string; url?: string }[]) ?? [];
      setQuestions((prev) => prev.map((card) => (card.id === id ? { ...card, sources: items } : card)));
      return;
    }
    if (type === "answer_reasoning") {
      const id = String(event.id);
      const delta = String(event.text ?? "");
      setQuestions((prev) =>
        prev.map((card) => (card.id === id ? { ...card, reasoning: (card.reasoning ?? "") + delta } : card)),
      );
      return;
    }
    if (type === "answer_delta") {
      const id = String(event.id);
      const delta = String(event.text ?? "");
      setQuestions((prev) => prev.map((card) => (card.id === id ? { ...card, answer: card.answer + delta } : card)));
      return;
    }
    if (type === "answer_reset") {
      const id = String(event.id);
      setQuestions((prev) => prev.map((card) => (card.id === id ? { ...card, answer: "" } : card)));
      return;
    }
    if (type === "answer_done") {
      const id = String(event.id);
      setQuestions((prev) =>
        prev.map((card) =>
          card.id === id
            ? {
                ...card,
                answer: String(event.text ?? card.answer),
                done: true,
                degraded: Boolean(event.degraded),
                modes: (event.modes as Record<string, { thinking?: boolean; web?: boolean }> | undefined) ?? card.modes,
                duration_ms: Number(event.duration_ms ?? card.duration_ms ?? 0),
              }
            : card,
        ),
      );
      return;
    }
    if (type === "answer_variants") {
      const id = String(event.id);
      const answers = (event.answers as Record<string, string>) ?? {};
      const totalMs = Number(event.duration_ms ?? 0);
      const modes = (event.modes as Record<string, { thinking?: boolean; web?: boolean }>) ?? {};
      setQuestions((prev) =>
        prev.map((card) =>
          card.id === id ? { ...card, answers, total_ms: totalMs, modes } : card,
        ),
      );
      return;
    }
    if (type === "judge_start") {
      setJudging({ raw: String(event.raw ?? ""), ts: Date.now() });
      return;
    }
    if (type === "intent_stats") {
      const stats = (event.stats as Record<string, number>) ?? {};
      setIntentStats(stats);
      return;
    }
    if (type === "judge_result") {
      setJudging(null);
      const entry: JudgeEntry = {
        raw: String(event.raw ?? ""),
        kind: String(event.kind ?? ""),
        confidence: Number(event.confidence ?? 0),
        question: String(event.question ?? ""),
        focus: (event.focus as string[]) ?? [],
        spans: Number(event.spans ?? 1),
        reason: String(event.reason ?? ""),
        accepted: Boolean(event.accepted),
        skip_reason: String(event.skip_reason ?? ""),
        trace: String(event.trace ?? ""),
        duration_ms: Number(event.duration_ms ?? 0),
        timed_out: Boolean(event.timed_out),
        ts: Number(event.ts ?? Date.now() / 1000),
      };
      setJudgeLog((prev) => [entry, ...prev].slice(0, 8));
      return;
    }
    if (type === "note") {
      const message = String(event.message ?? "");
      // 仿真逐题提示不弹 toast（保留后端其它提示）
      if (/^仿真第\s*\d+\s*问$/.test(message)) return;
      setNote(message);
      window.setTimeout(() => setNote(""), 6000);
      return;
    }
    if (type === "monitor") {
      const lv = (event.levels as { interviewer: number; candidate: number }) ?? {
        interviewer: 0,
        candidate: 0,
      };
      setMonitorLevels({ interviewer: lv.interviewer ?? 0, candidate: lv.candidate ?? 0 });
      return;
    }
    if (type === "stopped") {
      setActive(false);
      return;
    }
    if (type === "error") {
      setError(String(event.message ?? "未知错误"));
    }
  }, []);

  useEffect(() => {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    let socket: WebSocket | null = null;
    let disposed = false;
    let retry = 0;
    let retryTimer: number | null = null;

    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(`${protocol}://${location.host}/ws/events`);
      socketRef.current = socket;

      socket.onopen = () => {
        retry = 0;
        setConnected(true);
        // 服务端连上后会立刻推一条 session 快照，界面对齐状态靠它
      };
      socket.onclose = () => {
        setConnected(false);
        if (disposed) return;
        // 指数退避重连（0.5s → 5s），服务端重启后页面能自己接回来
        const delay = Math.min(5000, 500 * 2 ** retry++);
        retryTimer = window.setTimeout(connect, delay);
      };
      socket.onerror = () => {
        socket?.close();
      };
      socket.onmessage = (message) => {
        try {
          handleEvent(JSON.parse(message.data as string));
        } catch {
          /* 忽略非法帧 */
        }
      };
    };

    connect();

    const timer = window.setInterval(() => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "ping" }));
      }
    }, 20000);

    return () => {
      disposed = true;
      window.clearInterval(timer);
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, [handleEvent]);

  const loadDevices = useCallback(async () => {
    try {
      const resp = await fetch("/api/devices");
      if (!resp.ok) throw new Error(await resp.text());
      setDevices((await resp.json()) as DevicesPayload);
      setError("");
    } catch (err) {
      setError(`读取音频设备失败：${(err as Error).message}`);
    }
  }, []);

  // 服务重连后重新拉一次设备，避免“Failed to fetch”一直挂在页面上
  useEffect(() => {
    if (connected) void loadDevices();
  }, [connected, loadDevices]);

  const start = useCallback(async (config: StartConfig) => {
    setError("");
    const resp = await fetch("/api/session/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    if (!resp.ok) {
      let detail = await resp.text();
      try {
        detail = JSON.parse(detail).detail ?? detail;
      } catch {
        /* 保留原文 */
      }
      throw new Error(detail);
    }
    const data = await resp.json();
    setActive(true);
    setEngine(String(data.engine ?? ""));
    if (data.devices) setDevicesInUse(data.devices);
    resetSessionScopedState();
  }, [resetSessionScopedState]);

  const stop = useCallback(async () => {
    await fetch("/api/session/stop", { method: "POST" });
    setActive(false);
    setLevels({ interviewer: 0, candidate: 0 });
  }, []);

  /** 面试进行中切换扬声器环回/麦克风：服务端只重启采集线程，不重开会话。 */
  const switchDevices = useCallback(
    async (config: { output_device: string; input_device: string }) => {
      const resp = await fetch("/api/session/devices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      if (!resp.ok) {
        let detail = await resp.text();
        try {
          detail = JSON.parse(detail).detail ?? detail;
        } catch {
          /* 保留原文 */
        }
        throw new Error(detail);
      }
      const data = (await resp.json()) as {
        devices?: { interviewer: string; candidate: string };
      };
      if (data.devices) setDevicesInUse(data.devices);
      return data;
    },
    [],
  );

  const finish = useCallback(async (): Promise<Report> => {
    const resp = await fetch("/api/session/finish", { method: "POST" });
    if (!resp.ok) throw new Error(`评分失败：${(await resp.text()).slice(0, 200)}`);
    const data = (await resp.json()) as Report;
    setReport(data);
    return data;
  }, []);

  /** 指定某张卡片切换版本（每张卡片各自独立） */
  const setLevelFor = useCallback((id: string, level: string) => {
    setQuestions((prev) => prev.map((card) => (card.id === id ? { ...card, level } : card)));
  }, []);

  /** 实时电平检测：只采集、不识别，用来确认设备选对了。 */
  const startMonitor = useCallback(
    async (channel: "speaker" | "mic", cfg: { output_device: string; input_device: string }) => {
      const resp = await fetch("/api/devices/monitor", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ channel, ...cfg }),
      });
      if (!resp.ok) {
        let detail = await resp.text();
        try {
          detail = JSON.parse(detail).detail ?? detail;
        } catch {
          /* 保留原文 */
        }
        throw new Error(detail);
      }
    },
    [],
  );

  const stopMonitor = useCallback(async () => {
    await fetch("/api/devices/monitor/stop", { method: "POST" });
    setMonitorLevels({ interviewer: 0, candidate: 0 });
  }, []);

  /** 猜面试官可能真正想问的 5 个问题。 */
  const suggestQuestions = useCallback(async (raw: string, current: string): Promise<string[]> => {
    const resp = await fetch("/api/answer/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ raw, current }),
    });
    if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
    const data = (await resp.json()) as { questions?: string[] };
    return data.questions ?? [];
  }, []);

  /** 人工指定/修改问题，重新生成参考答案。 */
  const generateAnswer = useCallback(
    async (question: string, raw: string, options?: { thinking?: boolean; web_search?: boolean }) => {
    const resp = await fetch("/api/answer/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        raw,
        thinking: options?.thinking,
        web_search: Boolean(options?.web_search),
      }),
    });
    if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
    return resp.json();
    },
    [],
  );

  /** 刷新页面后从 SQLite 恢复对话流、提词卡片与报告。传 id 时只恢复那一场。 */
  const hydrate = useCallback(async (id?: string) => {
    try {
      const resp = await fetch(id ? `/api/interviews/${id}` : "/api/session/latest");
      if (!resp.ok) return;
      const doc = (await resp.json()) as Record<string, unknown>;
      if (!doc || !doc.id) return;
      setLastSaved({
        id: String(doc.id),
        at: String(doc.created_at ?? ""),
        turns: ((doc.questions as unknown[]) ?? []).length,
        utterances: ((doc.utterances as unknown[]) ?? []).length,
      });

      const rawQuestions = (doc.questions as Array<Record<string, unknown>>) ?? [];
      const cards: QuestionCard[] = rawQuestions
        .slice()
        .reverse()
        .map((item) => ({
          id: String(item.id),
          summary: String(item.question ?? ""),
          focus: (item.focus as string[]) ?? [],
          confidence: 1,
          raw: String(item.raw ?? ""),
          answer: String(item.answer ?? ""),
          level: String(item.level ?? "standard"),
        answers: (item.answers as Record<string, string> | null) ?? null,
        reasoning: String(item.reasoning ?? ""),
        duration_ms: Number(item.duration_ms ?? 0),
        total_ms: Number(item.total_ms ?? 0),
        sources: (item.sources as { title?: string; url?: string }[] | undefined) ?? [],
          done: Boolean(item.answer),
          degraded: false,
          ts: Number(item.ts ?? 0) * 1000,
          modes: (item.modes as Record<string, { thinking?: boolean; web?: boolean }> | undefined) ?? undefined,
        }));

      const rawUtterances = (doc.utterances as Array<Record<string, unknown>>) ?? [];
      const lines: Utterance[] = rawUtterances.map((item) => ({
        id: String(item.id ?? Math.random().toString(36).slice(2)),
        channel: (item.channel === "candidate" ? "candidate" : "interviewer") as "interviewer" | "candidate",
        text: String(item.text ?? ""),
        final: true,
        ts: Number(item.ts ?? 0) * 1000,
      }));

      lines.forEach((item) => seenRef.current.add(`${item.channel}|${item.text}`));
      setUtterances(lines);
      setQuestions(cards);
      setActiveId(cards[0]?.id ?? null);
      if (doc.report) setReport(doc.report as Report);
      const savedStats = doc.intent_stats as Record<string, number> | undefined;
      if (savedStats) setIntentStats(savedStats);
      const savedJudgeLog = doc.judge_log as JudgeEntry[] | undefined;
      if (Array.isArray(savedJudgeLog)) {
        // 记录里是按时间正序存的，界面按最新在前展示
        setJudgeLog(savedJudgeLog.slice().reverse());
      }
    } catch {
      /* 没有历史记录，忽略 */
    }
  }, []);

  /** 新建一场面试：岗位 + 简历必填，建记录即开始计时。 */
  const createInterview = useCallback(async (config: InterviewCreate) => {
    setError("");
    const resp = await fetch("/api/interviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    if (!resp.ok) {
      let detail = await resp.text();
      try {
        detail = JSON.parse(detail).detail ?? detail;
      } catch {
        /* 保留原文 */
      }
      throw new Error(detail);
    }
    const data = await resp.json();
    setActive(true);
    setEngine(String(data.engine ?? ""));
    if (data.devices) setDevicesInUse(data.devices);
    resetSessionScopedState();
    if (data.interview) setInterview(data.interview as InterviewInfo);
    return data as { interview?: InterviewInfo };
  }, [resetSessionScopedState]);

  /** 停止面试：结束计时、停止采集并生成评分。可指定某一场（页面刚打开时 state 里可能还没有）。 */
  const stopInterview = useCallback(async (targetId?: string): Promise<{
    report?: Report | null;
    interview?: InterviewInfo;
  }> => {
    const id = targetId ?? interview?.id;
    if (!id) {
      await fetch("/api/session/stop", { method: "POST" });
      setActive(false);
      return {};
    }
    const resp = await fetch(`/api/interviews/${id}/stop`, { method: "POST" });
    if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
    const data = (await resp.json()) as {
      report?: Report | null;
      interview?: InterviewInfo;
    };
    setActive(false);
    setLevels({ interviewer: 0, candidate: 0 });
    if (data.interview) setInterview(data.interview);
    if (data.report) setReport(data.report);
    return data;
  }, [interview]);

  /** 服务端当前还在进行中的面试（用于「继续这场面试」）。 */
  const fetchRunning = useCallback(async (): Promise<InterviewInfo | null> => {
    try {
      const resp = await fetch("/api/interviews/running");
      if (!resp.ok) return null;
      const data = (await resp.json()) as { item?: InterviewInfo | null };
      return data.item ?? null;
    } catch {
      return null;
    }
  }, []);

  /** 重新进入一场没结束的面试：恢复采集 + 对话流 + 提词。 */
  const resumeInterview = useCallback(
    async (id: string) => {
      const resp = await fetch(`/api/interviews/${id}/resume`, { method: "POST" });
      if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
      const data = await resp.json();
      setActive(true);
      if (data.interview) setInterview(data.interview as InterviewInfo);
      resetSessionScopedState();
      await hydrate(id);
      return data;
    },
    [hydrate, resetSessionScopedState],
  );

  /** 仿真唯一入口：{enabled} 开关 / {audible} 改声音 / {mode} 改提问来源。 */
  const setSim = useCallback(async (options: {
    enabled?: boolean;
    audible?: boolean;
    mode?: "bank" | "resume";
  }) => {
    const resp = await fetch("/api/session/sim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // 兼容旧后端：enabled 必填；局部更新（改声音/模式）在仿真已开时等价于 enabled=true
      body: JSON.stringify({ ...options, enabled: options.enabled ?? true }),
    });
    if (!resp.ok) {
      let detail = await resp.text();
      try {
        detail = JSON.parse(detail).detail ?? detail;
      } catch {
        /* 保留原文 */
      }
      throw new Error(detail);
    }
    const data = await resp.json();
    if (data.interview) setInterview(data.interview as InterviewInfo);
    return data as { ok?: boolean; enabled?: boolean; count?: number };
  }, []);

  /** 仿真声音开关（走统一入口）。 */
  const setAudible = useCallback(async (enabled: boolean) => {
    await setSim({ audible: enabled });
  }, [setSim]);

  /** 切换仿真提问模式：题库随机 / 简历 + 岗位职责生成（走统一入口）。 */
  const setSimMode = useCallback(async (mode: "bank" | "resume") => {
    await setSim({ mode });
    return { mode };
  }, [setSim]);

  /** 面试进行中切换识别引擎（本地/火山/云端），服务端只换 ASR 引擎，不重开会话。 */
  const switchEngine = useCallback(async (engine: string) => {
    const resp = await fetch("/api/session/engine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engine }),
    });
    if (!resp.ok) {
      let detail = await resp.text();
      try {
        detail = JSON.parse(detail).detail ?? detail;
      } catch {
        /* 保留原文 */
      }
      throw new Error(detail);
    }
    const data = (await resp.json()) as { engine?: string };
    if (data.engine) setEngine(String(data.engine));
    return data;
  }, []);

  const reportMistake = useCallback(
    async (payload: { last_text: string; correct_type: string; predicted_type?: string; note?: string }) => {
      await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    },
    [],
  );

  /** 中断后接续上一次会话（服务端沿用原 session id 保存）。 */
  const resume = useCallback(async () => {
    const resp = await fetch("/api/session/resume", { method: "POST" });
    if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
    setActive(true);
    await hydrate();
    return resp.json();
  }, [hydrate]);

  return {
    connected,
    active,
    engine,
    devices,
    devicesInUse,
    levels,
    note,
    lastSaved,
    resume,
    judging,
    judgeLog,
    intentStats,
    monitorLevels,
    dropped,
    utterances,
    questions,
    activeId,
    report,
    error,
    setError,
    loadDevices,
    start,
    setAudible,
    setSim,
    stop,
    switchDevices,
    switchEngine,
    setSimMode,
    finish,
    interview,
    createInterview,
    stopInterview,
    fetchRunning,
    resumeInterview,
    setLevelFor,
    reportMistake,
    startMonitor,
    stopMonitor,
    hydrate,
    suggestQuestions,
    generateAnswer,
  };
}

export type Bridge = ReturnType<typeof useBridge>;
