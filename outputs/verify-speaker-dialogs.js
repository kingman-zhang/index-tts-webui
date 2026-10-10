// 「组件层弹窗统一」的接线取证：真实 SpeakerPanel（角色卡）。
//
// 加载真实组件（src/__probe_speaker_panel.tsx），只把 /api/voice-presets、
// /api/preset-voices 用 fetch mock 喂数据。因此每条断言都是「真点击 → 真状态机 → 真 DOM」。
//
// 要证的这些事（= 本次改动的验收标准）：
//   ① 删除角色预设 → 弹**平台统一样式**确认框（不再是 window.confirm）
//   ② 该确认框「取消」→ **不删除**；再打开点「删除」→ 才真的调 DELETE
//   ③ 重命名角色预设 → 弹**平台统一样式**输入弹窗（不再是 window.prompt），默认值 = 原名
//   ④ 改名接口失败 → 用**全局 toast 红条**报错（不再是 window.alert）
//   ⑤ 全程原生 dialog 出现次数 = 0
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-speaker-dialogs.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-10-角色卡弹窗取证';
const VIEW = { width: 1280, height: 800 };

const PRESETS = [
  { id: 'pre_1', name: '角色甲', voice_name: 'jia.mp3', voice_path: '/x/jia.mp3', speed: 1.0 },
  { id: 'pre_2', name: '角色乙', voice_name: 'yi.mp3', voice_path: '/x/yi.mp3', speed: 1.0 },
];

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

const dialogState = (page, title) => page.evaluate((t) => {
  let dialog = null;
  for (const el of document.querySelectorAll('div')) {
    const cls = String(el.className || '');
    if (cls.includes('inset-0') && cls.includes('fixed') && (el.textContent || '').includes(t)) { dialog = el; break; }
  }
  if (!dialog) return null;
  const modal = dialog.querySelector('div.relative');
  const input = modal.querySelector('input');
  return {
    title: (modal.querySelector('h3') || {}).textContent || '',
    paragraphs: [...modal.querySelectorAll('p')].map(p => p.textContent),
    buttons: [...modal.querySelectorAll('button')].map(b => (b.textContent || '').trim()),
    hasInput: !!input,
    inputValue: input ? input.value : null,
    focusIsInput: input ? document.activeElement === input : null,
  };
}, title);

const toastState = (page, text) => page.evaluate((t) => {
  for (const el of document.querySelectorAll('div.fixed')) {
    if (!String(el.className || '').includes('bottom-6')) continue;
    const inner = el.firstElementChild;
    if (inner && (inner.textContent || '').includes(t)) return { bg: getComputedStyle(inner).backgroundColor, text: inner.textContent };
  }
  return null;
}, text);

