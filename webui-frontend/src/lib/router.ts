/**
 * 零依赖路径路由跳转（与 App.tsx 的 popstate 路由配套）。
 *
 * 为什么单独成文件、而不是留在 `lib/auth.ts` 里
 * -------------------------------------------
 * `lib/auth.ts` 在**模块作用域**读 `localStorage.getItem(TOKEN_KEY)`。落地页要做
 * 构建期静态预渲染（`scripts/prerender.mjs` 在 Node 里 `renderToStaticMarkup`），
 * 而 Node 侧没有 localStorage ⇒ 只要落地页的依赖链上出现 `lib/auth.ts`，预渲染就会抛错。
 *
 * 所以跳转能力抽到这里：谁需要跳转就引这里，不必顺路把「会员模块 + 浏览器存储」拖进来。
 * `lib/auth.ts` 仍然 `export { navigate }`，老的引用路径保持不变。
 */
export function navigate(path: string) {
  window.history.pushState(null, "", path);
  window.dispatchEvent(new PopStateEvent("popstate"));
}
