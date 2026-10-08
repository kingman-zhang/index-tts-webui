// 「加载更多历史」按钮文案溢出的量化验证 + 前后对比截图。
//
// 背景：前端有登录门禁，headless 浏览器没有 token，进不去工作区。
// 这里不登录、不创建账号，而是把「与 QueuePanel.tsx + ui.tsx 逐字一致」的标记
// 注入 dev server 的页面里 —— 用的是同一份实时 CSS，量出来的尺寸与真页面一致。
//
// 真实容器宽度：右栏 <aside class="w-80"> = 320px（PodcastPage/DubbingPage）
//               CardContent p-4 ⇒ 按钮可用宽度 288px。
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-queue-loadmore-overflow.js [输出前缀]
// 输出：<前缀>.json（测量表）、<前缀>.png（前后对比图）
//
// 判据：① button.scrollWidth > clientWidth ⇒ 溢出
//       ② 按钮**内容**【Range 包围盒】越过按钮左右边界 ⇒ 文案跑出边框（截图所见）
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-08-队列加载更多';

// ── 与源码逐字一致 ────────────────────────────────────────────────
// ui.tsx Button：base + variant(outline) + size(sm) + className(w-full)
const BTN = [
  'inline-flex items-center justify-center rounded-lg font-medium whitespace-nowrap shrink-0',
  'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500',
  'focus-visible:ring-offset-1 disabled:opacity-50 disabled:cursor-not-allowed',
  'border border-gray-300 bg-white text-gray-700 hover:bg-gray-50',
  'h-8 px-3 text-xs gap-1',
  'w-full',
].join(' ');
const ICON = 'w-3.5 h-3.5'; // size=sm ⇒ w-3.5 h-3.5
const history = (cls) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="${cls}"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M12 7v5l4 2"/></svg>`;
const trash = (cls) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="${cls}"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>`;

const copyBefore = '加载更多历史（共 242 条终态，每类已显示最近 50 条）';
const copyAfter = '加载更多历史（共 242 条）';

/** 复刻 QueuePanel 卡片底部：Card(p-4) 里两个 w-full 的 sm outline 按钮 */
function buildCard(width, which) {
  const text = which === 'before' ? copyBefore : copyAfter;
  return `
    <div class="flex flex-col gap-2" style="width:${width}px">
      <div class="text-[0.7rem] text-gray-400 tabular-nums" data-label="${width}">卡片宽 ${width}px</div>
      <div class="rounded-xl bg-white border border-gray-200 shadow-sm" data-card="${width}">
        <div class="p-4">
          <button type="button" data-probe="${which}" data-width="${width}" class="${BTN}">${history(ICON)}${text}</button>
          <button type="button" class="${BTN.replace(' w-full', ' w-full mt-2')}">${trash(ICON)}清空已完成任务</button>
        </div>
      </div>
    </div>`;
}

const WIDTHS = [240, 320, 400]; // 320 = 真实右栏 w-80
// 临界宽度扫描：新文案窄到多少才会再溢出（只为把结论量出来，不是真实布局）
const NARROW = [206, 216, 226];

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1000, height: 900, deviceScaleFactor: 2 });
  // 登录页即可：只要那份实时 CSS，不需要进工作区
  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });

  const html = `
    <div id="probe-root" style="position:fixed;left:-99999px;top:0;display:flex;flex-direction:column;
         gap:28px;padding:24px;background:#f8fafc;font-family:system-ui,sans-serif">
      <div id="legend" style="position:fixed;left:99999px;top:0"></div>
      ${WIDTHS.map(w => buildCard(w, 'before')).join('')}
      ${WIDTHS.map(w => buildCard(w, 'after')).join('')}
      <div style="font-size:11px;color:#94a3b8">↓ 临界宽度扫描（新文案）</div>
      ${NARROW.map(w => buildCard(w, 'after')).join('')}
    </div>`;

  await page.evaluate((markup) => {
    const host = document.createElement('div');
    host.id = 'probe-host';
    host.innerHTML = markup;
    document.body.appendChild(host);
  }, html);

  const rows = await page.evaluate(() => {
    const out = [];
    const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
    for (const btn of document.querySelectorAll('#probe-host button[data-probe]')) {
      const b = btn.getBoundingClientRect();
      const r = document.createRange();
      r.selectNodeContents(btn); // 含 svg 图标 + 全部文本节点
      const c = r.getBoundingClientRect();
      const card = btn.closest('[data-card]');
      out.push({
        variant: btn.dataset.probe,
        cardWidth: Number(btn.dataset.width),
        rem,
        cardClient: card.clientWidth,          // 含 p-4 的 padding
        pad4: rem,                              // p-4 = 1rem 一侧
        cardBox: Math.round(card.getBoundingClientRect().width),
        cardInner: card.clientWidth - 2 * rem,  // 真正的可用宽度
        btnWidth: Math.round(b.width),
        btnFontSize: getComputedStyle(btn).fontSize,
        contentWidth: Math.round(c.width),
        scrollOverflow: btn.scrollWidth - btn.clientWidth,
        overflowLeft: Math.max(0, Math.round(b.left - c.left)),
        overflowRight: Math.max(0, Math.round(c.right - b.right)),
        fits: btn.scrollWidth <= btn.clientWidth,
      });
    }
    return out;
  });

  // 标签文案也用实测值回填，别写推算值（实测 320 卡片下按钮盒是 282 而不是 288）
  await page.evaluate(() => {
    for (const lbl of document.querySelectorAll('#probe-host [data-label]')) {
      const card = lbl.parentElement.querySelector(`[data-card="${lbl.dataset.label}"]`);
      const btn = card.querySelector('button[data-probe]');
      lbl.textContent =
        `卡片 ${Math.round(card.getBoundingClientRect().width)}px · ` +
        `按钮可用 ${Math.round(btn.getBoundingClientRect().width)}px`;
    }
  });

  // 量出来之后把探针挪进可视区截图（左侧固定定位只是为了量尺寸时不闪）
  await page.evaluate(() => {
    const root = document.getElementById('probe-root');
    root.style.position = 'static';
    root.style.left = '0';
  });
  await page.evaluate(() => { document.getElementById('legend').remove(); });
  const handle = await page.$('#probe-root');
  await handle.screenshot({ path: `${OUT}.png` });

  fs.writeFileSync(`${OUT}.json`, JSON.stringify(rows, null, 2));
  console.log(`html font-size(rem)=${rows[0].rem}px  button font-size=${rows[0].btnFontSize}`);
  console.log('variant  cardBox  inner  btnBox  content  scroll  overL  overR  fits');
  for (const r of rows) {
    console.log(
      `${r.variant.padEnd(7)}  ${String(r.cardBox).padStart(7)}  ${String(r.cardInner).padStart(5)}` +
      `  ${String(r.btnWidth).padStart(6)}  ${String(r.contentWidth).padStart(7)}` +
      `  ${String(r.scrollOverflow).padStart(6)}  ${String(r.overflowLeft).padStart(5)}` +
      `  ${String(r.overflowRight).padStart(5)}  ${r.fits}`
    );
  }
  console.log(`\n截图: ${OUT}.png\n数据: ${OUT}.json`);
  await browser.close();
})();
