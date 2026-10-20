// 「使用文档开新标签页」+「举报与反馈弹窗」交互取证（组件级，离线）。
//
// 加载真组件（src/__probe_feedback.tsx 把 UserMenu / PointsCard 挂到 dev server 页面上），
// fetch 全量 mock —— 每条断言都是「真点击 → 真状态 → 真 DOM」。
//
// 要证的事：
//   ① 积分卡的「查看《产品使用文档》」：**新标签页**打开 /docs，当前页 URL 不变
//      （旧实现 `window.open(..., "noopener")` 恒返回 null ⇒ 每次都额外把当前页也跳走）
//   ② 用户菜单里「举报与反馈」在「使用文档」下方
//   ③ 弹窗字段齐全（6 类 chip / 描述必填 1000 上限 / 联系方式选填），未达字数时提交禁用
//   ④ 提交打的是 `POST /api/feedback`，带上 category/content/page_url/user_agent，成功后显示回执编号
//   ⑤ 后端 503 的原因**原样显示**给用户（不吞成"提交失败"）
//   ⑥ Esc 关闭；全程不出现原生 dialog
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-feedback.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-19-反馈与文档入口取证';
const VIEW = { width: 1280, height: 900 };

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};

const shot = async (page, name) => {
  const file = `${OUT}-${name}.png`;
  await page.screenshot({ path: file, fullPage: false });
  return file;
};

const btnByText = (t) => `[...document.querySelectorAll('#probe-root button')]
  .find(b => (b.textContent || '').trim() === ${JSON.stringify(t)})`;
const btnContains = (t) => `[...document.querySelectorAll('#probe-root button')]
  .find(b => (b.textContent || '').includes(${JSON.stringify(t)}))`;

const DIALOG_FIND = `(() => {
  for (const el of document.querySelectorAll('#probe-root div')) {
    const c = String(el.className || '');
    if (c.includes('inset-0') && c.includes('fixed')) return el;
  }
  return null;
})()`;

