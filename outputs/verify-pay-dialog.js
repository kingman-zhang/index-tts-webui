// 「积分商城 · 真实支付出码」组件级取证：出码弹窗（三形态）+ 付款后轮询入账 + 商城接线。
//
// 加载真组件（src/__probe_pay.tsx 把 ShopCard / PayDialog 挂到 dev server 页面上），
// fetch 全量 mock —— 下面每条断言都是「真点击 → 真状态机 → 真 DOM」。
//
// 要证的事（= 本次改动的验收标准）：
//   ① 点「购买」→ 调出码端点 1 次 → 弹出二维码弹窗；二维码**编的就是**通道给的 pay_info
//   ② 付款前一直在轮询（只读单条订单），未支付时不误判成功
//   ③ mpay 回调入账后（mock 里把订单翻成 paid）→ 轮询发现 → 弹窗转成功态并自动关闭 + 余额/订单刷新
//   ④ 「支付成功」回调只触发一次（轮询与手动查询不会双发）
//   ⑤ 待支付订单行「去支付」可重开弹窗；复用窗口内显示复用提示
//      （时长取自后端下发的 valid_seconds，不硬编码）
//   ⑥ jump / urlscheme / 未知形态 三种渲染各自正确
//   ⑦ 真实通道 + 模拟支付双开时给红色告警（MEMBER_MOCK_PAY=1 安全提示）
//   ⑧ 无支付通道时回落演示模式（点购买直接模拟入账）
//   ⑨ Esc 关闭弹窗且不触发入账回调
//   ⑩ 全程不出现原生 dialog
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-pay-dialog.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-14-支付出码取证';
const VIEW = { width: 1280, height: 980 };

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

const PROBE_BTN = (t) => `[...document.querySelectorAll('#probe-root > div > div button')].find(b => (b.textContent||'').trim() === ${JSON.stringify(t)})`;
const SHOP_BTN = (t) => `[...document.querySelectorAll('#probe-root button')].find(b => (b.textContent||'').trim() === ${JSON.stringify(t)})`;

/** 当前弹窗（fixed + inset-0）的文本；无弹窗返回 null */
const DIALOG_TEXT = `(() => {
  for (const el of document.querySelectorAll('div')) {
    const cls = String(el.className || '');
    if (cls.includes('inset-0') && cls.includes('fixed')) return el.textContent || '';
  }
  return null;
})()`;

const dialogText = (page) => page.evaluate(DIALOG_TEXT);

const dialogButtons = (page) => page.evaluate(`(() => {
  for (const el of document.querySelectorAll('div')) {
    const cls = String(el.className || '');
    if (cls.includes('inset-0') && cls.includes('fixed'))
      return [...el.querySelectorAll('button')].map(b => (b.textContent || '').trim());
  }
  return null;
})()`);

const homeText = (page) => page.evaluate(() =>
  (document.querySelector('#probe-root') || {}).textContent || '');

const mockStat = (page) => page.evaluate(() => ({ ...window.__mock }));

const setPacks = (page, p) => page.evaluate((o) => window.__setProbePacks(o), p);

const waitGone = (page) => page.waitForFunction(
  () => ![...document.querySelectorAll('div')].some(el => {
    const c = String(el.className || '');
    return c.includes('inset-0') && c.includes('fixed');
  }), { timeout: 6000, polling: 50 });

