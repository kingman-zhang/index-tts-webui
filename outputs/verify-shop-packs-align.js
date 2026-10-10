// 「积分商城 · 四个套餐卡片购买按钮对齐」取证。
//
// 背景：体验包（bonus_points=0）没有「含赠送 X 积分」那一行，而其它三档有 —— 卡片被
// grid 拉成等高后，无赠送那档的按钮会**贴着文字往上跑**，与另外三个不在同一水平线。
// 修法是给按钮加 mt-auto（把它压到卡片底边），而不是塞一个隐形占位行。
//
// 本脚本加载真实组件（src/__probe_shop_packs.tsx 挂真 ShopCard），只 mock /api，
// 因此量到的是**当前 CSS 下真渲染出来的几何**，不是照抄的 HTML。
//
// 用法（dev server 已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-shop-packs-align.js [输出前缀]
// 前缀建议带标记（如 -修复前 / -修复后），便于两次运行做对比。
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-09-积分商城按钮对齐';
const VIEW = { width: 1280, height: 800 };

// 四档套餐，字段与后端 packs.py 一致（体验包无赠送 ⇒ 少一行文案）
const PACKS_PAYLOAD = {
  packs: [
    { id: 'starter', name: '体验包', price_fen: 1000, price_yuan: '10', points: 1000,
      bonus_points: 0, bonus_percent: 0, est_chars: 20000, order: 1 },
    { id: 'standard', name: '标准包', price_fen: 3000, price_yuan: '30', points: 3200,
      bonus_points: 200, bonus_percent: 6.7, est_chars: 64000, order: 2 },
    { id: 'value', name: '超值包', price_fen: 5000, price_yuan: '50', points: 5750,
      bonus_points: 750, bonus_percent: 15, est_chars: 115000, order: 3 },
    { id: 'premium', name: '尊享包', price_fen: 10000, price_yuan: '100', points: 12500,
      bonus_points: 2500, bonus_percent: 25, est_chars: 250000, order: 4 },
  ],
  currency: 'CNY', points_per_yuan: 100, points_per_1000_chars: 50,
  mock_pay_enabled: true, pay_channel_ready: false,
};

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

/** 量四个套餐卡片：卡片盒、按钮盒、是否有「含赠送」行、按钮底边距卡片底边的间距 */
const measure = (page) => page.evaluate(() => {
  const cards = [...document.querySelectorAll('#probe-root .grid > div')];
  return {
    viewportW: window.innerWidth,
    cardCount: cards.length,
    cards: cards.map((card) => {
      const name = (card.querySelector('p') || {}).textContent || '';
      const btn = card.querySelector('button');
      const cr = card.getBoundingClientRect();
      const br = btn ? btn.getBoundingClientRect() : null;
      const hasBonusLine = [...card.querySelectorAll('p')]
        .some(p => (p.textContent || '').includes('含赠送'));
      const cs = getComputedStyle(card);
      return {
        name: name.trim(),
        hasBonusLine,
        cardTop: Math.round(cr.top), cardBottom: Math.round(cr.bottom),
        cardHeight: Math.round(cr.height),
        btnTop: br ? Math.round(br.top) : null,
        btnBottom: br ? Math.round(br.bottom) : null,
        btnHeight: br ? Math.round(br.height) : null,
        // 按钮底边 → 卡片底边（含卡片 padding-bottom）的距离，越小说明按钮越贴底
        gapToCardBottom: br ? Math.round(cr.bottom - br.bottom) : null,
        btnMarginTop: btn ? getComputedStyle(btn).marginTop : null,
        padBottom: parseFloat(cs.paddingBottom),
      };
    }),
  };
});

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: 'new',
    args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.setViewport({ ...VIEW, deviceScaleFactor: 2 });

  // 只拦 /api/points/*；其余 /api/* 返 404（不返 401，避免触发清登录态 reload）
  await page.evaluateOnNewDocument((payload) => {
    const orig = window.fetch.bind(window);
    const json = (o, status = 200) =>
      new Response(JSON.stringify(o), { status, headers: { 'Content-Type': 'application/json' } });
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (url.includes('/api/points/packs')) return json(payload);
      if (url.includes('/api/points/orders')) return json({ total: 0, offset: 0, orders: [] });
      if (url.includes('/api/')) return json({ detail: 'probe: not mocked' }, 404);
      return orig(input, init);
    };
  }, PACKS_PAYLOAD);

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_shop_packs.tsx')` });
  await page.waitForSelector('#probe-root .grid button', { timeout: 15000 });
  await new Promise(r => setTimeout(r, 500)); // 等字体/布局稳定

  const m = await measure(page);

  check('① 渲染出 4 个套餐卡片、每张 1 个购买按钮',
    m.cardCount === 4 && m.cards.every(c => c.btnTop !== null),
    `卡片 ${m.cardCount} 张`);

  check('② 体验包确实没有「含赠送」行，其余三档有（这是错位的成因）',
    m.cards[0] && m.cards[0].hasBonusLine === false &&
    m.cards.slice(1).every(c => c.hasBonusLine === true),
    JSON.stringify(m.cards.map(c => `${c.name}:${c.hasBonusLine}`)));

  const tops = m.cards.map(c => c.btnTop);
  const bottoms = m.cards.map(c => c.btnBottom);
  const gaps = m.cards.map(c => c.gapToCardBottom);
  const spread = Math.max(...tops) - Math.min(...tops);
  const gapSpread = Math.max(...gaps) - Math.min(...gaps);

  check('③ 四个购买按钮顶边对齐（同一水平线）',
    spread === 0, `按钮 top=${JSON.stringify(tops)}，极差 ${spread}px`);
  check('④ 四个购买按钮底边对齐',
    Math.max(...bottoms) - Math.min(...bottoms) === 0,
    `按钮 bottom=${JSON.stringify(bottoms)}`);
  check('⑤ 按钮底边到卡片底边的间距一致（都贴底，不靠一行占位撑着）',
    gapSpread === 0, `间距=${JSON.stringify(gaps)}，极差 ${gapSpread}px`);

  const host = await page.$('#probe-root');
  await host.screenshot({ path: `${OUT}.png` });

  const report = { viewport: VIEW, ...m, spread: { btnTop: spread, gapToCardBottom: gapSpread } };
  fs.writeFileSync(`${OUT}.json`, JSON.stringify(report, null, 2));

  console.log('\n── 实测几何 ──');
  console.log(`viewport ${m.viewportW}px`);
  for (const c of m.cards) {
    console.log(
      `${c.name.padEnd(4, '　')} 卡片 ${c.cardTop}→${c.cardBottom} (h${c.cardHeight})  ` +
      `按钮 top=${c.btnTop} bottom=${c.btnBottom} h=${c.btnHeight}  ` +
      `距卡底 ${c.gapToCardBottom}px  margin-top=${c.btnMarginTop}  ` +
      `含赠送行=${c.hasBonusLine ? '有' : '无'}`
    );
  }
  console.log('\n摘要：' + results.filter(r => r.ok).length + '/' + results.length + ' 通过');

  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(async (e) => {
  console.error('SCRIPT ERROR:', e && e.message);
  process.exit(2);
});
