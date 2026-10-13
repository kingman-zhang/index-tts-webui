// 「任务队列跟随页面模式 + 只请求本页类型」的取证。
//
// 加载真实组件（src/__probe_queue_kind.tsx 把两个 QueuePanel 分别按 podcast/mono
// 页面模式挂到 dev server 页面上），只把 /api/queue 用 fetch mock 喂一个**混合 kind**
// 的任务列表。因此下面每条断言都是「真组件 → 真过滤 → 真请求」。
//
// 要证的事：
//   ① 面板里**不再出现**「双人播客 / 单人配音」切换按钮（tab 已移除）
//   ② 播客页面板只渲染 podcast 任务、配音页面板只渲染 mono 任务
//   ③ header 的「共 N 个」= 本类型数量（不再受另一类型干扰）
//   ④ 状态筛选行仍在，且计数只统计本类型
//   ⑤ 「清空已完成任务」按钮仍在
//   ⑥ 请求带上本页类型：/api/queue?...&kind=podcast 与 ...&kind=mono（不再拉全量两种）
//   ⑦ 空闲退避：播客页数据**全是终态**（没有任何会自己变的东西）⇒ 不再 2s 空跑；
//      配音页有 running ⇒ 保持活跃轮询。用「同样时长内两边的请求次数」对比来证。
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-queue-kind-follow-page.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-10-队列类型跟随页面';
const VIEW = { width: 900, height: 900 };

// 一个**混合 kind** 的全量列表：后端一次返回两种类型，前端各面板按 kind 请求/过滤。
//   podcast：只有终态 ⇒ 「无活跃」⇒ 应退避      （用于证 ⑦ 的左半边）
//   mono   ：有 running ⇒ 「活跃」⇒ 应保持 2s  （用于证 ⑦ 的右半边）
const QUEUE_PAYLOAD = {
  tasks: [
    { id: 'p1', project_name: '播客·认知觉醒（完成）', kind: 'podcast', status: 'success',
      progress: 1, current_line: 0, total_lines: 0, message: '', audio_url: '/outputs/demo.mp3',
      duration_sec: 300, created_at: '2026-10-10T10:00:00' },
    { id: 'm1', project_name: '单人·第一章（完成）', kind: 'mono', status: 'success',
      progress: 1, current_line: 0, total_lines: 0, message: '', audio_url: '/outputs/demo.mp3',
      duration_sec: 120, created_at: '2026-10-10T09:00:00' },
    { id: 'm2', project_name: '单人·第二章（合成中）', kind: 'mono', status: 'running',
      progress: 0.4, current_line: 2, total_lines: 5, message: '',
      created_at: '2026-10-10T09:10:00' },
  ],
  count: 3, terminal_total: 2, has_more: false, current: 'm2', queued: 0, queue_order: [],
};

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

/** 读一个探针面板的可观测状态 */
const readPanel = (page, id) => page.evaluate((pid) => {
  const root = document.getElementById(pid);
  if (!root) return null;
  const btns = [...root.querySelectorAll('button')].map(b => (b.textContent || '').replace(/\s+/g, ' ').trim());
  const kindTabBtns = btns.filter(t => /^(双人播客|单人配音)/.test(t));
  const totalSpan = [...root.querySelectorAll('span')]
    .map(s => (s.textContent || '').trim())
    .find(t => /^共 \d+ 个$/.test(t)) || '';
  const filterBtns = {};
  for (const t of btns) {
    const m = t.match(/^(成功|失败|排队|暂停) (\d+)$/);
    if (m) filterBtns[m[1]] = Number(m[2]);
  }
  return {
    // 任务行独有 class 组合（rounded-lg border p-2.5 transition-colors）；
    // 只用 .rounded-lg.border 会连「清空已完成任务」按钮一起命中。
    rows: root.querySelectorAll('.rounded-lg.border.p-2\\.5').length,
    kindTabBtns,
    totalSpan,
    total: Number((totalSpan.match(/\d+/) || [0])[0]),
    filterBtns,
    hasClearBtn: btns.some(t => t.includes('清空已完成任务')),
  };
}, id);

