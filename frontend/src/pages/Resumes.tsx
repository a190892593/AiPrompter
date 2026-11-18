import { useCallback, useEffect, useRef, useState } from "react";

interface ResumeRow {
  id: number;
  name: string;
  updated_at: string;
}

interface ResumeDetail extends ResumeRow {
  content: string;
}

const EMPTY = { name: "", content: "" };

export default function Resumes() {
  const [items, setItems] = useState<ResumeRow[]>([]);
  const [current, setCurrent] = useState<ResumeDetail | null>(null);
  const [draft, setDraft] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [parsing, setParsing] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const load = useCallback(async () => {
    try {
      const resp = await fetch("/api/resumes");
      if (!resp.ok) throw new Error(await resp.text());
      const data = (await resp.json()) as { items: ResumeRow[] };
      setItems(data.items);
    } catch (err) {
      setError(`加载失败：${(err as Error).message}`);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const open = useCallback(async (id: number) => {
    const resp = await fetch(`/api/resumes/${id}`);
    if (!resp.ok) return;
    const data = (await resp.json()) as ResumeDetail;
    setCurrent(data);
    setDraft({ name: data.name, content: data.content });
  }, []);

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

  const upload = useCallback(async (file: File) => {
    setParsing(true);
    setError("");
    setMessage("");
    try {
      const buffer = await file.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      let binary = "";
      const chunk = 0x8000;
      for (let i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + chunk)));
      }
      const dataBase64 = btoa(binary);
      const resp = await fetch("/api/resumes/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, data_base64: dataBase64 }),
      });
      const data = (await resp.json()) as {
        ok?: boolean;
        name?: string;
        content?: string;
        chars?: number;
        detail?: string;
      };
      if (!resp.ok) throw new Error(data.detail || "解析失败");
      setCurrent(null);
      setDraft({ name: data.name || file.name, content: data.content || "" });
      setMessage(`已解析 ${data.chars ?? 0} 字，请检查内容后点「新增」保存`);
    } catch (err) {
      setError(`上传失败：${(err as Error).message}`);
    } finally {
      setParsing(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }, []);

  return (
    <div className="prompt-page">
      <header className="prompt-head">
        <div>
          <h1>简历管理</h1>
          <p className="lede">
            保存多份个人简历。新建面试时在下拉框里选一份，内容会作为「我的背景」传给意图识别和回答生成。
            支持上传 PDF / Word 简历，自动转成 Markdown。
          </p>
        </div>
        <span className="pill">共 {items.length} 份</span>
      </header>

      {error && <div className="alert">{error}</div>}
      {message && <div className="prompt-toast">{message}</div>}

      <section className="prompt-card">
        <header className="prompt-card-head">
          <strong>{current ? `编辑：${current.name}` : "新增简历"}</strong>
          <span className="spacer" />
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.docx,.doc,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            style={{ display: "none" }}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void upload(file);
            }}
          />
          <button
            className="btn ghost"
            disabled={parsing}
            onClick={() => fileRef.current?.click()}
            title="支持 PDF 和 Word（.docx），上传后自动转成 Markdown 简历"
          >
            {parsing ? "解析中…" : "上传简历（PDF/Word）"}
          </button>
          {current && (
            <button
              className="btn ghost"
              onClick={() => {
                setCurrent(null);
                setDraft(EMPTY);
              }}
            >
              新建
            </button>
          )}
          <button
            className="btn primary"
            disabled={busy || !draft.name.trim()}
            onClick={async () => {
              const ok = current
                ? await call(`/api/resumes/${current.id}`, "PUT", draft)
                : await call("/api/resumes", "POST", draft);
              if (ok) {
                setMessage("已保存");
                if (!current) {
                  setDraft(EMPTY);
                }
              }
            }}
          >
            {current ? "保存修改" : "新增"}
          </button>
        </header>

        <input
          className="bank-question"
          value={draft.name}
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          placeholder="简历名称，例如：RAG 算法岗简历"
        />
        <textarea
          rows={10}
          value={draft.content}
          onChange={(event) => setDraft({ ...draft, content: event.target.value })}
          placeholder="粘贴简历要点：负责的项目、技术栈、量化结果、踩过的坑…"
        />
      </section>

      <div className="prompt-list">
        {items.map((item) => (
          <section className="prompt-card" key={item.id}>
            <header className="prompt-card-head">
              <strong>{item.name}</strong>
              <span className="spacer" />
              <button className="link-btn" onClick={() => void open(item.id)}>
                编辑
              </button>
              <button className="link-btn" onClick={() => void call(`/api/resumes/${item.id}`, "DELETE")}>
                删除
              </button>
            </header>
          </section>
        ))}
      </div>
    </div>
  );
}
