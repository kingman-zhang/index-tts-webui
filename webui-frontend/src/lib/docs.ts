/** 产品使用文档（/docs）的打开入口。
 *
 *  为什么固定**新标签页**：说明书是「边看边做」的东西。这是个零依赖路径路由的 SPA，
 *  在当前标签页里跳走会卸载正在编辑的工作页 —— 草稿虽然存在 localStorage 里不会丢，
 *  但用户会觉得「我被踢出去了」。所以两个入口（用户菜单、个人中心）都开新页。
 *
 *  `noopener` 防止新页通过 window.opener 拿到原页面的引用；
 *  浏览器（或被弹窗拦截插件）拦下 window.open 时退回当前标签页跳转，保证点了一定有反应。
 */
import { navigate } from "./auth";

export function openDocs() {
  const w = window.open("/docs", "_blank", "noopener");
  if (!w) navigate("/docs");
}
