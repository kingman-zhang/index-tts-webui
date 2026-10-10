// 「书稿工作台」取证：真实 DubbingPage（单人配音页）。
//
// 加载真实页面（src/__probe_book_flow.tsx），走真导入弹窗（真 file input、真分章确认），
// /api/mono/extract 喂一份 3 章的"书"，/api/queue/submit 记账，其余 /api/* 一律 404。
//
// 要证的事（= 本轮 G2+G3 的验收标准）：
//   ① 普通单篇时左栏没有「章节目录」
//   ② 导入 → 先出分段确认（不自动生成）
//   ③ 确认「导入 N 章到书稿」→ 左栏出现章节目录，画布 = 第 1 章
//   ④ 点第 2 章 → 画布换文本（画布只装当前章）
//   ⑤ 编辑当前章 → 切走再切回 → 改动仍在（编辑写回当前章，不串章）
//   ⑥ 勾选 3 章 → 批量生成 → 恰好 3 个队列任务，任务名 = 章标题（各扣各的）
//   ⑦ 全程原生 dialog 出现 0 次
//
// 用法（dev server 需已在 :6008 运行）：
//   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-book-flow.js [输出前缀]
const puppeteer = require('puppeteer-core');
const fs = require('fs');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const OUT = process.argv[2] || 'outputs/2026-10-10-书稿工作台';
const VIEW = { width: 1560, height: 940 };

const results = [];
const check = (label, ok, extra = '') => {
  results.push({ label, ok, extra });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
};
const wait = (ms) => new Promise(r => setTimeout(r, ms));

/** 画布文本：MonoEditor 的 contentEditable 内文 */
const canvasText = (page) => page.evaluate(() => {
  const el = document.querySelector('#probe-root [contenteditable="true"]');
  return el ? (el.innerText || el.textContent || '') : '';
});

/** 章节目录卡片（含「章节目录」的圆角卡片） */
const bookCardText = (page) => page.evaluate(() => {
  const card = [...document.querySelectorAll('#probe-root div')]
    .find(el => String(el.className || '').includes('rounded-xl') && (el.textContent || '').includes('章节目录'));
  return card ? (card.textContent || '') : '';
});

/** 章节目录里的章节标题（按钮 title =「章名（N 字）」） */
const chapterTitles = (page) => page.evaluate(() => {
  const card = [...document.querySelectorAll('#probe-root div')]
    .find(el => String(el.className || '').includes('rounded-xl') && (el.textContent || '').includes('章节目录'));
  if (!card) return [];
  return [...card.querySelectorAll('button[title]')]
    .map(b => b.getAttribute('title') || '')
    .filter(t => t.includes('字）'))
    .map(t => t.replace(/（.*$/, ''));
});

/** 点章节标题（切章） */
const clickChapter = (page, name) => page.evaluate((n) => {
  const card = [...document.querySelectorAll('#probe-root div')]
    .find(el => String(el.className || '').includes('rounded-xl') && (el.textContent || '').includes('章节目录'));
  if (!card) return false;
  const b = [...card.querySelectorAll('button[title]')]
    .find(x => (x.getAttribute('title') || '').startsWith(n));
  if (b) b.click();
  return !!b;
}, name);

