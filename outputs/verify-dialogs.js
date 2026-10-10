// 「平台统一弹窗」组件级取证：ConfirmDialog（两档语义）+ PromptDialog + 全局 toast（三色）。
//
// 加载真实组件（src/__probe_dialogs.tsx 把三个组件单独挂到 dev server 页面上），无 fetch 依赖。
// 因此下面每条断言都是「真点击 → 真状态机 → 真 DOM」。
//
// 要证的这些事（= 本次改动的验收标准）：
//   ① 删除语义：红图标 + 红确认按钮 + 警示行
//   ② 覆盖语义（tone=default）：靛蓝图标 + 主色确认按钮 + 无警示行
//   ③ Esc / 点遮罩 → 关闭且**不触发回调**
//   ④ 输入弹窗：自动聚焦、默认值全选、空白禁确认、回车提交
//   ⑤ toast 三色：info 灰 / success 绿 / error 红
//   ⑥ 全程原生 dialog 出现次数 = 0
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-dialogs.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-10-平台弹窗取证';
const VIEW = { width: 1280, height: 800 };

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

const BTN = (t) => `[...document.querySelectorAll('#probe-root button')].find(b => (b.textContent||'').trim() === ${JSON.stringify(t)})`;

// 找弹窗层：fixed + inset-0 且含指定标题（原生 confirm 不可能是 DOM，故不会误判）
const measureDialog = (page, title) => page.evaluate((t) => {
  let dialog = null;
  for (const el of document.querySelectorAll('div')) {
    const cls = String(el.className || '');
    if (cls.includes('inset-0') && cls.includes('fixed') && (el.textContent || '').includes(t)) { dialog = el; break; }
  }
  if (!dialog) return null;
  const modal = dialog.querySelector('div.relative');
  const cs = getComputedStyle(modal);
  const mr = modal.getBoundingClientRect();
  const buttons = [...modal.querySelectorAll('button')].map(b => {
    const s = getComputedStyle(b);
    const r = b.getBoundingClientRect();
    return { text: (b.textContent || '').trim(), bg: s.backgroundColor, left: Math.round(r.left), w: Math.round(r.width), h: Math.round(r.height), disabled: b.disabled };
  });
  const iconEl = modal.querySelector('div.rounded-full');
  const svg = modal.querySelector('svg');
  const input = modal.querySelector('input');
  const confirmBtn = buttons.find(b => b.text === '确认');
  return {
    title: (modal.querySelector('h3') || {}).textContent || '',
    paragraphs: [...modal.querySelectorAll('p')].map(p => p.textContent),
    iconBg: iconEl ? getComputedStyle(iconEl).backgroundColor : null,
    iconColor: svg ? getComputedStyle(svg).color : null,
    buttons,
    modal: {
      w: Math.round(mr.width), h: Math.round(mr.height),
      maxWidth: cs.maxWidth, borderRadius: cs.borderRadius, padLeft: parseFloat(cs.paddingLeft),
    },
    overflowRight: Math.round(Math.max(0, mr.right - window.innerWidth)),
    hasInput: !!input,
    inputValue: input ? input.value : null,
    focusIsInput: input ? document.activeElement === input : null,
    inputFullySelected: input ? input.selectionStart === 0 && input.selectionEnd === input.value.length : null,
    confirmDisabled: confirmBtn ? confirmBtn.disabled : null,
    scrimCoversViewport: (() => {
      const scrim = dialog.querySelector(':scope > div');
      const r = scrim.getBoundingClientRect();
      return Math.round(r.width) === window.innerWidth && Math.round(r.height) === window.innerHeight;
    })(),
  };
}, title);

const measureToast = (page, text) => page.evaluate((t) => {
  for (const el of document.querySelectorAll('div.fixed')) {
    const cls = String(el.className || '');
    if (!cls.includes('bottom-6')) continue;
    const inner = el.firstElementChild;
    if (inner && (inner.textContent || '').includes(t)) {
      return { bg: getComputedStyle(inner).backgroundColor, text: inner.textContent };
    }
  }
  return null;
}, text);

