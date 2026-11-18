import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";

interface CallRow {
  id: number;
  created_at: string;
  model: string;
  kind: string;
  status: string;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
  error: string;
  trace: string;
  interview_id: string;
}

interface CallDetail extends CallRow {
  request: string;
  response: string;
}

function prettyRequest(raw: string): string {
  try {
    const messages = JSON.parse(raw) as { role: string; content: string }[];
    return messages.map((m) => `【${m.role}】\n${m.content}`).join("\n\n");
  } catch {
    return raw;
  }
}

/** 展示某个 trace 下的全部模型调用；点某一行用弹窗看完整入参/出参。 */
export default function CallTrace({ trace }: { trace: string }) {
  const [rows, setRows] = useState<CallRow[]>([]);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [loadingId, setLoadingId] = useState<number | null>(null);
  const [detailError, setDetailError] = useState("");

  useEffect(() => {
    if (!trace) return;
    let cancelled = false;
    fetch(`/api/llm/calls?limit=20&trace=${encodeURIComponent(trace)}`)
      .then((resp) => (resp.ok ? resp.json() : Promise.reject(new Error(`HTTP ${resp.status}`))))
      .then((data: { items?: CallRow[] }) => {
        if (!cancelled) setRows(data.items ?? []);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(`加载调用记录失败：${err.message}`);
      });
    return () => {
      cancelled = true;
    };
  }, [trace]);

  useEffect(() => {
    if (!detail) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDetail(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detail]);

  const openDetail = useCallback(async (id: number) => {
    setLoadingId(id);
    setDetailError("");
    try {
      const resp = await fetch(`/api/llm/calls/${id}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setDetail((await resp.json()) as CallDetail);
    } catch (err) {
      setDetailError(`加载调用详情失败：${(err as Error).message}`);
    } finally {
      setLoadingId(null);
    }
  }, []);

  if (error) return <p className="call-error">{error}</p>;
  if (rows.length === 0) return <p className="call-empty">没有关联的模型调用记录</p>;

  return (
    <div className="call-trace">
      {rows.map((row) => (
        <div className="call-row" key={row.id}>
          <button
            className="call-head"
            onClick={() => void openDetail(row.id)}
            title="点击查看完整入参 / 出参"
          >
            <span className="call-model">{row.model}</span>
            <span className="call-kind">{row.kind}</span>
            <span className="call-latency">{row.latency_ms}ms</span>
            <span className="call-tokens">
              {row.prompt_tokens}→{row.completion_tokens}
            </span>
            <span className={`call-status ${row.status === "ok" ? "ok" : "err"}`}>
              {row.status === "ok" ? "成功" : "失败"}
            </span>
            <span className="call-open-hint">{loadingId === row.id ? "加载中…" : "查看详情 ↗"}</span>
          </button>
        </div>
      ))}

      {detail &&
        createPortal(
          <div className="call-modal-overlay" onClick={() => setDetail(null)}>
            <div
              className="call-modal"
              role="dialog"
              aria-modal="true"
              aria-label="模型调用详情"
              onClick={(event) => event.stopPropagation()}
            >
              <header className="call-modal-head">
                <div className="call-modal-title">
                  <h3>
                    {detail.model}
                    <span className="call-kind">{detail.kind}</span>
                  </h3>
                  <p className="call-modal-meta">
                    调用ID #{detail.id} ·{" "}
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
                      "未绑定面试"
                    )}{" "}
                    · {detail.latency_ms}ms · {detail.prompt_tokens}→{detail.completion_tokens} tokens ·{" "}
                    <span className={detail.status === "ok" ? "call-ok" : "call-err"}>
                      {detail.status === "ok" ? "成功" : "失败"}
                    </span>{" "}
                    · {new Date(detail.created_at).toLocaleString()}
                  </p>
                </div>
                <button className="btn ghost" onClick={() => setDetail(null)}>
                  关闭
                </button>
              </header>

              <div className="call-modal-body">
                {detail.error && <p className="call-error">{detail.error}</p>}
                <p className="call-label">入参</p>
                <pre className="call-pre">{prettyRequest(detail.request)}</pre>
                <p className="call-label">出参</p>
                <pre className="call-pre">{detail.response || "（空）"}</pre>
              </div>
            </div>
          </div>,
          document.body,
        )}

      {detailError && <p className="call-error">{detailError}</p>}
    </div>
  );
}
