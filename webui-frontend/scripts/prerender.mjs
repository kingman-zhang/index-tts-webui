#!/usr/bin/env node
/**
 * 落地页静态预渲染：`vite build` 之后跑，产出 `dist/landing.html`。
 *
 * ## 为什么要这一步
 * 本站是 SPA：`index.html` 的 `<body>` 只有 `<div id="root"></div>`，正文全靠 JS 渲染。
 * Google 会执行 JS，但百度的抓取不保证 —— 而「首屏 HTML 里有没有正文」决定了
 * 这个页面能不能被收录。落地页承担的是「被搜到」这件事，所以它的正文必须是**静态的**。
 *
 * 做法：用 esbuild 把 `src/landing/entry-server.tsx` 打成一个 Node 可加载的 ESM 包，
 * 调 `react-dom/server` 的 `renderToStaticMarkup` 得到整页 HTML，再套上 head 写成
 * 一份独立文档 `dist/landing.html`。生产 nginx 里 `location = /` 优先吐它。
 *
 * ## 为什么不是「把正文塞进 index.html 的 #root」
 * 试过这个方案，会被否掉：index.html 同时服务 `/docs`、`/dubbing` 等路由，
 * 塞进去等于**每个工作页在 React 接管前都会闪一下落地页的内容**；
 * 而且这些路由的原始 HTML 会与 `/` 高度重复。独立文件两个问题都没有。
 *
 * ## 失败策略：**失败就让构建失败**（不是静默降级）
 * 预渲染是本页 SEO 的地基，静默失败会让人误以为「静态 HTML 有了」。
 * 于是这里任何异常都 `exit 1` 并打印可操作的原因；
 * 需要临时绕过（例如急着发一个无关的后端修复）用 `SKIP_PRERENDER=1 npm run build`。
 * 另：nginx 那条规则自带回落（`try_files /landing.html /index.html`），
 * 所以即使产物缺失，`/` 也只会退化成「React 渲染的落地页」，不会 404。
 */
import { build } from "esbuild";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DIST = join(ROOT, "dist");
const INDEX_HTML = join(DIST, "index.html");
const OUT_HTML = join(DIST, "landing.html");
const TMP_DIR = join(ROOT, "node_modules", ".cache", "landing-ssr");
// ⚠️ 必须是 **CJS**（.cjs）：`react-dom/server` 的 Node 入口内部 `require("stream")`，
//    打成 ESM 会在运行时报 `Dynamic require of "stream" is not supported`。
//    产物落在 node_modules 下，Node 从那里能正常往上找到 react（见下面 createRequire）。
const TMP_ENTRY = join(TMP_DIR, "landing.cjs");

function fail(msg) {
  console.error(`\n[prerender] ✗ ${msg}\n`);
  process.exit(1);
}

if (process.env.SKIP_PRERENDER === "1") {
  console.warn("[prerender] SKIP_PRERENDER=1 —— 跳过落地页静态预渲染（/ 将回落到 React 渲染）");
  process.exit(0);
}

if (!existsSync(INDEX_HTML)) {
  fail("找不到 dist/index.html —— 请先执行 `vite build`（本脚本由 npm run build 串在后面）");
}

// ─── 1. 把落地页（含其依赖）打成一个 Node 可加载的包 ─────────────
await mkdir(TMP_DIR, { recursive: true });
try {
  await build({
    entryPoints: [join(ROOT, "src", "landing", "entry-server.tsx")],
    outfile: TMP_ENTRY,
    bundle: true,
    format: "cjs",
    platform: "node",
    target: "node18",
    jsx: "automatic",
    // 全量打进产物：产物在 node_modules 下，若留 external 反而要处理解析路径
    // （尤其 react-dom/server 在 Node 下的条件导出）。构建期脚本不在乎体积。
    define: { "process.env.NODE_ENV": '"production"' },
    alias: { "@": join(ROOT, "src") },
    loader: { ".png": "dataurl", ".svg": "dataurl" },
    logLevel: "warning",
  });
} catch (e) {
  fail(`esbuild 打包失败：${e.message}`);
}

let mod;
try {
  mod = createRequire(import.meta.url)(TMP_ENTRY);
} catch (e) {
  fail(
    "加载预渲染产物失败 —— 最常见的原因是落地页（或其依赖）在**模块作用域**碰了浏览器 API" +
    `（window / localStorage / document）。\n  原始错误：${e.message}`
  );
}

// ─── 2. 渲染 ────────────────────────────────────────────────
let body;
try {
  body = mod.renderLanding();
} catch (e) {
  fail(`渲染落地页失败：${e.stack || e.message}`);
}
if (!body || body.length < 2000) {
  fail(`渲染结果异常（只有 ${body?.length ?? 0} 字符）—— 落地页可能整片渲染失败`);
}

