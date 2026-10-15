// 把运行中的 /docs 页导出成「单个 HTML 文件」，双击即可离线查看。
//
// 为什么不用整页截图：12 个章节的文档页纵向极长，2x 全页 PNG 会到几十 MB、
// 且不能选中文字、不能搜索。单文件 HTML 保留真实 DOM/样式/字体，可滚动可检索。
//
// 做三件事：
//   ① 抓渲染后的 document.body.innerHTML（真页面、真路由、真组件）
//   ② 把 vite dev 注入的所有 <style> 合并进 <head>
//   ③ 把 docs-assets/*.webp 全部转成 base64 data URL 内联 ⇒ 单文件自包含
//
// 用法：NODE_PATH=<node 沙箱>/node_modules <node> outputs/snapshot-docs.js [输出路径]
const puppeteer = require('puppeteer-core');

const CHROME = process.env.HOME +
  '/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const BASE = 'http://127.0.0.1:6009';
const OUT = process.argv[2] ||
  'outputs/2026-10-16-产品文档页-离线快照.html';

const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: true,
    args: ['--no-sandbox', '--font-render-hinting=none', '--hide-scrollbars'],
    defaultViewport: { width: 1440, height: 1000, deviceScaleFactor: 1 },
  });
  const page = await browser.newPage();
  await page.goto(BASE + '/docs', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('h1', { timeout: 20000 });
  await sleep(1200);

  const snap = await page.evaluate(async () => {
    // ① + ③：在页面里就地替换 img 的 src（有缓存，读 blob 很快）
    const imgs = Array.from(document.querySelectorAll('img'));
    let inlined = 0, failed = [];
    for (const im of imgs) {
      const src = im.getAttribute('src') || '';
      if (!src.startsWith('/docs-assets/')) continue;
      try {
        const r = await fetch(src);
        if (!r.ok) { failed.push(src + ' -> ' + r.status); continue; }
        const b = await r.blob();
        const dataUrl = await new Promise((res, rej) => {
          const fr = new FileReader();
          fr.onload = () => res(fr.result);
          fr.onerror = rej;
          fr.readAsDataURL(b);
        });
        im.setAttribute('src', dataUrl);
        im.removeAttribute('loading');
        inlined++;
      } catch (e) {
        failed.push(src + ' -> ' + String(e));
      }
    }
    // ②：合并 vite dev 注入的样式
    const css = Array.from(document.querySelectorAll('style'))
      .map(s => s.textContent || '').join('\n');
    return {
      body: document.body.innerHTML,
      css,
      title: document.title,
      inlined, failed,
      totalImgs: imgs.length,
    };
  });

  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${snap.title || '产品使用文档'}（离线快照）</title>
<style>${snap.css}</style>
<style>
  /* 快照外壳：与线上页面的差异只有顶部这条提示条 */
  body { margin: 0; }
  #snapshot-banner {
    position: sticky; top: 0; z-index: 9999;
    padding: 6px 14px; font: 500 12px/1.6 -apple-system, "PingFang SC", sans-serif;
    color: #78350f; background: #fef3c7; border-bottom: 1px solid #fde68a;
  }
  #snapshot-zoom {
    display: none; position: fixed; inset: 0; z-index: 9998;
    background: rgba(0,0,0,.82); padding: 24px; cursor: zoom-out;
    align-items: center; justify-content: center;
  }
  #snapshot-zoom.on { display: flex; }
  #snapshot-zoom img { max-width: 100%; max-height: 100%; border-radius: 8px; box-shadow: 0 20px 60px rgba(0,0,0,.5); }
  main img { cursor: zoom-in; }
</style>
</head>
<body>
<div id="snapshot-banner">离线快照 · 由 <code>/docs</code> 实际渲染结果导出，样式与配图已内联；正式版请在产品内点「使用文档」打开。</div>
${snap.body}
<div id="snapshot-zoom"><img alt=""></div>
<script>
// React 的交互在快照里已经失效，这里用原生事件把两个关键交互补回来：
//   ① 左侧目录点击 → 滚动到同名 h2（章节 id 由 DocsPage 写在 h2 上）
//   ② 正文点图 → 全屏放大，ESC / 点击任意处关闭
(function () {
  var headings = Array.prototype.slice.call(document.querySelectorAll('main h2[id]'));
  var norm = function (s) { return (s || '').replace(/[\s\u00a0]+/g, ''); };
  // 目录按钮的文字可能带序号前缀（"01 开始之前"），章节标题不带 ⇒ 双向包含匹配
  var find = function (t) {
    var n = norm(t);
    if (!n) return null;
    var hit = headings.filter(function (h) { return norm(h.textContent) === n; })[0] ||
              headings.filter(function (h) { return n.indexOf(norm(h.textContent)) >= 0; })[0] ||
              headings.filter(function (h) { return norm(h.textContent).indexOf(n) >= 0; })[0];
    return hit || null;
  };
  Array.prototype.forEach.call(document.querySelectorAll('aside nav button'), function (btn) {
    btn.addEventListener('click', function () {
      var target = find(btn.textContent) || headings.filter(function (h) {
        return norm(btn.textContent).slice(0, 6) && norm(h.textContent).slice(0, 6) === norm(btn.textContent).slice(0, 6);
      })[0];
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  });
  var zoom = document.getElementById('snapshot-zoom');
  var big = zoom.querySelector('img');
  document.addEventListener('click', function (e) {
    var im = e.target.closest && e.target.closest('main img');
    if (im) { big.src = im.src; zoom.classList.add('on'); }
  });
  zoom.addEventListener('click', function () { zoom.classList.remove('on'); });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') zoom.classList.remove('on');
  });
})();
</script>
</body>
</html>
`;

  require('fs').writeFileSync(OUT, html, 'utf8');
  console.log('截图内联：', snap.inlined, '/', snap.totalImgs);
  if (snap.failed.length) console.log('内联失败：', snap.failed);
  console.log('输出：', OUT, (html.length / 1024 / 1024).toFixed(2) + ' MB');
  await browser.close();
})().catch(e => { console.error('失败：', e); process.exit(1); });