const clickButton = (page, pred) => page.evaluate((p) => {
  const fn = new Function('t', `return ${p}`);
  const b = [...document.querySelectorAll('#probe-root button')].find(x => fn((x.textContent || '').trim()));
  if (b) b.click();
  return !!b;
}, pred);

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
  page.on('console', (m) => { if (m.type() === 'error') console.log('PAGE CONSOLE:', m.text()); });

  // ── mock：/api/mono/extract 喂一份 3 章的"书"；/api/queue/submit 记账；其余 404 ──
  await page.evaluateOnNewDocument(() => {
    const LINES = [
      '第一章 起点',
      '少年背起行囊出发了。',
      '他走过山与河。',
      '',
      '第二章 承',
      '三年后他回到了故乡。',
      '一切都变了。',
      '',
      '第三章 转',
      '他遇见了旧日友人。',
    ];
    const BOOK_TEXT = LINES.join('\n');
    const CHAPTERS = [
      { index: 0, title: '第一章 起点', start_line: 0, end_line: 3, chars: 0 },
      { index: 1, title: '第二章 承', start_line: 4, end_line: 7, chars: 0 },
      { index: 2, title: '第三章 转', start_line: 8, end_line: 10, chars: 0 },
    ];
    window.__submits = [];
    const orig = window.fetch.bind(window);
    const json = (o, status = 200) =>
      new Response(JSON.stringify(o), { status, headers: { 'Content-Type': 'application/json' } });
    window.fetch = async (input, init) => {
      const url = typeof input === 'string' ? input : (input && input.url) || '';
      if (url.includes('/api/mono/extract')) {
        return json({
          text: BOOK_TEXT,
          chars: BOOK_TEXT.replace(/\s/g, '').length,
          chapters: CHAPTERS,
          chapter_max_chars: 10000,
        });
      }
      if (url.includes('/api/queue/submit')) {
        let body = null;
        try { body = JSON.parse(init.body); } catch { /* 忽略 */ }
        window.__submits.push(body);
        return json({ queue_position: window.__submits.length, task_id: 't' + window.__submits.length });
      }
      if (url.includes('/api/')) return json({ detail: 'probe: not mocked' }, 404);
      return orig(input, init);
    };
  });

  await page.goto('http://localhost:6008/', { waitUntil: 'networkidle2' });
  await page.addScriptTag({ type: 'module', content: `import('/src/__probe_book_flow.tsx')` });
  await page.waitForFunction(
    () => [...document.querySelectorAll('#probe-root button')].some(b => (b.textContent || '').trim() === '导入'),
    { timeout: 15000 });
  await wait(500);

  const report = { viewport: VIEW, chapters: [], submits: [] };

  // ── ① 初始：普通单篇，无章节目录 ─────────────────────────────
  check('① 初始无「章节目录」（普通单篇）', !(await bookCardText(page)), '');
  await page.screenshot({ path: `${OUT}-①导入前.png` });

  // ── ② 导入 → 分段确认（不自动生成） ──────────────────────────
  check('② 点工具栏「导入」', await clickButton(page, "t === '导入'"));
  await page.waitForFunction(() => document.body.textContent.includes('上传文件'), { timeout: 5000, polling: 50 });

  const tmp = '/tmp/probe-book.txt';
  fs.writeFileSync(tmp, '（内容由 mock 提供，此处仅用于触发真实 file input）');
  // 注意：页面里有两个 file input（左栏音色卡的上传音频 + 画布工具栏的导入文档），
  // 必须按 accept 精确选中**导入文档**那个，否则会触发音色命名弹窗。
  const input = await page.$('#probe-root input[type=file][accept*=".docx"]');
  check('② 找到真实的导入文档 input', !!input);
  await input.uploadFile(tmp);

  await page.waitForFunction(() => document.body.textContent.includes('确认分段'), { timeout: 8000, polling: 50 });
  const panelText = await page.evaluate(() => {
    const d = [...document.querySelectorAll('#probe-root div')].find(el => (el.textContent || '').includes('确认分段'));
    return d ? (d.textContent || '') : '';
  });
  check('② 分段确认面板显示 3 章', panelText.includes('3 章'), panelText.slice(0, 60));
  check('② 确认前不产生任何合成任务',
    (await page.evaluate(() => (window.__submits || []).length)) === 0);
  await page.screenshot({ path: `${OUT}-②分段确认.png` });

  // ── ③ 确认 → 落成书稿 ────────────────────────────────────────
  check('③ 点「导入 3 章到书稿」', await clickButton(page, "t.includes('章到书稿')"));
  await page.waitForFunction(() => document.body.textContent.includes('章节目录'), { timeout: 6000, polling: 50 });
  await wait(400);

  const cardText = await bookCardText(page);
  const titles = await chapterTitles(page);
  report.chapters = titles;
  check('③ 左栏出现章节目录 · 3 章', cardText.includes('3 章') && titles.length === 3, titles.join('|'));
  check('③ 章标题保留正确', titles.join('|') === '第一章 起点|第二章 承|第三章 转', titles.join('|'));
  const canvas1 = await canvasText(page);
  check('③ 画布 = 第 1 章正文',
    canvas1.includes('少年背起行囊出发了') && !canvas1.includes('三年后他回到了故乡'),
    `首行「${(canvas1.split('\n')[0] || '').slice(0, 14)}」`);
  await page.screenshot({ path: `${OUT}-③书稿目录.png` });

  // ── ④ 切章：画布只装当前章 ──────────────────────────────────
  check('④ 点第 2 章', await clickChapter(page, '第二章 承'));
  await wait(400);
  const canvas2 = await canvasText(page);
  check('④ 画布换成第 2 章正文（不再含第 1 章内容）',
    canvas2.includes('三年后他回到了故乡') && !canvas2.includes('少年背起行囊'),
    `首行「${(canvas2.split('\n')[0] || '').slice(0, 14)}」`);
  await page.screenshot({ path: `${OUT}-④切到第2章.png` });

  // ── ⑤ 编辑写回当前章（切走再切回，改动仍在） ─────────────────
  const marker = '【探针】切章后追加的一段。';
  await page.evaluate((m) => {
    const el = document.querySelector('#probe-root [contenteditable="true"]');
    if (!el) return;
    el.focus();
    el.innerHTML = el.innerHTML + '<div>' + m + '</div>';
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }, marker);
  await wait(400);
  await clickChapter(page, '第一章 起点');
  await wait(300);
  await clickChapter(page, '第二章 承');
  await wait(400);
  const canvas3 = await canvasText(page);
  check('⑤ 编辑写回当前章：切走再切回，追加内容仍在', canvas3.includes(marker),
    canvas3.includes(marker) ? '' : `切回后「${canvas3.slice(0, 40)}」`);
  check('⑤ 未串章：第 1 章不含该标记', await (async () => {
    await clickChapter(page, '第一章 起点');
    await wait(300);
    const c = await canvasText(page);
    await clickChapter(page, '第二章 承');
    await wait(300);
    return !c.includes(marker);
  })());

  // ── ⑥ 批量生成：3 章 = 3 个任务 ──────────────────────────────
  const before = await page.evaluate(() => (window.__submits || []).length);
  check('⑥ 找到「生成选中的 N 章」按钮', await clickButton(page, "t.includes('生成选中的')"));
  await page.waitForFunction((n) => (window.__submits || []).length > n, { timeout: 8000, polling: 50 }, before);
  await wait(600);
  const subs = await page.evaluate(() => window.__submits || []);
  report.submits = subs.map(s => ({ project_name: s && s.project_name, lines: s && s.lines && s.lines.length }));
  check('⑥ 恰好 3 个队列任务', subs.length === 3, `实际 ${subs.length}`);
  check('⑥ 任务名 = 章标题（依次）',
    subs.map(s => s.project_name).join('|') === '第一章 起点|第二章 承|第三章 转',
    subs.map(s => s.project_name).join('|'));
  check('⑥ 每个任务都带正文（各扣各的）',
    subs.every(s => Array.isArray(s.lines) && s.lines.length > 0 && s.kind === 'mono'),
    JSON.stringify(subs.map(s => s && s.lines && s.lines.length)));
  await page.screenshot({ path: `${OUT}-⑤批量提交后.png` });

  // ── ⑦ 无原生弹窗 ────────────────────────────────────────────
  check('⑦ 全程原生 dialog 出现 0 次', nativeDialogs === 0, `${nativeDialogs} 次`);

  fs.writeFileSync(`${OUT}.json`, JSON.stringify(report, null, 2));
  console.log('\n摘要：' + results.filter(r => r.ok).length + '/' + results.length + ' 通过');
  console.log('原生 dialog：' + nativeDialogs + ' 次');

  await browser.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(async (e) => {
  console.error('SCRIPT ERROR:', e && e.message);
  process.exit(2);
});
