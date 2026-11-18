import { useCallback, useEffect, useState } from "react";
import { costLabel, engineLabel, modelsLabel } from "../usage";
import type { JudgeEntry, UsageInfo } from "../bridge/useBridge";
import AnswerColumns from "../components/AnswerColumns";
import CallTrace from "../components/CallTrace";
import IntentStatsBar from "../components/IntentStats";
import JudgeLogList from "../components/JudgeLogList";

interface Utterance {
  id: string;
  channel: string;
  text: string;
  ts: number;
}

interface Card {
  id: string;
  question: string;
  focus?: string[];
  answer: string;
  answers?: Record<string, string> | null;
  reasoning?: string;
  sources?: { title?: string; url?: string }[];
  user_answer?: string;
  level?: string;
  followup?: boolean;
  challenge?: boolean;
  depth?: number;
  parent_question?: string;
  rejected?: boolean;
  verify_reason?: string;
  modes?: Record<string, { thinking?: boolean; web?: boolean }>;
}

interface Report {
  overall_score?: number;
  verdict?: string;
  dimensions?: { name: string; score: number; comment: string }[];
  per_question?: { index: number; score: number; comment: string }[];
  strengths?: string[];
  gaps?: string[];
  action_plan?: string[];
  chain_stats?: {
    chains?: number;
    max_depth?: number;
    followups?: number;
    challenges?: number;
    chain_rounds?: number;
  };
  chains?: {
    root_question: string;
    depth: number;
    score: number;
    comment: string;
    weakest_layer?: number;
  }[];
}

interface Detail {
  id: string;
  position_name?: string;
  position_id?: number | null;
  status?: string;
  started_at?: number;
  ended_at?: number | null;
  elapsed?: number;
  end_reason?: string;
  config?: {
    role?: string;
    level?: string;
    engine?: string;
    answer_level?: string;
    output_device?: string;
    input_device?: string;
    background?: string;
    resume?: string;
  };
  resume?: string;
  utterances?: Utterance[];
  questions?: Card[];
  report?: Report | null;
  engine?: string;
  engines?: string[];
  usage?: UsageInfo;
  intent_stats?: Record<string, number>;
  judge_log?: JudgeEntry[];
}

const STATUS_LABEL: Record<string, string> = {
  running: "进行中",
  finished: "已结束",
  auto_interrupted: "系统自动中断",
  interrupted: "已中断（可继续）",
};

