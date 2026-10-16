/**
 * 落地页的 **Node 侧渲染入口**（只被 `scripts/prerender.mjs` 在构建期引用，不进浏览器包）。
 *
 * 它把落地页渲染成一段静态 HTML，写进 `dist/landing.html`；生产的 nginx 用
 * `location = /` 直接吐这个文件 —— 于是爬虫不执行 JS 也能读到全文。
 *
 * 这里刻意把 head 需要的数据一并再导出：让构建脚本只 import 这一个文件，
 * 不必关心内容散在 content.ts / jsonld.ts 哪一处。
 */
import { renderToStaticMarkup } from "react-dom/server";
import LandingPage from "@/pages/LandingPage";

export {
  LANDING_CANONICAL_PATH,
  LANDING_DESCRIPTION,
  LANDING_KEYWORDS,
  LANDING_OG_IMAGE,
  LANDING_TITLE,
} from "./content";
export { landingJsonLd } from "./jsonld";
export { SITE_LANG, SITE_NAME, SITE_URL } from "@/lib/site";

/** 渲染落地页正文（不含 <html>/<head>，由构建脚本拼装整份文档）。 */
export function renderLanding(): string {
  return renderToStaticMarkup(<LandingPage />);
}
