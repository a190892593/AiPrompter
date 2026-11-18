import { useCallback, useEffect, useState } from "react";

interface PromptItem {
  key: string;
  label: string;
  placeholders: string[];
  content: string;
  default: string;
  modified: boolean;
}

export default function PromptTemplates() {
  const [items, setItems] = useState<PromptItem[]>([]);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const resp = await fetch("/api/prompts");
      if (!resp.ok) throw new Error(await resp.text());
      const data = (await resp.json()) as { items: PromptItem[] };
      setItems(data.items);
      setDrafts(Object.fromEntries(data.items.map((item) => [item.key, item.content])));
    } catch (err) {
      setError(`加载提示词失败：${(err as Error).message}`);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = useCallback(
    async (key: string) => {
      setBusy(key);
      setError("");
      try {
        const resp = await fetch(`/api/prompts/${key}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: drafts[key] ?? "" }),
        });
        if (!resp.ok) throw new Error(await resp.text());
        const data = (await resp.json()) as { unknown_placeholders: string[] };
        setMessage(
          data.unknown_placeholders.length
            ? `已保存，但发现未知占位符：${data.unknown_placeholders.join("、")}（会被原样输出）`
            : "已保存，下次调用立即生效",
        );
        await load();
      } catch (err) {
        setError(`保存失败：${(err as Error).message}`);
      } finally {
        setBusy("");
      }
    },
    [drafts, load],
  );

  const reset = useCallback(
    async (key: string) => {
      setBusy(key);
      try {
        const resp = await fetch(`/api/prompts/${key}/reset`, { method: "POST" });
        if (!resp.ok) throw new Error(await resp.text());
        setMessage("已恢复默认");
        await load();
      } catch (err) {
        setError(`恢复失败：${(err as Error).message}`);
      } finally {
        setBusy("");
      }
    },
    [load],
  );

  return (
    <div className="prompt-page">
      <header className="prompt-head">
        <div>
          <h1>提示词模板</h1>
          <p className="lede">
            这里改的是每一次大模型调用的输入。保存后下一次调用立即生效，不用重启。
            带花括号的占位符不要删，否则那部分内容会丢。
          </p>
        </div>
        <button className="btn ghost" onClick={() => void load()}>
          重新加载
        </button>
      </header>

      {error && <div className="alert">{error}</div>}
      {message && <div className="prompt-toast">{message}</div>}

      <div className="prompt-list">
        {items.map((item) => (
          <section className="prompt-card" key={item.key}>
            <header className="prompt-card-head">
              <strong>{item.label}</strong>
              <code>{item.key}</code>
              {item.modified && <span className="badge badge-hot">已修改</span>}
              <span className="spacer" />
              <button className="btn ghost" onClick={() => void reset(item.key)} disabled={busy === item.key}>
                恢复默认
              </button>
              <button
                className="btn primary"
                onClick={() => void save(item.key)}
                disabled={busy === item.key || drafts[item.key] === item.content}
              >
                {busy === item.key ? "保存中…" : "保存"}
              </button>
            </header>

            {item.placeholders.length > 0 && (
              <div className="chip-row">
                {item.placeholders.map((name) => (
                  <span className="chip" key={name}>
                    {`{${name}}`}
                  </span>
                ))}
              </div>
            )}

            <textarea
              rows={Math.min(20, Math.max(6, Math.ceil((drafts[item.key]?.length ?? 0) / 90)))}
              value={drafts[item.key] ?? ""}
              onChange={(event) => setDrafts((prev) => ({ ...prev, [item.key]: event.target.value }))}
              spellCheck={false}
            />
          </section>
        ))}
      </div>
    </div>
  );
}
