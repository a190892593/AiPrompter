import type { UsageInfo } from "./bridge/useBridge";

const ENGINE_LABEL: Record<string, string> = {
  local: "本地识别",
  volc: "火山豆包",
  cloud: "云端识别",
  mock: "演示识别",
  auto: "自动识别",
};

/** 识别引擎（中途切换过会用 → 连接） */
export function engineLabel(engine?: string): string {
  if (!engine) return "识别未记录";
  return engine
    .split("/")
    .map((part) => ENGINE_LABEL[part] ?? part)
    .join(" → ");
}

/** 本场调用过的 LLM 模型 */
export function modelsLabel(usage?: UsageInfo): string {
  const models = usage?.models ?? [];
  return models.length ? models.join(" / ") : "未调用模型";
}

/** 模型费用（人民币，估算） */
export function costLabel(usage?: UsageInfo): string {
  if (!usage) return "—";
  if (usage.cost == null) return "未定价";
  return `¥${usage.cost < 0.1 ? usage.cost.toFixed(3) : usage.cost.toFixed(2)}`;
}

/** 悬停提示：把识别、模型、tokens 都列出来 */
export function usageTitle(item: { engine?: string; usage?: UsageInfo }): string {
  const usage = item.usage;
  if (!usage) return "本场没有模型调用记录";
  return [
    `识别：${engineLabel(item.engine)}`,
    `模型：${modelsLabel(usage)}`,
    `tokens：${(usage.total_tokens ?? 0).toLocaleString()}`,
    "费用为 LLM 估算（识别/语音另计）",
  ].join("；");
}
