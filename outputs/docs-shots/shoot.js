/**
 * 产品文档截图脚本（真机操作取证）。
 *
 * 环境（**与线上隔离**，见脚本结尾「环境说明」注释）：
 *   - 沙箱后端  : 127.0.0.1:3002  DATA_DIR=outputs/docs-sandbox/data，四个 store 全 file
 *   - 沙箱前端  : 127.0.0.1:6009  vite --config vite.config.docs.ts（/api → 3002）
 *   - 演示账号  : docs.demo@lizigege.com / 小柯（u_a5afc66e2d67）
 *
 * 用法：
 *   NODE_PATH=<node 沙箱>/node_modules <node> shoot.js [场景名 ...]
 *   不给场景名 = 跑全部；场景名见 SCENES 的 key。
 *
 * 产物：webui-frontend/public/docs/<name>.png（2x Retina）
 */
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const CHROME = '/Users/zhangjianwen/Library/Caches/ms-playwright/chromium-1161/chrome-mac/Chromium.app/Contents/MacOS/Chromium';
const BASE = 'http://127.0.0.1:6009';
const API = 'http://127.0.0.1:3002';
const SANDBOX = '/Users/zhangjianwen/Documents/Kingman/workbuddy/index-tts/outputs/docs-sandbox';
const OUT = '/Users/zhangjianwen/Documents/Kingman/workbuddy/index-tts/podcast-webui/webui-frontend/public/docs-assets';
const TOKEN = 'Fv8y6-kRXgR9l-rfpC_1RBAUyTHXng-v-3mHoBycMpw';

const V_PRESET_W = `${SANDBOX}/data/preset-voices/女-温暖闺蜜.mp3`;
const V_PRESET_M = `${SANDBOX}/data/preset-voices/男-不羁青年.mp3`;
const V_MINE = `${SANDBOX}/data/voices/u_a5afc66e2d67/voc_dcf77a73d20c.mp3`;
// 「不存在的参考音频」：**刻意用相对路径**，别用 SANDBOX 绝对路径 ——
// 绝对路径会把本机 home 目录（/Users/<me>/...）印进对外可见的报错截图里，
// 而相对路径的报错是 `data/voices/<user_id>/<voice_id>.wav`，像产品内部路径。
const V_MISSING = 'data/voices/u_a5afc66e2d67/voc_notexist.wav';

// ─── 演示内容 ──────────────────────────────────────────────
const MONO_TEXT = [
  '【平静】大家好，欢迎来到「小柯读书」，我是小柯。',
  '【喜悦】今天想和你聊聊一本书，叫《认知觉醒》。[pause:0.5]',
  '【平静】很多人以为，只要足够努力，就一定能变好。',
  '【平静】但作者周岭告诉我们，大多数人的努力，只是低水平的重复。',
  '【惊喜】真正拉开差距的，是认知。【/】你看到的世界，决定了你能走多远。',
  '【平静】书里有三个概念让我印象最深：大脑的三重结构、舒适区边缘，还有元认知。[pause:0.8]',
  '【平静】简单说，就是别在舒适区里重复，也别一步跨进困难区，要站在「有点难但够得着」的地方练习。',
  '【喜悦】如果你也常常焦虑、拖延、想改变却没方向，那这本书值得你认真读一遍。[pause:0.5]',
  '【喜悦】好了，今天先聊到这里。喜欢的话，记得点个关注，我们下期再见。',
].join('\n');

// 队列用长文：让任务跑得久一点，才截得到「合成中」的进度条
const MONO_LONG = [
  '【平静】第一章的内容先讲到这里。下面我们把书里的三个概念，一个一个说清楚。',
  '【平静】第一个概念，是大脑的三重结构：本能脑、情绪脑和理智脑。它们分别诞生于不同的年代，力量对比也很悬殊。',
  '【喜悦】换句话说，你并不是懒，你只是被一个更古老的自己在拉着走。[pause:0.6]',
  '【平静】第二个概念，是舒适区边缘。既不要在舒适区里重复，也不要一步跨进恐慌区。',
  '【平静】第三个概念，是元认知。简单说，就是"看见自己在想什么"的能力。[pause:0.5]',
  '【平静】这三个概念合起来，就是这本书最想告诉你的事情：成长不是靠硬撑，而是靠方法。',
  '【喜悦】好了，今天的分享就到这里。如果对你有帮助，欢迎点个关注，我们下期接着聊。',
].join('\n');