const waitDialogWith = (page, t) => page.waitForFunction((s) => {
  for (const el of document.querySelectorAll('div')) {
    const c = String(el.className || '');
    if (c.includes('inset-0') && c.includes('fixed')) return (el.textContent || '').includes(s);
  }
  return false;
}, { timeout: 8000, polling: 50 }, t);

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

  // ── mock：只拦 /api/*，其余放行 ─────────────────────────────
  await page.evaluateOnNewDocument(() => {
    // ⚠️ 与 src/__probe_pay.tsx 的 QR_VALUE 各写一份：对不上就该红。
    const QR_VALUE = 'https://mpay.lizigege.com/pay/T20260001';
    const PACKS = {
      packs: [{ id: 'p1', name: '体验包', price_fen: 1000, price_yuan: '10.00', points: 1000,
                bonus_points: 0, bonus_percent: 0, est_chars: 200000, order: 1 }],
      currency: 'CNY', points_per_yuan: 100, points_per_1000_chars: 5,
      mock_pay_enabled: false, pay_channel_ready: true, pay_provider: 'mpay',
    };
    const st = { orderPaid: false, payCalls: 0, orderCalls: 0, listCalls: 0, packsCalls: 0, mockPayCalls: 0 };
    window.__mock = st;
    const json = (o, s = 200) =>
      new Response(JSON.stringify(o), { status: s, headers: { 'Content-Type': 'application/json' } });
    const ORDER = () => ({
      order_id: 'ord_probe00001', pack_id: 'p1', pack_name: '体验包', points: 1000,
      amount_fen: 1000, status: st.orderPaid ? 'paid' : 'pending', provider: 'mpay',
      out_trade_no: 'ord_probe00001', created_at: '2026-10-14T10:00:00',
      paid_at: st.orderPaid ? '2026-10-14T10:01:00' : null,
    });
    window.fetch = async (input, init) => {
      const raw = typeof input === 'string' ? input : (input && input.url) || '';
      const path = raw.split('?')[0];
      const method = ((init && init.method) || 'GET').toUpperCase();
      if (path === '/api/points/packs') {
        st.packsCalls++;
        return json({ ...PACKS, ...(window.__packsOverride || {}) });
      }
      if (/^\/api\/points\/orders\/[^/]+\/pay$/.test(path)) {
        st.payCalls++;
        return json({ order: ORDER(), pay: { pay_type: 'qrcode', pay_info: QR_VALUE,
                                             trade_no: 'T20260001',
                                             valid_seconds: 900,
                                             reused: st.payCalls > 1 } });
      }
      if (/^\/api\/points\/orders\/[^/]+\/mock-pay$/.test(path)) {
        st.mockPayCalls++; st.orderPaid = true;
        return json({ order: ORDER(), added: 1000, balance: 1000, already_paid: false });
      }
      if (/^\/api\/points\/orders\/[^/]+$/.test(path)) {
        st.orderCalls++;
        return json({ order: ORDER() });
      }
      if (path === '/api/points/orders' && method === 'POST') return json({ order: ORDER() });
      if (path === '/api/points/orders') {
        st.listCalls++;
        return json({ total: st.payCalls ? 1 : 0, offset: 0, orders: st.payCalls ? [ORDER()] : [] });
      }
      return json({ detail: 'probe: not mocked ' + path }, 404);
    };
  });

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_pay.tsx')` });
  await page.waitForSelector('#probe-root', { timeout: 15000 });
  await page.waitForSelector('#probe-ref-qr svg', { timeout: 8000 });
  await new Promise(r => setTimeout(r, 500));

  const report = { viewport: VIEW };

  // ── ① 初始：商城不再挂「已接入真实支付通道」那行说明，无弹窗 ──────
  //    2026-10-15 用户要求删掉那段文案（通道名一变它就得跟着改，且它不是必需信息）。
  let ht = await homeText(page);
  check('① 「已接入真实支付通道」说明文案已删除', !ht.includes('已接入真实支付通道'),
    JSON.stringify(ht.slice(0, 80)));
  check('① 初始无弹窗', (await dialogText(page)) === null);

  // ── ① 点「购买」→ 出码 → 二维码弹窗 ──────────────────────────
  await page.evaluate(`${SHOP_BTN('购买')}.click()`);
  await waitDialogWith(page, '体验包');
  let st = await mockStat(page);
  check('① 点购买只调出码端点 1 次', st.payCalls === 1, `payCalls=${st.payCalls}`);
  check('① 未走模拟支付', st.mockPayCalls === 0, `mockPayCalls=${st.mockPayCalls}`);

  let dt = await dialogText(page);
  check('① 弹窗显示金额 ¥10 与积分', /¥10/.test(dt) && dt.includes('1000'), JSON.stringify(dt.slice(0, 90)));
  check('① 含「微信扫码」引导', dt.includes('微信「扫一扫」'));
  check('① 含等待付款提示', dt.includes('等待付款结果'));
  check('① 未显示成功态（还没付款）', !dt.includes('支付成功'));

  // 二维码内容必须是通道给的 pay_info（与隐藏参照码逐字节比对）
  // ⚠️ 弹窗里不止一个 svg（右上角关闭按钮也是），lucide 图标 viewBox 恒为 0 0 24 24，据此排除。
  const qr = await page.evaluate(() => {
    let dlg = null;
    for (const el of document.querySelectorAll('div')) {
      const c = String(el.className || '');
      if (c.includes('inset-0') && c.includes('fixed')) { dlg = el; break; }
    }
    const pick = (root) => root && [...root.querySelectorAll('svg')]
      .find(s => s.getAttribute('viewBox') !== '0 0 24 24');
    const a = pick(dlg);
    const b = pick(document.querySelector('#probe-ref-qr'));
    if (!a || !b) return { ok: false, hasA: !!a, hasB: !!b };
    const r = a.getBoundingClientRect();
    return { ok: a.innerHTML === b.innerHTML, w: Math.round(r.width), h: Math.round(r.height),
             viewBox: a.getAttribute('viewBox'), len: a.innerHTML.length };
  });
  report.qr = qr;
  check('① 二维码编的就是通道返回的 pay_info（与参照码逐字节相同）', qr.ok === true,
    JSON.stringify(qr).slice(0, 140));
  check('① 二维码渲染尺寸 ≈168×168', qr.w === 168 && qr.h === 168, `${qr.w}×${qr.h}`);
  await page.screenshot({ path: `${OUT}-①二维码弹窗.png` });

  // ── ② 付款前一直轮询，且不误判 ───────────────────────────────
  await new Promise(r => setTimeout(r, 2600));
  st = await mockStat(page);
  check('② 轮询在跑（订单查询 ≥1 次）', st.orderCalls >= 1, `orderCalls=${st.orderCalls}`);
  check('② 未付款时不显示成功', !(await dialogText(page)).includes('支付成功'));

  // ── ③ mock 回调入账 → 轮询发现 → 成功态 + 自动关闭 ────────────
  // 走 ShopCard 这条路时，「入账回调」对外的可见形态就是那句成功提示（余额刷新 + toast），
  // 所以用它的**出现次数**来证明回调没双发 —— 比数内部计数器更贴近用户能看到的事实。
  const PAID_MSG = '支付成功，+1000 积分';
  await page.evaluate(() => { window.__mock.orderPaid = true; });
  await waitDialogWith(page, '支付成功');
  await page.screenshot({ path: `${OUT}-③支付成功.png` });
  const listCallsBeforeClose = (await mockStat(page)).listCalls;
  let log = await page.evaluate(() => window.__probeLog || []);
  check('③ 轮询发现已支付 → 转成功态', true);
  check('③ 弹出「' + PAID_MSG + '」提示',
    log.filter(x => x.includes(PAID_MSG)).length === 1, JSON.stringify(log.slice(-4)));
  check('③ 入账后刷新了订单列表', listCallsBeforeClose > 0, `listCalls=${listCallsBeforeClose}`);

  await waitGone(page);
  check('③ 成功态展示后弹窗自动关闭', (await dialogText(page)) === null);

  st = await mockStat(page);
  check('③ 全程只出码 1 次（未重复下单）', st.payCalls === 1, `payCalls=${st.payCalls}`);
  log = await page.evaluate(() => window.__probeLog || []);
  check('④ 成功提示总计仍为 1 次（轮询未双发）',
    log.filter(x => x.includes(PAID_MSG)).length === 1,
    JSON.stringify(log.filter(x => x.includes('支付成功'))));

  // ── ⑤ 待支付订单行「去支付」+ 复用提示 ───────────────────────
  await page.evaluate(() => { window.__mock.orderPaid = false; });
  await setPacks(page, {});                       // 重挂商城 → 重新拉列表（此时回到 pending）
  await page.waitForFunction(() => (document.querySelector('#probe-root') || {}).textContent.includes('去支付'),
    { timeout: 6000, polling: 50 });
  await page.evaluate(`${SHOP_BTN('去支付')}.click()`);
  await waitDialogWith(page, '体验包');
  st = await mockStat(page);
  check('⑤「去支付」重新出码（payCalls=2）', st.payCalls === 2, `payCalls=${st.payCalls}`);
  dt = await dialogText(page);
  check('⑤ 复用窗口内提示沿用支付码', dt.includes('沿用本次会话已生成的支付码'), JSON.stringify(dt.slice(-80)));
  check('⑤ 订单号展示正确', dt.includes('ord_probe00001'));
  await page.screenshot({ path: `${OUT}-⑤去支付复用.png` });

  // ── ⑨ Esc 关闭且不触发入账 ───────────────────────────────────
  const paidBefore = (await page.evaluate(() => window.__probeLog || []))
    .filter(x => x.includes(PAID_MSG)).length;
  await page.keyboard.press('Escape');
  await waitGone(page);
  const paidAfter = (await page.evaluate(() => window.__probeLog || []))
    .filter(x => x.includes(PAID_MSG)).length;
  check('⑨ Esc 关闭弹窗且未触发入账', paidAfter === paidBefore && paidAfter === 1, `${paidBefore} → ${paidAfter}`);

  // ── ⑦ 双开告警 ───────────────────────────────────────────────
  await setPacks(page, { mock_pay_enabled: true, pay_channel_ready: true });
  await page.waitForFunction(() => (document.querySelector('#probe-root') || {}).textContent.includes('MEMBER_MOCK_PAY'),
    { timeout: 6000, polling: 50 });
  ht = await homeText(page);
  check('⑦ 真实通道 + 模拟支付双开 → 红色告警', ht.includes('MEMBER_MOCK_PAY') && ht.includes('免费入账'));
  await page.screenshot({ path: `${OUT}-⑦双开告警.png` });

  // ── ⑧ 无通道 → 回落演示模式（模拟支付） ───────────────────────
  await page.evaluate(() => { window.__mock.orderPaid = false; });
  await setPacks(page, { mock_pay_enabled: true, pay_channel_ready: false, pay_provider: '' });
  await page.waitForFunction(() => (document.querySelector('#probe-root') || {}).textContent.includes('演示模式'),
    { timeout: 6000, polling: 50 });
  const payBefore = (await mockStat(page)).payCalls;
  await page.evaluate(`${SHOP_BTN('购买')}.click()`);
  await new Promise(r => setTimeout(r, 800));
  st = await mockStat(page);
  check('⑧ 无通道时点购买走模拟支付、不出码',
    st.mockPayCalls >= 1 && st.payCalls === payBefore, `mock=${st.mockPayCalls} pay=${st.payCalls}`);
  check('⑧ 不弹支付码弹窗', (await dialogText(page)) === null);

  // ── ⑥ 三种出码形态渲染 ───────────────────────────────────────
  const variants = [
    ['PayDialog:jump', ['打开支付页面', '复制支付链接'], 'jump'],
    ['PayDialog:urlscheme', ['打开小程序支付'], 'urlscheme'],
    ['PayDialog:unknown', ['raw-payload-from-channel'], 'unknown'],
  ];
  for (const [btn, expects, shot] of variants) {
    await page.evaluate(`${PROBE_BTN(btn)}.click()`);
    await waitDialogWith(page, '体验包');
    await new Promise(r => setTimeout(r, 250));
    const txt = await dialogText(page);
    const btns = await dialogButtons(page);
    const ok = expects.every(e => txt.includes(e)) || expects.every(e => (btns || []).includes(e));
    check(`⑥ ${shot} 形态渲染正确（${expects.join(' / ')}）`, ok,
      JSON.stringify((btns || []).concat(txt.slice(0, 60))).slice(0, 160));
    await page.screenshot({ path: `${OUT}-⑥${shot}.png` });
    await page.keyboard.press('Escape');
    await waitGone(page);
  }

  // ── ⑩ 无原生 dialog ─────────────────────────────────────────
  check('⑩ 全程原生 dialog 出现 0 次', nativeDialogs === 0, `${nativeDialogs} 次`);

  report.stats = await mockStat(page);
  report.log = await page.evaluate(() => window.__probeLog || []);
  fs.writeFileSync(`${OUT}.json`, JSON.stringify(report, null, 2));

  console.log('\n── mock 计数 ──');
  console.log(JSON.stringify(report.stats));
  console.log('\n摘要：' + results.filter(r => r.ok).length + '/' + results.length + ' 通过');
  console.log('原生 dialog：' + nativeDialogs + ' 次');

  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(async (e) => {
  console.error('SCRIPT ERROR:', e && e.message);
  process.exit(2);
});
