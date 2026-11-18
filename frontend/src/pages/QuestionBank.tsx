import { useCallback, useEffect, useState } from "react";

interface BankItem {
  id: number;
  category: string;
  question: string;
  answer: string;
  updated_at: string;
}

const EMPTY = { category: "通用", question: "", answer: "" };

export default function QuestionBank() {
  const [items, setItems] = useState<BankItem[]>([]);
  const [categories, setCategories] = useState<string[]>([]);
  const [form, setForm] = useState(EMPTY);
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      const resp = await fetch("/api/bank");
      if (!resp.ok) throw new Error(await resp.text());
      const data = (await resp.json()) as { items: BankItem[]; categories: string[] };
      setItems(data.items);
      setCategories(data.categories);
    } catch (err) {
      setError(`加载题库失败：${(err as Error).message}`);
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
    if (!form.question.trim()) return;
    const ok = await call("/api/bank", "POST", form);
    if (ok) {
      setForm({ ...EMPTY, category: form.category });
      setMessage("已添加");
    }
  }, [call, form]);

  return (
    <div className="prompt-page bank-page">
      <header className="prompt-head">
        <div>
          <h1>面试题库</h1>
          <p className="lede">
            录入常见面试题和参考答案。仿真面试会按这里的题目顺序逐条提问，最多 10 条。
            参考答案留空也没关系，系统会自己生成。
          </p>
        </div>
        <span className="pill">共 {items.length} 题</span>
      </header>

      {error && <div className="alert">{error}</div>}
      {message && <div className="prompt-toast">{message}</div>}

      <section className="prompt-card">
        <header className="prompt-card-head">
          <strong>新增题目</strong>
          <span className="spacer" />
          <button className="btn primary" onClick={() => void add()} disabled={busy || !form.question.trim()}>
            添加
          </button>
        </header>

        <div className="bank-form">
          <input
            className="bank-category"
            value={form.category}
            onChange={(event) => setForm({ ...form, category: event.target.value })}
            placeholder="分类"
            list="bank-categories"
          />
          <input
            className="bank-question"
            value={form.question}
            onChange={(event) => setForm({ ...form, question: event.target.value })}
            placeholder="面试题，例如：如何减少 LLM 的幻觉问题？"
            onKeyDown={(event) => {
              if (event.key === "Enter") void add();
            }}
          />
        </div>
        <textarea
          rows={2}
          value={form.answer}
          onChange={(event) => setForm({ ...form, answer: event.target.value })}
          placeholder="参考答案（可选）"
        />
        <datalist id="bank-categories">
          {categories.map((name) => (
            <option key={name} value={name} />
          ))}
        </datalist>
      </section>

      <div className="prompt-list">
        {items.map((item) => (
          <section className="prompt-card" key={item.id}>
            <header className="prompt-card-head">
              <span className="bubble-who">{item.category}</span>
              {editing === item.id ? (
                <>
                  <span className="spacer" />
                  <button
                    className="btn ghost"
                    onClick={() => {
                      setEditing(null);
                    }}
                  >
                    取消
                  </button>
                  <button
                    className="btn primary"
                    disabled={busy}
                    onClick={async () => {
                      const ok = await call(`/api/bank/${item.id}`, "PUT", draft);
                      if (ok) {
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
                      setDraft({ category: item.category, question: item.question, answer: item.answer });
                    }}
                  >
                    编辑
                  </button>
                  <button
                    className="link-btn"
                    onClick={() => void call(`/api/bank/${item.id}`, "DELETE")}
                  >
                    删除
                  </button>
                </>
              )}
            </header>

            {editing === item.id ? (
              <>
                <input
                  className="bank-question"
                  value={draft.question}
                  onChange={(event) => setDraft({ ...draft, question: event.target.value })}
                />
                <textarea
                  rows={3}
                  value={draft.answer}
                  onChange={(event) => setDraft({ ...draft, answer: event.target.value })}
                  placeholder="参考答案"
                />
              </>
            ) : (
              <>
                <p className="bank-q">{item.question}</p>
                {item.answer && <p className="bank-a">{item.answer}</p>}
              </>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
