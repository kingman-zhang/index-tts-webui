// 「任务队列 · 删除确认弹窗」的交互取证。
//
// 与前两个脚本（静态复刻标记）不同：这次**加载真实组件**（src/__probe_queue_delete.tsx
// 把 QueuePanel 单独挂到 dev server 页面上），只把 /api/queue 用 fetch mock 喂数据。
// 因此下面每条断言都是「真点击 → 真状态机 → 真 DOM」，不是照抄 HTML 的自证。
//
// 要证的这些事：
//   ① 点删除图标**不再直接删**，而是弹出确认框（含任务名 + 「删除后无法恢复。」）
//   ② 「取消中」的任务文案不同（点了不一定真删，说清两种结果）
//   ③ 「取消」按钮 → 关闭且**不发** DELETE
//   ④ Esc → 关闭（高风险动作的默认键位落在安全侧）
//   ⑤ 点遮罩 → 关闭
//   ⑥ 点确认「删除」→ 才真的发 DELETE；删完自动关框
//   ⑦ 「清空已完成任务」同样要确认：写明条数与口径（完成/失败/中断/取消）、
//      取消/Esc 都不发请求、点「清空」才发 DELETE /api/queue?kind=…
//      （注意与单条删除的路径不同）
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-queue-delete-confirm.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-09-队列删除确认';
const VIEW = { width: 1280, height: 800 };

const TITLE_TERMINAL = '删除';
const TITLE_CANCELLING = '删除；若该任务已无执行器在运行则直接移除';

// 三种状态各一条，正好覆盖删除按钮的两种文案分支
const QUEUE_PAYLOAD = {
  tasks: [
    { id: 't-success', project_name: '认知觉醒·第一期（完成）', kind: 'podcast', status: 'success',
      progress: 1, current_line: 0, total_lines: 0, message: '', audio_url: '/outputs/demo.mp3',
      duration_sec: 312.4, created_at: '2026-10-09T10:00:00' },
    { id: 't-cancelled', project_name: '认知觉醒·第二期（已取消）', kind: 'podcast', status: 'cancelled',
      progress: 0.2, current_line: 0, total_lines: 5, message: '已取消', created_at: '2026-10-09T10:10:00' },
    { id: 't-running', project_name: '认知觉醒·第三期（取消中）', kind: 'podcast', status: 'running',
      progress: 0.6, current_line: 3, total_lines: 5,
      message: '正在取消，等待进行中的合成结束', cancel_requested: true,
      created_at: '2026-10-09T10:20:00' },
  ],
  count: 3, terminal_total: 2, has_more: false, current: 't-running', queued: 0, queue_order: [],
};

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

/** 弹窗是否存在。两种确认框（删除单条 / 清空批量）的警示语不同，
 *  但它们都以「…无法恢复。」结尾，用这个作判据不会误认报错详情弹窗。 */
const DIALOG_FINDER = `(() => {
  for (const el of document.querySelectorAll('div')) {
    const cls = String(el.className || '');
    if (cls.includes('inset-0') && cls.includes('fixed') && el.textContent.includes('无法恢复')) return el;
  }
  return null;
})()`;

const waitDialog = (page, timeout = 3000) =>
  page.waitForFunction(
    (finder) => !!eval(finder), { timeout, polling: 50 }, DIALOG_FINDER
  );

const dialogGone = (page) =>
  page.waitForFunction(
    (finder) => !eval(finder), { timeout: 3000, polling: 50 }, DIALOG_FINDER
  );

const hasDialog = (page) => page.evaluate((finder) => !!eval(finder), DIALOG_FINDER);

