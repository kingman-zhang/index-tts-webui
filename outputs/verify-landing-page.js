/**
 * 落地页端到端验收（只读、离线）：把 `dist/` 按**生产 nginx 的规则**伺服起来，
 * 再用真实 Chromium 打开 `/`，量布局、断言内容、跑真点击，并留下截图。
 *
 * 为什么要自己起一个服务器：生产的 `/` 不是 SPA 的 index.html，而是构建期预渲染出来的
 * `dist/landing.html`（见 webui-frontend/scripts/prerender.mjs 与 nginx.conf 的
 * `location = /`）。用 `vite preview` 或 python -m http.server 都伺服不出这条规则，
 * 于是「预渲染产物到底能不能正常打开」这件事永远验不到。这里把那条规则复刻了一遍：
 *
 *     /                → landing.html 存在则给 landing.html，否则回落到 index.html
 *     /landing.html    → 301 → /
 *     /assets/*、其他   → 磁盘上的文件；不存在则回落到 index.html（SPA 路由）
 *
 * 用法：
 *   NODE_PATH=/Users/zhangjianwen/.workbuddy/binaries/node/workspace/node_modules \
 *   /Users/zhangjianwen/.workbuddy/binaries/node/versions/22.22.2-6/bin/node \
 *   outputs/verify-landing-page.js [--dev http://localhost:6008]
 *
 * 传 --dev 时只做「开发服务器」那一路的检查（内容断言 + 截图），不量预渲染产物。
 */
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const puppeteer = require("puppeteer-core");

const DIST = path.resolve(__dirname, "..", "webui-frontend", "dist");
const OUT = path.resolve(__dirname);
const PORT = 6099;
const CHROME = "/Users/zhangjianwen/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium";
const DEV_URL = process.argv.includes("--dev") ? "http://localhost:6008" : null;

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
  ".webp": "image/webp", ".jpg": "image/jpeg", ".png": "image/png", ".svg": "image/svg+xml",
  ".mp3": "audio/mpeg", ".txt": "text/plain; charset=utf-8", ".xml": "application/xml",
  ".json": "application/json", ".woff2": "font/woff2",
};

/** 复刻 nginx：`location = /` 优先 landing.html，`location = /landing.html` 301 回 /。 */
function startStatic() {
  return new Promise(resolve => {
    const server = http.createServer((req, res) => {
      const url = new URL(req.url, "http://x");
      let p = decodeURIComponent(url.pathname);
      // 「已登录」那条断言要用的假后端：只认带 Authorization 的请求，行为与 backend 一致
      // （没 token = 401）。不连真后端，保证这个脚本始终离线可跑。
      if (p === "/api/auth/me") {
        const ok = /^Bearer\s+\S+/.test(req.headers.authorization || "");
        res.writeHead(ok ? 200 : 401, { "Content-Type": "application/json" });
        return res.end(JSON.stringify(ok ? { user: MOCK_USER } : { detail: "未登录" }));
      }
      const landing = path.join(DIST, "landing.html");
      if (p === "/landing.html") { res.writeHead(301, { Location: "/" }); return res.end(); }
      if (p === "/") p = fs.existsSync(landing) ? "/landing.html" : "/index.html";
      let file = path.join(DIST, p);
      if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST, "index.html");
      res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream" });
      fs.createReadStream(file).pipe(res);
    });
    server.listen(PORT, "127.0.0.1", () => resolve(server));
  });
}

/** 假用户（字段对齐 `MemberUser`；只用于「已登录导航」这一条断言）。 */
const MOCK_USER = {
  user_id: "u_000000000000", username: "verify", nickname: "验证账号",
  bio: "", points: 500, disabled: false,
};
/** 预置到 localStorage 的 token（本脚本不校验它，`/api/auth/me` 只认「有没有」）。 */
const MOCK_TOKEN = "verify-token";

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`  ${ok ? "✓" : "✗"} ${name}${detail ? " — " + detail : ""}`);
}

