// 「同面额拦截弹窗 + 全部订单页」组件级取证（本批 4 条改动的验收）。
//
// 加载真组件（src/__probe_orders.tsx 把 AccountPage 的 ShopCard 与 OrdersPage 挂到
// dev server 页面上），fetch 全量 mock —— 下面每条断言都是「真点击 → 真状态机 → 真 DOM」。
//
// 要证的事：
//   ① 商城已删掉「已接入真实支付通道」说明；「最近订单」右侧出现「查看更多」
//   ② 同面额被占时点「购买」→ 弹的是**只有确认按钮**的告知弹窗（不是角落 toast），
//      正文 = **后端下发的原句**，点确认后关闭
//   ②b ★弹窗内容随「占位的是谁」变：
//        other（别人的单占着）→ **不出现**「最近订单 / 去支付」那句指引
//        self （自己那笔占着）→ 才补上那句指引
//   ③ 被拦时列表条数不变（前端没有凭空多渲染一笔）
//   ④ 点「查看更多」后路由切到 /account/orders
//   ⑤ 全部订单页渲染完整字段（订单号 / 交易号 / 通道 / 三态徽章）并能「加载更多」补齐
//   ⑥ 全程不出现原生 dialog
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-order-flow.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-15-订单流取证';
const VIEW = { width: 1280, height: 980 };

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

/** 后端下发的原句 —— 必须与 service.SAME_AMOUNT_BUSY_MSG 逐字相同。 */
const BUSY_MSG = '该面额当前有一笔待支付的订单，请约 5 分钟后重试';

const PROBE_BTN = (t) =>
  `[...document.querySelectorAll('#probe-root button')].find(b => (b.textContent||'').trim() === ${JSON.stringify(t)})`;

/** 当前弹窗（fixed + inset-0）的文本与按钮文字；无弹窗返回 null */
const DIALOG_INFO = `(() => {
  for (const el of document.querySelectorAll('div')) {
    const cls = String(el.className || '');
    if (cls.includes('inset-0') && cls.includes('fixed')) {
      return {
        text: el.textContent || '',
        buttons: [...el.querySelectorAll('button')].map(b => (b.textContent || '').trim()),
      };
    }
  }
  return null;
})()`;
const dialogInfo = (page) => page.evaluate(DIALOG_INFO);
const probeText = (page) => page.evaluate(() =>
  (document.querySelector('#probe-root') || {}).textContent || '');
const mockStat = (page) => page.evaluate(() => ({ ...window.__mock }));

/** 切一下"占位的是谁"（other / self），点「购买」，等告知弹窗出现，返回弹窗内容。 */
async function buyAndWaitDialog(page, kind) {
  await page.evaluate((k) => { window.__mock.busyKind = k; }, kind);
  await page.evaluate(`${PROBE_BTN('购买')}.click()`);
  await page.waitForFunction((s) => {
    for (const el of document.querySelectorAll('div')) {
      const c = String(el.className || '');
      if (c.includes('inset-0') && c.includes('fixed')) return (el.textContent || '').includes(s);
    }
    return false;
  }, { timeout: 8000, polling: 50 }, BUSY_MSG);
  return dialogInfo(page);
}

