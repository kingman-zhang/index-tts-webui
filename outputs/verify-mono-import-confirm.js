// 「导入覆盖确认弹窗」的交互取证。
//
// 与前两个脚本（静态复刻标记）不同：这次**加载真实组件**
// （src/__probe_mono_import_confirm.tsx 把 MonoEditor 单独挂到 dev server 页面上），
// 只把 /api/mono/extract 用 fetch mock 喂数据。
// 因此下面每条断言都是「真点击 → 真状态机 → 真 DOM」，不是照抄 HTML 的自证。
//
// 要证的这些事（= 本次改动的验收标准）：
//   ① 画布有内容 + 导入文档 → 出「分段确认」面板（不会自动写画布）
//   ② 点「确认分章」→ 先弹**平台统一样式的自绘确认框**（不再用原生 window.confirm）
//   ③ 弹窗分层正确：确认框盖在导入弹窗之上；内容/几何不溢出
//   ④ 「取消」→ 关闭且**画布不变**
//   ⑤ Esc → 关闭且画布不变
//   ⑥ 点遮罩 → 关闭且画布不变
//   ⑦ 点「继续导入」→ 才真的替换画布
//   ⑧ 全程原生 dialog 出现次数 = 0（证明 window.confirm 已被移除）
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-mono-import-confirm.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-10-导入覆盖确认';
const VIEW = { width: 1280, height: 800 };

// ── mock 数据：一本两章的书（行号必须与 text 严格对应，前端按行号还原章节）──
const PARA = '这是书籍正文的每一段内容，用来把行数撑起来，方便验证按章切分。';
const body1 = ['第一章 起点', ...Array(20).fill(PARA)].join('\n');          // 行 0..20
const body2 = ['第二章 转折', ...Array(20).fill(PARA)].join('\n');          // 行 21..41
const BOOK = body1 + '\n' + body2;                                         // 共 42 行
const countChars = (s) => s.replace(/\s/g, '').length;

const EXTRACT_PAYLOAD = {
  text: BOOK,
  chars: countChars(BOOK),
  chapter_max_chars: 20000,
  chapters: [
    { index: 0, title: '第一章 起点', start_line: 0,  end_line: 21, chars: countChars(body1) },
    { index: 1, title: '第二章 转折', start_line: 21, end_line: 42, chars: countChars(body2) },
  ],
};

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

/** 覆盖确认框的判据：fixed 全屏层 + 标题文案。原生 confirm 不可能是 DOM，故不会误判。 */
const DIALOG_FINDER = `(() => {
  for (const el of document.querySelectorAll('div')) {
    const cls = String(el.className || '');
    if (cls.includes('inset-0') && cls.includes('fixed') && el.textContent.includes('替换当前文稿')) return el;
  }
  return null;
})()`;

const waitDialog = (page, timeout = 3000) =>
  page.waitForFunction((f) => !!eval(f), { timeout, polling: 50 }, DIALOG_FINDER);
const dialogGone = (page) =>
  page.waitForFunction((f) => !eval(f), { timeout: 3000, polling: 50 }, DIALOG_FINDER);
const hasDialog = (page) => page.evaluate((f) => !!eval(f), DIALOG_FINDER);
const canvasText = (page) => page.evaluate(() => window.__monoText || '');

