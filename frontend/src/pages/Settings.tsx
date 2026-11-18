import { useCallback, useEffect, useState } from "react";

interface Item {
  key: string;
  kind: string;
  group: string;
  desc: string;
  secret: boolean;
  value: string;
  overridden: boolean;
}

export default function Settings() {
  const [items, setItems] = useState<Item[]>([]);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const resp = await fetch("/api/settings");
      if (!resp.ok) throw new Error(await resp.text());
      const data = (await resp.json()) as { items: Item[] };
      setItems(data.items);
      const next: Record<string, string> = {};
      data.items.forEach((item) => {
        next[item.key] = item.secret ? "" : item.value;
      });
      setValues(next);
    } catch (err) {
      setError(`加载失败：${(err as Error).message}`);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = useCallback(async () => {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const payload: Record<string, string> = {};
      items.forEach((item) => {
        const raw = values[item.key] ?? "";
        // 敏感项留空表示"不修改"，不能发空值，否则会清掉配置
        if (item.secret && raw.trim() === "") return;
        payload[item.key] = raw;
      });
      const resp = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values: payload }),
      });
      if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
      const data = (await resp.json()) as { changed: string[] };
      setMessage(`已保存并生效：${data.changed.length} 项`);
      await load();
    } catch (err) {
      setError(`保存失败：${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  }, [items, load, values]);

  const groups = Array.from(new Set(items.map((item) => item.group)));

  return (
    <div className="prompt-page">
      <header className="prompt-head">
        <div>
          <h1>设置</h1>
          <p className="lede">
            改完立即生效，会覆盖 .env 里的同名配置。敏感项留空表示不修改；清空某个非敏感项则回落到 .env 默认值。
          </p>
        </div>
        <button className="btn primary" onClick={() => void save()} disabled={busy}>
          {busy ? "保存中…" : "保存并生效"}
        </button>
      </header>

      {error && <div className="alert">{error}</div>}
      {message && <div className="prompt-toast">{message}</div>}

      {groups.map((group) => (
        <section className="prompt-card" key={group}>
          <header className="prompt-card-head">
            <strong>{group}</strong>
          </header>
          <div className="settings-grid">
            {items
              .filter((item) => item.group === group)
              .map((item) => (
                <label className="field" key={item.key}>
                  <span>
                    {item.key}
                    {item.overridden && <em className="overridden"> 已覆盖 .env</em>}
                  </span>
                  {item.kind === "bool" ? (
                    <select
                      value={values[item.key] ?? "1"}
                      onChange={(event) => setValues({ ...values, [item.key]: event.target.value })}
                    >
                      <option value="1">开</option>
                      <option value="0">关</option>
                    </select>
                  ) : (
                    <input
                      type={item.secret ? "password" : "text"}
                      value={values[item.key] ?? ""}
                      placeholder={item.secret ? `${item.value}（留空不修改）` : item.desc}
                      onChange={(event) => setValues({ ...values, [item.key]: event.target.value })}
                    />
                  )}
                  <small className="field-hint">{item.desc}</small>
                </label>
              ))}
          </div>
        </section>
      ))}
    </div>
  );
}
