// 冒烟：确认 puppeteer + chromium 能用，并截登录页
const puppeteer = require('puppeteer-core');
const path = require('path');

const CHROME = '/Users/zhangjianwen/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const BASE = 'http://127.0.0.1:6009';

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: true,
    args: ['--no-sandbox', '--font-render-hinting=none', '--force-color-profile=srgb'],
    defaultViewport: { width: 1600, height: 1000, deviceScaleFactor: 2 },
  });
  const page = await browser.newPage();
  await page.goto(`${BASE}/account`, { waitUntil: 'networkidle2' });
  await new Promise(r => setTimeout(r, 1200));
  const out = '/tmp/smoke-login.png';
  await page.screenshot({ path: out });
  console.log('saved', out);
  const box = await page.evaluate(() => {
    const el = document.querySelector('main');
    return el ? { w: el.clientWidth, h: el.clientHeight, html: el.innerHTML.slice(0, 200) } : null;
  });
  console.log(JSON.stringify(box));
  await browser.close();
})().catch(e => { console.error('FAIL', e); process.exit(1); });