/** 点告知弹窗里的「知道了」关掉它。 */
async function closeAlert(page) {
  await page.evaluate(`(() => {
    for (const el of document.querySelectorAll('div')) {
      const c = String(el.className || '');
      if (c.includes('inset-0') && c.includes('fixed')) {
        const b = [...el.querySelectorAll('button')].find(x => (x.textContent||'').trim() === '知道了');
        if (b) { b.click(); return true; }
      }
    }
    return false;
  })()`);
  await new Promise(r => setTimeout(r, 300));
}

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
  await page.evaluateOnNewDocument((busyMsg) => {
    // 记录所有前端发起的导航。
    // ⚠️ 不能直接断言 `location.pathname`：dev server 上真实 app 也在跑，而探针里
    //    是**未登录**状态 —— App.tsx 的强制登录逻辑会把 /account/orders 又踢回
    //    /account（那是**期望行为**，生产里用户已登录不会发生）。所以这里测的是
    //    "前端请求导航到哪"，与门禁无关。
    const nav = [];
    const origPush = history.pushState.bind(history);
    window.__nav = nav;
    history.pushState = function (...args) { nav.push(String(args[2])); return origPush(...args); };

    const PACKS = {
      // 单价与线上同款（50 积分/千字）⇒ 1000 积分 = 2 万字；est_chars 与之自洽，
      // 这样「字数 → 时长」的换算能被断言锁住具体数字，而不是只断言"出现了小时"。
      packs: [{ id: 'p1', name: '体验包', price_fen: 1000, price_yuan: '10.00', points: 1000,
                bonus_points: 0, bonus_percent: 0, est_chars: 20000, order: 1 }],
      currency: 'CNY', points_per_yuan: 100, points_per_1000_chars: 50,
      // 真实通道就绪 + 关掉模拟支付 = 走真实的「出码」分支，也就是会被同面额预检拦的那条路
      mock_pay_enabled: false, pay_channel_ready: true, pay_provider: 'mpay',
    };
    // 25 笔：第 1 条待支付、第 2 条已过期、其余已支付 —— 三态徽章都要能被看到
    const mkOrder = (i) => ({
      order_id: `ord_probe${String(i).padStart(5, '0')}`,
      pack_id: 'p1', pack_name: '体验包', points: 1000, amount_fen: 1000,
      status: i === 1 ? 'pending' : i === 2 ? 'expired' : 'paid',
      provider: 'mpay', out_trade_no: `ord_probe${String(i).padStart(5, '0')}`,
      created_at: '2026-10-15T15:58:00',
      paid_at: i > 2 ? '2026-10-15T15:59:00' : null,
      ...(i > 2 ? { trade_no: `T2026${String(i).padStart(4, '0')}` } : {}),
    });
    const st = {
      createCalls: 0, listCalls: 0, payCalls: 0,
      // 'other' = 别人的单占着这个面额（默认）；'self' = 用户自己那笔占着。
      // 后端就是靠这两个码告诉前端"弹窗里那句指引给不给"的。
      busyKind: 'other',
      orders: Array.from({ length: 25 }, (_, k) => mkOrder(k + 1)),
    };
    window.__mock = st;
    const json = (o, s = 200) =>
      new Response(JSON.stringify(o), { status: s, headers: { 'Content-Type': 'application/json' } });
    window.fetch = async (input, init) => {
      const raw = typeof input === 'string' ? input : (input && input.url) || '';
      const p = raw.split('?')[0];
      const q = new URLSearchParams(raw.split('?')[1] || '');
      const method = ((init && init.method) || 'GET').toUpperCase();
      if (p === '/api/points/packs') return json(PACKS);
      // ★ 同面额被占：下单接口在**创建之前**就 409，且带机器可读的 code
      //    （self / other 决定前端弹窗里那句「去最近订单付款」的指引给不给）
      if (p === '/api/points/orders' && method === 'POST') {
        st.createCalls++;
        return json({ detail: busyMsg, code: 'same_amount_busy_' + st.busyKind }, 409);
      }
      if (p === '/api/points/orders') {
        st.listCalls++;
        const offset = Number(q.get('offset') || 0);
        const limit = Number(q.get('limit') || 20);
        return json({ total: st.orders.length, offset,
                      orders: st.orders.slice(offset, offset + limit) });
      }
      if (/^\/api\/points\/orders\/[^/]+\/pay$/.test(p)) { st.payCalls++; return json({}, 500); }
      return json({ detail: 'probe: not mocked ' + p }, 404);
    };
  }, BUSY_MSG);

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_orders.tsx')` });
  await page.waitForSelector('#probe-root', { timeout: 15000 });
  await page.waitForFunction(() =>
    (document.querySelector('#probe-root') || {}).textContent.includes('最近订单'),
    { timeout: 10000, polling: 50 });
  await new Promise(r => setTimeout(r, 400));

  // ── ① 商城：说明文案已删 + 有「查看更多」 ─────────────────────
  const ht = await probeText(page);
  check('① 「已接入真实支付通道」说明已删除', !ht.includes('已接入真实支付通道'),
    JSON.stringify(ht.slice(0, 80)));
  check('① 「最近订单」区还在', ht.includes('最近订单'));
  check('① 右侧有「查看更多」', ht.includes('查看更多'));
  // 时长的唯一真源是前端常量 TTS_CHARS_PER_SECOND=5.08，与同屏的「N 万字」由同一份字数派生：
  // 2 万字 ÷ 5.08 字/秒 ≈ 3937s ≈ 1.1 小时。这两条同时锁住「加了时长」和「换算没写反」。
  check('① 商城顶部那行补了时长（1000 积分 ≈ 2 万字 ≈ 1.1 小时音频）',
    ht.includes('1000 积分 ≈ 2 万字 ≈ 1.1 小时音频'), JSON.stringify(ht.slice(0, 200)));
  check('① 套餐卡片补了时长（约 2 万字 ≈ 1.1 小时）',
    ht.includes('约 2 万字 ≈ 1.1 小时'), JSON.stringify(ht.slice(0, 260)));
  await page.screenshot({ path: `${OUT}-①商城.png` });

  // ── ② 占位的是**别人** ⇒ 弹窗**不该**指引「去最近订单」─────────
  //    用户原话："我明明没下过单，为什么要让我找最近订单中未支付的"。
  const before = await mockStat(page);
  const dlgOther = await buyAndWaitDialog(page, 'other');
  check('② 弹出的是弹窗（不是角落 toast）', !!dlgOther, JSON.stringify(dlgOther));
  check('② 弹窗正文 = 后端下发的原句', !!dlgOther && dlgOther.text.includes(BUSY_MSG),
    JSON.stringify(((dlgOther || {}).text || '').slice(0, 120)));
  check('② 弹窗**只有确认按钮**', !!dlgOther && dlgOther.buttons.length === 1,
    JSON.stringify((dlgOther || {}).buttons));
  check('② 那个按钮写着「知道了」', !!dlgOther && dlgOther.buttons[0] === '知道了',
    JSON.stringify((dlgOther || {}).buttons));
  check('② 弹窗里没有「取消」（它不是二选一）', !!dlgOther && !dlgOther.buttons.includes('取消'),
    JSON.stringify((dlgOther || {}).buttons));
  check('★★占位的是别人 ⇒ **没有**「最近订单」指引（用户手里根本没单）',
    !!dlgOther && !dlgOther.text.includes('最近订单'),
    JSON.stringify(((dlgOther || {}).text || '').slice(0, 160)));
  check('★★同理也不提「去支付」', !!dlgOther && !dlgOther.text.includes('去支付'),
    JSON.stringify(((dlgOther || {}).text || '').slice(0, 160)));
  await page.screenshot({ path: `${OUT}-②a同面额弹窗-别人占位.png` });

  await closeAlert(page);
  check('② 点确认后弹窗关闭', (await dialogInfo(page)) === null);

  // ── ②b 占位的是**自己** ⇒ 这时才补那句指引 ───────────────────
  const dlgSelf = await buyAndWaitDialog(page, 'self');
  check('②b 正文仍是后端下发的同一句', !!dlgSelf && dlgSelf.text.includes(BUSY_MSG),
    JSON.stringify(((dlgSelf || {}).text || '').slice(0, 120)));
  check('★★占位的是自己 ⇒ 补上「最近订单 + 去支付」指引（他确实有一笔能付的单）',
    !!dlgSelf && dlgSelf.text.includes('最近订单') && dlgSelf.text.includes('去支付'),
    JSON.stringify(((dlgSelf || {}).text || '').slice(0, 200)));
  check('②b 同样只有确认按钮', !!dlgSelf && dlgSelf.buttons.length === 1,
    JSON.stringify((dlgSelf || {}).buttons));
  await page.screenshot({ path: `${OUT}-②b同面额弹窗-自己占位.png` });
  await closeAlert(page);

  const after = await mockStat(page);

  // ── ③ 被拦 = 什么都没发生 ────────────────────────────────────
  check('③ 两次点击 = 两次下单请求（没有级联重试）', after.createCalls === 2,
    `createCalls=${after.createCalls}`);
  check('③ 列表条数不变（没有凭空多出一笔）',
    after.orders.length === before.orders.length,
    `${before.orders.length} -> ${after.orders.length}`);
  check('③ 没有误进出码分支', after.payCalls === 0, `payCalls=${after.payCalls}`);

  // ── ④ 「查看更多」→ 前端请求导航到 /account/orders ───────────
  await page.evaluate(`${PROBE_BTN('查看更多')}.click()`);
  await new Promise(r => setTimeout(r, 400));
  const nav = await page.evaluate(() => [...window.__nav]);
  check('④ 点「查看更多」发起了到 /account/orders 的导航',
    nav.includes('/account/orders'), JSON.stringify(nav));

  // ── ⑤ 全部订单页 ─────────────────────────────────────────────
  await page.evaluate(`${PROBE_BTN('看订单页')}.click()`);
  await page.waitForFunction(() =>
    (document.querySelector('#probe-root') || {}).textContent.includes('全部订单'),
    { timeout: 10000, polling: 50 });
  await new Promise(r => setTimeout(r, 500));

  let ot = await probeText(page);
  check('⑤ 标题「全部订单」', ot.includes('全部订单'));
  check('⑤ 显示总笔数 25 笔', ot.includes('25 笔'), JSON.stringify(ot.slice(0, 120)));
  check('⑤ 显示订单号（对账要用）', ot.includes('ord_probe00001'));
  check('⑤ 显示交易号', ot.includes('T20260003'), JSON.stringify(ot.slice(0, 260)));
  check('⑤ 显示通道', ot.includes('mpay'));
  check('⑤ 三态徽章齐全', ot.includes('已支付') && ot.includes('已过期') && ot.includes('待支付'));
  check('⑤ 待支付行有「去支付」', ot.includes('去支付'));
  check('⑤ 有「加载更多（还有 5 笔）」', ot.includes('还有 5 笔'),
    JSON.stringify(ot.slice(-120)));
  await page.screenshot({ path: `${OUT}-③全部订单页.png` });

  await page.evaluate(`${PROBE_BTN('加载更多（还有 5 笔）')}.click()`);
  await new Promise(r => setTimeout(r, 600));
  const ot2 = await probeText(page);
  check('⑤ 加载更多后 25 条齐了（按钮消失）', !ot2.includes('还有 5 笔'),
    JSON.stringify(ot2.slice(-120)));
  check('⑤ 最后一条（ord_probe00025）出现', ot2.includes('ord_probe00025'));
  await page.screenshot({ path: `${OUT}-④加载更多后.png` });

  check('⑥ 全程无原生 dialog', nativeDialogs === 0, `nativeDialogs=${nativeDialogs}`);

  await browser.close();

  const failed = results.filter(r => !r.ok);
  fs.writeFileSync(`${OUT}.json`, JSON.stringify({ VIEW, results }, null, 2));
  console.log(`\n=== 订单流取证：${results.length - failed.length} 通过 / ${failed.length} 失败 ===`);
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error('崩溃：', e); process.exit(2); });
