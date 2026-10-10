// 「切换项目确认」取证：真实 DubbingPage（单人配音页）。
//
// 加载真实页面（src/__probe_switch_project.tsx），预置 localStorage（画布非空 + 一份存档），
// 所有 /api/* 用 mock 兜底。因此每条断言都是「真点击 → 真状态机 → 真 DOM」。
//
// 要证的这些事（= 本次改动的验收标准）：
//   ① 画布有内容 + 点 Header 的项目下拉里另一份存档 → 弹**平台统一样式**确认框
//      （不再是原生 window.confirm）
//   ② 「取消」→ 关闭且画布不变
//   ③ 「继续切换」→ 才真的换掉画布内容
//   ④ 全程原生 dialog 出现次数 = 0
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-switch-project.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-10-切换项目确认';
const VIEW = { width: 1440, height: 900 };

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

/** 确认框判据：fixed 全屏层 + 标题文案（原生 confirm 不可能是 DOM，故不会误判） */
const dialogInfo = (page) => page.evaluate(() => {
  let dialog = null;
  for (const el of document.querySelectorAll('div')) {
    const cls = String(el.className || '');
    if (cls.includes('inset-0') && cls.includes('fixed') && (el.textContent || '').includes('切换项目？')) { dialog = el; break; }
  }
  if (!dialog) return null;
  const modal = dialog.querySelector('div.relative');
  return {
    title: (modal.querySelector('h3') || {}).textContent || '',
    paragraphs: [...modal.querySelectorAll('p')].map(p => p.textContent),
    buttons: [...modal.querySelectorAll('button')].map(b => (b.textContent || '').trim()),
  };
});

/** 画布文本唯一真源 = localStorage 草稿（DubbingPage 每次 monoText 变化都写） */
const canvasText = (page) => page.evaluate(() => {
  try { return (JSON.parse(localStorage.getItem('wb-mono-draft-v2') || '{}').text) || ''; } catch { return ''; }
});

const clickByTitle = (page, title) => page.evaluate((t) => {
  const b = [...document.querySelectorAll('#probe-root button')].find(x => x.getAttribute('title') === t);
  if (b) b.click();
  return !!b;
}, title);

/** 点下拉里名为 name 的存档项（按下拉项文本的几何中心点） */
const clickArchive = async (page, name) => {
  const box = await page.evaluate((n) => {
    const el = [...document.querySelectorAll('#probe-root *')]
      .find(e => e.children.length === 0 && (e.textContent || '').trim() === n);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
  }, name);
  if (!box) return false;
  await page.mouse.click(box.x, box.y);
  return true;
};

const waitDialog = (page) => page.waitForFunction(() => [...document.querySelectorAll('div')].some(el =>
  String(el.className || '').includes('inset-0') && (el.textContent || '').includes('切换项目？')),
  { timeout: 4000, polling: 50 });
const dialogGone = (page) => page.waitForFunction(() => ![...document.querySelectorAll('div')].some(el =>
  String(el.className || '').includes('inset-0') && (el.textContent || '').includes('切换项目？')),
  { timeout: 4000, polling: 50 });
