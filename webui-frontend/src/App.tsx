import { useEffect, useState, ComponentType } from "react";
import { Loader2 } from "lucide-react";
import PodcastPage from "./pages/PodcastPage";
import DubbingPage from "./pages/DubbingPage";
import AccountPage from "./pages/AccountPage";
import OrdersPage from "./pages/OrdersPage";
import DocsPage from "./pages/DocsPage";
import LandingPage from "./pages/LandingPage";
import { initAuth, navigate, useAuth } from "@/lib/auth";

/**
 * 零依赖路径路由：双人播客与单人配音拆为两个独立页面，便于日后
 * 在统一入口页分别跳转。路由约定：
 *   /                 → 产品落地页（**免登录**；SEO 的首页权重落在这一页）
 *   /podcast          → 双人播客
 *   /dubbing          → 单人配音
 *   /account          → 个人中心（登录/注册/积分/最近订单）
 *   /account/orders   → 全部订单（从个人中心的「查看更多」进入）
 *   /docs             → 产品使用文档（**免登录**：说明书不该拦在登录后面）
 * 强制登录：除 /、/account、/docs 外的所有页面，未登录一律跳转 /account（登录/注册页）。
 *
 * ⚠️ 落地页为什么放在 `/`：`/` 是外部链接默认指向的地址，也是唯一会被自然收录的入口。
 *    生产环境的 `/` 由 nginx 直接吐**静态预渲染**的 `dist/landing.html`（见
 *    `scripts/prerender.mjs`）；这里这一份是开发环境与「预渲染产物缺失」时的回落 ——
 *    两条路径渲染的是同一个组件，内容一致。
 * ⚠️ 工作台成了 `/podcast`、`/dubbing`，所以未知路径不再回落到「播客工作台」而是
 *    显式给到 `/podcast`（保持旧行为，且这类 URL 已被 robots.txt 排除收录）。
 *
 * ⚠️ **不要恢复 `wb-mode` 的「老用户迁移」**（2026-10-16 删除）：那条逻辑是「访问 `/` 且
 *    localStorage 里 `wb-mode === 'dubbing'` 就 replaceState 到 `/dubbing`」，在 `/` 还是
 *    工作台时成立（把没有 URL 路由的年代的用户接回去）。`/` 变成落地页之后它就成了 bug：
 *    用户只要在单配页工作过，`wb-mode` 就恒为 `dubbing` ⇒ **在地址栏输入站点根地址永远
 *    会被弹回 `/dubbing`，首页反而进不去**。URL 路由已经是显式的，这个迁移没有存在意义了，
 *    连 `wb-mode` 的写入也一并删掉（否则会留下一个「看起来还有人读」的假象）。
 *    浏览器里遗留的 `wb-mode` 键无害（已无任何读取方），不主动清理。
 */
function resolvePage(pathname: string): ComponentType {
  // ⚠️ `/account/orders` 必须排在 `/account` 之前 —— 前缀匹配下两者都会命中后者。
  if (pathname.startsWith("/account/orders")) return OrdersPage;
  if (pathname.startsWith("/dubbing")) return DubbingPage;
  if (pathname.startsWith("/account")) return AccountPage;
  if (pathname.startsWith("/docs")) return DocsPage;
  if (pathname === "/") return LandingPage;
  return PodcastPage;
}

function normalizePath(pathname: string): string {
  // 同上：更长的前缀先判，否则 /account/orders 会被归一成 /account。
  if (pathname.startsWith("/account/orders")) return "/account/orders";
  if (pathname.startsWith("/dubbing")) return "/dubbing";
  if (pathname.startsWith("/podcast")) return "/podcast";
  if (pathname.startsWith("/account")) return "/account";
  if (pathname.startsWith("/docs")) return "/docs";
  // 落地页的三种等价写法都归一到 `/`（`/landing.html` 是预渲染产物的文件名，
  // 生产 nginx 已把它 301 到 `/`；这里兜住开发环境与直连静态文件的场景）
  if (pathname === "/" || pathname === "/index.html" || pathname === "/landing.html") return "/";
  return "/podcast";
}

function BootSpinner() {
  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center">
      <Loader2 className="w-6 h-6 text-gray-300 animate-spin" />
    </div>
  );
}

export default function App() {
  const [path, setPath] = useState(() => normalizePath(window.location.pathname));

  // 登录态恢复（App 级只调一次；auth.ts 内部防重入）
  useEffect(() => {
    void initAuth();
  }, []);

  useEffect(() => {
    const onPop = () => setPath(normalizePath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // 强制登录：未登录访问工作页 → 跳登录/注册页（落地页、个人中心与使用文档除外）
  const { ready, token, user } = useAuth();
  const needsLogin = ready && (!token || !user) && path !== "/" && path !== "/account" && path !== "/docs";
  useEffect(() => {
    if (needsLogin) navigate("/account");
  }, [needsLogin]);

  // 落地页是纯静态内容、不依赖登录态，**必须绕开登录态恢复的 gate**：
  // 那个 gate 在 `initAuth` 完成前先渲染一个 loading 圈 —— 对 `/`（首屏 / LCP 页面）
  // 意味着「先闪 loading 再出正文」；生产环境 `/` 本来就是静态预渲染产物，
  // React 接管时更不该把已有内容换成 spinner。
  const Page = resolvePage(path);
  if (path === "/") return <Page key={path} />;

  if (!ready || needsLogin) return <BootSpinner />;
  return <Page key={path} />;
}