const BOOK_CHAPTERS = [
  {
    id: 'ch_demo_01', title: '第一章 大脑的三重结构',
    text: [
      '【平静】要理解自己为什么总是拖延，得先认识自己的大脑。',
      '【平静】我们的大脑并不是一个整体，而是三层结构叠加出来的：本能脑、情绪脑和理智脑。',
      '【喜悦】本能脑负责生存，情绪脑负责感受，理智脑负责思考。[pause:0.5]',
      '【平静】问题在于，理智脑太年轻了。它诞生得最晚，力量却最弱，所以它常常指挥不动另外两个。',
    ].join('\n'),
  },
  {
    id: 'ch_demo_02', title: '第二章 舒适区边缘',
    text: [
      '【平静】很多人努力了很久却没有变化，原因不是不够拼，而是一直在舒适区里重复。',
      '【平静】真正的成长，发生在舒适区的边缘：有点难，但够得着。[pause:0.6]',
      '【喜悦】太简单没有进步，太难会直接放弃，只有边缘那一圈，才是有效的练习。',
      '【平静】所以别急着给自己加码。先把目标切小，小到今天就能做完。',
    ].join('\n'),
  },
  {
    id: 'ch_demo_03', title: '第三章 元认知：看见自己',
    text: [
      '【平静】元认知，就是"看见自己在想什么"的能力。',
      '【平静】当你意识到"我正在焦虑"的那一刻，你就已经不再完全被焦虑控制了。[pause:0.5]',
      '【惊喜】这也是这本书最有用的一句话：看见，就是改变的开始。',
      '【喜悦】好了，第一章到第三章的内容就分享完了。感谢收听，我们下期再见。',
    ].join('\n'),
  },
];

const PODCAST_SCRIPT = [
  '【A】大家好，欢迎收听「深夜读书会」，我是阿May。',
  '【B】我是老周。今天我们要聊的这本书，叫《认知觉醒》。',
  '【A】【喜悦】我读完最大的感受是，它不像鸡汤，更像一份大脑使用说明书。',
  '【B】对，尤其是「舒适区边缘」这个概念。我最近做项目卡了很久，[pause:0.4]后来才发现自己一直在舒适区里重复。',
  '【A】那你后来怎么破的？',
  '【B】【平静】把目标切小，小到「有点难但今天能做到」。做完就记一笔，攒够信心再加码。',
  '【A】听起来很朴素，但确实比"逼自己自律"有用得多。',
  '【B】对，自律靠的不是意志力，是方法和反馈。',
  '【A】【喜悦】那今天就先聊到这里。喜欢的话，记得点个关注，我们下期见。',
  '【B】下期见。',
].join('\n');

// ─── 通用工具 ──────────────────────────────────────────────
const sleep = ms => new Promise(r => setTimeout(r, ms));

