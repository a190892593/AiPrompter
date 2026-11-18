import { useCallback, useEffect, useRef, useState } from "react";
import { stripPronunciation } from "../text";

const LEVELS = [
  { key: "brief", label: "极简" },
  { key: "standard", label: "标准" },
  { key: "deep", label: "深度" },
] as const;

type LevelKey = (typeof LEVELS)[number]["key"];

type Modes = Record<string, { thinking?: boolean; web?: boolean }> | undefined;

interface Props {
  answers?: Record<string, string> | null;
  fallback?: string;
  modes?: Modes;
  pending?: boolean;
  className?: string;
}

/** 默认列宽比例 1:2:3（极简 1/6、标准 2/6、深度 3/6），可拖拽列边界调整并记忆。 */
const DEFAULT_RATIOS = [1 / 6, 2 / 6, 3 / 6];
const STORAGE_KEY = "answer-col-ratios";
const MIN_RATIO = 0.08;

function loadRatios(): number[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULT_RATIOS;
    const parsed = JSON.parse(raw) as unknown;
    if (
      Array.isArray(parsed) &&
      parsed.length === 3 &&
      parsed.every((value) => typeof value === "number" && value > MIN_RATIO)
    ) {
      const sum = (parsed as number[]).reduce((acc, value) => acc + value, 0);
      return (parsed as number[]).map((value) => value / sum);
    }
  } catch {
    /* 忽略 */
  }
  return DEFAULT_RATIOS;
}

function modeLabel(mode?: { thinking?: boolean; web?: boolean }): { text: string; cls: string } {
  if (!mode) return { text: "", cls: "" };
  if (mode.thinking && mode.web) return { text: "深度思考+联网搜索", cls: "think" };
  if (mode.thinking) return { text: "深度思考", cls: "think" };
  if (mode.web) return { text: "联网搜索", cls: "web" };
  return { text: "", cls: "" };
}

export default function AnswerColumns({ answers, fallback, modes, pending, className }: Props) {
  const [ratios, setRatios] = useState<number[]>(loadRatios);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ index: number; startX: number; startRatios: number[]; width: number } | null>(
    null,
  );

  // 列宽记忆到 localStorage
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(ratios));
    } catch {
      /* 忽略 */
    }
  }, [ratios]);

  const textFor = useCallback(
    (key: LevelKey): string => {
      if (key === "standard") return answers?.standard ?? fallback ?? "";
      return answers?.[key] ?? "";
    },
    [answers, fallback],
  );

  const onMove = useCallback((event: MouseEvent) => {
    const drag = dragRef.current;
    if (!drag || !containerRef.current) return;
    const delta = (event.clientX - drag.startX) / Math.max(1, drag.width);
    const next = [...drag.startRatios];
    next[drag.index] = Math.max(MIN_RATIO, drag.startRatios[drag.index] + delta);
    next[drag.index + 1] = Math.max(MIN_RATIO, drag.startRatios[drag.index + 1] - delta);
    // 归一化，保证总和为 1（拖动时其余列按比例保持）
    const sum = next.reduce((acc, value) => acc + value, 0);
    setRatios(next.map((value) => value / sum));
  }, []);

  const onUp = useCallback(() => {
    dragRef.current = null;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    window.removeEventListener("mousemove", onMove);
    window.removeEventListener("mouseup", onUp);
  }, [onMove]);

  const startDrag = useCallback(
    (index: number) => (event: React.MouseEvent<HTMLDivElement>) => {
      event.preventDefault();
      if (!containerRef.current) return;
      dragRef.current = {
        index,
        startX: event.clientX,
        startRatios: [...ratios],
        width: containerRef.current.getBoundingClientRect().width,
      };
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [onMove, onUp, ratios],
  );

  useEffect(() => {
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [onMove, onUp]);

  return (
    <div className={`answer-table ${className ?? ""}`} ref={containerRef}>
      {LEVELS.map((level, index) => {
        const text = textFor(level.key);
        const mode = modeLabel(modes?.[level.key]);
        const isPending = Boolean(pending) && level.key === "standard" && text;
        return (
          <div className="answer-table-frag" key={level.key} style={{ display: "contents" }}>
            <div
              className={`answer-col ${level.key} answer-table-cell`}
              style={{ flexGrow: ratios[index], flexBasis: 0 }}
            >
              <div className="answer-col-head">
                <span className={`col-tag ${level.key}`}>{level.label}</span>
                {mode.text && <span className={`answer-mode ${mode.cls}`}>{mode.text}</span>}
              </div>
              <p className="answer-col-body">
                {text ? stripPronunciation(text) : pending ? "生成中…" : "（未生成）"}
                {isPending && <span className="caret" />}
              </p>
            </div>
            {index < LEVELS.length - 1 && (
              <div
                className="answer-col-resize"
                onMouseDown={startDrag(index)}
                title="拖动调整列宽"
                role="separator"
                aria-orientation="vertical"
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