/** 弹窗内的几何与样式 —— 只用实测值，不做换算 */
const measure = (page) => page.evaluate((finder) => {
  const dialog = eval(finder);
  const modal = dialog.querySelector('div.relative');
  const cs = getComputedStyle(modal);
  const mr = modal.getBoundingClientRect();
  const padL = parseFloat(cs.paddingLeft), padR = parseFloat(cs.paddingRight);
  const inner = { left: mr.left + padL, right: mr.right - padR };
  let maxRight = -Infinity, widest = '';
  for (const el of modal.querySelectorAll('h3,p,button')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0) continue;
    if (r.right > maxRight) { maxRight = r.right; widest = (el.textContent || '').slice(0, 24); }
  }
  const btns = [...modal.querySelectorAll('button')].map(b => {
    const r = b.getBoundingClientRect();
    const s = getComputedStyle(b);
    return {
      text: (b.textContent || '').trim(),
      left: Math.round(r.left), right: Math.round(r.right),
      width: Math.round(r.width), height: Math.round(r.height),
      bg: s.backgroundColor, color: s.color,
    };
  });
  return {
    viewportW: window.innerWidth,
    modal: {
      width: Math.round(mr.width), height: Math.round(mr.height),
      left: Math.round(mr.left), top: Math.round(mr.top),
      maxWidth: cs.maxWidth, borderRadius: cs.borderRadius,
      padLeft: padL, padRight: padR, padTop: parseFloat(cs.paddingTop),
    },
    overflowLeft: Math.round(Math.max(0, -mr.left)),
    overflowRight: Math.round(Math.max(0, mr.right - window.innerWidth)),
    contentOverflowRight: Math.round(Math.max(0, maxRight - inner.right)),
    widestEl: widest,
    title: (modal.querySelector('h3') || {}).textContent || '',
    lines: [...modal.querySelectorAll('p')].map(p => p.textContent),
    iconBg: (() => {
      const d = modal.querySelector('div.rounded-full');
      return d ? getComputedStyle(d).backgroundColor : null;
    })(),
    iconStroke: (() => {
      const svg = modal.querySelector('svg');
      return svg ? getComputedStyle(svg).color : null;
    })(),
    buttons: btns,
    scrimCoversViewport: (() => {
      const scrim = dialog.querySelector(':scope > div');
      const r = scrim.getBoundingClientRect();
      return Math.round(r.width) === window.innerWidth && Math.round(r.height) === window.innerHeight;
    })(),
    // 弹窗中心点处，最上层命中的是否为确认框内的元素（验证盖在导入弹窗之上）
    topmostAtCenter: (() => {
      const el = document.elementFromPoint(Math.round(mr.left + mr.width / 2), Math.round(mr.top + 10));
      return !!el && modal.contains(el);
    })(),
  };
}, DIALOG_FINDER);

const clickByText = (page, text, scope = '#probe-root') => page.evaluate((t, sc) => {
  const root = document.querySelector(sc);
  const b = [...root.querySelectorAll('button')].find(x => (x.textContent || '').trim() === t);
  if (b) b.click();
  return !!b;
}, text, scope);

