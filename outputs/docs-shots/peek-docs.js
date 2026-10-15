// 快速看一眼 /docs 页渲染（开发期自检用）
const puppeteer = require('puppeteer-core');
const CHROME = '/Users/zhangjianwen/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME, headless: true,
    args: ['--no-sandbox', '--font-render-hinting=none'],
    defaultViewport: { width: 1600, height: 1000, deviceScaleFactor: 1 },
  });
  const page = await browser.newPage();
  const errs = [];
  page.on('console', m => { if (m.type() === 'error') errs.push(m.text().slice(0, 200)); });
  page.on('pageerror', e => errs.push('PAGEERROR ' + String(e).slice(0, 300)));
  await page.goto('http://127.0.0.1:6009/docs', { waitUntil: 'networkidle2' });
  await new Promise(r => setTimeout(r, 1500));
  const info = await page.evaluate(() => ({
    h1: document.querySelector('h1')?.textContent,
    sections: [...document.querySelectorAll('h2')].map(h => h.textContent?.slice(0, 20)),
    imgs: [...document.querySelectorAll('img')].length,
    toc: [...document.querySelectorAll('aside button')].length,
    len: document.body.innerText.length,
    overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  console.log(JSON.stringify(info, null, 1));
  if (errs.length) console.log('ERRORS:', errs.slice(0, 5));
  await page.screenshot({ path: '/tmp/docs-top.png' });
  await browser.close();
})().catch(e => { console.error('FAIL', e); process.exit(1); });
