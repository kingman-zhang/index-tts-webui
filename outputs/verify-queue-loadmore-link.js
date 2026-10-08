// 「加载更多历史」改成列表末尾的超链：可见性 + 不溢出 的量化验证 + 前后态截图。
//
// 背景：前端有登录门禁，headless 浏览器没有 token，进不去工作区。
// 这里不登录、不创建账号，而是把「与 QueuePanel.tsx 逐字一致」的标记注入 dev server
// 页面里 —— 用的是同一份实时 CSS，量出来的尺寸与真页面一致。
//
// 本次要证的两件事：
//   ① 链接放在**滚动容器内部**最末尾 ⇒ 首屏看不见，滚到底才看见（用户要的行为）
//   ② 链接不溢出：内容包围盒不得越过按钮左右边界（上一版按钮形态曾左 27 / 右 31）
//
// 真实容器：右栏 <aside class="w-80">=320px → 卡片 320px → p-4 ⇒ 可用 282px
//           列表容器 class 取自源码：space-y-1.5 max-h-[520px] overflow-y-auto scrollbar-thin
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-queue-loadmore-link.js [输出前缀]
// 输出：<前缀>.json、<前缀>.png（上：首屏 / 下：滚到底）、<前缀>-hover.png
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-08-队列加载更多-链接式';
const CARD_W = 320; // 右栏 w-80

// ── 与源码逐字一致：QueuePanel.tsx 里的「加载更多历史」超链 ──────────────
const LINK = 'flex w-full items-center justify-center gap-1 py-1.5 text-[0.75rem] ' +
             'text-indigo-600 transition-colors hover:text-indigo-700 hover:underline';
// 列表容器（源码 QueuePanel.tsx 约 426 行）
const LIST = 'space-y-1.5 max-h-[520px] overflow-y-auto scrollbar-thin';

