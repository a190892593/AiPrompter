import { useCallback, useEffect, useState } from "react";
import { useTheme } from "./theme";
import { useBridge } from "./bridge/useBridge";
import {
  PAGE_PATHS,
  detailPath,
  interviewPath,
  navigate,
  parseRoute,
  type PageKey,
  type Route,
} from "./router";
import InterviewList from "./pages/InterviewList";
import Teleprompter from "./pages/Teleprompter";
import PromptTemplates from "./pages/PromptTemplates";
import QuestionBank from "./pages/QuestionBank";
import Positions from "./pages/Positions";
import Resumes from "./pages/Resumes";
import Balance from "./pages/Balance";
import LLMLogs from "./pages/LLMLogs";
import InterviewDetail from "./pages/InterviewDetail";
import Settings from "./pages/Settings";

type NavKey = "interviews" | PageKey;

const MAIN_NAV: { key: NavKey; icon: string; label: string }[] = [
  { key: "interviews", icon: "🎙", label: "面试列表" },
  { key: "prompts", icon: "📝", label: "提示词模板" },
  { key: "bank", icon: "📚", label: "面试题库" },
  { key: "positions", icon: "💼", label: "岗位管理" },
  { key: "resumes", icon: "📄", label: "简历管理" },
  { key: "logs", icon: "🧾", label: "调用日志" },
];

const TITLES: Record<NavKey, string> = {
  interviews: "面试列表",
  prompts: "提示词模板",
  bank: "面试题库",
  positions: "岗位管理",
  resumes: "简历管理",
  settings: "设置",
  balance: "余额",
  logs: "调用日志",
};

function pathOf(key: NavKey): string {
  if (key === "interviews") return "/";
  return PAGE_PATHS[key as PageKey];
}

/** 当前页面在菜单里的高亮项；详情页/实时面试页归到「面试列表」。 */
function activeNav(route: Route): NavKey | null {
  if (route.name === "list") return "interviews";
  if (route.name === "page") return route.page;
  return "interviews";
}

function pageTitle(route: Route): string {
  if (route.name === "interview") return "面试进行中";
  if (route.name === "detail") return "面试详情";
  if (route.name === "list") return "面试列表";
  return TITLES[route.page];
}

export default function App() {
  const bridge = useBridge();
  const [route, setRoute] = useState<Route>(() => parseRoute());
  const [collapsed, setCollapsed] = useState(false);
  const { theme, setTheme } = useTheme();

  // 浏览器前进/后退
  useEffect(() => {
    const onPop = () => setRoute(parseRoute());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // 兼容旧的 ?tab=xxx 深链：转成正式路径
  useEffect(() => {
    try {
      const wanted = new URLSearchParams(window.location.search).get("tab");
      if (!wanted) return;
      if (wanted === "interviews") {
        navigate("/", true);
        setRoute({ name: "list" });
        return;
      }
      if (wanted in PAGE_PATHS) {
        navigate(PAGE_PATHS[wanted as PageKey], true);
        setRoute({ name: "page", page: wanted as PageKey });
      }
    } catch {
      /* 忽略 */
    }
  }, []);

  useEffect(() => {
    document.title = `${pageTitle(route)} · 面试陪练`;
  }, [route]);

  const clickNav = (event: React.MouseEvent<HTMLAnchorElement>, path: string) => {
    // 保留「新标签页打开」等浏览器原生行为
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    navigate(path);
  };

  // 稳定的回列表回调：不要用内联箭头，否则会触发面试页的 effect 反复拉取数据
  const exitInterview = useCallback(() => navigate("/"), []);

  const renderPage = () => {
    if (route.name === "interview") {
      return <Teleprompter bridge={bridge} interviewId={route.id} onEnded={exitInterview} />;
    }
    if (route.name === "detail") {
      return <InterviewDetail id={route.id} onBack={exitInterview} />;
    }
    if (route.name === "page") {
      switch (route.page) {
        case "prompts":
          return <PromptTemplates />;
        case "bank":
          return <QuestionBank />;
        case "positions":
          return <Positions />;
        case "resumes":
          return <Resumes />;
        case "settings":
          return <Settings />;
        case "balance":
          return <Balance />;
        case "logs":
          return <LLMLogs />;
      }
    }
    return (
      <InterviewList
        bridge={bridge}
        onOpen={(id) => navigate(interviewPath(id))}
        onReplay={(id) => navigate(detailPath(id))}
      />
    );
  };

  const current = activeNav(route);

  const NavLink = ({ item }: { item: { key: NavKey; icon: string; label: string } }) => (
    <a
      href={pathOf(item.key)}
      className={`rail-item ${current === item.key ? "on" : ""}`}
      onClick={(event) => clickNav(event, pathOf(item.key))}
      title={item.label}
    >
      <span className="rail-icon">{item.icon}</span>
      <span className="rail-label">{item.label}</span>
      {current === item.key && <span className="rail-check">✓</span>}
    </a>
  );

  return (
    <div className="app-shell">
      <div className="app-body">
        <aside className={`rail ${collapsed ? "collapsed" : ""}`}>
          <div className="rail-head">
            <div className="brand" title="面试陪练 · AI 实时提词">
              <span className="brand-mark">陪</span>
              <div className="brand-text">
                <strong>面试陪练</strong>
                <small>AI 实时提词</small>
              </div>
            </div>
            <button
              className="rail-collapse"
              onClick={() => setCollapsed((value) => !value)}
              title={collapsed ? "展开菜单" : "收起菜单"}
            >
              {collapsed ? "»" : "«"}
            </button>
          </div>

          <nav className="rail-nav">
            {MAIN_NAV.map((item) => (
              <NavLink key={item.key} item={item} />
            ))}
          </nav>
        </aside>

        <main className="app-main">
          <header className="top-nav">
            <div className="top-actions">
              <a
                href={PAGE_PATHS.balance}
                className={`top-link ${current === "balance" ? "on" : ""}`}
                onClick={(event) => clickNav(event, PAGE_PATHS.balance)}
                title="余额"
              >
                💰 余额
              </a>
              <button
                className="top-link"
                onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
                title={theme === "dark" ? "切换到白天模式" : "切换到夜间模式"}
              >
                {theme === "dark" ? "🌙 夜间模式" : "☀️ 白天模式"}
              </button>
              <a
                href={PAGE_PATHS.settings}
                className={`top-link ${current === "settings" ? "on" : ""}`}
                onClick={(event) => clickNav(event, PAGE_PATHS.settings)}
                title="设置"
              >
                ⚙ 设置
              </a>
            </div>
          </header>
          <div className="app-content">{renderPage()}</div>
        </main>
      </div>
    </div>
  );
}
