/** 「意图识别记录」列表：实时面试页和面试详情页共用。
 *
 * 每条记录来自后端 pipeline.judge_log（随会话落库），
 * 真在用的数据和刷新后看到的、回放看到的是同一份。
 */
import CallTrace from "./CallTrace";
import type { JudgeEntry } from "../bridge/useBridge";

export default function JudgeLogList({
  entries,
  open = false,
  title = "意图识别记录",
}: {
  entries: JudgeEntry[];
  open?: boolean;
  title?: string;
}) {
  if (!entries.length) return null;
  return (
    <details className="judge-history" open={open}>
      <summary>
        {title} {entries.length}
      </summary>
      <ul>
        {entries.map((entry, index) => (
          <li key={`${entry.ts}-${index}`} className={entry.accepted ? "ok" : "skip"}>
            <details>
              <summary>
                <span className="judge-kind">{entry.kind}</span>
                <span className="judge-conf">{Math.round(entry.confidence * 100)}%</span>
                <span className="judge-timer">{entry.duration_ms}ms</span>
                <span className="judge-raw" title={entry.raw}>
                  「{entry.raw.slice(0, 26)}」
                </span>
                <span
                  className="judge-note"
                  title={entry.accepted ? entry.question : entry.skip_reason || entry.reason}
                >
                  {entry.accepted ? entry.question : entry.skip_reason || entry.reason}
                </span>
              </summary>
              <CallTrace trace={entry.trace} />
            </details>
          </li>
        ))}
      </ul>
    </details>
  );
}
