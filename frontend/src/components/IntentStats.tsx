/** 意图识别统计条：实时面试页和面试详情页共用。
 *
 * 数字来自后端 pipeline.stats（随会话落库的 intent_stats），
 * 刷新 / 回放时能从记录里恢复，不是只存在内存里。
 */
export default function IntentStatsBar({
  stats,
  open = false,
}: {
  stats?: Record<string, number> | null;
  /** 详情页传 true 直接展开 */
  open?: boolean;
}) {
  const data = stats ?? {};
  const total = data.judge_total ?? 0;
  const chip = (label: string, value: number | undefined, cls = "") => (
    <span className={`stat-chip ${cls}`}>
      {label} {value ?? 0}
    </span>
  );
  return (
    <details className="intent-stats" open={open}>
      <summary>
        {chip("意图识别", total)}
        {chip("出卡", data.accepted, "ok")}
        {chip("跳过", data.skipped, "muted")}
        {(data.verify_runs ?? 0) > 0 &&
          chip(
            `复核 撤${data.verify_rejected ?? 0} 修${data.verify_fixed ?? 0}`,
            data.verify_runs,
            "amber",
          )}
        <span className="stat-chip violet">
          追问 {data.followups ?? 0} · 反问 {data.challenges ?? 0}
        </span>
        <span className="stat-chip">平均 {data.avg_ms ?? 0}ms</span>
        {(data.corrections ?? 0) > 0 && chip("人工纠错", data.corrections, "danger")}
      </summary>
      <div className="intent-stats-detail">
        <span>旁白/离场 {data.skip_aside ?? 0}</span>
        <span>非提问 {data.skip_statement ?? 0}</span>
        <span>低置信 {data.skip_low_conf ?? 0}</span>
        <span>重复 {data.skip_duplicate ?? 0}</span>
        <span>复核撤销 {data.skip_verify ?? 0}</span>
        <span>最深追问 L{data.max_depth ?? 0}</span>
        <span>最长耗时 {data.judge_ms_max ?? 0}ms</span>
      </div>
    </details>
  );
}
