/** 极简 history 路由：每个菜单页都有独立 URL，刷新/转发/后退都能回到原页面。
 *
 * - `/`                     面试列表
 * - `/interviews/<id>`      实时面试页
 * - `/detail/<id>`          面试详情
 * - `/prompts` `/bank` `/positions` `/resumes` `/settings` `/balance` `/logs`
 */

export type PageKey =
  | "prompts"
  | "bank"
  | "positions"
  | "resumes"
  | "settings"
  | "balance"
  | "logs";

export type Route =
  | { name: "list" }
  | { name: "interview"; id: string }
  | { name: "detail"; id: string }
  | { name: "page"; page: PageKey };

export const PAGE_PATHS: Record<PageKey, string> = {
  prompts: "/prompts",
  bank: "/bank",
  positions: "/positions",
  resumes: "/resumes",
  settings: "/settings",
  balance: "/balance",
  logs: "/logs",
};

const INTERVIEW_PATH = /^\/interviews\/([A-Za-z0-9_-]+)\/?$/;
const DETAIL_PATH = /^\/detail\/([A-Za-z0-9_-]+)\/?$/;

export function parseRoute(pathname: string = window.location.pathname): Route {
  const interview = INTERVIEW_PATH.exec(pathname);
  if (interview) return { name: "interview", id: interview[1] };
  const detail = DETAIL_PATH.exec(pathname);
  if (detail) return { name: "detail", id: detail[1] };
  for (const [page, path] of Object.entries(PAGE_PATHS)) {
    if (pathname === path || pathname === `${path}/`) {
      return { name: "page", page: page as PageKey };
    }
  }
  return { name: "list" };
}

export function interviewPath(id: string): string {
  return `/interviews/${encodeURIComponent(id)}`;
}

export function detailPath(id: string): string {
  return `/detail/${encodeURIComponent(id)}`;
}

export function navigate(path: string, replace = false): void {
  if (window.location.pathname === path) return;
  if (replace) window.history.replaceState({}, "", path);
  else window.history.pushState({}, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