const calls = (page) => page.evaluate(() => window.__probeCalls || []);
const clickTitle = (page, title) => page.evaluate((t) => {
  const b = [...document.querySelectorAll('#probe-root button')].find(x => x.getAttribute('title') === t);
  if (b) b.click();
  return !!b;
}, title);
const clickInDialog = (page, marker, text) => page.evaluate((m, t) => {
  const d = [...document.querySelectorAll('div')]
    .find(el => String(el.className || '').includes('inset-0') && (el.textContent || '').includes(m));
  const b = [...d.querySelectorAll('button')].find(x => (x.textContent || '').trim() === t);
  if (b) b.click();
  return !!b;
}, marker, text);
const setInput = (page, v) => page.evaluate((val) => {
  const input = document.querySelector('.relative input');
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, val);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}, v);

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

  await page.evaluateOnNewDocument((presets) => {
    const orig = window.fetch.bind(window);
    const json = (o, status = 200) =>
      new Response(JSON.stringify(o), { status, headers: { 'Content-Type': 'application/json' } });
    window.__probeCalls = [];
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (url.includes('/api/voice-presets')) {
        window.__probeCalls.push(`${method} ${url}`);
        if (method === 'GET') return json({ presets, count: presets.length });
        // 改名故意失败：用来验证「报错走 toast 而不是 alert」
        if (method === 'PUT') return json({ detail: 'probe: 改名接口故意失败' }, 500);
        if (method === 'DELETE') return json({ deleted: url.split('/').pop() });
      }
      if (url.includes('/api/preset-voices')) {
        return json({ categories: { female: [], male: [], emotion: [] }, count: 0 });
      }
      if (url.includes('/api/')) return json({ detail: 'probe: not mocked' }, 404);
      return orig(input, init);
    };
  }, PRESETS);

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_speaker_panel.tsx')` });
  await page.waitForSelector('#probe-root button', { timeout: 15000 });
  await new Promise(r => setTimeout(r, 500));

  const report = { viewport: VIEW, dialogs: {}, toasts: {} };

  // 展开角色预设列表
  check('① 展开角色预设列表', await clickTitle(page, '打开角色预设列表'));
  await page.waitForFunction(() => document.body.textContent.includes('角色甲'), { timeout: 5000, polling: 50 });
  check('① 预设列表渲染出 2 个角色', (await page.evaluate(() => document.body.textContent)).includes('角色乙'));
  await page.screenshot({ path: `${OUT}-①预设列表.png` });

  // ── ③ 改名：window.prompt → PromptDialog ─────────────────────
  await clickTitle(page, '重命名');
  await page.waitForFunction((t) => [...document.querySelectorAll('div')].some(el =>
    String(el.className || '').includes('inset-0') && (el.textContent || '').includes(t)),
    { timeout: 3000, polling: 50 }, '重命名角色预设');
  await new Promise(r => setTimeout(r, 250));
  let d = await dialogState(page, '重命名角色预设');
  report.dialogs.rename = d;
  check('③ 弹出的是**自绘**输入弹窗（DOM）而非原生 prompt', !!d && nativeDialogs === 0, `原生 dialog ${nativeDialogs}`);
  check('③ 默认值 = 原预设名', d.inputValue === '角色甲', String(d.inputValue));
  check('③ 自动聚焦输入框', d.focusIsInput === true);
  await page.screenshot({ path: `${OUT}-③改名弹窗.png` });

  // ── ④ 改名接口失败 → 全局 toast 红条（而非 alert）─────────────
  await setInput(page, '角色甲改');
  await page.keyboard.press('Enter');
  await page.waitForFunction((t) => ![...document.querySelectorAll('div')].some(el =>
    String(el.className || '').includes('inset-0') && (el.textContent || '').includes(t)),
    { timeout: 3000, polling: 50 }, '重命名角色预设');
  await new Promise(r => setTimeout(r, 200));
  const t = await toastState(page, '改名失败');
  report.toasts.rename = t;
  check('③ 回车提交 → 发起了 PUT 改名请求',
    (await calls(page)).some(c => c.startsWith('PUT /api/voice-presets/pre_1')), JSON.stringify(await calls(page)));
  check('④ 接口失败 → 全局 toast 红条报错（不再 alert）',
    !!t && t.bg === 'rgb(220, 38, 38)', t && `${t.bg} «${t.text}»`);
  await page.screenshot({ path: `${OUT}-④改名失败toast.png` });
  await new Promise(r => setTimeout(r, 300));

  // ── ① 删除预设 → ConfirmDialog ───────────────────────────────
  await clickTitle(page, '删除');
  await page.waitForFunction((t) => [...document.querySelectorAll('div')].some(el =>
    String(el.className || '').includes('inset-0') && (el.textContent || '').includes(t)),
    { timeout: 3000, polling: 50 }, '删除这个角色预设？');
  d = await dialogState(page, '删除这个角色预设？');
  report.dialogs.deletePreset = d;
  check('① 弹出的是**自绘**确认框（DOM）而非原生 confirm',
    !!d && d.title === '删除这个角色预设？' && nativeDialogs === 0, d && d.title);
  check('① 有警示行「删除后无法恢复」', d.paragraphs.some(p => p.includes('删除后无法恢复')), JSON.stringify(d.paragraphs));
  await page.screenshot({ path: `${OUT}-①删除确认.png` });

  // ── ② 取消 → 不删；再确认 → 才删 ─────────────────────────────
  await clickInDialog(page, '删除这个角色预设？', '取消');
  await page.waitForFunction((t) => ![...document.querySelectorAll('div')].some(el =>
    String(el.className || '').includes('inset-0') && (el.textContent || '').includes(t)),
    { timeout: 3000, polling: 50 }, '删除这个角色预设？');
  check('② 点「取消」→ 未发起删除请求',
    !(await calls(page)).some(c => c.startsWith('DELETE')), JSON.stringify(await calls(page)));

  await clickTitle(page, '删除');
  await page.waitForFunction((t) => [...document.querySelectorAll('div')].some(el =>
    String(el.className || '').includes('inset-0') && (el.textContent || '').includes(t)),
    { timeout: 3000, polling: 50 }, '删除这个角色预设？');
  await clickInDialog(page, '删除这个角色预设？', '删除');
  await new Promise(r => setTimeout(r, 300));
  check('② 点「删除」→ 真的发起 DELETE',
    (await calls(page)).some(c => c.startsWith('DELETE /api/voice-presets/pre_1')), JSON.stringify(await calls(page)));

  check('⑤ 全程原生 dialog 出现 0 次', nativeDialogs === 0, `${nativeDialogs} 次`);

  fs.writeFileSync(`${OUT}.json`, JSON.stringify(report, null, 2));
  console.log('\n── 实际请求 ──');
  for (const c of await calls(page)) console.log('  ' + c);
  console.log('\n摘要：' + results.filter(r => r.ok).length + '/' + results.length + ' 通过');
  console.log('原生 dialog：' + nativeDialogs + ' 次');

  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(async (e) => {
  console.error('SCRIPT ERROR:', e && e.message);
  process.exit(2);
});
