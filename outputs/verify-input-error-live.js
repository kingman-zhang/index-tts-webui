/**
 * 「参考音频缺失」这一真实场景的**线上行为取证**（沙箱 :3002，纯 fetch，不用浏览器）。
 *
 * 验的是 2026-10-16 那笔修复的真实效果 —— 单测覆盖了代码路径，但「真跑起来是什么文案、
 * 会不会连累后续任务」只能在这里看到：
 *
 *   修前：第 1 条报「资源 art 合成失败，提交状态可能未知」（指错方向），
 *         且该资源被冷却 120s ⇒ 之后所有任务报「所有 TTS 资源均处于冷却中」
 *         （当天实测：6 条任务全灭，见 .workbuddy/memory/2026-10-16.md）。
 *   修后：每条都报「参考音频文件不存在，请检查任务音色配置」，且互不连累。
 *
 * 为什么用「同一个坏音色连提 3 条」：这正是区分新旧行为的**最小可观测实验** ——
 * 旧行为在第 1 条之后就把资源冷却掉了，第 2、3 条必然换一种报错。
 *
 * 用法（沙箱后端需已在 :3002 运行）：
 *   NODE_PATH=<node 沙箱>/node_modules <node> outputs/verify-input-error-live.js
 * 产物：outputs/<日期>-参考音频缺失-线上取证.json
 */
const fs = require('fs');

const API = 'http://127.0.0.1:3002';
const TOKEN = 'Fv8y6-kRXgR9l-rfpC_1RBAUyTHXng-v-3mHoBycMpw';
const SANDBOX = '/Users/zhangjianwen/Documents/Kingman/workbuddy/index-tts/outputs/docs-sandbox';
// 刻意用**不存在**的参考音频路径：模拟「音色条目还在、磁盘上的文件没了」。
// 用相对路径而不是沙箱绝对路径：绝对路径会把本机 home 目录印进报错里
// （这个报错会出现在产品文档的截图上），相对路径更像产品内部路径。
const BAD_VOICE = 'data/voices/u_a5afc66e2d67/voc_notexist_live.wav';
const OUT = process.argv[2] || 'outputs/2026-10-16-参考音频缺失-线上取证.json';

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

function submit(name) {
  return api('/api/queue/submit', {
    method: 'POST',
    body: {
      project_name: name, kind: 'mono',
      lines: [{ speaker: 'A', text: '大家好，欢迎来到小柯读书。', emotion: null }],
      voices: { A: BAD_VOICE },
      params: { speed: 1.0, speaker_speeds: { A: 1.0 } },
      glossary_enabled: true,
    },
  });
}

async function listTasks() {
  const r = await api('/api/queue?kind=mono&terminal_limit=20');
  return r.tasks || [];
}

const TERMINAL = new Set(['success', 'failed', 'interrupted', 'cancelled']);

async function main() {
  const results = [];
  const check = (name, pass, detail) => {
    results.push({ name, pass: !!pass, detail });
    console.log(`${pass ? '✓' : '✗'} ${name}  ${JSON.stringify(detail)}`);
    return !!pass;
  };

  // 清空队列，保证下面的观测不受历史任务干扰
  await api('/api/queue?kind=mono', { method: 'DELETE' });
  await sleep(800);
  check('前置：队列已清空', (await listTasks()).length === 0, (await listTasks()).length);

  const names = ['输入侧错误实测 ①', '输入侧错误实测 ②', '输入侧错误实测 ③'];
  for (const n of names) await submit(n);

  // 等三条都到终态（都是「提交前就失败」，很快）
  let tasks = [];
  for (let i = 0; i < 40; i++) {
    await sleep(1000);
    tasks = await listTasks();
    const mine = tasks.filter(t => names.includes(t.project_name));
    if (mine.length === 3 && mine.every(t => TERMINAL.has(t.status))) break;
  }

  const mine = names.map(n => tasks.find(t => t.project_name === n)).filter(Boolean);
  console.log('\n项目名                 状态      message                          error');
  for (const t of mine) {
    console.log(`${(t.project_name || '').padEnd(22)} ${String(t.status).padEnd(8)} ${String(t.message || '').padEnd(32)} ${String(t.error || '').slice(0, 80)}`);
  }
  console.log('');

  check('三条任务都进入终态', mine.length === 3 && mine.every(t => TERMINAL.has(t.status)),
        mine.map(t => t.status));
  check('三条任务的 message 都点明「参考音频文件不存在」',
        mine.every(t => t.message === '参考音频文件不存在，请检查任务音色配置'),
        mine.map(t => t.message));
  check('三条任务的 error 都带上原始路径（便于定位）',
        mine.every(t => (t.error || '').includes(BAD_VOICE)),
        mine.map(t => String(t.error || '').slice(0, 60)));
  check('没有任何一条报「资源 xx 合成失败」（修前的指错方向症状）',
        mine.every(t => !String(t.error || '').includes('合成失败')),
        mine.filter(t => String(t.error || '').includes('合成失败')).length);
  check('第 2、3 条没有变成「所有 TTS 资源均处于冷却中」（修前会连累）',
        mine.every(t => !String(t.error || '').includes('冷却中')),
        mine.filter(t => String(t.error || '').includes('冷却中')).length);

  fs.writeFileSync(OUT, JSON.stringify({
    when: new Date().toISOString(),
    bad_voice: BAD_VOICE,
    tasks: mine.map(t => ({
      project_name: t.project_name, status: t.status,
      message: t.message, error: t.error,
    })),
    results, failed: results.filter(r => !r.pass).map(r => r.name),
  }, null, 2), 'utf8');

  const failed = results.filter(r => !r.pass).length;
  console.log(`\n===== ${results.length - failed}/${results.length} 通过 =====`);
  console.log('产物：', OUT);
  process.exit(failed ? 1 : 0);
}

main().catch(e => { console.error('失败：', e); process.exit(1); });