const dialogText = (page) => page.evaluate(`(() => { const d = ${DIALOG_FIND}; return d ? d.textContent : null; })()`);
const waitGone = (page) => page.waitForFunction(
  () => ![...document.querySelectorAll('#probe-root div')].some(el => {
    const c = String(el.className || '');
    return c.includes('inset-0') && c.includes('fixed');
  }), { timeout: 6000, polling: 50 });

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
  page.on('console', (m) => { if (m.type() === 'error' && !/404|not mocked/.test(m.text())) console.log('CONSOLE ERROR:', m.text()); });

  // ── mock：只拦 /api/*；并预置登录态（必须在页面脚本之前写入 localStorage）──
  await page.evaluateOnNewDocument(() => {
    try { localStorage.setItem('wb-auth-token', 'probe-token'); } catch {}

    const USER = {
      user_id: 'u_probe000001', username: 'probe_user', nickname: '测试用户',
      bio: '', points: 3228, disabled: false,
    };
    window.__fbCalls = [];
    window.__failNext = false;
    const json = (o, s = 200) =>
      new Response(JSON.stringify(o), { status: s, headers: { 'Content-Type': 'application/json' } });

    window.fetch = async (input, init) => {
      const raw = typeof input === 'string' ? input : (input && input.url) || '';
      const p = raw.split('?')[0];
      const method = ((init && init.method) || 'GET').toUpperCase();

      if (p === '/api/auth/me') return json({ user: USER });
      if (p === '/api/points/balance') return json({ points: USER.points });
      if (p.startsWith('/api/points/checkin')) {
        return json(method === 'POST'
          ? { added: 20, balance: USER.points + 20, checked_in_today: true, total_days: 2 }
          : { checked_in_today: false, total_days: 1 });
      }
      if (p === '/api/points/logs') return json({ total: 0, offset: 0, logs: [] });
      if (p === '/api/feedback' && method === 'POST') {
        let body = null;
        try { body = JSON.parse(init.body); } catch {}
        window.__fbCalls.push({ path: p, body });
        if (window.__failNext) {
          return json({ detail: '反馈通道暂时不可用（数据库未就绪），请稍后再试' }, 503);
        }
        return json({ ok: true, id: 'fb_probe000001', message: '反馈已收到，感谢你帮我们改进' });
      }
      return json({ detail: 'probe: not mocked ' + p }, 404);
    };
  });

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_feedback.tsx')` });
  await page.waitForSelector('#probe-root', { timeout: 15000 });
  // 等登录态恢复（UserMenu 的 initAuth 是异步的）
  await page.waitForFunction(
    () => (document.querySelector('#probe-root') || {}).innerText?.includes('测试用户'),
    { timeout: 10000, polling: 50 });
  await new Promise(r => setTimeout(r, 300));

  const report = { viewport: VIEW, steps: [] };

  // ── ① 积分卡文档入口：新标签页 + 当前页不动 ─────────────────────
  const beforeUrl = page.url();
  const targetReady = new Promise((resolve) => {
    const timer = setTimeout(() => resolve(null), 6000);
    browser.once('targetcreated', async (t) => {
      clearTimeout(timer);
      let p = null;
      try { p = await t.page(); } catch {}
      resolve({ target: t, page: p });
    });
  });
  await page.evaluate(`${btnContains('产品使用文档')}.click()`);
  const opened = await targetReady;
  report.steps.push({ step: '①docs', beforeUrl, openedUrl: opened && opened.page ? 'pending' : null });

  let docsUrl = '';
  if (opened && opened.page) {
    try {
      await opened.page.waitForFunction(() => location.pathname.startsWith('/docs'), { timeout: 8000, polling: 100 });
      docsUrl = opened.page.url();
    } catch {
      docsUrl = opened.page.url();
    }
  }
  await new Promise(r => setTimeout(r, 400));

  check('① 点《产品使用文档》开出了新标签页', !!opened, opened ? '' : '没有 targetcreated 事件');
  check('① 新标签页落在 /docs', /\/docs(\?|#|$)/.test(docsUrl), docsUrl || '（未取到 URL）');
  check('① 当前页 URL 没有跳走（仍在原页面）', page.url() === beforeUrl, `${beforeUrl} → ${page.url()}`);
  check('① 当前页仍是探针页（内容没被 /docs 顶掉）',
    (await page.evaluate(() => !!document.querySelector('#probe-root') && document.querySelector('#probe-root').innerText.includes('测试用户'))));
  report.steps.push({ step: '①docs-result', docsUrl, currentUrl: page.url() });
  await shot(page, '①文档入口点击后当前页不变');
  if (opened && opened.page) await opened.page.close().catch(() => {});

  // ── ② 用户菜单：举报与反馈在「使用文档」下方 ─────────────────────
  await page.evaluate(`${btnContains('测试用户')}.click()`);
  await page.waitForFunction(
    () => (document.querySelector('#probe-root') || {}).innerText?.includes('举报与反馈'),
    { timeout: 5000, polling: 50 });
  const menuItems = await page.evaluate(`(() => {
    const t = document.querySelector('#probe-root').innerText;
    const a = t.indexOf('使用文档'), b = t.indexOf('举报与反馈');
    return { hasDocs: a >= 0, hasFb: b >= 0, fbAfterDocs: b > a };
  })()`);
  check('② 菜单里有「使用文档」与「举报与反馈」', menuItems.hasDocs && menuItems.hasFb, JSON.stringify(menuItems));
  check('②「举报与反馈」排在「使用文档」下方', menuItems.fbAfterDocs, JSON.stringify(menuItems));
  await shot(page, '②用户菜单');

  // ── ③ 弹窗字段 ────────────────────────────────────────────────
  await page.evaluate(`${btnByText('举报与反馈')}.click()`);
  await page.waitForFunction(
    () => [...document.querySelectorAll('#probe-root div')].some(el => {
      const c = String(el.className || '');
      return c.includes('inset-0') && c.includes('fixed');
    }), { timeout: 5000, polling: 50 });

  let dt = await dialogText(page);
  check('③ 弹窗打开（模态）', !!dt && dt.includes('举报与反馈'));
  const chips = ['功能异常', '音色与音质', '积分与订单', '内容问题', '功能建议', '其他'];
  check('③ 6 个反馈类型 chip 齐全', chips.every(c => dt.includes(c)), dt ? dt.slice(0, 80) : '无弹窗');
  check('③ 有「详细描述」必填标记与字数计数', dt.includes('详细描述') && /\d+\/1000/.test(dt), (dt || '').slice(0, 120));
  check('③ 联系方式标为选填', dt.includes('联系方式') && dt.includes('选填'));
  check('③ 说明了会记录账号与页面地址', dt.includes('账号') && dt.includes('页面地址'));
  await shot(page, '③反馈弹窗');

  const submitDisabled0 = await page.evaluate(`(() => {
    const b = ${btnByText('提交反馈')}; return b ? b.disabled : null;
  })()`);
  check('③ 未填内容时「提交反馈」禁用', submitDisabled0 === true, String(submitDisabled0));

  // ── ④ 填太短 → 仍禁用；填够 → 提交成功 ─────────────────────────
  await page.type('#probe-root textarea', '点了');
  await new Promise(r => setTimeout(r, 150));
  const submitDisabled1 = await page.evaluate(`(() => {
    const b = ${btnByText('提交反馈')}; return b ? b.disabled : null;
  })()`);
  check('④ 少于 5 个字仍禁用（前端先挡一次）', submitDisabled1 === true, String(submitDisabled1));

  // 切到「音色与音质」再补足内容
  await page.evaluate(`${btnByText('音色与音质')}.click()`);
  await page.type('#probe-root textarea', '段音色发闷，像隔着被子');
  await page.type('#probe-root input', 'me@example.com');
  await new Promise(r => setTimeout(r, 150));
  const submitDisabled2 = await page.evaluate(`(() => {
    const b = ${btnByText('提交反馈')}; return b ? b.disabled : null;
  })()`);
  check('④ 内容够了就可以提交', submitDisabled2 === false, String(submitDisabled2));

  await page.evaluate(`${btnByText('提交反馈')}.click()`);
  await page.waitForFunction(
    () => (document.querySelector('#probe-root').innerText || '').includes('已收到你的反馈'),
    { timeout: 6000, polling: 50 });

  const calls = await page.evaluate(() => window.__fbCalls);
  check('④ 只打了一次 POST /api/feedback', calls.length === 1, JSON.stringify(calls.map(c => c.path)));
  const sent = calls[0] ? calls[0].body : {};
  check('④ 提交的类型是 chip 选中的 key（voice）', sent.category === 'voice', JSON.stringify(sent.category));
  check('④ 描述取用户输入（已 strip）', (sent.content || '').includes('音色发闷'), JSON.stringify(sent.content));
  check('④ 联系方式一并提交', sent.contact === 'me@example.com', JSON.stringify(sent.contact));
  check('④ 自动带上当前页面地址', typeof sent.page_url === 'string' && sent.page_url.startsWith('http'), String(sent.page_url));
  check('④ 自动带上 UA', typeof sent.user_agent === 'string' && sent.user_agent.length > 10, String(sent.user_agent));
  dt = await dialogText(page);
  check('④ 成功态显示回执编号（用户能确认发出去了）', !!dt && dt.includes('fb_probe000001'), (dt || '').slice(0, 100));
  await shot(page, '④提交成功');
  await page.evaluate(`${btnByText('关闭')}.click()`);
  await waitGone(page);

  // ── ⑤ 失败：后端原因原样显示 ───────────────────────────────────
  await page.evaluate(() => { window.__failNext = true; });
  await page.evaluate(`${btnContains('测试用户')}.click()`);
  await page.waitForFunction(
    () => (document.querySelector('#probe-root') || {}).innerText?.includes('举报与反馈'),
    { timeout: 5000, polling: 50 });
  await page.evaluate(`${btnByText('举报与反馈')}.click()`);
  await page.waitForFunction(
    () => [...document.querySelectorAll('#probe-root div')].some(el => {
      const c = String(el.className || '');
      return c.includes('inset-0') && c.includes('fixed');
    }), { timeout: 5000, polling: 50 });
  await page.type('#probe-root textarea', '再试一次：音色还是发闷');
  await page.evaluate(`${btnByText('提交反馈')}.click()`);
  await page.waitForFunction(
    () => (document.querySelector('#probe-root').innerText || '').includes('数据库未就绪'),
    { timeout: 6000, polling: 50 });
  dt = await dialogText(page);
  check('⑤ 503 的原因原样显示（不吞成"提交失败"）', !!dt && dt.includes('数据库未就绪'), (dt || '').slice(-90));
  check('⑤ 失败后仍停在表单（可重试，不回退到成功态）', !!dt && dt.includes('提交反馈') && !dt.includes('已收到你的反馈'));
  await shot(page, '⑤提交失败原因');

  // ── ⑥ Esc 关闭 ─────────────────────────────────────────────────
  await page.keyboard.press('Escape');
  await waitGone(page).then(() => check('⑥ Esc 可关闭弹窗', true)).catch(() => check('⑥ Esc 可关闭弹窗', false, '弹窗仍在'));
  check('⑥ 全程未出现原生 dialog', nativeDialogs === 0, String(nativeDialogs));

  await browser.close();

  const failed = results.filter(r => !r.ok);
  fs.writeFileSync(`${OUT}.json`,
    JSON.stringify({ ...report, results, failed: failed.length }, null, 2));
  console.log(`\n${results.length - failed.length}/${results.length} 通过 → ${OUT}.json`);
  process.exit(failed.length ? 1 : 0);
})().catch((e) => { console.error('FATAL', e); process.exit(1); });