const clickInDialog = (page, text) => page.evaluate((t) => {
  const d = [...document.querySelectorAll('div')]
    .find(el => String(el.className || '').includes('inset-0') && el.textContent.includes('替换当前文稿'));
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

  // 原生 dialog 记账：出现即说明还有 window.confirm 残留
  let nativeDialogs = 0;
  page.on('dialog', async (d) => { nativeDialogs++; await d.dismiss(); });

  // 只拦 /api/mono/extract；其余 /api/* 返回 404（不返 401，避免 app 清登录态触发 reload）
  await page.evaluateOnNewDocument((payload) => {
    const orig = window.fetch.bind(window);
    const json = (o, status = 200) =>
      new Response(JSON.stringify(o), { status, headers: { 'Content-Type': 'application/json' } });
    window.__probeCalls = [];
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (url.includes('/api/mono/extract')) {
        window.__probeCalls.push(`${method} ${url}`);
        return json(payload);
      }
      if (url.includes('/api/')) return json({ detail: 'probe: not mocked' }, 404);
      return orig(input, init);
    };
  }, EXTRACT_PAYLOAD);

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_mono_import_confirm.tsx')` });
  await page.waitForSelector('#probe-root button', { timeout: 15000 });
  await page.waitForFunction(
    () => [...document.querySelectorAll('#probe-root button')].some(b => (b.textContent || '').trim() === '导入'),
    { timeout: 10000 }
  );
  await new Promise(r => setTimeout(r, 400));

  const report = { viewport: VIEW, dialog: null, interactions: [] };
  const original = await canvasText(page);

  // ── ① 点「导入」→ 弹导入弹窗 ──────────────────────────────────
  check('① 画布预置了非空文稿', original.length > 100, `${original.length} 字`);
  check('① 点「导入」→ 打开导入弹窗', await clickByText(page, '导入'));
  await page.waitForSelector('#probe-root input[type=file]', { timeout: 5000 });

  // ── ① 上传文档（走真实 input[type=file].onChange）──────────────
  const tmp = path.join(os.tmpdir(), 'probe-book.txt');
  fs.writeFileSync(tmp, BOOK, 'utf-8');
  const fileInput = await page.$('#probe-root input[type=file]');
  await fileInput.uploadFile(tmp);
  await page.waitForFunction(
    () => [...document.querySelectorAll('#probe-root button')].some(b => (b.textContent || '').includes('确认分章')),
    { timeout: 10000 }
  );
  await new Promise(r => setTimeout(r, 300));
  check('① 解析后进入「分段确认」步（不自动写画布）',
    (await canvasText(page)) === original,
    `画布仍为原稿（${(await canvasText(page)).length} 字）`);
  await page.screenshot({ path: `${OUT}-①分段确认.png` });

  // ── ② 点「确认分章」→ 应弹自绘确认框，且画布不变 ───────────────
  const confirmBtn = await page.evaluate(() => {
    const b = [...document.querySelectorAll('#probe-root button')]
      .find(x => (x.textContent || '').includes('确认分章'));
    return b ? b.textContent.trim() : null;
  });
  check('② 「确认分章」按钮文案含章数', !!confirmBtn && confirmBtn.includes('2 章'), String(confirmBtn));

  await page.evaluate(() => {
    [...document.querySelectorAll('#probe-root button')]
      .find(x => (x.textContent || '').includes('确认分章')).click();
  });
  await waitDialog(page);
  const t = await measure(page);
  report.dialog = t;

  check('② 弹出的是**自绘**确认框（DOM），而非原生 confirm',
    (await hasDialog(page)) === true && nativeDialogs === 0,
    `原生 dialog 次数 ${nativeDialogs}`);
  check('② 标题为「替换当前文稿？」', t.title === '替换当前文稿？', `实际「${t.title}」`);
  check('② 文案含被替换的字数', t.lines.some(l => /字/.test(l) && /\d/.test(l)), JSON.stringify(t.lines));
  check('② 画布仍未变化（确认前不替换）',
    (await canvasText(page)) === original);
  check('② 确认框盖在导入弹窗之上', t.topmostAtCenter === true, String(t.topmostAtCenter));
  check('② 遮罩铺满视口', t.scrimCoversViewport === true, String(t.scrimCoversViewport));
  check('② 不溢出视口与内边界',
    t.overflowLeft === 0 && t.overflowRight === 0 && t.contentOverflowRight === 0,
    `视口 ${t.overflowLeft}/${t.overflowRight}，内边界 ${t.contentOverflowRight}（最宽：${t.widestEl}）`);

  const [cancelBtn, okBtn] = t.buttons;
  check('② 两个按钮：取消（浅色，左）+ 继续导入（主色，右）',
    !!cancelBtn && !!okBtn && cancelBtn.text === '取消' && okBtn.text === '继续导入' &&
    okBtn.left > cancelBtn.left &&
    /rgb\(79, 70, 229\)/.test(okBtn.bg) && !/rgb\(79, 70, 229\)/.test(cancelBtn.bg),
    JSON.stringify(t.buttons.map(b => ({ t: b.text, bg: b.bg, left: b.left }))));
  check('② 非删除语义：图标为靛蓝（indigo-50）而非警示红',
    /rgb\(238, 242, 255\)/.test(t.iconBg || '') && !/rgb\(254, 242, 242\)/.test(t.iconBg || ''),
    `图标底 ${t.iconBg}`);
  await page.screenshot({ path: `${OUT}-②确认框.png` });

  // ── ④ 点「取消」──────────────────────────────────────────────
  await clickInDialog(page, '取消');
  await dialogGone(page);
  check('④ 点「取消」→ 关闭且画布不变',
    !(await hasDialog(page)) && (await canvasText(page)) === original);
  report.interactions.push({ step: '取消', replaced: (await canvasText(page)) !== original });

  // ── ⑤ Esc ───────────────────────────────────────────────────
  await page.evaluate(() => {
    [...document.querySelectorAll('#probe-root button')]
      .find(x => (x.textContent || '').includes('确认分章')).click();
  });
  await waitDialog(page);
  await page.keyboard.press('Escape');
  await dialogGone(page);
  check('⑤ Esc → 关闭且画布不变',
    !(await hasDialog(page)) && (await canvasText(page)) === original);
  report.interactions.push({ step: 'Esc', replaced: (await canvasText(page)) !== original });

  // ── ⑥ 点遮罩（点左上角空白，避开卡片）──────────────────────────
  await page.evaluate(() => {
    [...document.querySelectorAll('#probe-root button')]
      .find(x => (x.textContent || '').includes('确认分章')).click();
  });
  await waitDialog(page);
  await page.mouse.click(20, 20);
  await dialogGone(page);
  check('⑥ 点遮罩 → 关闭且画布不变',
    !(await hasDialog(page)) && (await canvasText(page)) === original);
  report.interactions.push({ step: '遮罩', replaced: (await canvasText(page)) !== original });

  // ── ⑦ 点「继续导入」→ 才真的替换 ─────────────────────────────
  await page.evaluate(() => {
    [...document.querySelectorAll('#probe-root button')]
      .find(x => (x.textContent || '').includes('确认分章')).click();
  });
  await waitDialog(page);
  await clickInDialog(page, '继续导入');
  await dialogGone(page);
  const after = await canvasText(page);
  check('⑦ 点「继续导入」→ 画布被替换为所选第一章',
    after !== original && after.includes('第一章 起点') && !after.includes('原有的第 1 段'),
    `替换后 ${after.length} 字，首行「${after.split('\n')[0]}」`);
  check('⑦ 确认后导入弹窗自动关闭',
    !(await page.evaluate(() => !![...document.querySelectorAll('#probe-root button')]
      .find(x => (x.textContent || '').includes('确认分章')))));
  report.interactions.push({ step: '继续导入', replaced: true, firstLine: after.split('\n')[0] });
  await page.screenshot({ path: `${OUT}-⑦替换后.png` });

  // ── ⑧ 全程无原生 dialog ──────────────────────────────────────
  check('⑧ 全程原生 dialog 出现 0 次（window.confirm 已移除）', nativeDialogs === 0, `${nativeDialogs} 次`);

  fs.writeFileSync(`${OUT}.json`, JSON.stringify(report, null, 2));

  console.log('\n── 实测几何（确认框）──');
  const m = t.modal;
  console.log(`viewport ${t.viewportW}px`);
  console.log(`弹窗 宽 ${m.width} 高 ${m.height} left ${m.left} top ${m.top} max-width ${m.maxWidth} 圆角 ${m.borderRadius}`);
  console.log(`内边距 上/左 ${m.padTop}/${m.padLeft}  内容右溢 ${t.contentOverflowRight}px`);
  console.log(`图标底 ${t.iconBg}  图标色 ${t.iconStroke}`);
  console.log('按钮：');
  for (const b of t.buttons) console.log(`  「${b.text}」 ${b.width}×${b.height} left ${b.left} bg ${b.bg}`);
  console.log('\n摘要：' + results.filter(r => r.ok).length + '/' + results.length + ' 通过');
  console.log('原生 dialog：' + nativeDialogs + ' 次');

  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(async (e) => {
  console.error('SCRIPT ERROR:', e && e.message);
  process.exit(2);
});
