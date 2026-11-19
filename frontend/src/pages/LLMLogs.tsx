import { useCallback, useEffect, useState } from "react";
import Dropdown from "../components/Dropdown";

/** 分类标签按用途配色：意图识别蓝、答案绿、评分黄、简历紫、预热灰。 */
function categoryTone(category: string): string {
  if (!category) return "cat-other";
  if (
    category.startsWith("意图识别") ||
    category.startsWith("问题重写") ||
    category.startsWith("二次复核")
  ) {
    return "cat-judge";
  }
  if (category.startsWith("答案") || category.startsWith("候选问题")) return "cat-answer";
  if (category.startsWith("评分")) return "cat-report";
  if (category.includes("简历")) return "cat-resume";
  if (category === "预热") return "cat-warm";
  return "cat-other";
}

/** 后端存的是 UTC ISO 串，这里按浏览器本地时区显示（否则东八区会差 8 小时）。 */
function fmtLogTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.replace("T", " ").slice(5, 19);
  const pad = (number: number) => String(number).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(
    date.getMinutes(),
  )}:${pad(date.getSeconds())}`;
}

function fmtLogDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.replace("T", " ").slice(0, 19);
  const pad = (number: number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

interface CallRow {
  id: number;
  created_at: string;
  model: string;
  kind: string;
  status: string;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  error: string;
  interview_id: string;
  category: string;
}

interface CallDetail extends CallRow {
  request: string;
  response: string;
}

interface CallStats {
  total_calls: number;
  total_tokens: number;
  avg_latency_ms: number;
  errors: number;
  today_calls: number;
  today_tokens: number;
}

const CATEGORIES = [
  "意图识别主判",
  "二次复核",
  "问题重写",
  "简历提问",
  "答案·极简",
  "答案·标准",
  "答案·深度",
  "答案·校对",
  "评分报告",
  "候选问题",
  "预热",
];

function prettyRequest(raw: string): string {
  try {
    const messages = JSON.parse(raw) as { role: string; content: string }[];
    return messages.map((m) => `【${m.role}】\n${m.content}`).join("\n\n");
  } catch {
    return raw;
  }
}

export default function LLMLogs() {
  const [rows, setRows] = useState<CallRow[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<CallStats | null>(null);
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [filterInput, setFilterInput] = useState("");
  const [interviewFilter, setInterviewFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");
  const [onlyError, setOnlyError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const query = new URLSearchParams({ limit: "80" });
      if (interviewFilter) query.set("interview_id", interviewFilter);
      if (categoryFilter) query.set("category", categoryFilter);
      if (onlyError) query.set("only_error", "true");
      const [callsResp, statsResp] = await Promise.all([
        fetch(`/api/llm/calls?${query.toString()}`),
        fetch("/api/llm/stats"),
      ]);
      if (!callsResp.ok) throw new Error(await callsResp.text());
      const data = (await callsResp.json()) as { items: CallRow[]; total: number };
      setRows(data.items);
      setTotal(data.total);
      if (statsResp.ok) setStats((await statsResp.json()) as CallStats);
    } catch (err) {
      setError(`加载失败：${(err as Error).message}`);
    } finally {
      setLoading(false);
    }
  }, [categoryFilter, interviewFilter, onlyError]);

  useEffect(() => {
    void load();
  }, [load]);

  const open = useCallback(async (id: number) => {
    const resp = await fetch(`/api/llm/calls/${id}`);
    if (resp.ok) setDetail((await resp.json()) as CallDetail);
  }, []);

  const applyFilter = useCallback(() => {
    setInterviewFilter(filterInput.trim());
  }, [filterInput]);

  const clearFilter = useCallback(() => {
    setFilterInput("");
    setInterviewFilter("");
    setCategoryFilter("");
    setOnlyError(false);
  }, []);

  const deleteCall = useCallback(
    async (id: number) => {
      if (!window.confirm(`确定删除调用 #${id} 的日志？`)) return;
      setNotice("");
      try {
        const resp = await fetch(`/api/llm/calls/${id}`, { method: "DELETE" });
        if (!resp.ok) throw new Error(await resp.text());
        if (detail?.id === id) setDetail(null);
        setNotice(`已删除调用 #${id}`);
        await load();
      } catch (err) {
        setError(`删除失败：${(err as Error).message}`);
      }
    },
    [detail, load],
  );

  const clearAll = useCallback(async () => {
    if (!window.confirm(`确定清空全部 ${stats?.total_calls ?? total} 条调用日志？此操作不可恢复。`)) return;
    setNotice("");
    try {
      const resp = await fetch("/api/llm/calls", { method: "DELETE" });
      if (!resp.ok) throw new Error(await resp.text());
      setDetail(null);
      setNotice("调用日志已清空");
      await load();
    } catch (err) {
      setError(`清空失败：${(err as Error).message}`);
    }
  }, [load, stats?.total_calls, total]);

  const filtering = Boolean(interviewFilter || categoryFilter || onlyError);

  return (
    <div className="prompt-page">
      <header className="prompt-head">
        <div>
          <h1>调用日志</h1>
          <p className="lede">每次大模型调用的完整出入参、耗时和 token 消耗。排查"为什么这么答"最快的地方。</p>
        </div>
        <button className="btn ghost" onClick={() => void clearAll()} disabled={loading || total === 0}>
          清空日志
        </button>
      </header>

      {stats && (
        <div className="log-stats">
          <div className="log-stat">
            <span>调用总数</span>
            <strong>{stats.total_calls}</strong>
          </div>
          <div className="log-stat">
            <span>累计 tokens</span>
            <strong>{stats.total_tokens.toLocaleString()}</strong>
          </div>
          <div className="log-stat">
            <span>平均耗时</span>
            <strong>{stats.avg_latency_ms} ms</strong>
          </div>
          <div className="log-stat">
            <span>失败调用</span>
            <strong className={stats.errors > 0 ? "log-stat-bad" : ""}>{stats.errors}</strong>
          </div>
          <div className="log-stat">
            <span>今日调用 / tokens</span>
            <strong>
              {stats.today_calls} / {stats.today_tokens.toLocaleString()}
            </strong>
          </div>
        </div>
      )}

      {error && <div className="alert">{error}</div>}
      {notice && <div className="notice">{notice}</div>}

      <div className="log-filter">
        <input
          className="log-filter-input"
          value={filterInput}
          onChange={(event) => setFilterInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") applyFilter();
          }}
          placeholder="按面试 ID 筛选，例如 202611181558-1544"
        />
        <button className="btn" onClick={applyFilter} disabled={loading}>
          筛选
        </button>
        <Dropdown
          className="log-filter-select"
          value={categoryFilter}
          options={[
            { value: "", label: "全部分类" },
            ...CATEGORIES.map((item) => ({ value: item, label: item })),
          ]}
          onChange={(value) => setCategoryFilter(value)}
          minWidth={180}
        />
        <label className="switch log-filter-switch">
          <input
            type="checkbox"
            checked={onlyError}
            onChange={(event) => setOnlyError(event.target.checked)}
          />
          <span>只看失败</span>
        </label>
        {filtering && (
          <button className="btn ghost" onClick={clearFilter}>
            清除筛选
          </button>
        )}
        {filtering && <span className="log-filter-active">已筛选</span>}
      </div>

      <div className="log-list">
        <div className="log-row log-head-row">
          <span>时间</span>
          <span>面试 ID</span>
          <span>分类</span>
          <span>模型</span>
          <span>类型</span>
          <span>耗时</span>
          <span>tokens</span>
          <span>状态</span>
          <span>操作</span>
        </div>
        {rows.map((row) => (
          <div
            key={row.id}
            className={`log-row log-click-row ${row.status !== "ok" ? "bad" : ""}`}
            role="button"
            tabIndex={0}
            onClick={() => void open(row.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") void open(row.id);
            }}
          >
            <span className="log-time">{fmtLogTime(row.created_at)}</span>
            <span
              className={`log-interview-cell ${row.interview_id ? "" : "empty"}`}
              title={row.interview_id ? `面试 ${row.interview_id}` : "未绑定面试"}
            >
              {row.interview_id || "未绑定"}
            </span>
            <span className={`log-category ${categoryTone(row.category)} ${row.category ? "" : "empty"}`}>
              {row.category || "未分类"}
            </span>
            <span className="log-model">{row.model}</span>
            <span className="log-kind">{row.kind}</span>
            <span className="log-latency">{row.latency_ms}ms</span>
            <span className="log-tokens">
              {row.prompt_tokens}/{row.completion_tokens}
            </span>
            <span className={`log-status ${row.status === "ok" ? "ok" : "err"}`}>
              {row.status === "ok" ? "成功" : "失败"}
            </span>
            <button
              className="log-delete"
              title="删除这条日志"
              onClick={(event) => {
                event.stopPropagation();
                void deleteCall(row.id);
              }}
            >
              删
            </button>
          </div>
        ))}
        {rows.length === 0 && (
          <div className="empty-state">
            <p>{filtering ? "当前筛选条件下没有调用记录" : "还没有调用记录"}</p>
          </div>
        )}
        <p className="footnote">
          共 {total} 条{filtering ? "（筛选后）" : ""}，当前显示 {rows.length} 条，点一行看详情
        </p>
      </div>

      {detail && (
        <div className="report-overlay" onClick={() => setDetail(null)}>
          <div className="report-card log-detail" onClick={(e) => e.stopPropagation()}>
            <header className="report-head">
              <h2>调用 #{detail.id}</h2>
              <div className="log-detail-actions">
                <button className="btn ghost" onClick={() => void deleteCall(detail.id)}>
                  删除这条
                </button>
                <button className="btn ghost" onClick={() => setDetail(null)}>
                  关闭
                </button>
              </div>
            </header>
            <div className="log-meta">
              <span>调用ID #{detail.id}</span>
              {detail.interview_id ? (
                <a
                  className="call-interview-link"
                  href={`/detail/${encodeURIComponent(detail.interview_id)}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  面试 {detail.interview_id}
                </a>
              ) : (
                <span>未绑定面试</span>
              )}
              <span className={`log-category ${categoryTone(detail.category)}`}>{detail.category || "未分类"}</span>
              <span>{detail.model}</span>
              <span>{detail.kind}</span>
              <span>{detail.latency_ms} ms</span>
              <span>
                tokens {detail.prompt_tokens} → {detail.completion_tokens}
              </span>
              <span>{fmtLogDateTime(detail.created_at)}</span>
            </div>
            {detail.error && <div className="alert">{detail.error}</div>}
            <h3 className="log-h">入参</h3>
            <pre className="log-pre">{prettyRequest(detail.request)}</pre>
            <h3 className="log-h">出参</h3>
            <pre className="log-pre">{detail.response || "（空）"}</pre>
          </div>
        </div>
      )}
    </div>
  );
}
