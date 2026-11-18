import { useCallback, useEffect, useState } from "react";

interface BalanceInfo {
  supported: boolean;
  reason?: string;
  available?: boolean;
  currency?: string;
  total?: number;
  granted?: number;
  topped_up?: number;
}

interface Stats {
  total_calls: number;
  total_tokens: number;
  avg_latency_ms: number;
  errors: number;
  today_calls: number;
  today_tokens: number;
}

export default function Balance() {
  const [balance, setBalance] = useState<BalanceInfo | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const [b, s] = await Promise.all([
        fetch("/api/balance").then((r) => r.json()),
        fetch("/api/llm/stats").then((r) => r.json()),
      ]);
      setBalance(b as BalanceInfo);
      setStats(s as Stats);
    } catch (err) {
      setError(`读取失败：${(err as Error).message}`);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="prompt-page">
      <header className="prompt-head">
        <div>
          <h1>余额</h1>
          <p className="lede">大模型账户余额与用量统计。DeepSeek 官方提供余额接口，火山方舟没有公开接口。</p>
        </div>
        <button className="btn ghost" onClick={() => void load()}>
          刷新
        </button>
      </header>

      {error && <div className="alert">{error}</div>}

      <section className="prompt-card">
        {balance?.supported ? (
          <div className="balance-grid">
            <div className="balance-main">
              <span className="balance-label">账户余额</span>
              <span className="balance-value">
                {balance.total?.toFixed(2)} <small>{balance.currency}</small>
              </span>
              <span className={`badge ${balance.available ? "badge-live" : "badge-hot"}`}>
                {balance.available ? "可用" : "余额不足或不可用"}
              </span>
            </div>
            <div className="balance-sub">
              <div>
                <span>赠送余额</span>
                <b>{balance.granted?.toFixed(2)}</b>
              </div>
              <div>
                <span>充值余额</span>
                <b>{balance.topped_up?.toFixed(2)}</b>
              </div>
            </div>
          </div>
        ) : (
          <div className="empty-state">
            <p>余额查询不可用</p>
            <p className="muted">{balance?.reason ?? "加载中…"}</p>
          </div>
        )}
      </section>

      {stats && (
        <section className="prompt-card">
          <header className="prompt-card-head">
            <strong>用量统计</strong>
            <span className="spacer" />
            <button
              className="link-btn"
              onClick={async () => {
                await fetch("/api/llm/calls", { method: "DELETE" });
                await load();
              }}
            >
              清空调用日志
            </button>
          </header>
          <div className="stat-grid">
            <div className="stat">
              <span>总调用</span>
              <b>{stats.total_calls}</b>
            </div>
            <div className="stat">
              <span>今日调用</span>
              <b>{stats.today_calls}</b>
            </div>
            <div className="stat">
              <span>今日 tokens</span>
              <b>{stats.today_tokens.toLocaleString()}</b>
            </div>
            <div className="stat">
              <span>累计 tokens</span>
              <b>{stats.total_tokens.toLocaleString()}</b>
            </div>
            <div className="stat">
              <span>平均耗时</span>
              <b>{stats.avg_latency_ms} ms</b>
            </div>
            <div className="stat">
              <span>失败次数</span>
              <b style={{ color: stats.errors ? "#ffb3b3" : undefined }}>{stats.errors}</b>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
