// 《产品使用文档》页（/docs）端到端验收。
//
// 验的是真页面：真实路由 / 真实图片 / 真实组件，不打桩。
// 覆盖 7 件事：
//   ① 免登录可达（/docs 不需要 token —— 这是设计的核心要求）
//   ② 12 个章节齐全、目录条目数与章节数一致
//   ③ 35 张截图全部真的加载出来（naturalWidth>0，防「路径写错但没人发现」）
//   ④ 三种视口宽度都不横向溢出
//   ⑤ 点图放大 → 出现大图浮层；ESC 能关掉
//   ⑥ 点目录能跳到对应章节
//   ⑦ 两个入口真的存在：用户菜单「使用文档」、个人中心积分卡上的引导链接
//
// 用法（沙箱 dev server 需已在 :6009 运行，/api 代理到沙箱后端 3002）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-docs.js [输出前缀]
// 输出：<前缀>.json、<前缀>-top.png、<前缀>-zoom.png
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const BASE = 'http://127.0.0.1:6009';
const TOKEN = process.env.DOCS_TOKEN || '';
const OUT = process.argv[2] || 'outputs/2026-10-16-产品文档页-验收';

const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
function check(name, pass, detail) {
  results.push({ name, pass: !!pass, detail });
  console.log(`${pass ? '✓' : '✗'} ${name}${detail === undefined ? '' : `  ${JSON.stringify(detail)}`}`);
  return !!pass;
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: true,
    args: ['--no-sandbox', '--font-render-hinting=none', '--hide-scrollbars'],
    defaultViewport: { width: 1600, height: 1000, deviceScaleFactor: 2 },
  });

  // ── ① 免登录可达 ────────────────────────────────────────
  const page = await browser.newPage();
  const errs = [];
  page.on('pageerror', e => errs.push(String(e).slice(0, 200)));
  page.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text().slice(0, 200)); });
  const failedReq = [];
  page.on('requestfailed', r => failedReq.push(`${r.url()} ${r.failure()?.errorText}`));
  page.on('response', r => { if (r.status() >= 400) failedReq.push(`${r.status()} ${r.url()}`); });

  await page.goto(BASE + '/docs', { waitUntil: 'networkidle2' });
  await sleep(1200);

  const basic = await page.evaluate(() => ({
    path: location.pathname,
    h1: document.querySelector('h1')?.textContent?.trim(),
    h2: [...document.querySelectorAll('h2')].map(h => h.textContent.replace(/^\d+/, '').trim()),
    toc: [...document.querySelectorAll('aside nav button')].map(b => b.textContent.trim()),
    imgTotal: document.querySelectorAll('img').length,
    // 登录门禁的特征是「有密码输入框」（文档正文里出现「登录/注册」字样是正常的）
    hasPasswordField: !!document.querySelector('input[type=password]'),
  }));
  check('免登录可达：渲染出文档标题', basic.h1 === '播客工坊：把文字变成音频', basic.h1);
  check('免登录可达：没有被登录门禁拦下', basic.path === '/docs' && !basic.hasPasswordField,
    { path: basic.path, hasPasswordField: basic.hasPasswordField });
  check('章节数 = 12', basic.h2.length === 12, basic.h2);
  check('目录条目数 = 章节数', basic.toc.length === basic.h2.length, { toc: basic.toc.length, h2: basic.h2.length });
  check('入口「使用文档」在目录里（跳转锚点自洽）', basic.toc.some(t => t.includes('最佳实践')), undefined);

  // ── ③ 图片全部真的加载 ──────────────────────────────────
  await page.evaluate(async () => {
    // 触发全部懒加载：滚到底再回顶
    const step = window.innerHeight;
    for (let y = 0; y < document.body.scrollHeight; y += step) {
      window.scrollTo(0, y);
      await new Promise(r => setTimeout(r, 60));
    }
    window.scrollTo(0, 0);
  });
  await page.waitForFunction(
    () => [...document.querySelectorAll('img')].every(i => i.complete),
    { timeout: 30000 },
  ).catch(() => {});
  const imgs = await page.evaluate(() => [...document.querySelectorAll('img')].map(i => ({
    src: i.getAttribute('src'), w: i.naturalWidth, h: i.naturalHeight, complete: i.complete,
  })));
  const broken = imgs.filter(i => !i.complete || i.w === 0);
  const unique = new Set(imgs.map(i => i.src));
  check('全部截图加载成功', broken.length === 0, broken.map(b => b.src));
  check('截图张数 ≥ 30（去重后）', unique.size >= 30, { 引用处: imgs.length, 去重后: unique.size });

  // ── ④ 不横向溢出 ────────────────────────────────────────
  const widths = [1600, 1024, 480];
  const overflow = {};
  for (const w of widths) {
    await page.setViewport({ width: w, height: 1000, deviceScaleFactor: 1 });
    await sleep(500);
    overflow[w] = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
  }
  check('三种宽度都不横向溢出', Object.values(overflow).every(v => v <= 0), overflow);
  await page.setViewport({ width: 1600, height: 1000, deviceScaleFactor: 2 });
  await sleep(400);

  // ── ⑤ 点图放大 ──────────────────────────────────────────
  await page.evaluate(() => window.scrollTo(0, 0));
  await sleep(300);
  const zoomOpened = await page.evaluate(() => {
    const btn = [...document.querySelectorAll('figure button')].find(b => b.querySelector('img'));
    if (!btn) return false;
    btn.click();
    return true;
  });
  await sleep(600);
  const zoomState = await page.evaluate(() => {
    const ov = [...document.querySelectorAll('div')].find(d =>
      typeof d.className === 'string' && d.className.includes('fixed') && d.className.includes('inset-0'));
    const big = ov ? ov.querySelector('img') : null;
    return { hasOverlay: !!ov, bigW: big ? big.naturalWidth : 0 };
  });
  check('点图能放大（出现大图浮层）', zoomOpened && zoomState.hasOverlay && zoomState.bigW > 0, zoomState);
  await page.screenshot({ path: `${OUT}-zoom.png` });
  await page.keyboard.press('Escape');
  await sleep(400);
  const zoomClosed = await page.evaluate(() =>
    ![...document.querySelectorAll('div')].some(d =>
      typeof d.className === 'string' && d.className.includes('fixed') && d.className.includes('inset-0')));
  check('ESC 能关掉大图', zoomClosed, undefined);

  // ── ⑥ 目录跳转 ──────────────────────────────────────────
  const jumped = await page.evaluate(async () => {
    const btn = [...document.querySelectorAll('aside button')].find(b => b.textContent.includes('最佳实践'));
    if (!btn) return null;
    btn.click();
    await new Promise(r => setTimeout(r, 900));
    const el = document.getElementById('practice');
    return el ? Math.round(el.getBoundingClientRect().top) : null;
  });
  check('点目录能跳到「最佳实践」章节', jumped !== null && Math.abs(jumped) < 200, { top: jumped });

  await page.evaluate(() => window.scrollTo(0, 0));
  await sleep(400);
  await page.screenshot({ path: `${OUT}-top.png` });

  // ── ⑦ 两个入口存在（需要已登录态） ────────────────────────
  let menuOk = null, accountOk = null, opensNewTab = null;
  if (TOKEN) {
    const p2 = await browser.newPage();
    await p2.evaluateOnNewDocument(t => {
      localStorage.setItem('wb-auth-token', t);
      localStorage.setItem('wb-mode', 'dubbing');
    }, TOKEN);
    await p2.goto(BASE + '/dubbing', { waitUntil: 'networkidle2' });
    await sleep(2000);
    await p2.evaluate(() => {
      const btn = [...document.querySelectorAll('header button')].find(b => b.querySelector('span'));
      if (btn) btn.click();
    });
    await sleep(700);
    menuOk = await p2.evaluate(() => document.body.innerText.includes('使用文档'));
    // 点它是否新开标签（拦 window.open 记调用）
    opensNewTab = await p2.evaluate(() => {
      const orig = window.open;
      let called = null;
      window.open = (u, t) => { called = { u, t }; return null; };
      const item = [...document.querySelectorAll('button, a')].find(e => e.textContent.includes('使用文档'));
      if (item) item.click();
      window.open = orig;
      return called;
    });
    await p2.close();

    const p3 = await browser.newPage();
    await p3.evaluateOnNewDocument(t => {
      localStorage.setItem('wb-auth-token', t);
      localStorage.setItem('wb-mode', 'dubbing');
    }, TOKEN);
    await p3.goto(BASE + '/account', { waitUntil: 'networkidle2' });
    await sleep(2200);
    accountOk = await p3.evaluate(() => document.body.innerText.includes('查看《产品使用文档》'));
    await p3.close();

    check('用户菜单里有「使用文档」', menuOk, undefined);
    check('点「使用文档」是新开标签', !!opensNewTab && opensNewTab.t === '_blank', opensNewTab);
    check('个人中心积分卡有引导链接', accountOk, undefined);
  } else {
    console.log('… 未给 DOCS_TOKEN，跳过「用户菜单 / 个人中心入口」两项');
  }

  check('无 JS 报错', errs.length === 0, errs.slice(0, 5));
  check('无 4xx/5xx 与失败请求', failedReq.length === 0, failedReq.slice(0, 5));

  await browser.close();

  const failed = results.filter(r => !r.pass);
  fs.writeFileSync(`${OUT}.json`, JSON.stringify({ results, failed: failed.map(f => f.name) }, null, 2));
  console.log(`\n${results.length - failed.length}/${results.length} 通过`);
  if (failed.length) { console.log('未通过：' + failed.map(f => f.name).join(' / ')); process.exit(2); }
})().catch(e => { console.error('FAIL', e); process.exit(1); });