const svg = (d, cls) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="${cls}">${d}</svg>`;
const HISTORY = svg('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/>', 'w-3 h-3');
const CHECK = svg('<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="m9 11 3 3L22 4"/>', 'w-3.5 h-3.5 shrink-0 text-green-600');

/** 任务行（示意数据，class 取自源码；本脚本的测量目标是链接，不是这些行） */
const row = (i) => `
  <div class="rounded-lg border p-2.5 transition-colors border-green-200 bg-green-50">
    <div class="flex items-center justify-between gap-2">
      <div class="flex items-center gap-2 min-w-0 flex-1">
        ${CHECK}
        <span class="text-xs font-medium text-gray-700 truncate">2026-10-08 演示任务 ${i}</span>
        <span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium bg-green-100 text-green-700 shrink-0">完成</span>
      </div>
    </div>
    <p class="text-[0.75rem] text-gray-500 mt-1">时长 ${(30 + i).toFixed(1)} 秒</p>
  </div>`;

const panel = (tag, rows, scrollBottom) => `
  <div class="flex flex-col gap-2" data-panel="${tag}">
    <div class="text-[0.7rem] text-gray-500">
      ${scrollBottom ? '滚到底（链接可见）' : '首屏（scrollTop=0）'}
    </div>
    <div class="rounded-xl bg-white border border-gray-200 shadow-sm" style="width:${CARD_W}px">
      <div class="p-4">
        <div class="${LIST}" data-list="${tag}">
          ${Array.from({ length: rows }, (_, i) => row(i + 1)).join('')}
          <button type="button" data-link="${tag}" class="${LINK}"
                  title="已加载每个类型的最近 50 条，点击再各取 50 条">
            ${HISTORY}加载更多历史（共 242 条）
          </button>
        </div>
        <button type="button" class="mt-2 inline-flex items-center justify-center rounded-lg font-medium whitespace-nowrap shrink-0 border border-gray-300 bg-white text-gray-700 h-8 px-3 text-xs gap-1 w-full">
          清空已完成任务
        </button>
      </div>
    </div>
  </div>`;

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1000, height: 1200, deviceScaleFactor: 2 });
  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });

  await page.evaluate((markup) => {
    const host = document.createElement('div');
    host.id = 'probe-host';
    host.innerHTML = markup;
    Object.assign(host.style, {
      position: 'fixed', left: '-99999px', top: '0', display: 'flex',
      gap: '28px', padding: '20px', background: '#f8fafc',
      fontFamily: 'system-ui,sans-serif',
    });
    document.body.appendChild(host);
  }, `<div style="display:flex;gap:28px">
        ${panel('top', 9, false)}
        ${panel('bottom', 9, true)}
      </div>`);

  // 先把「滚到底」那屏真的滚到底（scrollTop = max）
  const geom = await page.evaluate(() => {
    const out = {};
    for (const tag of ['top', 'bottom']) {
      const list = document.querySelector(`[data-list="${tag}"]`);
      if (tag === 'bottom') list.scrollTop = list.scrollHeight;
      const link = list.querySelector('button[data-link]');
      const lr = link.getBoundingClientRect();
      const cr = list.getBoundingClientRect();
      const r = document.createRange();
      r.selectNodeContents(link);
      const box = r.getBoundingClientRect();
      const br = link.getBoundingClientRect();
      // 链接与「上一行任务」是否有重叠（>0 就是压住了内容，属实现问题）
      const prevR = link.previousElementSibling.getBoundingClientRect();
      out[tag] = {
        listH: list.clientHeight,
        contentH: list.scrollHeight,
        scrollTop: Math.round(list.scrollTop),
        maxScroll: list.scrollHeight - list.clientHeight,
        overlapPrevRow: Math.round(Math.max(0, prevR.bottom - lr.top)),
        gapPrevRow: Math.round(lr.top - prevR.bottom),
        // 链接相对容器顶部的偏移：> listH 即「首屏看不见」
        linkRelTop: Math.round(lr.top - cr.top),
        linkRelBottom: Math.round(lr.bottom - cr.top),
        fullyVisible: lr.top >= cr.top - 0.5 && lr.bottom <= cr.bottom + 0.5,
        linkWidth: Math.round(br.width),
        contentWidth: Math.round(box.width),
        btnFontSize: getComputedStyle(link).fontSize,
        overflowLeft: Math.max(0, Math.round(br.left - box.left)),
        overflowRight: Math.max(0, Math.round(box.right - br.right)),
        rem: parseFloat(getComputedStyle(document.documentElement).fontSize),
      };
    }
    return out;
  });

  // 挪进可视区截图（上=首屏 / 下=滚到底）
  await page.evaluate(() => {
    const host = document.getElementById('probe-host');
    host.style.position = 'static';
    host.style.left = '0';
  });
  const grid = await page.$('#probe-host');
  await grid.screenshot({ path: `${OUT}.png` });

  // hover 态：截图那条链接本身，看下划线是否有
  const linkBottom = await page.$('button[data-link="bottom"]');
  await linkBottom.hover();
  await new Promise(r => setTimeout(r, 150));
  await linkBottom.screenshot({ path: `${OUT}-hover.png` });

  fs.writeFileSync(`${OUT}.json`, JSON.stringify(geom, null, 2));
  console.log(`html font-size(rem)=${geom.top.rem}px  link font-size=${geom.top.btnFontSize}`);
  console.log('panel   listH  contentH  scrollTop/max  linkRelTop  linkRelBottom  fullyVisible');
  for (const tag of ['top', 'bottom']) {
    const g = geom[tag];
    console.log(
      `${tag.padEnd(7)} ${String(g.listH).padStart(5)}  ${String(g.contentH).padStart(8)}` +
      `  ${String(g.scrollTop).padStart(5)}/${String(g.maxScroll).padEnd(5)}` +
      `  ${String(g.linkRelTop).padStart(10)}  ${String(g.linkRelBottom).padStart(13)}` +
      `  ${String(g.fullyVisible).padStart(12)}`
    );
  }
  console.log('panel   链接宽 内容宽 溢出左 溢出右  与上一行重叠  间距');
  for (const tag of ['top', 'bottom']) {
    const g = geom[tag];
    console.log(`${tag.padEnd(7)} ${String(g.linkWidth).padStart(5)} ${String(g.contentWidth).padStart(6)}` +
                ` ${String(g.overflowLeft).padStart(6)} ${String(g.overflowRight).padStart(6)}` +
                ` ${String(g.overlapPrevRow).padStart(12)} ${String(g.gapPrevRow).padStart(6)}`);
  }
  console.log(`\n截图: ${OUT}.png / ${OUT}-hover.png\n数据: ${OUT}.json`);
  await browser.close();
})();