/** 弹窗内的几何与样式 —— 只用实测值，不做换算 */
const measure = (page) => page.evaluate((finder) => {
  const dialog = eval(finder);
  const modal = dialog.querySelector('div.relative');
  const cs = getComputedStyle(modal);
  const mr = modal.getBoundingClientRect();
  const padL = parseFloat(cs.paddingLeft), padR = parseFloat(cs.paddingRight);
  const inner = { left: mr.left + padL, right: mr.right - padR };
  let maxRight = -Infinity, maxLeft = Infinity, widest = '';
  for (const el of modal.querySelectorAll('h3,p,button')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0) continue;
    if (r.right > maxRight) { maxRight = r.right; widest = (el.textContent || '').slice(0, 24); }
    maxLeft = Math.min(maxLeft, r.left);
  }
  const btns = [...modal.querySelectorAll('button')].map(b => {
    const r = b.getBoundingClientRect();
    const s = getComputedStyle(b);
    return {
      text: (b.textContent || '').trim(),
      left: Math.round(r.left), right: Math.round(r.right),
      width: Math.round(r.width), height: Math.round(r.height),
      bg: s.backgroundColor, color: s.color, disabled: b.disabled,
    };
  });
  return {
    viewportW: window.innerWidth,
    rem: parseFloat(getComputedStyle(document.documentElement).fontSize),
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
    warningColor: (() => {
      const p = [...modal.querySelectorAll('p')].find(x => x.textContent.includes('无法恢复'));
      return p ? getComputedStyle(p).color : null;
    })(),
    warningVisible: (() => {
      const p = [...modal.querySelectorAll('p')].find(x => x.textContent.includes('无法恢复'));
      if (!p) return false;
      const r = p.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && r.top >= mr.top - 1 && r.bottom <= mr.bottom + 1;
    })(),
    buttons: btns,
    scrimCoversViewport: (() => {
      const scrim = dialog.querySelector(':scope > div');
      const r = scrim.getBoundingClientRect();
      return Math.round(r.width) === window.innerWidth && Math.round(r.height) === window.innerHeight;
    })(),
  };
}, DIALOG_FINDER);

