import { useEffect, useState, ComponentType } from "react";
import PodcastPage from "./pages/PodcastPage";
import DubbingPage from "./pages/DubbingPage";
import AccountPage from "./pages/AccountPage";

/**
 * 零依赖路径路由：双人播客与单人配音拆为两个独立页面，便于日后
 * 在统一入口页分别跳转。路由约定：
 *   /            → 双人播客（默认；老用户 localStorage wb-mode=dubbing 时自动迁到 /dubbing）
 *   /podcast     → 双人播客
 *   /dubbing     → 单人配音
 *   /account     → 个人中心（登录/注册/积分）
 * 页面内部没有相互跳转入口（专注当前功能）；统一入口页做好后从这里分发。
 */
function resolvePage(pathname: string): ComponentType {
  if (pathname.startsWith("/dubbing")) return DubbingPage;
  if (pathname.startsWith("/account")) return AccountPage;
  return PodcastPage;
}

function normalizePath(pathname: string): string {
  if (pathname.startsWith("/dubbing")) return "/dubbing";
  if (pathname.startsWith("/podcast")) return "/podcast";
  if (pathname.startsWith("/account")) return "/account";
  return "/";
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

  useEffect(() => {
    const onPop = () => setPath(normalizePath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // 同步 wb-mode，保留旧约定的兼容性
  useEffect(() => {
    localStorage.setItem("wb-mode", path === "/dubbing" ? "dubbing" : "podcast");
  }, [path]);

  const Page = resolvePage(path);
  return <Page key={path} />;
}