const queueCalls = (page) => page.evaluate(() => window.__probeCalls || []);
const countKind = (calls, kind) => calls.filter(c => c.includes(`kind=${kind}`)).length;

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.setViewport({ ...VIEW, deviceScaleFactor: 2 });

  await page.evaluateOnNewDocument((payload) => {
    window.__probeQueue = payload;
    const orig = window.fetch.bind(window);
    const calls = [];
    window.__probeCalls = calls;
    const json = (o, status = 200) =>
      new Response(JSON.stringify(o), { status, headers: { 'Content-Type': 'application/json' } });
    const isListGet = (url) => !/\/api\/queue\/[^/?]+/.test(url);
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (url.includes('/api/queue')) {
        if (method === 'GET' && isListGet(url)) {
          // 后端列表接口已支持 kind：这里**如实模拟**它按 kind 过滤后再返回，
          // 否则「无活跃任务应退避」这条永远验不出来（两个面板都看到同一份含 running 的数据）。
          calls.push(url);
          const m = url.match(/[?&]kind=([a-z]+)/);
          const kind = m ? m[1] : null;
          const tasks = kind
            ? payload.tasks.filter(t => (t.kind || 'podcast') === kind)
            : payload.tasks;
          const terminal = tasks.filter(
            t => !['queued', 'running', 'syncing', 'paused'].includes(t.status));
          return json({ ...payload, tasks, count: tasks.length, terminal_total: terminal.length });
        }
        return json({ ok: true });
      }
      if (url.includes('/api/')) return json({ detail: 'probe: not mocked' }, 404);
      return orig(input, init);
    };
  }, QUEUE_PAYLOAD);

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_queue_kind.tsx')` });
  await page.waitForSelector('#probe-podcast .rounded-lg.border.p-2\\.5', { timeout: 15000 });
  await page.waitForSelector('#probe-mono .rounded-lg.border.p-2\\.5', { timeout: 15000 });

  const P = await readPanel(page, 'probe-podcast');
  const M = await readPanel(page, 'probe-mono');
  const report = { viewport: VIEW, podcast: P, mono: M };

  // ── ① tab 已移除 ──────────────────────────────────────────────
  check('① 播客页面板无「双人播客 / 单人配音」切换按钮', P.kindTabBtns.length === 0, JSON.stringify(P.kindTabBtns));
  check('① 配音页面板无「双人播客 / 单人配音」切换按钮', M.kindTabBtns.length === 0, JSON.stringify(M.kindTabBtns));

  // ── ② 各取各的任务 ────────────────────────────────────────────
  check('② 播客页面板只渲染 1 条 podcast 任务', P.rows === 1, `rows=${P.rows}`);
  check('② 配音页面板只渲染 2 条 mono 任务', M.rows === 2, `rows=${M.rows}`);

  // ── ③ header 计数只看本类型 ───────────────────────────────────
  check('③ 播客页「共 1 个」', P.total === 1, `"${P.totalSpan}"`);
  check('③ 配音页「共 2 个」', M.total === 2, `"${M.totalSpan}"`);

  // ── ④ 筛选行仍在且计数只算本类型 ──────────────────────────────
  check('④ 播客页筛选计数：成功1 失败0 排队0',
    P.filterBtns['成功'] === 1 && P.filterBtns['失败'] === 0 && P.filterBtns['排队'] === 0,
    JSON.stringify(P.filterBtns));
  check('④ 配音页筛选计数：成功1 失败0 排队0（running 不计入任何筛选桶）',
    M.filterBtns['成功'] === 1 && M.filterBtns['失败'] === 0 && M.filterBtns['排队'] === 0,
    JSON.stringify(M.filterBtns));

  // ── ⑤ 批量清空入口未丢 ────────────────────────────────────────
  check('⑤ 两面板的「清空已完成任务」按钮仍在',
    P.hasClearBtn === true && M.hasClearBtn === true,
    `podcast=${P.hasClearBtn} mono=${M.hasClearBtn}`);

  await page.screenshot({ path: `${OUT}.png`, fullPage: true });

  // ── ⑥ 请求带上本页类型 ────────────────────────────────────────
  const callsFirst = await queueCalls(page);
  check('⑥ 播客页请求带 kind=podcast', countKind(callsFirst, 'podcast') >= 1,
    JSON.stringify(callsFirst.slice(0, 6)));
  check('⑥ 配音页请求带 kind=mono', countKind(callsFirst, 'mono') >= 1,
    JSON.stringify(callsFirst.slice(0, 6)));
  check('⑥ 没有不带 kind 的列表请求（不再拉全量两种类型）',
    callsFirst.every(u => u.includes('kind=')), JSON.stringify(callsFirst.slice(0, 6)));

  // ── ⑦ 空闲退避：终态面板不再 2s 空跑，活跃面板照常 ────────────
  // podcast 全是终态 ⇒ 下一次轮询在 20s 后；mono 有 running ⇒ 每 2s 一次。
  // 采样 4.5s：podcast 的请求总数仍应是 1；mono 至少 3 次。
  const t0 = Date.now();
  await new Promise(r => setTimeout(r, 4500));
  const callsLater = await queueCalls(page);
  const podN = countKind(callsLater, 'podcast');
  const monN = countKind(callsLater, 'mono');
  report.polling = { sampledMs: Date.now() - t0, podcastCalls: podN, monoCalls: monN };
  check('⑦ 终态面板 4.5s 内只请求 1 次（已退避，不再 2s 空跑）', podN === 1, `podcast 次数=${podN}`);
  check('⑦ 活跃面板同期请求 ≥3 次（保持 2s 节奏）', monN >= 3, `mono 次数=${monN}`);

  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(`${OUT}.json`, JSON.stringify(report, null, 2));

  console.log('\n── 实测 ──');
  console.log('播客页：', JSON.stringify(P));
  console.log('配音页：', JSON.stringify(M));
  console.log('轮询：', JSON.stringify(report.polling));
  console.log('列表请求：', JSON.stringify(callsLater));
  console.log('\n摘要：' + results.filter(r => r.ok).length + '/' + results.length + ' 通过');

  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(async (e) => {
  console.error('SCRIPT ERROR:', e && e.message);
  process.exit(2);
});