(async () => {
  const server = DEV_URL ? null : await startStatic();
  const base = DEV_URL || `http://127.0.0.1:${PORT}`;
  const browser = await puppeteer.launch({ executablePath: CHROME, args: ["--no-sandbox", "--font-render-hinting=none"] });

  try {
    // ── 1. 内容断言（桌面宽）────────────────────────────────
    const page = await browser.newPage();
    const consoleErrors = [];
    page.on("console", m => { if (m.type() === "error") consoleErrors.push(m.text()); });
    page.on("pageerror", e => consoleErrors.push("pageerror: " + e.message));
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    const resp = await page.goto(base + "/", { waitUntil: "networkidle2" });
    console.log(`\n== ${DEV_URL ? "开发服务器" : "静态产物（生产路径）"}：${base}/ ==`);
    check("HTTP 200", resp && resp.status() === 200, `status=${resp && resp.status()}`);

    const title = await page.title();
    check("title 含主词「AI 配音」", /AI\s*配音/.test(title), title);

    const canonical = await page.$eval('link[rel="canonical"]', el => el.href).catch(() => null);
    if (!DEV_URL) {
      check("canonical 指向首页", canonical === "https://media.lizigege.com/", String(canonical));
      const ldjson = await page.$$eval('script[type="application/ld+json"]', els => els.map(e => e.textContent));
      check("结构化数据含 FAQPage 与 SoftwareApplication",
        ldjson.some(t => t.includes("FAQPage")) && ldjson.some(t => t.includes("SoftwareApplication")),
        `${ldjson.length} 段`);
      const faqCount = JSON.parse(ldjson.find(t => t.includes("FAQPage"))).mainEntity.length;
      const detailsCount = await page.$$eval("details", els => els.length);
      check("FAQ 结构化条目数 == 页面可见问答数", faqCount === detailsCount, `${faqCount} vs ${detailsCount}`);
      // 静态 HTML（不执行 JS）也能读到正文：直接抓原始响应
      const raw = await page.evaluate(async u => (await fetch(u)).text(), base + "/");
      const textOnly = raw.replace(/<script[\s\S]*?<\/script>/g, "").replace(/<[^>]+>/g, "").replace(/\s+/g, "");
      check("静态 HTML 里就有正文（不执行 JS 也可见）", textOnly.length > 4000, `${textOnly.length} 字`);
      check("静态 HTML 里有 H1 与最佳实践正文", raw.includes("<h1") && raw.includes("照着案例 A 做第一条"));
      // 预渲染产物是**未登录**形态：爬虫/首屏看到的永远是注册入口，不会带用户态内容。
      check("静态 HTML 是未登录形态（不含「控制台」）", !raw.includes("控制台"));
    }

    // ── 2.5 登录态：已登录用户的导航是「控制台」，不是注册入口 ────────
    // 生产 `/` 是静态产物，React 接管后才按 localStorage 里的 token 决定导航形态。
    // 这里预置 token + 用上面 mock 的 `/api/auth/me`，跑一次真点击。仅静态产物路径可跑
    // （开发服务器那一侧没有后端）。
    if (!DEV_URL) {
      // ⚠️ 必须用**独立的 browser context**：`localStorage` 是按 origin 共享的，
      //    在同一个 context 里预置 token 会污染后面「未登录」那几条真点击断言
      //    （页面重新加载后变成已登录形态，于是找不到「免费注册」）。
      const ctx = await browser.createBrowserContext();
      const p2 = await ctx.newPage();
      await p2.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
      await p2.evaluateOnNewDocument(t => localStorage.setItem("wb-auth-token", t), MOCK_TOKEN);
      await p2.goto(base + "/", { waitUntil: "networkidle2" });
      const nav = await p2.evaluate(() => {
        const labels = [...document.querySelectorAll("header button")].map(b => b.textContent.trim());
        return { labels, hasDash: labels.includes("控制台"), hasRegister: labels.some(l => l.includes("免费注册")) };
      });
      check("已登录：导航显示「控制台」", nav.hasDash, nav.labels.join(" / "));
      check("已登录：导航不再显示「免费注册」与「登录」",
        !nav.hasRegister && !nav.labels.includes("登录"), nav.labels.join(" / "));
      await p2.screenshot({
        path: path.join(OUT, "2026-10-16-落地页-已登录导航.png"),
        clip: { x: 0, y: 0, width: 1440, height: 120 },
      });
      const clicked = await p2.evaluate(() => {
        const b = [...document.querySelectorAll("header button")].find(x => x.textContent.trim() === "控制台");
        if (!b) return false;
        b.click();
        return true;
      });
      await new Promise(r => setTimeout(r, 400));
      check("已登录：点「控制台」跳到单人配音 /dubbing",
        clicked && p2.url().endsWith("/dubbing"), p2.url());
      await ctx.close();
    }

    // ── 2. 关键内容在位 ────────────────────────────────────
    const bodyText = await page.evaluate(() => document.body.innerText);
    for (const kw of ["AI 配音工具", "375 条音色", "最佳实践", "案例 A", "案例 B", "案例 C",
                      "注册即送 500 积分", "常见问题", "9 种情绪"]) {
      check(`页面可见「${kw}」`, bodyText.includes(kw));
    }
    check("页脚/正文未出现模板残留（如 未命名、TODO、Lorem）",
      !/未命名|Lorem|TODO\(/.test(bodyText));

    // ── 3. 布局：无横向溢出 ────────────────────────────────
    const sizes = [
      { w: 1440, h: 900, tag: "desktop" },
      { w: 1024, h: 800, tag: "laptop" },
      { w: 768, h: 1024, tag: "tablet" },
      { w: 390, h: 844, tag: "mobile" },
      { w: 360, h: 800, tag: "mobile-sm" },
    ];
    console.log("\n== 布局测量（overflow = scrollWidth - clientWidth）==");
    const shots = [];
    for (const s of sizes) {
      await page.setViewport({ width: s.w, height: s.h, deviceScaleFactor: 1 });
      await new Promise(r => setTimeout(r, 350));
      const m = await page.evaluate(() => {
        const de = document.documentElement;
        // ⚠️ 只看 de.scrollWidth - clientWidth 会被 overflow-x-hidden 掩盖：
        //    根节点一旦隐藏横溢，scrollWidth 就等于 clientWidth，越界内容被静默裁掉。
        //    所以必须同时找「越界元素」，并区分「故意出血的装饰图」与「真被切的内容」。
        const clipped = [];   // 非装饰性：内容被切 → 判失败
        const decor = [];     // 装饰性（pointer-events-none / aria-hidden）→ 允许，仅提示
        document.querySelectorAll("body *").forEach(el => {
          const r = el.getBoundingClientRect();
          if (!(r.width > 0 && (r.right > window.innerWidth + 1 || r.left < -1))) return;
          const cs = getComputedStyle(el);
          if (cs.position === "fixed" || cs.visibility === "hidden" || cs.display === "none") return;
          const isDecor = cs.pointerEvents === "none" || el.getAttribute("aria-hidden") === "true";
          const label = `${el.tagName.toLowerCase()}.${String(el.className).slice(0, 34)}(right=${Math.round(r.right)})`;
          (isDecor ? decor : clipped).push(label);
        });
        return {
          overflow: de.scrollWidth - de.clientWidth,
          clipped: clipped.slice(0, 5),
          clippedTotal: clipped.length,
          decor: decor.slice(0, 5),
        };
      });
      check(
        `${s.w}px 无横向溢出且内容未被裁`,
        m.overflow <= 0 && m.clippedTotal === 0,
        `overflow=${m.overflow}px 被裁内容元素=${m.clippedTotal}`
          + (m.clipped.length ? " [" + m.clipped.join(" | ") + "]" : "")
          + (m.decor.length ? ` · 装饰性出血 ${m.decor.length} 个（允许）` : "")
      );
      const file = path.join(OUT, `2026-10-16-落地页-${s.tag}-${s.w}.png`);
      await page.screenshot({ path: file, fullPage: false });
      shots.push(file);
    }

    // ── 4. 真点击：注册入口 / 锚点 / FAQ ───────────────────
    await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 1 });
    await page.goto(base + "/", { waitUntil: "networkidle2" });
    console.log("\n== 真点击 ==");
    const clickByText = async (text, sel = "button") => page.evaluate((t, s) => {
      const el = [...document.querySelectorAll(s)].find(b => b.textContent.trim().includes(t));
      if (!el) return false;
      el.click();
      return true;
    }, text, sel);

    check("导航里有「免费注册」按钮", await clickByText("免费注册"));
    await new Promise(r => setTimeout(r, 500));
    const afterReg = await page.evaluate(() => location.pathname + location.search);
    check("点「免费注册」跳到 /account?tab=register", afterReg === "/account?tab=register", afterReg);
    const regTabActive = await page.evaluate(() => document.body.innerText.includes("注册"));
    check("账户页渲染出注册表单", regTabActive);

    await page.goto(base + "/", { waitUntil: "networkidle2" });
    const anchorOk = await page.evaluate(async () => {
      const el = [...document.querySelectorAll("button")].find(b => b.textContent.trim() === "最佳实践");
      if (!el) return "no-button";
      el.click();
      await new Promise(r => setTimeout(r, 900));
      const target = document.getElementById("practice");
      if (!target) return "no-target";
      return Math.abs(target.getBoundingClientRect().top) < 260 ? "ok" : `top=${Math.round(target.getBoundingClientRect().top)}`;
    });
    check("点锚点「最佳实践」滚动到该节", anchorOk === "ok", String(anchorOk));

    const faqOk = await page.evaluate(async () => {
      const d = document.querySelector("details");
      if (!d) return "no-details";
      d.querySelector("summary").click();
      await new Promise(r => setTimeout(r, 200));
      return d.open ? "ok" : "not-open";
    });
    check("FAQ 可展开", faqOk === "ok", String(faqOk));

    // 折叠内容也必须在 DOM 里（爬虫读的是 HTML，不是「用户点开之后的样子」）
    await page.goto(base + "/", { waitUntil: "networkidle2" });
    const faqDom = await page.evaluate(() => {
      const all = [...document.querySelectorAll("details")];
      const hidden = all.filter(d => !d.open).length;
      const answers = all.map(d => (d.querySelector("p")?.textContent || "").length);
      return { total: all.length, closed: hidden, minLen: Math.min(...answers) };
    });
    check("FAQ 默认收起且答案仍在 DOM 中",
      faqDom.total > 0 && faqDom.closed === faqDom.total && faqDom.minLen > 40,
      `${faqDom.closed}/${faqDom.total} 收起，最短答案 ${faqDom.minLen} 字`);

    // ── 5. 媒体资源真的能加载（不是 404 的图/音频）──────────
    const media = await page.evaluate(async () => {
      const list = [...document.querySelectorAll("img[src], audio[src]")].map(el => el.getAttribute("src"));
      const uniq = [...new Set(list)];
      const bad = [];
      for (const u of uniq) {
        const r = await fetch(u, { method: "GET" });
        if (!r.ok) bad.push(`${u} → ${r.status}`);
      }
      return { total: uniq.length, bad };
    });
    check(`全部图片/音频可加载（${media.total} 个）`, media.bad.length === 0, media.bad.join(", "));

    check("浏览器控制台无报错", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | "));

    console.log("\n截图：\n  " + shots.join("\n  "));
  } finally {
    await browser.close();
    if (server) server.close();
  }

  const failed = results.filter(r => !r.ok);
  console.log(`\n结果：${results.length - failed.length}/${results.length} 通过`);
  if (failed.length) {
    console.log("未通过：\n  " + failed.map(f => `${f.name}（${f.detail}）`).join("\n  "));
    process.exit(1);
  }
})();
