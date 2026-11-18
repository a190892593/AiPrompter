import { useCallback, useEffect, useState } from "react";

export type Theme = "dark" | "light";

const STORAGE_KEY = "theme";

export function readTheme(): Theme {
  // 支持 ?theme=light/dark 强制指定（截图、演示、排查样式时用）
  try {
    const forced = new URLSearchParams(window.location.search).get("theme");
    if (forced === "light" || forced === "dark") return forced;
  } catch {
    /* 忽略 */
  }
  try {
    return localStorage.getItem(STORAGE_KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(theme: Theme): void {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* 隐私模式下 localStorage 可能不可用，忽略 */
  }
}

/** 主题 hook：读写主题、同步到 <html data-theme>，供所有页面共用。 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(readTheme);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((prev) => (prev === "dark" ? "light" : "dark"));
  }, []);

  return { theme, setTheme, toggle };
}