// ─── 3. 复用 SPA 产物的样式与 JS 指纹 ────────────────────────
const indexHtml = await readFile(INDEX_HTML, "utf8");
const cssHref = indexHtml.match(/<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"/)?.[1];
const jsSrc = indexHtml.match(/<script[^>]+type="module"[^>]+src="([^"]+)"/)?.[1];
if (!cssHref) fail("没能从 dist/index.html 里取到样式表地址（vite 产物结构变了？）");

// ─── 3b. 复用统计片段 ────────────────────────────────────────
// 本文件的 head 是**手拼**的，不会自动继承 index.html 里的 <script>。
// 而 `/` 是流量入口 —— 统计只在 index.html 生效、落地页却没有，
// 等于丢掉了最大的一块数据，而且不会报任何错。所以这里按标记抽取同一段。
// 抽不到 = 没开统计（正常，本地构建就是这样），静默跳过；
// 抽到了却没有 website-id = 注入逻辑坏了，必须失败。
const umamiBlock = indexHtml.match(/<!-- umami:start[\s\S]*?<!-- umami:end -->/)?.[0] ?? "";
if (umamiBlock && !/data-website-id="[^"]+"/.test(umamiBlock)) {
  fail("dist/index.html 里有 umami 标记，但没读到 data-website-id —— 统计注入可能坏了");
}

const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
// canonical 的域名只有一个来源：`src/lib/site.ts`（由 entry-server 转出），不在这里再写一遍
const url = mod.SITE_URL + mod.LANDING_CANONICAL_PATH;
const jsonLd = mod
  .landingJsonLd()
  .map(o => `<script type="application/ld+json">${JSON.stringify(o)}</script>`)
  .join("\n    ");

// ─── 4. 拼整份文档 ──────────────────────────────────────────
// 站点身份（域名 / 名称 / 语言）只有一个来源：`src/lib/site.ts`，经 entry-server 转出。
// 这里不再手抄字面量 —— 改站名却漏改这份产物，是很容易发生也不会报错的那种漂移。
const html = `<!doctype html>
<html lang="${mod.SITE_LANG}">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>${esc(mod.LANDING_TITLE)}</title>
    <meta name="description" content="${esc(mod.LANDING_DESCRIPTION)}" />
    <meta name="keywords" content="${esc(mod.LANDING_KEYWORDS.join(","))}" />
    <meta name="robots" content="index,follow,max-image-preview:large" />
    <!-- 百度搜索资源平台的「HTML 标签验证」凭据。本文件的 head 是手拼的，不会继承
         index.html —— 与之**同值**，两处都要有（漏一处不报错，验证就是过不了）。 -->
    <meta name="baidu-site-verification" content="codeva-rvteFquscq" />
    <link rel="canonical" href="${url}" />
    <link rel="alternate" hreflang="${mod.SITE_LANG}" href="${url}" />
    <link rel="alternate" hreflang="x-default" href="${url}" />
    <meta property="og:type" content="website" />
    <meta property="og:site_name" content="${esc(mod.SITE_NAME)}" />
    <meta property="og:locale" content="zh_CN" />
    <meta property="og:title" content="${esc(mod.LANDING_TITLE)}" />
    <meta property="og:description" content="${esc(mod.LANDING_DESCRIPTION)}" />
    <meta property="og:url" content="${url}" />
    <meta property="og:image" content="${mod.LANDING_OG_IMAGE}" />
    <meta property="og:image:width" content="1200" />
    <meta property="og:image:height" content="630" />
    <meta name="twitter:card" content="summary_large_image" />
    <meta name="twitter:title" content="${esc(mod.LANDING_TITLE)}" />
    <meta name="twitter:description" content="${esc(mod.LANDING_DESCRIPTION)}" />
    <meta name="twitter:image" content="${mod.LANDING_OG_IMAGE}" />
    <meta name="theme-color" content="#06060c" />
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%236366f1' stroke-width='2'%3E%3Cpath d='M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z'/%3E%3Cpath d='M19 10v2a7 7 0 0 1-14 0v-2'/%3E%3Cline x1='12' y1='19' x2='12' y2='23'/%3E%3C/svg%3E" />
    <link rel="preload" as="image" href="/landing-assets/hero.webp" fetchpriority="high" />
    <link rel="stylesheet" href="${cssHref}" />
    ${jsonLd}
    ${umamiBlock}
  </head>
  <body>
    <div id="root">${body}</div>
    ${jsSrc ? `<script type="module" src="${jsSrc}"></script>` : ""}
  </body>
</html>
`;

await writeFile(OUT_HTML, html, "utf8");
await rm(TMP_DIR, { recursive: true, force: true });

// 文本量是个有用的哨兵：正常在 40k 字符上下；若掉到几千，说明内容没渲染出来
const textOnly = body.replace(/<[^>]+>/g, "").replace(/\s+/g, "");
console.log(
  `[prerender] ✓ dist/landing.html 已生成：${(html.length / 1024).toFixed(1)} KB HTML / ` +
  `正文文本 ${textOnly.length} 字 · 结构化数据 ${mod.landingJsonLd().length} 段 · ` +
  `样式 ${cssHref}${jsSrc ? ` · 脚本 ${jsSrc}` : ""}`
);