const setInputValue = (page, v) => page.evaluate((val) => {
  const input = document.querySelector('#probe-root input, .relative input');
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, val);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}, v);

const log = (page) => page.evaluate(() => window.__probeLog || []);

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.setViewport({ ...VIEW, deviceScaleFactor: 2 });

  let nativeDialogs = 0;
  page.on('dialog', async (d) => { nativeDialogs++; await d.dismiss(); });
  page.on('console', (m) => { if (m.type() === 'error') console.log('CONSOLE ERROR:', m.text()); });
  page.on('pageerror', (e) => console.log('PAGE ERROR:', e.message));

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
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_dialogs.tsx')` });
  await page.waitForSelector('#probe-root button', { timeout: 15000 });
  await new Promise(r => setTimeout(r, 400));

  const report = { viewport: VIEW, dialogs: {}, toasts: {} };
  const click = async (t) => { await page.evaluate(`${BTN(t)}.click()`); };
  const gone = (t) => page.waitForFunction(
    (title) => ![...document.querySelectorAll('div')].some(el =>
      String(el.className || '').includes('inset-0') && (el.textContent || '').includes(title)),
    { timeout: 3000, polling: 50 }, t);

  check('① 初始无弹窗、无原生 dialog', (await measureDialog(page, '删除这个音色？')) === null && nativeDialogs === 0);

  // ── ① 删除语义 ───────────────────────────────────────────────
  await click('打开删除确认');
  await page.waitForFunction((t) => [...document.querySelectorAll('div')].some(el =>
    String(el.className || '').includes('inset-0') && (el.textContent || '').includes(t)),
    { timeout: 3000, polling: 50 }, '删除这个音色？');
  const del = await measureDialog(page, '删除这个音色？');
  report.dialogs.destructive = del;
  check('① 标题正确', del.title === '删除这个音色？', del.title);
  check('① 图标底为警示红（red-50）', /rgb\(254, 242, 242\)/.test(del.iconBg || ''), del.iconBg);
  check('① 图标为红色（red-500）', /rgb\(239, 68, 68\)/.test(del.iconColor || ''), del.iconColor);
  const delBtn = del.buttons.find(b => b.text === '删除');
  check('① 确认按钮为红色（destructive）', !!delBtn && /rgb\(239, 68, 68\)/.test(delBtn.bg), delBtn && delBtn.bg);
  check('① 有警示行「删除后无法恢复」', del.paragraphs.some(p => p.includes('删除后无法恢复')), JSON.stringify(del.paragraphs));
  check('① 遮罩铺满视口 + 不溢出', del.scrimCoversViewport === true && del.overflowRight === 0);
  await page.screenshot({ path: `${OUT}-①删除语义.png` });

  // ── ③ Esc → 关闭且不触发回调 ─────────────────────────────────
  await page.keyboard.press('Escape');
  await gone('删除这个音色？');
  check('③ Esc → 关闭且未触发确认回调',
    (await measureDialog(page, '删除这个音色？')) === null && !(await log(page)).includes('confirm:destructive'));

  // ── ② 覆盖语义（default）────────────────────────────────────
  await click('打开覆盖确认');
  await page.waitForFunction((t) => [...document.querySelectorAll('div')].some(el =>
    String(el.className || '').includes('inset-0') && (el.textContent || '').includes(t)),
    { timeout: 3000, polling: 50 }, '替换当前文稿？');
  const ov = await measureDialog(page, '替换当前文稿？');
  report.dialogs.default = ov;
  check('② 标题正确', ov.title === '替换当前文稿？', ov.title);
  check('② 图标底为靛蓝（indigo-50）', /rgb\(238, 242, 255\)/.test(ov.iconBg || ''), ov.iconBg);
  const okBtn = ov.buttons.find(b => b.text === '继续导入');
  check('② 确认按钮为主色（indigo-600）', !!okBtn && /rgb\(79, 70, 229\)/.test(okBtn.bg), okBtn && okBtn.bg);
  check('② 非删除语义：无警示行', !ov.paragraphs.some(p => p.includes('无法恢复')), JSON.stringify(ov.paragraphs));
  check('② 取消在左、确认在右',
    ov.buttons[0].text === '取消' && ov.buttons[0].left < okBtn.left, JSON.stringify(ov.buttons.map(b => [b.text, b.left])));
  await page.screenshot({ path: `${OUT}-②覆盖语义.png` });

  // ── ③ 点遮罩 → 关闭且不触发回调 ──────────────────────────────
  await page.mouse.click(20, 20);
  await gone('替换当前文稿？');
  check('③ 点遮罩 → 关闭且未触发确认回调',
    (await measureDialog(page, '替换当前文稿？')) === null && !(await log(page)).includes('confirm:default'));

  // ── ④ 输入弹窗 ───────────────────────────────────────────────
  await click('打开输入弹窗');
  await page.waitForFunction((t) => [...document.querySelectorAll('div')].some(el =>
    String(el.className || '').includes('inset-0') && (el.textContent || '').includes(t)),
    { timeout: 3000, polling: 50 }, '重命名音色');
  await new Promise(r => setTimeout(r, 250));
  let pr = await measureDialog(page, '重命名音色');
  check('④ 有输入框且默认值 = 原名', pr.hasInput === true && pr.inputValue === '旧名字', String(pr.inputValue));
  check('④ 自动聚焦输入框', pr.focusIsInput === true, String(pr.focusIsInput));
  check('④ 默认值被全选（直接输入即替换）', pr.inputFullySelected === true, String(pr.inputFullySelected));
  await page.screenshot({ path: `${OUT}-④输入弹窗.png` });

  await setInputValue(page, '   ');
  pr = await measureDialog(page, '重命名音色');
  check('④ 空白输入 → 确认按钮禁用', pr.confirmDisabled === true, String(pr.confirmDisabled));

  await setInputValue(page, '新名字');
  await page.keyboard.press('Enter');
  await gone('重命名音色');
  check('④ 回车提交 → 弹窗关闭且回调收到新值',
    (await log(page)).includes('prompt:新名字'), JSON.stringify(await log(page)));

  // ── ⑤ toast 三色 ─────────────────────────────────────────────
  const colors = [
    ['toast.info', 'info：普通提示', 'rgb(31, 41, 55)', '灰'],
    ['toast.success', 'success：成功提示', 'rgb(5, 150, 105)', '绿'],
    ['toast.error', 'error：失败提示', 'rgb(220, 38, 38)', '红'],
  ];
  for (const [btn, text, expectBg, name] of colors) {
    await page.evaluate(`${BTN(btn)}.click()`);
    await new Promise(r => setTimeout(r, 150));
    const t = await measureToast(page, text);
    report.toasts[btn] = t;
    check(`⑤ ${btn} → ${name}底 ${expectBg}`, !!t && t.bg === expectBg, t && t.bg);
    if (btn === 'toast.error') await page.screenshot({ path: `${OUT}-⑤toast三色.png` });
    await new Promise(r => setTimeout(r, 200));
  }

  check('⑥ 全程原生 dialog 出现 0 次', nativeDialogs === 0, `${nativeDialogs} 次`);

  fs.writeFileSync(`${OUT}.json`, JSON.stringify(report, null, 2));

  console.log('\n── 实测几何 ──');
  for (const [k, d] of Object.entries(report.dialogs)) {
    console.log(`${k}: ${d.modal.w}×${d.modal.h} max-width ${d.modal.maxWidth} 圆角 ${d.modal.borderRadius} 图标底 ${d.iconBg}`);
  }
  console.log('\n摘要：' + results.filter(r => r.ok).length + '/' + results.length + ' 通过');
  console.log('原生 dialog：' + nativeDialogs + ' 次');

  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(async (e) => {
  console.error('SCRIPT ERROR:', e && e.message);
  process.exit(2);
});
