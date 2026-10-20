/** 产品使用文档（/docs）的打开入口。
 *
 *  为什么固定**新标签页**：说明书是「边看边做」的东西。这是个零依赖路径路由的 SPA，
 *  在当前标签页里跳走会卸载正在编辑的工作页 —— 草稿虽然存在 localStorage 里不会丢，
 *  但用户会觉得「我被踢出去了」。所以两个入口（用户菜单、个人中心）都开新页。
 *
 *  🔴 为什么用 <a target="_blank"> 而不是 `window.open(url, "_blank", "noopener")`：
 *  后者在 Chrome / Safari 里**恒返回 null**（`noopener` 让新窗口不共享 window，
 *  规范上就没有句柄可回）—— 于是「打不开 ⇒ 退回当前页跳转」这段兜底**每次都会执行**：
 *  表现就是「新标签页开了一个 + 当前页也跳走了」，正好抵消本意（2026-10-19 修）。
 *  用真实链接点击既保证新标签页，也保留浏览器自己的中键/新窗口行为，不需要兜底。
 */
export function openDocs() {
  const a = document.createElement("a");
  a.href = "/docs";
  a.target = "_blank";
  // noopener：新页拿不到 window.opener（防被开页面反控原页面）；
  // noreferrer：不把当前 URL 带进 Referer。
  a.rel = "noopener noreferrer";
  document.body.appendChild(a);
  a.click();
  a.remove();
}
