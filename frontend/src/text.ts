/** 文本清洗：去掉历史答案里残留的「（中文拟音"…"）」注音。
 *
 * 「中文拟音」功能已下线，新答案不会再带注音；这里只负责把旧记录里的注音洗掉，
 * 免得回放时看到一长串“艾尔-艾尔-艾姆-诶震特”。
 */
const PRON_PATTERN = /[（(]\s*(?:英文读音[^）)]*，)?\s*中文拟音\s*[“"][^”"]*[”"]\s*[）)]/g;

export function stripPronunciation(text: string): string {
  if (!text) return text;
  return text.replace(PRON_PATTERN, "").replace(/\s{2,}/g, " ").trim();
}