const clickInDialog = (page, text) => page.evaluate((t) => {
  const d = [...document.querySelectorAll('div')]
    .find(el => String(el.className || '').includes('inset-0') && (el.textContent || '').includes('切换项目？'));
  const b = [...d.querySelectorAll('button')].find(x => (x.textContent || '').trim() === t);
  if (b) b.click();
  return !!b;
}, text);

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.setViewport({ ...VIEW, deviceScaleFactor: 2 });

  let nativeDialogs = 0;
  page.on('dialog', async (d) => { nativeDialogs++; await d.dismiss(); });
  page.on('pageerror', (e) => console.log('PAGE ERROR:', e.message));

  // 只拦 /api/*：一律 404（不返 401，避免 app 清登录态触发 reload）
  await page.evaluateOnNewDocument(() => {
    const orig = window.fetch.bind(window);
    const json = (o, status = 200) =>
      new Response(JSON.stringify(o), { status, headers: { 'Content-Type': 'application/json' } });
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (url.includes('/api/')) return json({ detail: 'probe: not mocked' }, 404);
      return orig(input, init);
    };
  });

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_switch_project.tsx')` });
  await page.waitForFunction(() => !!document.querySelector('#probe-root button[title="切换项目"]'), { timeout: 15000 });
  await new Promise(r => setTimeout(r, 600));

  const report = { viewport: VIEW, dialog: null, interactions: [] };
  const original = await canvasText(page);
  check('① 画布预置了非空文稿', original.length > 100, `${original.length} 字`);
  check('① 初始无弹窗', (await dialogInfo(page)) === null);
  await page.screenshot({ path: `${OUT}-①页面.png` });

  // ── ① 打开项目下拉 ───────────────────────────────────────────
  check('① 点「切换项目」展开下拉', await clickByTitle(page, '切换项目'));
  await page.waitForFunction(() => document.body.textContent.includes('存档甲'), { timeout: 5000, polling: 50 });

  // ── ② 点存档 → 弹确认框；点取消 → 画布不变 ───────────────────
  check('② 点下拉里的「存档甲」', await clickArchive(page, '存档甲'));
  await waitDialog(page);
  const d = await dialogInfo(page);
  report.dialog = d;
  check('② 弹出的是**自绘**确认框（DOM），而非原生 confirm',
    !!d && d.title === '切换项目？' && nativeDialogs === 0, d && `「${d.title}」原生 ${nativeDialogs}`);
  check('② 标题为「切换项目？」', d.title === '切换项目？', d.title);
  check('② 文案点出目标存档名与被替换的字数',
    d.paragraphs.some(p => p.includes('存档甲')) && d.paragraphs.some(p => /\d/.test(p) && p.includes('字')),
    JSON.stringify(d.paragraphs));
  check('② 画布仍未变化（确认前不切换）', (await canvasText(page)) === original);
  check('② 按钮为「取消」+「继续切换」', d.buttons.join('|') === '取消|继续切换', d.buttons.join('|'));
  await page.screenshot({ path: `${OUT}-②切换确认.png` });

  await clickInDialog(page, '取消');
  await dialogGone(page);
  check('② 点「取消」→ 关闭且画布不变',
    (await dialogInfo(page)) === null && (await canvasText(page)) === original);
  report.interactions.push({ step: '取消', canvasChanged: (await canvasText(page)) !== original });

  // ── ③ 再来一次，点「继续切换」→ 才真的换 ─────────────────────
  await clickByTitle(page, '切换项目');
  await page.waitForFunction(() => document.body.textContent.includes('存档甲'), { timeout: 5000, polling: 50 });
  await clickArchive(page, '存档甲');
  await waitDialog(page);
  await clickInDialog(page, '继续切换');
  await dialogGone(page);
  const after = await canvasText(page);
  check('③ 点「继续切换」→ 画布被替换为存档内容',
    after !== original && after.includes('存档甲的文稿内容'),
    `替换后 ${after.length} 字，首行「${(after.split('\n')[0] || '').slice(0, 20)}」`);
  report.interactions.push({ step: '继续切换', canvasChanged: true });
  await page.screenshot({ path: `${OUT}-③切换后.png` });

  check('④ 全程原生 dialog 出现 0 次', nativeDialogs === 0, `${nativeDialogs} 次`);

  fs.writeFileSync(`${OUT}.json`, JSON.stringify(report, null, 2));
  console.log('\n摘要：' + results.filter(r => r.ok).length + '/' + results.length + ' 通过');
  console.log('原生 dialog：' + nativeDialogs + ' 次');

  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(async (e) => {
  console.error('SCRIPT ERROR:', e && e.message);
  process.exit(2);
});
