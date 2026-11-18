import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { stripPronunciation } from "../text";

interface SessionRow {
  id: string;
  created_at: string;
  role: string;
  engine: string;
  turns: number;
  utterances: number;
  score: number | null;
  verdict: string;
  status: string;
  status_label: string;
  elapsed: number;
  started_at: number;
}

interface Utterance {
  id: string;
  channel: string;
  text: string;
  ts: number;
}

interface Card {
  id: string;
  question: string;
  answer: string;
  focus?: string[];
  level?: string;
  answers?: Record<string, string> | null;
  user_answer?: string;
  ts: number;
}

interface Detail {
  id: string;
  config: { role?: string };
  utterances: Utterance[];
  questions: Card[];
  report: Record<string, unknown> | null;
}

function fmtClock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function fmtDate(ts: string): string {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return ts.slice(0, 16);
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export default function History({ initialId }: { initialId?: string | null }) {
  const [rows, setRows] = useState<SessionRow[]>([]);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  // 回放状态
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const streamRef = useRef<HTMLDivElement | null>(null);
  const promptRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const resp = await fetch("/api/interviews?limit=100");
      if (!resp.ok) throw new Error(await resp.text());
      const data = (await resp.json()) as { items: SessionRow[] };
      setRows(data.items);
    } catch (err) {
      setError(`加载失败：${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (initialId) void open(initialId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialId]);

  const open = useCallback(async (id: string) => {
    const resp = await fetch(`/api/interviews/${id}`);
    if (!resp.ok) return;
    const data = (await resp.json()) as Detail;
    setDetail(data);
    setCursor(0);
    setPlaying(false);
  }, []);

  const remove = useCallback(
    async (id: string) => {
      await fetch(`/api/sessions/${id}`, { method: "DELETE" });
      if (detail?.id === id) setDetail(null);
      await load();
    },
    [detail?.id, load],
  );

  // 时间轴：把所有事件按时间排好
  const timeline = useMemo(() => {
    if (!detail) return { start: 0, end: 0, duration: 0 };
    const stamps: number[] = [];
    (detail.utterances ?? []).forEach((u) => stamps.push(u.ts));
    (detail.questions ?? []).forEach((q) => stamps.push(q.ts));
    const valid = stamps.filter((t) => t > 0);
    if (valid.length === 0) return { start: 0, end: 0, duration: 0 };
    const start = Math.min(...valid);
    const end = Math.max(...valid);
    return { start, end, duration: Math.max(1, end - start) };
  }, [detail]);

  // 播放推进
  useEffect(() => {
    if (!playing || !detail) return;
    const timer = window.setInterval(() => {
      setCursor((prev) => {
        const next = prev + 0.1 * speed;
        if (next >= timeline.duration) {
          setPlaying(false);
          return timeline.duration;
        }
        return next;
      });
    }, 100);
    return () => window.clearInterval(timer);
  }, [playing, speed, detail, timeline.duration]);

  // 自动滚到底部
  useEffect(() => {
    if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight;
    if (promptRef.current) promptRef.current.scrollTop = promptRef.current.scrollHeight;
  }, [cursor]);

  const visibleUtterances = useMemo(() => {
    if (!detail) return [];
    return (detail.utterances ?? []).filter((u) => u.ts <= timeline.start + cursor);
  }, [detail, cursor, timeline.start]);

  const visibleCards = useMemo(() => {
    if (!detail) return [];
    return (detail.questions ?? []).filter((q) => q.ts <= timeline.start + cursor);
  }, [detail, cursor, timeline.start]);

  return (
    <div className="prompt-page">
      <header className="prompt-head">
        <div>
          <h1>历史面试</h1>
          <p className="lede">
            一场面试一条记录：从「新建面试」开始计时，手动「停止面试」才结束；超过 2 小时没停的，标记为系统自动中断。
            点任意一条可以按时间轴回放对话流和提词器。
          </p>
        </div>
        <div className="history-actions">
          <button className="btn ghost" onClick={() => void load()} disabled={loading}>
            {loading ? "加载中…" : "刷新"}
          </button>
        </div>
      </header>

      {error && <div className="alert">{error}</div>}

      <div className="log-list">
        {rows.map((row) => (
          <div key={row.id} className="log-row session-row">
            <button className="session-open" onClick={() => void open(row.id)}>
              <span className="log-time">{fmtDate(row.created_at)}</span>
              <span className="log-model">{row.role || "（未设置岗位）"}</span>
              <span className={`status-chip ${row.status}`}>{row.status_label || row.status}</span>
              <span className="log-latency">{row.turns} 题</span>
              <span className="log-tokens">{row.utterances} 句</span>
              <span className="log-status ok">
                {fmtClock(row.elapsed)} · {row.score ?? "-"} 分
              </span>
            </button>
            <button className="link-btn" onClick={() => void remove(row.id)}>
              删除
            </button>
          </div>
        ))}
        {rows.length === 0 && (
          <div className="empty-state">
            <p>还没有历史记录</p>
            <p className="muted">跑一场面试或仿真，这里就会出现（一场一条，不是按消息条数）。</p>
          </div>
        )}
      </div>

      {detail && (
        <div className="report-overlay" onClick={() => setDetail(null)}>
          <div className="report-card replay-card" onClick={(e) => e.stopPropagation()}>
            <header className="report-head">
              <h2>{detail.config.role || "会话"} · 回放</h2>
              <button className="btn ghost" onClick={() => setDetail(null)}>
                关闭
              </button>
            </header>

            {detail.report && (
              <p className="report-verdict">
                综合 {String(detail.report.overall_score ?? "-")} 分 ·{" "}
                {String(detail.report.verdict ?? "")}
              </p>
            )}

            <div className="replay-controls">
              <button
                className="btn primary"
                onClick={() => {
                  if (cursor >= timeline.duration) setCursor(0);
                  setPlaying((v) => !v);
                }}
              >
                {playing ? "⏸ 暂停" : "▶ 播放"}
              </button>
              <button
                className="btn ghost"
                onClick={() => {
                  setPlaying(false);
                  setCursor(0);
                }}
              >
                ⏮ 重来
              </button>
              <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
                <option value={0.5}>0.5x</option>
                <option value={1}>1x</option>
                <option value={2}>2x</option>
                <option value={4}>4x</option>
              </select>
              <input
                className="replay-slider"
                type="range"
                min={0}
                max={Math.max(1, timeline.duration)}
                step={0.5}
                value={cursor}
                onChange={(e) => {
                  setPlaying(false);
                  setCursor(Number(e.target.value));
                }}
              />
              <span className="replay-clock">
                {fmtClock(cursor)} / {fmtClock(timeline.duration)}
              </span>
            </div>

            <div className="replay-grid">
              <section className="replay-pane">
                <h3>对话流</h3>
                <div className="replay-body" ref={streamRef}>
                  {visibleUtterances.length === 0 && <p className="muted">还没有内容</p>}
                  {visibleUtterances.map((item) => (
                    <div key={item.id} className={`bubble-row ${item.channel}`}>
                      <span className={`avatar ${item.channel}`}>
                        {item.channel === "interviewer" ? "官" : "候"}
                      </span>
                      <div className="bubble">
                        <span className="bubble-who">
                          {item.channel === "interviewer" ? "面试官" : "候选人"}
                        </span>
                        <p>{item.text}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </section>

              <section className="replay-pane">
                <h3>提词器</h3>
                <div className="replay-body" ref={promptRef}>
                  {visibleCards.length === 0 && <p className="muted">还没有卡片</p>}
                  {visibleCards.map((card) => (
                    <div className="replay-card-item" key={card.id}>
                      <p className="replay-q">{card.question}</p>
                      {card.focus && card.focus.length > 0 && (
                        <div className="chip-row">
                          {card.focus.map((f) => (
                            <span className="chip" key={f}>
                              {f}
                            </span>
                          ))}
                        </div>
                      )}
                      <p className="replay-a">
                        {stripPronunciation(card.answers?.standard ?? card.answer ?? "") || "（未生成）"}
                      </p>
                      {card.user_answer && (
                        <p className="replay-u">候选人实际回答：{card.user_answer}</p>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