const callsOf = (page) => page.evaluate(() => window.__probeCalls || []);

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.setViewport({ ...VIEW, deviceScaleFactor: 2 });

  // 只拦 /api/queue；其余 /api/* 返回 404（不返 401，避免真实 app 清登录态触发 reload）
  await page.evaluateOnNewDocument((payload) => {
    window.__probeQueue = payload;
    const orig = window.fetch.bind(window);
    const calls = [];
    window.__probeCalls = calls;
    const json = (o, status = 200) =>
      new Response(JSON.stringify(o), { status, headers: { 'Content-Type': 'application/json' } });
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (url.includes('/api/queue')) {
        calls.push(`${method} ${url}`);
        if (method === 'GET' && !/\/api\/queue\/[^/?]+/.test(url)) return json(window.__probeQueue);
        if (method === 'DELETE') return json({ deleted: 'ok' });
        return json({ ok: true });
      }
      if (url.includes('/api/')) return json({ detail: 'probe: not mocked' }, 404);
      return orig(input, init);
    };
  }, QUEUE_PAYLOAD);

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  // 用真组件探针替掉整页 UI
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_queue_delete.tsx')` });
  await page.waitForSelector('#probe-root button', { timeout: 15000 });
  await page.waitForFunction(
    (t) => [...document.querySelectorAll('button')].some(b => b.title === t),
    { timeout: 10000 }, TITLE_TERMINAL
  );
  await new Promise(r => setTimeout(r, 400)); // 让首屏样式稳定

  const report = { viewport: VIEW, terminal: null, cancelling: null, clear: null, interactions: [] };

  // ── ① 点击前：面板（含删除图标）────────────────────────────────
  const host = await page.$('#probe-root');
  await host.screenshot({ path: `${OUT}-点击前.png` });

  const rowCount = await page.evaluate(() => ({
    rows: document.querySelectorAll('#probe-root .rounded-lg.border').length,
    deleteBtns: [...document.querySelectorAll('button')].filter(b => b.title.startsWith('删除')).length,
    terminalBtns: [...document.querySelectorAll('button')].filter(b => b.title === '删除').length,
    cancellingBtns: [...document.querySelectorAll('button')]
      .filter(b => b.title.includes('若该任务已无执行器')).length,
  }));
  check('① 面板渲染出 3 条任务、3 个删除图标（2 终态 + 1 取消中）',
    rowCount.rows >= 3 && rowCount.deleteBtns === 3 && rowCount.terminalBtns === 2 && rowCount.cancellingBtns === 1,
    JSON.stringify(rowCount));

  // ── ② 点终态任务的删除：应先弹确认框，且不发 DELETE ────────────
  let calls = await callsOf(page);
  const before = calls.filter(c => c.startsWith('DELETE')).length;
  const terminalBtn = (await page.$$(`button[title="${TITLE_TERMINAL}"]`))[0];
  await terminalBtn.click();
  await waitDialog(page);
  const t = await measure(page);
  report.terminal = t;

  calls = await callsOf(page);
  check('② 点删除 → 弹出确认框（未直接删除，未发 DELETE）',
    (await hasDialog(page)) && calls.filter(c => c.startsWith('DELETE')).length === before,
    `DELETE 次数 ${before} → ${calls.filter(c => c.startsWith('DELETE')).length}`);
  check('② 标题为「删除这个任务？」', t.title === '删除这个任务？', `实际「${t.title}」`);
  check('② 文案含任务名与「删除后无法恢复。」',
    t.lines.some(l => l.includes('认知觉醒·第一期')) && t.warningVisible === true,
    JSON.stringify(t.lines));
  check('② 警示行是红色', /rgb\(239, 68, 68\)/.test(t.warningColor || ''), String(t.warningColor));
  check('② 弹窗不溢出视口', t.overflowLeft === 0 && t.overflowRight === 0,
    `左 ${t.overflowLeft} / 右 ${t.overflowRight}`);
  check('② 内容不溢出弹窗内边界', t.contentOverflowRight === 0,
    `超出 ${t.contentOverflowRight}px（最宽元素：${t.widestEl}）`);
  const [cancelBtn, deleteBtn] = t.buttons;
  check('② 两个按钮：取消（浅色，左）+ 删除（红色，右）',
    cancelBtn && deleteBtn && deleteBtn.left > cancelBtn.left &&
    /rgb\(239, 68, 68\)/.test(deleteBtn.bg) && !/rgb\(239, 68, 68\)/.test(cancelBtn.bg),
    JSON.stringify(t.buttons.map(b => ({ t: b.text, bg: b.bg, left: b.left }))));
  check('② 遮罩铺满视口', t.scrimCoversViewport === true, String(t.scrimCoversViewport));

  await page.screenshot({ path: `${OUT}-终态弹窗.png` });

  // ── ③ 点「取消」：关框、不发 DELETE ────────────────────────────
  await page.evaluate(() => {
    const dialog = [...document.querySelectorAll('div')]
      .find(el => String(el.className || '').includes('inset-0') && el.textContent.includes('无法恢复'));
    [...dialog.querySelectorAll('button')].find(b => b.textContent.trim() === '取消').click();
  });
  await dialogGone(page);
  calls = await callsOf(page);
  const afterCancel = calls.filter(c => c.startsWith('DELETE')).length;
  check('③ 点「取消」→ 关闭且不发 DELETE', afterCancel === before, `DELETE 次数 ${afterCancel}`);
  report.interactions.push({ step: '取消', deleteCalls: afterCancel });

  // ── ④ Esc 关闭 ────────────────────────────────────────────────
  await (await page.$$(`button[title="${TITLE_TERMINAL}"]`))[0].click();
  await waitDialog(page);
  await page.keyboard.press('Escape');
  await dialogGone(page);
  calls = await callsOf(page);
  check('④ Esc → 关闭且不发 DELETE',
    calls.filter(c => c.startsWith('DELETE')).length === before,
    `DELETE 次数 ${calls.filter(c => c.startsWith('DELETE')).length}`);
  report.interactions.push({ step: 'Esc', deleteCalls: calls.filter(c => c.startsWith('DELETE')).length });

  // ── ⑤ 点遮罩关闭（点右上角空白，避开左侧面板）──────────────────
  await (await page.$$(`button[title="${TITLE_TERMINAL}"]`))[0].click();
  await waitDialog(page);
  await page.mouse.click(VIEW.width - 20, 20);
  await dialogGone(page);
  calls = await callsOf(page);
  check('⑤ 点遮罩 → 关闭且不发 DELETE',
    calls.filter(c => c.startsWith('DELETE')).length === before,
    `DELETE 次数 ${calls.filter(c => c.startsWith('DELETE')).length}`);
  report.interactions.push({ step: '遮罩', deleteCalls: calls.filter(c => c.startsWith('DELETE')).length });

  // ── ⑥ 点确认「删除」：才真的发 DELETE，并自动关框 ──────────────
  await (await page.$$(`button[title="${TITLE_TERMINAL}"]`))[0].click();
  await waitDialog(page);
  await page.evaluate(() => {
    const dialog = [...document.querySelectorAll('div')]
      .find(el => String(el.className || '').includes('inset-0') && el.textContent.includes('无法恢复'));
    [...dialog.querySelectorAll('button')].find(b => b.textContent.trim() === '删除').click();
  });
  await dialogGone(page);
  calls = await callsOf(page);
  const deleteCalls = calls.filter(c => c.startsWith('DELETE'));
  check('⑥ 点确认「删除」→ 发出 DELETE 且弹窗自动关闭',
    deleteCalls.length === before + 1 && deleteCalls.some(c => c.includes('/api/queue/t-')),
    JSON.stringify(deleteCalls));
  report.interactions.push({ step: '确认删除', deleteCalls: deleteCalls.length, calls: deleteCalls });

  // ── ⑦ 「取消中」任务的弹窗：文案必须不同 ────────────────────────
  await (await page.$$(`button[title="${TITLE_CANCELLING}"]`))[0].click();
  await waitDialog(page);
  const c = await measure(page);
  report.cancelling = c;
  check('⑦ 取消中的任务标题为「移除这个任务？」', c.title === '移除这个任务？', `实际「${c.title}」`);
  check('⑦ 文案说清「可能直接删 / 可能只再请求取消」',
    c.lines.some(l => l.includes('正在取消中')) && c.lines.some(l => l.includes('再次请求取消')),
    JSON.stringify(c.lines));
  check('⑦ 同样带「删除后无法恢复。」', c.warningVisible === true);
  check('⑦ 不溢出视口与内边界',
    c.overflowLeft === 0 && c.overflowRight === 0 && c.contentOverflowRight === 0,
    `视口 ${c.overflowLeft}/${c.overflowRight}，内边界 ${c.contentOverflowRight}`);
  await page.screenshot({ path: `${OUT}-取消中弹窗.png` });

  await page.keyboard.press('Escape');
  await dialogGone(page);
  check('⑧ Esc 关闭「取消中」弹窗', !(await hasDialog(page)));
  // 该行确认删除也只应发 1 次 DELETE —— 这里不改状态（mock 数据固定），不再点确认

  // ── ⑨ 「清空已完成任务」是批量删除，同样不可恢复，也必须确认 ──────
  const countDeletes = async () => (await callsOf(page)).filter(c => c.startsWith('DELETE')).length;
  const clickClear = () => page.evaluate(() => {
    const b = [...document.querySelectorAll('#probe-root button')]
      .find(x => (x.textContent || '').includes('清空已完成任务'));
    if (b) b.click();
    return !!b;
  });
  const clickInDialog = (text) => page.evaluate((t) => {
    const d = [...document.querySelectorAll('div')]
      .find(el => String(el.className || '').includes('inset-0') && el.textContent.includes('无法恢复'));
    const b = [...d.querySelectorAll('button')].find(x => x.textContent.trim() === t);
    if (b) b.click();
    return !!b;
  }, text);

  const nd0 = await countDeletes(page);
  check('⑨ 面板上有「清空已完成任务」按钮', (await clickClear()) === true);
  await waitDialog(page);
  const cl = await measure(page);
  report.clear = cl;
  check('⑨ 点清空 → 只弹确认框，不直接清空',
    (await countDeletes(page)) === nd0, `DELETE 次数 ${nd0} → ${await countDeletes(page)}`);
  check('⑨ 标题为「清空已完成任务？」', cl.title === '清空已完成任务？', `实际「${cl.title}」`);
  const desc = cl.lines.join('').replace(/\s+/g, '');
  check('⑨ 文案写明条数与口径（「当前 2 条已结束任务」+ 四态列举）',
    desc.includes('将删除当前2条已结束任务（完成、失败、中断、取消）。'), desc);
  check('⑨ 带「清空后无法恢复。」', cl.warningVisible === true);
  check('⑨ 确认按钮文案为「清空」、红色、在右',
    cl.buttons.length === 2 && cl.buttons[1].text === '清空' &&
    cl.buttons[1].left > cl.buttons[0].left && /rgb\(239, 68, 68\)/.test(cl.buttons[1].bg),
    JSON.stringify(cl.buttons.map(b => ({ t: b.text, bg: b.bg, left: b.left }))));
  check('⑨ 同款弹窗尺寸（与删除确认一致，无样式漂移）',
    cl.modal.width === t.modal.width && cl.modal.maxWidth === t.modal.maxWidth,
    `${cl.modal.width} × ${cl.modal.height}`);
  await page.screenshot({ path: `${OUT}-清空弹窗.png` });

  // 取消 → 关闭且不发 DELETE
  await clickInDialog('取消');
  await dialogGone(page);
  check('⑨ 点取消 → 关闭且不发 DELETE', (await countDeletes(page)) === nd0, `DELETE 次数 ${await countDeletes(page)}`);

  // Esc → 关闭
  await clickClear();
  await waitDialog(page);
  await page.keyboard.press('Escape');
  await dialogGone(page);
  check('⑨ Esc → 关闭', !(await hasDialog(page)));

  // 确认「清空」→ 才发清空接口（DELETE /api/queue?kind=podcast，注意不是单条那个路径）
  await clickClear();
  await waitDialog(page);
  await clickInDialog('清空');
  await dialogGone(page);
  const allCalls = await callsOf(page);
  const clearCalls = allCalls.filter(c => c.startsWith('DELETE') && !/\/api\/queue\//.test(c));
  check('⑨ 点确认「清空」→ 发出 DELETE /api/queue?kind=podcast 且弹窗关闭',
    clearCalls.length === 1 && clearCalls[0].includes('kind=podcast'), JSON.stringify(clearCalls));
  report.interactions.push({ step: '确认清空', calls: clearCalls });

  fs.writeFileSync(`${OUT}.json`, JSON.stringify(report, null, 2));

  console.log('\n── 实测几何（终态弹窗）──');
  const m = t.modal;
  console.log(`viewport ${t.viewportW}px  rem ${t.rem}px`);
  console.log(`弹窗 宽 ${m.width}  高 ${m.height}  left ${m.left}  top ${m.top}  max-width ${m.maxWidth}  圆角 ${m.borderRadius}`);
  console.log(`内边距 上/左 ${m.padTop}/${m.padLeft}  内容右溢 ${t.contentOverflowRight}px`);
  console.log('按钮：');
  for (const b of t.buttons) console.log(`  「${b.text}」 ${b.width}×${b.height}  left ${b.left}  bg ${b.bg}`);
  console.log('\n── 实测几何（取消中弹窗）──');
  console.log(`弹窗 宽 ${c.modal.width}  高 ${c.modal.height}  内容右溢 ${c.contentOverflowRight}px`);
  console.log('\n── 实测几何（清空弹窗）──');
  console.log(`弹窗 宽 ${cl.modal.width}  高 ${cl.modal.height}  max-width ${cl.modal.maxWidth}  内容右溢 ${cl.contentOverflowRight}px`);
  console.log(`按钮：` + cl.buttons.map(b => `「${b.text}」${b.width}×${b.height} bg ${b.bg}`).join('  '));
  console.log('\n摘要：' + results.filter(r => r.ok).length + '/' + results.length + ' 通过');
  console.log('接口调用：' + JSON.stringify(allCalls, null, 0));

  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(async (e) => {
  console.error('SCRIPT ERROR:', e && e.message);
  process.exit(2);
});
