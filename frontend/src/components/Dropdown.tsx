import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface DropdownOption {
  value: string;
  label: string;
  hint?: string;
  disabled?: boolean;
}

interface Props {
  value: string;
  options: DropdownOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  title?: string;
  placeholder?: string;
  /** 弹层最小宽度，默认 200 */
  minWidth?: number;
  /** 追加到最外层容器的 class（用于贴合各页面的布局） */
  className?: string;
}

/**
 * 自绘下拉。
 *
 * 原生 <select> 的弹层由系统绘制：暗色主题下会冒出白底黑字的系统菜单，
 * 和界面完全对不上，所以这里用 button + 固定定位面板自己画。
 * 面板用 position: fixed，避免被 .panel / .control-bar 的 overflow 裁掉。
 */
export default function Dropdown({
  value,
  options,
  onChange,
  disabled = false,
  title,
  placeholder = "请选择",
  minWidth = 200,
  className = "",
}: Props) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [box, setBox] = useState<{ left: number; width: number; top?: number; bottom?: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const current = options.find((item) => item.value === value);

  const place = useCallback(() => {
    const el = triggerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const width = Math.max(minWidth, Math.round(rect.width));
    const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
    // 下方空间不够就向上弹
    if (window.innerHeight - rect.bottom < 280) {
      setBox({ left, width, bottom: window.innerHeight - rect.top + 6 });
    } else {
      setBox({ left, width, top: rect.bottom + 6 });
    }
  }, [minWidth]);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (triggerRef.current?.contains(target) || panelRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onViewportChange = () => setOpen(false);
    window.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onEscape);
    window.addEventListener("resize", onViewportChange);
    return () => {
      window.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onEscape);
      window.removeEventListener("resize", onViewportChange);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const index = options.findIndex((item) => item.value === value);
    setActive(index >= 0 ? index : 0);
  }, [open, options, value]);

  const pick = (option: DropdownOption) => {
    if (option.disabled) return;
    setOpen(false);
    triggerRef.current?.focus();
    if (option.value !== value) onChange(option.value);
  };

  const onTriggerKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (disabled) return;
    if (!open) {
      if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => Math.min(options.length - 1, index + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => Math.max(0, index - 1));
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const option = options[active];
      if (option) pick(option);
    }
  };

  return (
    <span className={`dd ${className}`.trim()}>
      <button
        type="button"
        ref={triggerRef}
        className={`dd-trigger ${open ? "open" : ""}`}
        disabled={disabled}
        title={title}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((next) => !next)}
        onKeyDown={onTriggerKeyDown}
      >
        <span className="dd-value">{current?.label ?? placeholder}</span>
        <svg className="dd-caret" width="10" height="6" viewBox="0 0 10 6" aria-hidden="true">
          <path
            d="M1 1l4 4 4-4"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>
      {open &&
        box &&
        createPortal(
          <div
            className="dd-panel"
            ref={panelRef}
            role="listbox"
            style={{ left: box.left, width: box.width, top: box.top, bottom: box.bottom }}
          >
            {options.length === 0 && <div className="dd-empty">没有可选项</div>}
            {options.map((option, index) => (
              <button
                key={option.value}
                type="button"
                role="option"
                aria-selected={option.value === value}
                disabled={option.disabled}
                className={`dd-option ${option.value === value ? "on" : ""} ${
                  index === active ? "active" : ""
                }`}
                onMouseEnter={() => setActive(index)}
                onClick={() => pick(option)}
              >
                <span className="dd-label">{option.label}</span>
                {option.hint ? <span className="dd-hint">{option.hint}</span> : null}
                {option.value === value ? <span className="dd-check">✓</span> : null}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </span>
  );
}
