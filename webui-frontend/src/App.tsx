import { useEffect, useState, ComponentType } from "react";
import { Loader2 } from "lucide-react";
import PodcastPage from "./pages/PodcastPage";
import DubbingPage from "./pages/DubbingPage";
import AccountPage from "./pages/AccountPage";
import OrdersPage from "./pages/OrdersPage";
import DocsPage from "./pages/DocsPage";
import { initAuth, navigate, useAuth } from "@/lib/auth";

/**
 * 零依赖路径路由：双人播客与单人配音拆为两个独立页面，便于日后
 * 在统一入口页分别跳转。路由约定：
 *   /                 → 双人播客（默认；老用户 localStorage wb-mode=dubbing 时自动迁到 /dubbing）
 *   /podcast          → 双人播客
 *   /dubbing          → 单人配音
 *   /account          → 个人中心（登录/注册/积分/最近订单）
 *   /account/orders   → 全部订单（从个人中心的「查看更多」进入）
 *   /docs             → 产品使用文档（**免登录**：说明书不该拦在登录后面）
 * 强制登录：除 /account 与 /docs 外的所有页面，未登录一律跳转 /account（登录/注册页）。
 */
function resolvePage(pathname: string): ComponentType {
  // ⚠️ `/account/orders` 必须排在 `/account` 之前 —— 前缀匹配下两者都会命中后者。
  if (pathname.startsWith("/account/orders")) return OrdersPage;
  if (pathname.startsWith("/dubbing")) return DubbingPage;
  if (pathname.startsWith("/account")) return AccountPage;
  if (pathname.startsWith("/docs")) return DocsPage;
  return PodcastPage;
}

function normalizePath(pathname: string): string {
  // 同上：更长的前缀先判，否则 /account/orders 会被归一成 /account。
  if (pathname.startsWith("/account/orders")) return "/account/orders";
  if (pathname.startsWith("/dubbing")) return "/dubbing";
  if (pathname.startsWith("/podcast")) return "/podcast";
  if (pathname.startsWith("/account")) return "/account";
  if (pathname.startsWith("/docs")) return "/docs";
  return "/";
}

function BootSpinner() {
  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center">
      <Loader2 className="w-6 h-6 text-gray-300 animate-spin" />
    </div>
  );
}

export default function App() {
  const [path, setPath] = useState(() => {
    const p = normalizePath(window.location.pathname);
    // 老用户迁移：此前靠 localStorage wb-mode 进入配音模式，落地到对应 URL
    if (p === "/" && localStorage.getItem("wb-mode") === "dubbing") {
      window.history.replaceState(null, "", "/dubbing");
      return "/dubbing";
    }
    return p;
  });

  // 登录态恢复（App 级只调一次；auth.ts 内部防重入）
  useEffect(() => {
    void initAuth();
  }, []);

  useEffect(() => {
    const onPop = () => setPath(normalizePath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // 同步 wb-mode，保留旧约定的兼容性
  useEffect(() => {
    localStorage.setItem("wb-mode", path === "/dubbing" ? "dubbing" : "podcast");
  }, [path]);

  // 强制登录：未登录访问工作页 → 跳登录/注册页（个人中心与使用文档除外）
  const { ready, token, user } = useAuth();
  const needsLogin = ready && (!token || !user) && path !== "/account" && path !== "/docs";
  useEffect(() => {
    if (needsLogin) navigate("/account");
  }, [needsLogin]);

  if (!ready || needsLogin) return <BootSpinner />;

  const Page = resolvePage(path);
  return <Page key={path} />;
}