function formatDateTime(seconds: number): string {
  if (!seconds) return "";
  const date = new Date(seconds * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function fmtClock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function fmtDateTime(seconds?: number | null): string {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export default function InterviewDetail({
  id,
  onBack,
  onOpen,
}: {
  id: string;
  onBack: () => void;
  onOpen: (id: string) => void;
}) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState("");
  const [reportOpen, setReportOpen] = useState(false);
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<"" | "resume" | "restart">("");

  const load = useCallback(async () => {
    setError("");
    try {
      const resp = await fetch(`/api/interviews/${id}`);
      if (!resp.ok) throw new Error(await resp.text());
      setDetail((await resp.json()) as Detail);
    } catch (err) {
      setError(`加载失败：${(err as Error).message}`);
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!reportOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setReportOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [reportOpen]);

  // 继续这场面试：恢复采集、对话流与提词器（已中断/进行中的都支持）
  const resumeInterview = useCallback(async () => {
    setNotice("");
    setBusy("resume");
    try {
      const resp = await fetch(`/api/interviews/${id}/resume`, { method: "POST" });
      const data = (await resp.json().catch(() => ({}))) as { detail?: string; ok?: boolean };
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      onOpen(id);
    } catch (err) {
      setNotice(`继续失败：${(err as Error).message}`);
    } finally {
      setBusy("");
    }
  }, [id, onOpen]);

  // 重新开始：沿用同一岗位/简历新建一场，计时从 0 开始
  const restartInterview = useCallback(async () => {
    if (!detail) return;
    setNotice("");
    setBusy("restart");
    try {
      const cfg = detail.config ?? {};
      const resp = await fetch("/api/interviews", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          role: detail.position_name || cfg.role || "面试练习",
          resume: detail.resume || "",
          position_id: detail.position_id ?? null,
          position_name: detail.position_name ?? "",
          level: cfg.level ?? "mid",
          engine: cfg.engine ?? "auto",
          answer_level: cfg.answer_level ?? "standard",
          output_device: cfg.output_device ?? "",
          input_device: cfg.input_device ?? "",
          mode: "real",
          background: cfg.background ?? "",
        }),
      });
      const data = (await resp.json().catch(() => ({}))) as {
        id?: string;
        session_id?: string;
        interview?: { id?: string };
        detail?: string;
      };
      if (!resp.ok) throw new Error(data.detail || `HTTP ${resp.status}`);
      const createdId = data.interview?.id ?? data.id ?? data.session_id;
      if (!createdId) throw new Error("服务端没有返回新的面试 ID");
      onOpen(createdId);
    } catch (err) {
      setNotice(`重新开始失败：${(err as Error).message}`);
    } finally {
      setBusy("");
    }
  }, [detail, onOpen]);

  if (error) {
    return (
      <div className="prompt-page">
        <div className="alert">{error}</div>
        <button className="btn ghost" onClick={onBack}>
          返回列表
        </button>
      </div>
    );
  }

  if (!detail) {
    return <div className="prompt-page">加载中…</div>;
  }

  const startTs = detail.started_at ?? 0;
  const endTs = detail.ended_at ?? (startTs ? Math.floor(Date.now() / 1000) : 0);
  const duration = detail.elapsed ?? (startTs && endTs ? endTs - startTs : 0);
  const report = detail.report ?? null;
  const resumable = detail.status === "running" || detail.status === "interrupted";
  const canRestart = Boolean((detail.resume ?? detail.config?.resume ?? "").trim());

  return (
    <div className="prompt-page detail-page">
      <header className="detail-head">
        <div>
          <h1>{detail.position_name || detail.config?.role || "面试详情"}</h1>
          <p className="detail-id">
            <span className="interview-id" title="面试ID">
              ID {detail.id}
            </span>
          </p>
          <div className="detail-meta">
            <span>{fmtDateTime(detail.started_at)} 开始</span>
            <span>时长 {fmtClock(duration)}</span>
            <span>{(detail.utterances ?? []).length} 句对话</span>
            <span>{(detail.questions ?? []).length} 个问题</span>
          </div>
          <div className="detail-meta detail-meta-sub">
            {detail.status && (
              <span className={`status-chip ${detail.status}`}>
                {STATUS_LABEL[detail.status] ?? detail.status}
              </span>
            )}
            {detail.end_reason && <span>结束原因：{detail.end_reason}</span>}
            {detail.usage && (
              <>
                <span>
                  {engineLabel(
                    detail.engines && detail.engines.length ? detail.engines.join("/") : detail.engine,
                  )}
                </span>
                <span>{modelsLabel(detail.usage)}</span>
                <span>{costLabel(detail.usage)}</span>
              </>
            )}
          </div>
        </div>
        <div className="detail-head-actions">
          <button className="btn ghost" onClick={onBack}>
            ← 返回面试列表
          </button>
          {resumable && (
            <button
              className="btn primary"
              disabled={busy !== ""}
              onClick={() => void resumeInterview()}
              title="恢复采集和提词，接着上一场继续"
            >
              {busy === "resume" ? "正在恢复…" : "▶ 继续这场面试"}
            </button>
          )}
          <button
            className="btn"
            disabled={busy !== "" || !canRestart}
            onClick={() => void restartInterview()}
            title="用同样的岗位和简历新建一场，从 0 开始计时"
          >
            {busy === "restart" ? "正在开始…" : "🔄 重新开始"}
          </button>
          {report && (
            <button className="btn primary" onClick={() => setReportOpen(true)}>
              📊 评分报告
            </button>
          )}
        </div>
      </header>

      {notice && <div className="alert">{notice}</div>}

      <div className="detail-grid">
        <section className="detail-pane">
          <h3>对话流</h3>
          <div className="detail-body">
            {(detail.utterances ?? []).length === 0 && <p className="muted">这场没有对话记录</p>}
            {(detail.utterances ?? []).map((item) => (
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
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="detail-pane">
          <h3>提词器</h3>
          <div className="detail-body">
            {(detail.intent_stats?.judge_total ?? 0) > 0 && (
              <div className="detail-intent-stats">
                <IntentStatsBar stats={detail.intent_stats} open />
                <JudgeLogList entries={detail.judge_log ?? []} open title="意图识别记录" />
              </div>
            )}
            {(detail.questions ?? []).length === 0 && <p className="muted">这场没有生成问题卡片</p>}
            {(detail.questions ?? []).map((card, index) => (
              <div className="detail-card" key={card.id}>
                <p className="detail-q">
                  {index + 1}. {card.question}
                  {card.followup && (
                    <span className="badge badge-followup" style={{ marginLeft: 8 }}>
                      追问{card.depth && card.depth > 1 ? ` · L${card.depth}` : ""}
                    </span>
                  )}
                  {card.challenge && (
                    <span className="badge badge-challenge" style={{ marginLeft: 6 }}>
                      反问{card.depth && card.depth > 1 ? ` · L${card.depth}` : ""}
                    </span>
                  )}
                </p>
                {card.parent_question && (
                  <p className="answer-chain">↳ 承接：{card.parent_question}</p>
                )}
                {card.rejected && (
                  <div className="alert rejected-tip">
                    已撤销（复核未通过）：{card.verify_reason || "问题不成立"}
                  </div>
                )}
                {(card.focus ?? []).length > 0 && (
                  <div className="chip-row">
                    {(card.focus ?? []).map((focus) => (
                      <span className="chip" key={focus}>
                        {focus}
                      </span>
                    ))}
                  </div>
                )}

                {!card.rejected && (
                  <AnswerColumns
                    answers={card.answers}
                    fallback={card.answer}
                    modes={card.modes}
                  />
                )}

                <details className="card-calls">
                  <summary>模型调用（意图识别 + 三版答案）</summary>
                  <CallTrace trace={card.id} />
                </details>

                {card.reasoning && (
                  <details className="reasoning">
                    <summary>思考过程</summary>
                    <pre>{card.reasoning}</pre>
                  </details>
                )}

                {card.user_answer && (
                  <p className="detail-user">候选人实际回答：{card.user_answer}</p>
                )}

                {(card.sources ?? []).length > 0 && (
                  <div className="answer-sources">
                    <span className="sources-label">来源</span>
                    {(card.sources ?? []).slice(0, 6).map((src, idx) => (
                      <a
                        key={`${src.url ?? idx}`}
                        className="source-chip"
                        href={src.url ?? "#"}
                        target="_blank"
                        rel="noreferrer"
                      >
                        {idx + 1}. {(src.title ?? src.url ?? "").slice(0, 24)}
                      </a>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </section>
      </div>

      {reportOpen && report && (
        <div className="report-overlay" onClick={() => setReportOpen(false)}>
          <div
            className="report-card detail-report-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="report-head">
              <h2>面试评分报告</h2>
              <button className="btn ghost" onClick={() => setReportOpen(false)}>
                关闭
              </button>
            </header>
            <div className="report-modal-body">
              <section className="detail-report">
          <div className="report-score">
            <span className="report-score-value">{report.overall_score ?? "-"}</span>
            <span className="report-score-label">综合得分</span>
            <p className="report-verdict">{report.verdict}</p>
          </div>

          {(report.dimensions ?? []).length > 0 && (
            <div className="dimension-grid">
              {(report.dimensions ?? []).map((dim) => (
                <div className="dimension" key={dim.name}>
                  <span>{dim.name}</span>
                  <b>{dim.score}</b>
                  <small>{dim.comment}</small>
                </div>
              ))}
            </div>
          )}

          {report.chain_stats && (report.chain_stats.chains ?? 0) > 0 && (
            <div className="ended-stats" style={{ marginBottom: 12 }}>
              <div>
                <span>话题数</span>
                <b>{report.chain_stats.chains}</b>
              </div>
              <div>
                <span>最深追问</span>
                <b>{report.chain_stats.max_depth} 层</b>
              </div>
              <div>
                <span>追问次数</span>
                <b>{report.chain_stats.followups}</b>
              </div>
              <div>
                <span>反问次数</span>
                <b>{report.chain_stats.challenges}</b>
              </div>
            </div>
          )}

          {(report.chains ?? []).length > 0 && (
            <div className="chain-scores">
              <h4>话题链评分</h4>
              {(report.chains ?? []).map((chain, index) => (
                <div className="chain-score-row" key={`${chain.root_question}-${index}`}>
                  <span className="chain-question" title={chain.root_question}>
                    {index + 1}. {chain.root_question}
                  </span>
                  <span className="chain-depth">追问 {Math.max(0, chain.depth - 1)} 层</span>
                  <b className={`chain-score ${chain.score < 60 ? "low" : ""}`}>{chain.score}</b>
                  <span className="chain-comment">
                    {chain.comment}
                    {chain.weakest_layer ? `（最虚：第 ${chain.weakest_layer} 轮）` : ""}
                  </span>
                </div>
              ))}
            </div>
          )}

          {(report.per_question ?? []).length > 0 && (
            <div className="per-question">
              {(report.per_question ?? []).map((item) => (
                <div className="per-item" key={item.index}>
                  <span>第 {item.index} 题</span>
                  <b>{item.score} 分</b>
                  <small>{item.comment}</small>
                </div>
              ))}
            </div>
          )}

          <div className="report-columns">
            {(report.strengths ?? []).length > 0 && (
              <div>
                <h4>亮点</h4>
                <ul>
                  {(report.strengths ?? []).map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
            {(report.gaps ?? []).length > 0 && (
              <div>
                <h4>短板</h4>
                <ul>
                  {(report.gaps ?? []).map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
            {(report.action_plan ?? []).length > 0 && (
              <div>
                <h4>行动计划</h4>
                <ul>
                  {(report.action_plan ?? []).map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
              </section>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
