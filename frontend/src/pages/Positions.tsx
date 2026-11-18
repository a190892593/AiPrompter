import { useCallback, useEffect, useState } from "react";

interface Position {
  id: number;
  name: string;
  jd: string;
  skills: string;
  updated_at: string;
}

const EMPTY = { name: "", jd: "", skills: "" };

export default function Positions() {
  const [items, setItems] = useState<Position[]>([]);
  const [form, setForm] = useState(EMPTY);
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const resp = await fetch("/api/positions");
      if (!resp.ok) throw new Error(await resp.text());
      const data = (await resp.json()) as { items: Position[] };
      setItems(data.items);
    } catch (err) {
      setError(`加载岗位失败：${(err as Error).message}`);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const call = useCallback(
    async (path: string, method: string, body?: unknown) => {
      setBusy(true);
      setError("");
      try {
        const resp = await fetch(path, {
          method,
          headers: body ? { "Content-Type": "application/json" } : undefined,
          body: body ? JSON.stringify(body) : undefined,
        });
        if (!resp.ok) throw new Error((await resp.text()).slice(0, 200));
        await load();
        return true;
      } catch (err) {
        setError((err as Error).message);
        return false;
      } finally {
        setBusy(false);
      }
    },
    [load],
  );

  const add = useCallback(async () => {
    if (!form.name.trim()) return;
    if (await call("/api/positions", "POST", form)) {
      setForm(EMPTY);
      setMessage("已添加岗位");
    }
  }, [call, form]);

  return (
    <div className="prompt-page">
      <header className="prompt-head">
        <div>
          <h1>岗位管理</h1>
          <p className="lede">
            岗位名称、JD、技能要求会一起作为上下文传给意图识别和参考答案生成——岗位写得越具体，参考答案越贴岗位。
            面试时在提词器底部的岗位下拉里选它，JD 和技能会自动带进上下文。
          </p>
        </div>
        <span className="pill">共 {items.length} 个岗位</span>
      </header>

      {error && <div className="alert">{error}</div>}
      {message && <div className="prompt-toast">{message}</div>}

      <section className="prompt-card">
        <header className="prompt-card-head">
          <strong>新增岗位</strong>
          <span className="spacer" />
          <button className="btn primary" onClick={() => void add()} disabled={busy || !form.name.trim()}>
            添加
          </button>
        </header>

        <input
          className="bank-question"
          value={form.name}
          onChange={(event) => setForm({ ...form, name: event.target.value })}
          placeholder="岗位名称，例如：RAG 算法工程师"
        />
        <textarea
          rows={3}
          value={form.jd}
          onChange={(event) => setForm({ ...form, jd: event.target.value })}
          placeholder="岗位 JD：负责什么、要求什么经验、团队情况…"
        />
        <textarea
          rows={2}
          value={form.skills}
          onChange={(event) => setForm({ ...form, skills: event.target.value })}
          placeholder="技能要求，用顿号或换行分隔：RAG、向量检索、Rerank、Python"
        />
      </section>

      <div className="prompt-list">
        {items.map((item) => (
          <section className="prompt-card" key={item.id}>
            <header className="prompt-card-head">
              <strong>{item.name}</strong>
              {editing === item.id ? (
                <>
                  <span className="spacer" />
                  <button className="btn ghost" onClick={() => setEditing(null)}>
                    取消
                  </button>
                  <button
                    className="btn primary"
                    disabled={busy}
                    onClick={async () => {
                      if (await call(`/api/positions/${item.id}`, "PUT", draft)) {
                        setEditing(null);
                        setMessage("已保存");
                      }
                    }}
                  >
                    保存
                  </button>
                </>
              ) : (
                <>
                  <span className="spacer" />
                  <button
                    className="link-btn"
                    onClick={() => {
                      setEditing(item.id);
                      setDraft({ name: item.name, jd: item.jd, skills: item.skills });
                    }}
                  >
                    编辑
                  </button>
                  <button className="link-btn" onClick={() => void call(`/api/positions/${item.id}`, "DELETE")}>
                    删除
                  </button>
                </>
              )}
            </header>

            {editing === item.id ? (
              <>
                <input
                  className="bank-question"
                  value={draft.name}
                  onChange={(event) => setDraft({ ...draft, name: event.target.value })}
                />
                <textarea
                  rows={3}
                  value={draft.jd}
                  onChange={(event) => setDraft({ ...draft, jd: event.target.value })}
                  placeholder="岗位 JD"
                />
                <textarea
                  rows={2}
                  value={draft.skills}
                  onChange={(event) => setDraft({ ...draft, skills: event.target.value })}
                  placeholder="技能要求"
                />
              </>
            ) : (
              <>
                {item.jd && <p className="bank-a">{item.jd}</p>}
                {item.skills && (
                  <div className="chip-row" style={{ marginTop: 8 }}>
                    {item.skills
                      .split(/[、,，\n]/)
                      .map((skill) => skill.trim())
                      .filter(Boolean)
                      .map((skill) => (
                        <span className="chip" key={skill}>
                          {skill}
                        </span>
                      ))}
                  </div>
                )}
              </>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