function api(pathname, { method = 'GET', body } = {}) {
  return fetch(API + pathname, {
    method,
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${TOKEN}` },
    body: body ? JSON.stringify(body) : undefined,
  }).then(async r => {
    const txt = await r.text();
    try { return JSON.parse(txt); } catch { return { raw: txt, status: r.status }; }
  });
}

/** 提交一条 mono 队列任务（直接打后端，绕开 UI —— UI 的提交路径在其它场景里单独截） */
function submitMono(projectName, text, voicePath, speed = 1.0) {
  return api('/api/queue/submit', {
    method: 'POST',
    body: {
      project_name: projectName, kind: 'mono',
      lines: text.split('\n').filter(s => s.trim()).map(s => ({ speaker: 'A', text: stripMarkers(s), emotion: null })),
      voices: { A: voicePath },
      params: { speed, speaker_speeds: { A: speed } },
      glossary_enabled: true,
    },
  });
}

/** 只把标记从文本里去掉（后端不需要【】与 [pause:N]，它们是前端协议） */
function stripMarkers(line) {
  return line.replace(/【[^】]*】/g, '').replace(/\[pause:\s*[\d.]+\s*\]/g, '').trim();
}

function clearQueue(kind = 'mono') {
  return api(`/api/queue?kind=${kind}`, { method: 'DELETE' });
}

class Shooter {
  constructor() {
    this.page = null;
    this.log = [];
  }

  async launch() {
    this.browser = await puppeteer.launch({
      executablePath: CHROME,
      headless: true,
      args: ['--no-sandbox', '--font-render-hinting=none', '--force-color-profile=srgb', '--hide-scrollbars'],
      defaultViewport: { width: 1600, height: 1000, deviceScaleFactor: 2 },
    });
  }

  /** 新开一页并预置 localStorage（token + 草稿 + 存档） */
  async open({ path: url = '/dubbing', mono, podcast, monoProjects, noAuth = false, wait = 1500 } = {}) {
    if (this.page) await this.page.close().catch(() => {});
    const page = await this.browser.newPage();
    await page.evaluateOnNewDocument((seed) => {
      try {
        localStorage.clear();
        if (seed.TOKEN) localStorage.setItem('wb-auth-token', seed.TOKEN);
        if (seed.mono) localStorage.setItem('wb-mono-draft-v2', JSON.stringify(seed.mono));
        if (seed.podcast) localStorage.setItem('wb-podcast-draft-v1', JSON.stringify(seed.podcast));
        if (seed.monoProjects) localStorage.setItem('wb-mono-projects', JSON.stringify(seed.monoProjects));
        localStorage.setItem('wb-mode', seed.mode || 'dubbing');
      } catch { /* 忽略 */ }
    }, {
      TOKEN: noAuth ? null : TOKEN,
      mono, podcast, monoProjects,
      mode: url.startsWith('/podcast') ? 'podcast' : 'dubbing',
    });
    await page.goto(BASE + url, { waitUntil: 'networkidle2' });
    await sleep(wait);
    this.page = page;
    return page;
  }

  /** 视口截图 */
  async shot(name, { full = false } = {}) {
    const file = path.join(OUT, name);
    await this.page.screenshot({ path: file, fullPage: full });
    this.log.push(`${name} ${(fs.statSync(file).size / 1024).toFixed(0)}KB`);
  }

  /** 按元素文字定位「最近的卡片/面板」并截取该区域 */
  async shotNear(name, text, { pad = 10, up = 3 } = {}) {
    const box = await this.page.evaluate((t, upN) => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node;
      while ((node = walker.nextNode())) {
        const s = (node.textContent || '').trim();
        if (!s || !s.includes(t)) continue;
        let el = node.parentElement;
        for (let i = 0; i < upN && el && el.parentElement && el.parentElement !== document.body; i++) el = el.parentElement;
        const r = el.getBoundingClientRect();
        return { x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height };
      }
      return null;
    }, text, up);
    if (!box) throw new Error(`找不到文本「${text}」`);
    await this.shotBox(name, box, pad);
  }

  /** 按卡片标题文字截取整张 Card（Card = div.rounded-xl） */
  async shotCard(name, titleText, { pad = 10, index = 0 } = {}) {
    const box = await this.page.evaluate((t, idx) => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      let node, hit = 0;
      while ((node = walker.nextNode())) {
        if (!(node.textContent || '').trim().includes(t)) continue;
        let el = node.parentElement;
        while (el && el !== document.body) {
          if (typeof el.className === 'string' && el.className.includes('rounded-xl')) break;
          el = el.parentElement;
        }
        if (!el || el === document.body) continue;
        if (hit++ < idx) continue;
        const r = el.getBoundingClientRect();
        return { x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height };
      }
      return null;
    }, titleText, index);
    if (!box) throw new Error(`找不到卡片「${titleText}」`);
    await this.shotBox(name, box, pad);
  }

  /** 按选择器截取元素 */
  async shotEl(name, selector, { pad = 0, index = 0 } = {}) {
    const box = await this.page.evaluate((sel, idx) => {
      const el = document.querySelectorAll(sel)[idx];
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return { x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height };
    }, selector, index);
    if (!box) throw new Error(`找不到元素 ${selector}[${index}]`);
    await this.shotBox(name, box, pad);
  }

  /** 打开中的弹窗面板（fixed inset-0 里最大的那块） */
  async shotModal(name, { pad = 6 } = {}) {
    await sleep(400);
    const box = await this.page.evaluate(() => {
      const cands = [...document.querySelectorAll('div')].filter(d => {
        const c = d.className;
        return typeof c === 'string' && c.includes('fixed') && c.includes('inset-0');
      });
      if (!cands.length) return null;
      const ov = cands[cands.length - 1];
      let best = null, area = 0;
      for (const ch of ov.children) {
        const r = ch.getBoundingClientRect();
        if (r.width * r.height > area) { area = r.width * r.height; best = r; }
      }
      const r = best || ov.getBoundingClientRect();
      return { x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height };
    });
    if (!box) throw new Error('没有找到打开的弹窗');
    await this.shotBox(name, box, pad);
  }

  async shotBox(name, box, pad = 0) {
    const file = path.join(OUT, name);
    await this.page.screenshot({
      path: file,
      clip: {
        x: Math.max(0, box.x - pad),
        y: Math.max(0, box.y - pad),
        width: box.w + pad * 2,
        height: box.h + pad * 2,
      },
    });
    this.log.push(`${name} ${(fs.statSync(file).size / 1024).toFixed(0)}KB`);
  }

  /** 按可见文字点按钮 */
  async clickText(text, { exact = false, inSel = null, index = 0 } = {}) {
    const ok = await this.page.evaluate((t, ex, sel, idx) => {
      const root = sel ? document.querySelector(sel) : document.body;
      if (!root) return false;
      const els = [...root.querySelectorAll('button, a, [role="button"]')]
        .filter(e => {
          const s = (e.textContent || '').trim();
          return ex ? s === t : s.includes(t);
        });
      const el = els[idx];
      if (!el) return false;
      el.scrollIntoView({ block: 'nearest' });
      el.click();
      return true;
    }, text, exact, inSel, index);
    if (!ok) throw new Error(`点不到「${text}」`);
    await sleep(600);
  }

  /** 在某个输入框里打字（React 受控组件：用 keyboard 才能触发 onChange） */
  async typeInto(placeholderOrSel, text, { byPlaceholder = false, index = 0 } = {}) {
    const sel = byPlaceholder ? `input[placeholder="${placeholderOrSel}"]` : placeholderOrSel;
    const handle = (await this.page.$$(sel))[index];
    if (!handle) throw new Error(`找不到输入框 ${sel}`);
    await handle.click({ clickCount: 3 });
    await handle.type(text, { delay: 12 });
    await sleep(200);
  }

  close() { return this.browser.close(); }
}

// ─── 场景 ──────────────────────────────────────────────────
const S = {};

S['login'] = async (s) => {
  await s.open({ path: '/account', noAuth: true, wait: 1800 });
  await s.shotNear('01-login.png', '用户名或邮箱', { pad: 14, up: 4 });
};

S['register'] = async (s) => {
  const page = await s.open({ path: '/account', noAuth: true, wait: 1800 });
  await s.clickText('注册', { exact: true, inSel: 'main' });
  await s.typeInto('you@example.com', 'docs.demo@lizigege.com', { byPlaceholder: true });
  await s.typeInto('6 位数字', '888888', { byPlaceholder: true });
  await s.typeInto('不填则使用邮箱前缀', '小柯', { byPlaceholder: true });
  await s.typeInto('至少 6 位', 'demo123456', { byPlaceholder: true });
  await page.evaluate(() => document.activeElement && document.activeElement.blur());
  await sleep(300);
  await s.shotNear('02-register.png', '邮箱', { pad: 14, up: 4 });
};

S['usermenu'] = async (s) => {
  const page = await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2000 });
  // 打开用户菜单（按钮上写着昵称）
  const ok = await page.evaluate(() => {
    const btn = [...document.querySelectorAll('header button')].find(b => (b.textContent || '').includes('小柯'));
    if (!btn) return false;
    btn.click();
    return true;
  });
  if (!ok) throw new Error('找不到用户菜单按钮');
  await sleep(600);
  await s.shotBox('03-usermenu.png', { x: 1080, y: 0, w: 520, h: 300 }, 0);
};

S['account-top'] = async (s) => {
  await s.open({ path: '/account', wait: 2200 });
  await s.shotBox('04-account-top.png', { x: 290, y: 66, w: 1020, h: 640 }, 0);
};

S['checkin'] = async (s) => {
  await s.open({ path: '/account', wait: 2200 });
  await s.clickText('每日签到', { inSel: 'main' });
  await sleep(700); // toast 2.5s 内截到
  await s.shot('05-checkin.png');
};

S['redeem'] = async (s) => {
  await s.open({ path: '/account', wait: 2200 });
  await s.typeInto('输入优惠码', '2DBA709477', { byPlaceholder: true });
  await s.clickText('兑换', { inSel: 'main' });
  await sleep(900);
  await s.shot('06-redeem.png');
};

S['shop'] = async (s) => {
  await s.open({ path: '/account', wait: 2200 });
  await s.shotNear('07-shop.png', '积分商城', { pad: 12, up: 2 });
};

S['pay-qr'] = async (s) => {
  // ⚠️ 这一步会**真实调用支付通道出码**（沙箱本地账本，订单 5 分钟后自动过期）
  const page = await s.open({ path: '/account', wait: 2200 });
  const btns = await page.$$('button');
  // 「购买」按钮在套餐卡里；点第一个（体验包）
  const idx = await page.evaluate(() => {
    const all = [...document.querySelectorAll('button')];
    return all.findIndex(b => b.textContent.trim() === '购买');
  });
  if (idx < 0) throw new Error('找不到「购买」按钮');
  await btns[idx].click();
  await sleep(6000); // 等下单 + 出码
  await s.shotModal('08-pay-qr.png');
  await s.shot('99-account-after-pay.png'); // 顺带留一张（下面 pending 订单要用）
};

S['orders'] = async (s) => {
  await s.open({ path: '/account/orders', wait: 2500 });
  await s.shotBox('09-orders.png', { x: 290, y: 66, w: 1020, h: 620 }, 0);
};

S['points-logs'] = async (s) => {
  await s.open({ path: '/account', wait: 2200 });
  await s.shotNear('10-points-logs.png', '积分记录', { pad: 12, up: 2 });
};

S['dubbing-overview'] = async (s) => {
  await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2200 });
  await s.shot('11-dubbing-overview.png');
};

S['voice-picker'] = async (s) => {
  await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2000 });
  await s.clickText('选择音色', { inSel: 'aside' });
  await sleep(2500); // 等 BreezeBlue 310 条加载
  await s.shotModal('12-voice-picker.png');
};

S['voice-upload'] = async (s) => {
  const page = await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2000 });
  // 侧栏音色卡的上传口（accept="audio/*"）
  const input = await page.$('input[accept*="audio"]');
  if (!input) throw new Error('找不到音色上传的 file input');
  await input.uploadFile(`${SANDBOX}/data/preset-voices/女-播客1.mp3`);
  await sleep(900);
  await s.shotModal('13-voice-upload.png');
};

S['speed'] = async (s) => {
  await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2000 });
  await s.shotEl('14-speed.png', 'aside > div', { pad: 10, index: 0 });
};

S['emotion-menu'] = async (s) => {
  const page = await s.open({ path: '/dubbing', mono: { ...monoDraft(), text: MONO_TEXT.split('\n').slice(0, 3).join('\n') }, wait: 2000 });
  // 光标放到画布末尾
  await page.evaluate(() => {
    const canvas = document.querySelector('.mono-canvas');
    canvas.focus();
    const sel = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(canvas);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
  });
  await s.clickText('情绪', { inSel: 'main' });
  await sleep(400);
  await s.shotBox('15-emotion-menu.png', { x: 300, y: 700, w: 620, h: 300 }, 0);
};

S['pause-popover'] = async (s) => {
  const page = await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2000 });
  // 点画布里第一个停顿标签（chip）弹出改时长的浮层
  const ok = await page.evaluate(() => {
    const chip = document.querySelector('.mono-canvas [data-marker^="[pause"]');
    if (!chip) return false;
    const r = chip.getBoundingClientRect();
    const opts = { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
    chip.dispatchEvent(new MouseEvent('mousedown', opts));
    return true;
  });
  if (!ok) throw new Error('画布里没有停顿标签');
  await sleep(700);
  await s.shotModal('16-pause-popover.png', { pad: 16 });
};

S['editor-toolbar'] = async (s) => {
  const page = await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2200 });
  // 从「生成配音」向上找到同时含「情绪」的那一行 —— 即整条底部工具条
  const box = await page.evaluate(() => {
    const gen = [...document.querySelectorAll('button')].find(b => (b.textContent || '').trim() === '生成配音');
    if (!gen) return null;
    let el = gen;
    while (el.parentElement && !(el.textContent || '').includes('情绪')) el = el.parentElement;
    const r = el.getBoundingClientRect();
    return { x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height };
  });
  if (!box) throw new Error('找不到底部工具条');
  await s.shotBox('17-editor-toolbar.png', box, 12);
};

S['import-modal'] = async (s) => {
  await s.open({ path: '/dubbing', mono: { ...monoDraft(), text: '' }, wait: 2000 });
  await s.clickText('导入', { inSel: 'main' });
  await sleep(800);
  await s.shotModal('18-import-modal.png');
};

S['chapter-confirm'] = async (s) => {
  const page = await s.open({ path: '/dubbing', mono: { ...monoDraft(), text: '' }, wait: 2000 });
  // ① 先点「导入」把弹窗打开（否则上传完也不会渲染确认面板）
  await s.clickText('导入', { inSel: 'main' });
  await sleep(700);
  // ② 用一份真实的多章文本走导入流程（走 .txt 文件上传，与用户操作一致）
  //    ⚠️ 侧栏「上传本地音色」也是 input[type=file]，必须按 accept 挑出文档那个
  const tmp = '/tmp/docs-book.txt';
  fs.writeFileSync(tmp, bookAsPlainText(), 'utf8');
  const input = await page.$('input[accept*=".txt"]');
  if (!input) throw new Error('找不到文档导入的 file input');
  await input.uploadFile(tmp);
  await sleep(4000);
  await s.shotModal('19-chapter-confirm.png');
};

S['book-mode'] = async (s) => {
  await s.open({
    path: '/dubbing',
    mono: { ...monoDraft(), text: BOOK_CHAPTERS[0].text, book: BOOK_CHAPTERS, activeChapterId: BOOK_CHAPTERS[0].id },
    wait: 2400,
  });
  await s.shot('20-book-mode.png');
};

S['glossary'] = async (s) => {
  const page = await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2000 });
  // ⚠️ 面板**默认是展开的**，点标题会折叠 ⇒ 这里只展开「内置词库」并搜一个词
  await s.clickText('内置词库', { inSel: 'aside' });
  await sleep(500);
  await s.typeInto('搜索内置词', '重', { byPlaceholder: true }).catch(() => {});
  await sleep(500);
  await s.shotCard('21-glossary.png', '术语词汇表');
};

S['queue-running'] = async (s) => {
  await clearQueue('mono');
  await sleep(500);
  // 第三方 API 型引擎默认并发 3 ⇒ 提交 6 条，才能同时看到「合成中」与「排队中」
  const names = ['第一节', '第二节', '第三节', '第四节', '第五节', '第六节'];
  for (let i = 0; i < names.length; i++) {
    await submitMono(`读书分享 · ${names[i]}`, MONO_LONG, i % 3 === 2 ? V_PRESET_M : V_PRESET_W);
  }
  // 轮询到「有进度且尚有排队」的那一刻再截图（进度条才有内容）
  for (let i = 0; i < 40; i++) {
    const r = await api('/api/queue?kind=mono');
    const ts = r.tasks || [];
    const running = ts.filter(t => t.status === 'running');
    const queued = ts.filter(t => t.status === 'queued' || t.status === 'pending');
    if (running.some(t => (t.progress || 0) > 0.05) && queued.length > 0) break;
    await sleep(500);
  }
  await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2500 });
  await s.shot('22-queue-running.png');
};

S['queue-success'] = async (s) => {
  // ⚠️ 沙箱里 art 通道扛不住 3 并发（会被隔离 120s），这里只跑 1 条，保证是成功路径
  const has = async () => ((await api('/api/queue?kind=mono&terminal_limit=5')).tasks || []).some(t => t.status === 'success');
  if (!(await has())) {
    await clearQueue('mono');
    await sleep(500);
    await submitMono('读书分享 · 第一节', MONO_TEXT, V_PRESET_W);
    for (let i = 0; i < 24; i++) {
      await sleep(2500);
      if (await has()) break;
    }
  }
  if (!(await has())) throw new Error('任务没有变成「完成」');
  await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2500 });
  await s.shotCard('23-queue-success.png', '任务队列');
};

S['queue-failed'] = async (s) => {
  await clearQueue('mono');
  await sleep(500);
  const page = await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2500 });
  // 用一个不存在的音色路径 → 必然失败，且**提交前**就失败（不发请求、不计费）
  // ✅ 2026-10-16 修掉池门面的异常包装后，这里如实报「参考音频文件不存在，请检查任务音色配置」
  //    （修前被包成「资源 xxx 合成失败，提交状态可能未知」，指错方向；取证见
  //     outputs/verify-input-error-live.js 与 .workbuddy/memory/2026-10-16.md）
  await submitMono('演示：音色缺失', MONO_TEXT.split('\n').slice(0, 2).join('\n'), V_MISSING);
  // ⚠️ 这条任务是「瞬败」的，靠 UI 的自动弹窗抓不稳 ⇒ 直接点任务卡上的 ⚠（title=查看报错详情）
  let hit = false;
  for (let i = 0; i < 30; i++) {
    await sleep(1500);
    hit = await page.evaluate(() => {
      const b = document.querySelector('[title="查看报错详情"]');
      if (!b) return false;
      b.click();
      return true;
    });
    if (hit) break;
  }
  if (!hit) throw new Error('任务没有出现「查看报错详情」入口');
  await sleep(700);
  await s.shotModal('24-queue-failed.png');
};

S['podcast-overview'] = async (s) => {
  await s.open({ path: '/podcast', podcast: podcastDraft(), wait: 2500 });
  await s.shot('25-podcast-overview.png');
};

S['speaker-panel'] = async (s) => {
  await s.open({ path: '/podcast', podcast: podcastDraft(), wait: 2500 });
  await s.shotEl('26-speaker-panel.png', 'aside', { pad: 10, index: 0 });
};

S['podcast-import'] = async (s) => {
  const page = await s.open({ path: '/podcast', podcast: { ...podcastDraft(), script: '' }, wait: 2200 });
  await s.clickText('导入', { inSel: 'main' });
  await sleep(700);
  await page.evaluate(() => {
    const ta = document.querySelector('textarea');
    if (ta) {
      ta.focus();
    }
  });
  await s.typeInto('textarea', 'A:哈喽，大家好，欢迎收听我们的播客。\nB:大家好，我是主持人B。\nA:今天咱们聊一个很现实的话题。');
  await sleep(400);
  await s.shotModal('27-podcast-import.png');
};

S['topnav'] = async (s) => {
  const page = await s.open({ path: '/dubbing', mono: monoDraft(), monoProjects: monoProjects(), wait: 2000 });
  await page.evaluate(() => {
    const btn = [...document.querySelectorAll('header button')].find(b => b.getAttribute('aria-label') === '切换项目');
    if (btn) btn.click();
  });
  await sleep(500);
  await s.shotBox('28-topnav.png', { x: 0, y: 0, w: 1600, h: 340 }, 0);
};

S['practice-canvas'] = async (s) => {
  // 最佳实践页的配图，队列里不该出现演示用的失败任务
  await clearQueue('mono');
  await sleep(500);
  await s.open({ path: '/dubbing', mono: { ...monoDraft(), text: MONO_TEXT }, wait: 2200 });
  await s.shotBox('29-practice-canvas.png', { x: 0, y: 56, w: 1600, h: 944 }, 0);
};

S['emotion-chip'] = async (s) => {
  const page = await s.open({ path: '/dubbing', mono: monoDraft(), wait: 2200 });
  const ok = await page.evaluate(() => {
    const chip = document.querySelector('.mono-canvas [data-marker^="【"]');
    if (!chip) return false;
    const r = chip.getBoundingClientRect();
    const opts = { bubbles: true, clientX: r.x + r.width / 2, clientY: r.y + r.height / 2 };
    chip.dispatchEvent(new MouseEvent('mousedown', opts));
    return true;
  });
  if (!ok) throw new Error('画布里没有情绪标签');
  await sleep(700);
  await s.shotModal('30-emotion-chip.png', { pad: 16 });
};

// ─── 草稿/存档构造 ─────────────────────────────────────────

function defaultEmotion() {
  return { mode: 0, audio_path: null, vector: Array(8).fill(0), weight: 0.65, text: null, random: false };
}

function monoDraft(extra = {}) {
  return {
    name: '读书分享_20261016',
    voice: { voice_path: V_PRESET_W, voice_name: '女-温暖闺蜜.mp3' },
    speed: 1.0,
    text: MONO_TEXT,
    ...extra,
  };
}

function podcastDraft() {
  return {
    name: '深夜读书会_20261016',
    voices: {
      A: { name: '主持人A', voice_path: V_PRESET_W, voice_name: '女-温暖闺蜜.mp3', speed: 1.0, emotion: defaultEmotion() },
      B: { name: '主持人B', voice_path: V_PRESET_M, voice_name: '男-不羁青年.mp3', speed: 1.05, emotion: defaultEmotion() },
    },
    script: PODCAST_SCRIPT,
  };
}

function monoProjects() {
  return [
    { id: 'p_demo_1', name: '读书分享_20261016', voice: { voice_path: V_PRESET_W, voice_name: '女-温暖闺蜜.mp3' }, speed: 1.0, text: MONO_TEXT, savedAt: new Date().toISOString() },
    { id: 'p_demo_2', name: '认知觉醒·第三章', voice: { voice_path: V_MINE, voice_name: '我的录音-安静室内（手机）' }, speed: 1.0, text: BOOK_CHAPTERS[2].text, savedAt: new Date(Date.now() - 3600e3).toISOString() },
    { id: 'p_demo_3', name: '产品介绍-30秒版', voice: { voice_path: V_PRESET_M, voice_name: '男-不羁青年.mp3' }, speed: 1.05, text: '【喜悦】把文字变成声音，只需要三步。', savedAt: new Date(Date.now() - 86400e3).toISOString() },
  ];
}

/** 把三章书稿拼成一份带标题层级标记的纯文本（走真实的导入分章路径） */
function bookAsPlainText() {
  return BOOK_CHAPTERS.map(c => `第${c.title.replace(/^第/, '')}\n\n${c.text.split('\n').map(stripMarkers).join('\n')}`).join('\n\n');
}

// ─── 入口 ──────────────────────────────────────────────────
(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const want = process.argv.slice(2);
  const names = want.length ? want : Object.keys(S);
  const s = new Shooter();
  await s.launch();
  let failed = [];
  for (const n of names) {
    if (!S[n]) { console.log(`跳过未知场景 ${n}`); continue; }
    const t0 = Date.now();
    try {
      await S[n](s);
      console.log(`✓ ${n}  ${((Date.now() - t0) / 1000).toFixed(1)}s`);
    } catch (e) {
      failed.push(n);
      console.log(`✗ ${n}  ${e.message}`);
      // 失败也留一张现场图，便于定位
      try { await s.page.screenshot({ path: `/tmp/docs-fail-${n}.png` }); } catch {}
    }
  }
  await s.close();
  console.log('\n--- 产物 ---');
  s.log.forEach(l => console.log(l));
  if (failed.length) { console.log('\n失败场景:', failed.join(', ')); process.exit(2); }
})();
