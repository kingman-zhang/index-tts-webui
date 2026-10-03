// 对话脚本工具条「是否溢出被裁」的量化验证 + 前后对比截图。
//
// 背景：前端有登录门禁（未登录会跳 /account），headless 浏览器没有 token。
// 这里不登录、不创建账号，而是把「与 MonoEditor.tsx 逐字一致」的标记注入到
// dev server 的页面里 —— 用的是同一份实时 CSS，量出来的尺寸与真页面一致。
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-toolbar-layout.js [输出前缀]
// 输出：<前缀>.json（测量表）、<前缀>-compare.png（A/B 对比图）
//
// 判据：scrollWidth > clientWidth ⇒ 溢出；并单独量主操作按钮是否越过容器右边界。
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/toolbar-layout';

const TOOL_BTN = 'inline-flex items-center gap-1 h-8 px-3 rounded-full border border-gray-200 bg-white text-xs text-gray-600 whitespace-nowrap shrink-0 transition-colors';
const GEN_BTN = 'inline-flex items-center gap-1.5 h-9 px-5 rounded-full bg-emerald-600 text-white text-sm font-medium whitespace-nowrap shrink-0 shadow-sm transition-colors';
const ICON = 'w-3.5 h-3.5';
const svg = (d, cls) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="${cls}">${d}</svg>`;
const CHEVRON = svg('<path d="m6 9 6 6 6-6"/>', ICON);
const PAUSE = svg('<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>', ICON);
const FILEUP = svg('<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v5h5"/><path d="M12 12v6"/><path d="m9 15 3-3 3 3"/>', ICON);
const SPARKLES = svg('<path d="m12 3-1.9 5.8L4 10.7l6.1 1.9L12 18.4l1.9-5.8L20 10.7l-6.1-1.9Z"/>', 'w-4 h-4');

/** wrap=false → 旧写法；true → 修复后 */
function buildBar(wrap) {
  const cls = 'shrink-0 border-t border-gray-100 bg-white px-5 py-3 flex items-center gap-2' + (wrap ? ' flex-wrap' : '');
  return `
    <div class="${cls}" data-bar="${wrap ? 'after' : 'before'}">
      <button type="button" class="${TOOL_BTN}"><span class="w-2 h-2 rounded-full shrink-0 bg-indigo-500"></span>A 发言</button>
      <button type="button" class="${TOOL_BTN}"><span class="w-2 h-2 rounded-full shrink-0 bg-teal-500"></span>B 发言</button>
      <div class="relative"><button type="button" class="${TOOL_BTN}">${CHEVRON}情绪</button></div>
      <button type="button" class="${TOOL_BTN}">${PAUSE}停顿 0.5s</button>
      <button type="button" class="${TOOL_BTN}">${FILEUP}导入</button>
      <div class="ml-auto flex items-center gap-3 shrink-0">
        <span class="text-xs text-gray-400 tabular-nums whitespace-nowrap hidden sm:inline">3714 字 · 50 段 · 约 186 积分</span>
        <button type="button" class="${GEN_BTN}">${SPARKLES}生成配音</button>
      </div>
    </div>`;
}
const card = inner => `<div class="rounded-2xl border border-gray-200 bg-white shadow-sm overflow-hidden">${inner}</div>`;

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new', args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 2 });
  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2', timeout: 60000 });
  await new Promise(r => setTimeout(r, 1200));

  // 中列宽 = 窗口宽 - 左侧 w-72(288) - 右侧 w-80(320) - 外边距(p-3×2=24) - 两处 gap-3(24)
  const colW = w => w - 656;

  const rows = await page.evaluate((afterHtml, beforeHtml, widths) => {
    const out = [];
    const host = document.createElement('div');
    host.style.cssText = 'position:fixed;left:-99999px;top:0';
    document.body.appendChild(host);
    for (const [label, w] of widths) {
      host.innerHTML = `<div style="width:${w}px">${afterHtml}${beforeHtml}</div>`;
      const rec = { 窗口宽: label, 中列宽: w };
      for (const bar of host.querySelectorAll('[data-bar]')) {
        const gen = [...bar.querySelectorAll('button')].find(b => b.textContent.includes('生成配音'));
        const br = bar.getBoundingClientRect(), gr = gen.getBoundingClientRect();
        // 行数：按子项 top 聚类（容差 12px，避免 h-8/h-9 垂直居中差被误判成两行）
        const tops = [...bar.children].map(c => c.getBoundingClientRect().top).sort((a, b) => a - b);
        const lines = tops.reduce((n, t) => (n === 0 || t - tops[n - 1] > 12 ? n + 1 : n), 0);
        rec[bar.dataset.bar === 'before' ? '修复前' : '修复后'] = {
          flexWrap: getComputedStyle(bar).flexWrap,
          内容需要px: bar.scrollWidth - 40,          // 去掉 px-5 的左右内边距
          可用px: bar.clientWidth - 40,
          溢出px: bar.scrollWidth - bar.clientWidth,
          行数: lines,
          工具条高px: bar.clientHeight,
          生成按钮越过右边缘px: Math.max(0, Math.round(gr.right - br.right)),
        };
      }
      out.push(rec);
    }
    host.remove();
    return out;
  }, card(buildBar(true)), card(buildBar(false)),
     [[1280, colW(1280)], [1366, colW(1366)], [1440, colW(1440)], [1512, colW(1512)]]);

  console.log(JSON.stringify(rows, null, 2));
  fs.writeFileSync(`${OUT}.json`, JSON.stringify(rows, null, 2), 'utf8');

  // 视觉对照：按用户截图对应的窗口宽（1366）渲染，两种写法上下各一条
  const shotW = colW(1366);
  await page.evaluate((afterHtml, beforeHtml, w) => {
    document.body.innerHTML = '';
    document.body.style.cssText = 'margin:0;padding:20px;background:#f9fafb';
    const wrap = document.createElement('div');
    wrap.style.width = w + 'px';
    wrap.innerHTML =
      `<p style="font:600 12px/1.6 system-ui;color:#6b7280;margin:0 0 6px">修复前 · flex items-center（溢出，生成按钮被裁）</p>`
      + beforeHtml
      + `<p style="font:600 12px/1.6 system-ui;color:#6b7280;margin:18px 0 6px">修复后 · 加 flex-wrap（换行，全部可见）</p>`
      + afterHtml;
    document.body.appendChild(wrap);
  }, card(buildBar(true)), card(buildBar(false)), shotW);

  await page.setViewport({ width: shotW + 40, height: 300, deviceScaleFactor: 2 });
  await new Promise(r => setTimeout(r, 300));
  await page.screenshot({ path: `${OUT}-compare.png` });
  console.log('screenshot →', `${OUT}-compare.png`);
  await browser.close();
})().catch(e => { console.error('ERR', e.message); process.exit(1); });
