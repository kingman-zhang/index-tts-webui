#!/usr/bin/env node
/**
 * 落地页**数字自洽**检查：正文里手写的数字，必须与页面自动算出来的数字一致。
 *
 * ## 为什么需要它
 * 落地页有两类数字来源：
 *   a) **自动算出来的** —— 「本次花费：约 13 积分（¥0.13）· 约合 1 分钟音频」这类，
 *      由 `content.ts` 的 `caseCost()` 用页面自己那份计费函数现算；
 *   b) **手写在文案里的** —— 标题的「（约 1 分钟）」、params 的「约 250 字」。
 * 两类数字挨着显示，一旦不一致，用户会当场发现页面在自相矛盾 —— 而这类毛病
 * **不报错、不构建失败、肉眼也容易漏**（本页初版就有 3 处：
 * 「3 分钟读书分享音频」实际约 1 分钟、「5 分钟双人播客对谈」实际约 1 分钟、
 * 「约 300 字」实际 248 字）。这个脚本就是钉住这一类。
 *
 * 用法：`node outputs/verify-landing-numbers.mjs`（无需 NODE_PATH，自己从
 *      webui-frontend/node_modules 里取 esbuild；改完 content.ts / 文稿后请重跑）
 * 退出码：全部通过 0，有不一致 1。
 */
const { join, resolve } = require("path");
const { mkdirSync, rmSync, writeFileSync } = require("fs");

const HERE = __dirname;
const ROOT = resolve(HERE, "..");
const FRONTEND = join(ROOT, "webui-frontend");
const TMP_DIR = join(FRONTEND, "node_modules", ".cache", "landing-numbers");
const TMP_SRC = join(TMP_DIR, "_entry.ts");
const TMP_ENTRY = join(TMP_DIR, "content.cjs");

let pass = 0;
const fails = [];
function check(name, ok, detail) {
  if (ok) { pass++; console.log(`  ✓ ${name}${detail ? " — " + detail : ""}`); }
  else { fails.push(name); console.log(`  ✗ ${name}${detail ? " — " + detail : ""}`); }
}

// ─── 0. 用 esbuild 把 content.ts（含其依赖）打成 CommonJS 再 require ─────────
// 临时入口把脚本要用到的东西一并转出，避免为了这个检查去改产品代码的导出。
mkdirSync(TMP_DIR, { recursive: true });
writeFileSync(TMP_SRC, [
  `export * from "@/landing/content";`,
  `export { SITE_NAME, SITE_URL, SITE_LANG } from "@/lib/site";`,
  `export { TTS_CHARS_PER_SECOND } from "@/lib/format";`,
].join("\n"), "utf8");

const esbuild = require(join(FRONTEND, "node_modules", "esbuild"));
esbuild.buildSync({
  entryPoints: [TMP_SRC],
  outfile: TMP_ENTRY,
  bundle: true,
  format: "cjs",
  platform: "node",
  target: "node18",
  alias: { "@": join(FRONTEND, "src") },
  logLevel: "warning",
});
const C = require(TMP_ENTRY);

// ─── 1. 每个实践案例：手写数字 == 自动算出的数字 ──────────────────────────
// ⚠️ 匹配**不要求带「约」字**：本页初版的两处时长错账恰恰是裸写的
//    （「5 分钟双人播客对谈」），若只查「约 N 分钟」就会整片漏过。
console.log("\n== 实践案例的数字自洽 ==");
for (const c of C.PRACTICE_CASES) {
  const m = C.caseCost(c);
  // 只查「对外声明类」字段：标题 / 目标 / 交付物 / 参数表 / 文稿说明。
  // 步骤与坑（steps / pitfalls）不谈总量，故不扫 —— 那里全是操作细节里的零碎数字。
  const fields = { 标题: [c.title], 目标: [c.goal], 交付物: [c.deliverable], 参数: c.params.flat(), 文稿说明: [c.scriptLabel] };
  const label = `${c.id}（实测 ${m.chars} 字 / ${m.points} 积分 / ${m.duration}）`;

  const seconds = m.chars / C.TTS_CHARS_PER_SECOND;
  const realMin = seconds < 3600 ? Math.max(1, Math.round(seconds / 60)) : null;
  const realHour = seconds >= 3600 ? Number((seconds / 3600).toFixed(1)) : null;

  // (a) 时长：任何 `N 分钟` / `N 小时` 都必须是这个案例的总时长
  let durOk = true;
  for (const [where, list] of Object.entries(fields)) {
    for (const s of list) {
      if (typeof s !== "string") continue;
      for (const x of s.matchAll(/(\d+(?:\.\d+)?)\s*(分钟|小时)/g)) {
        const n = Number(x[1]);
        const real = x[2] === "分钟" ? realMin : realHour;
        if (real === null) { durOk = false; check(`${label} ${where}「${n} ${x[2]}」`, false, `单位不对：实测${x[2] === "分钟" ? "不足 1 小时" : "不足 60 分钟"}`); }
        else if (Math.abs(n - real) / real > 0.15) { durOk = false; check(`${label} ${where}「${n} ${x[2]}」`, false, `与实测 ${real} ${x[2]} 相差 >15%`); }
      }
    }
  }
  if (durOk) check(`${label} 时长声明`, true, realHour ? `约 ${realHour} 小时` : `约 ${realMin} 分钟`);

  // (b) 字数：`约 N 字` 必须与实测可计费字数一致（容差 10%，作者会取整到顺手的数）
  let charOk = true;
  for (const [where, list] of Object.entries(fields)) {
    for (const s of list) {
      if (typeof s !== "string") continue;
      for (const x of s.matchAll(/约\s*([\d,]+)\s*字/g)) {
        const n = Number(x[1].replace(/,/g, ""));
        const dev = Math.abs(n - m.chars) / Math.max(m.chars, 1);
        if (dev > 0.1) { charOk = false; check(`${label} ${where}「约 ${n} 字」`, false, `与实测 ${m.chars} 字差 ${(dev * 100).toFixed(0)}%（>10%）`); }
      }
    }
  }
  if (charOk) check(`${label} 字数声明`, true, `${m.chars} 字`);

  // (c) 「N 万字」的量词口径：
  //     ·「上限 N 万字」= 书稿模式单章上限这个**常量**
  //     ·「每 N 万字」  = 折算基准，必须等于该案例的 costBasisChars（否则成本行会看不懂）
  let wanOk = true;
  for (const [where, list] of Object.entries(fields)) {
    for (const s of list) {
      if (typeof s !== "string") continue;
      for (const x of s.matchAll(/上限\s*([\d.]+)\s*万字/g)) {
        const n = Number(x[1]) * 10000;
        if (n !== C.CHAPTER_MAX_CHARS) { wanOk = false; check(`${label} ${where}「上限 ${x[1]} 万字」`, false, `与常量 CHAPTER_MAX_CHARS=${C.CHAPTER_MAX_CHARS} 不符`); }
      }
      for (const x of s.matchAll(/每\s*([\d.]+)\s*万字/g)) {
        const n = Number(x[1]) * 10000;
        if (c.costBasisChars && n !== c.costBasisChars) { wanOk = false; check(`${label} ${where}「每 ${x[1]} 万字」`, false, `与 costBasisChars=${c.costBasisChars} 不符`); }
      }
    }
  }
  if (wanOk) check(`${label} 万字口径`, true, "上限与折算基准都对得上");

  // (d) 积分：标题/交付物里若手写了积分，必须与实测一致
  let ptOk = true;
  for (const where of ["标题", "交付物", "目标"]) {
    for (const s of fields[where]) {
      if (typeof s !== "string") continue;
      for (const x of s.matchAll(/([\d,]+)\s*积分/g)) {
        const n = Number(x[1].replace(/,/g, ""));
        if (n !== m.points) { ptOk = false; check(`${label} ${where}「${n} 积分」`, false, `与实测 ${m.points} 积分不符`); }
      }
    }
  }
  if (ptOk) check(`${label} 积分声明`, true, `${m.points} 积分`);
}

// ─── 2. 计费常量与文本口径 ────────────────────────────────────────────────
console.log("\n== 计费常量与文本口径 ==");
const freeChars = C.REG_BONUS_POINTS / C.POINTS_PER_1000_CHARS * 1000;
// description 里的"约 N 字"可以直接写字数，也可以写成"N 万字" —— 两种口径都接受，
// 只要能由 REG_BONUS_POINTS 推出来（2026-10-19 礼包 500→200 后，200 积分 = 4000 字，
// 写成"约 0.4 万字"绕口，所以改用字数表述）。
check(
  `注册赠送可合成字数与 description 一致`,
  C.LANDING_DESCRIPTION.includes(`约 ${freeChars} 字`) ||
    C.LANDING_DESCRIPTION.includes(`约 ${freeChars / 10000} 万字`),
  `${C.REG_BONUS_POINTS} 积分 / ${C.POINTS_PER_1000_CHARS} 积分每千字 = ${freeChars} 字；`
  + `description 里写的需含「约 ${freeChars} 字」或「约 ${freeChars / 10000} 万字」`
);
check("最低收费小于一个 300 字稿子的花费", C.MIN_CHARGE_POINTS <= 300 * C.POINTS_PER_1000_CHARS / 1000, `最低 ${C.MIN_CHARGE_POINTS} 积分`);
check("定位锚点 1 积分 = ¥0.01", Math.abs(C.YUAN_PER_POINT - 0.01) < 1e-9, `¥${C.YUAN_PER_POINT}`);

// 套餐：价格递增、积分递增、每积分价格不升、bonus 与 points/price 自洽
const packs = C.POINTS_PACKS;
let packOk = true, why = [];
for (let i = 0; i < packs.length; i++) {
  const p = packs[i];
  const expectBonus = p.points - p.priceFen / 100 / C.YUAN_PER_POINT;
  if (Math.abs(expectBonus - p.bonus) > 1e-6) { packOk = false; why.push(`${p.id} 的 bonus=${p.bonus}，但 points-应得=${expectBonus}`); }
  if (i > 0) {
    const prev = packs[i - 1];
    if (p.priceFen <= prev.priceFen) { packOk = false; why.push(`${p.id} 价格没比 ${prev.id} 高`); }
    if (p.points <= prev.points) { packOk = false; why.push(`${p.id} 积分没比 ${prev.id} 多`); }
    if (p.priceFen / p.points > prev.priceFen / prev.points) { packOk = false; why.push(`${p.id} 的每积分单价反而比 ${prev.id} 贵`); }
  }
}
check("套餐自洽（价格递增 / 单价递减 / bonus 与价格对得上）", packOk, why.join("；") || `${packs.length} 档`);

// ─── 3. 站点身份与 head ───────────────────────────────────────────────────
console.log("\n== 站点身份与 head ==");
check("title 含站名", C.LANDING_TITLE.includes(C.SITE_NAME), C.LANDING_TITLE);
check("title 含音色总数", C.LANDING_TITLE.includes(String(C.VOICE_TOTAL_COUNT)), `${C.VOICE_TOTAL_COUNT} 条`);
check("title 长度适配搜索结果（≤ 40 字）", C.LANDING_TITLE.length <= 40, `${C.LANDING_TITLE.length} 字`);
check("description 长度适配摘要（60~160 字）", C.LANDING_DESCRIPTION.length >= 60 && C.LANDING_DESCRIPTION.length <= 160, `${C.LANDING_DESCRIPTION.length} 字`);
check("canonical 是首页", C.LANDING_CANONICAL_PATH === "/", C.LANDING_CANONICAL_PATH);
check("keywords 无重复且非空", new Set(C.LANDING_KEYWORDS).size === C.LANDING_KEYWORDS.length && C.LANDING_KEYWORDS.length > 0, `${C.LANDING_KEYWORDS.length} 个`);
check("og 图与 canonical 同域", C.LANDING_OG_IMAGE.startsWith(C.SITE_URL), C.LANDING_OG_IMAGE);

// ─── 4. FAQ（页面正文与结构数据共用，空壳会被当成凑数）──────────────────
console.log("\n== FAQ ==");
check("FAQ 条目数 ≥ 8", C.FAQ_ITEMS.length >= 8, `${C.FAQ_ITEMS.length} 条`);
check("FAQ 问题不重复", new Set(C.FAQ_ITEMS.map(x => x.q)).size === C.FAQ_ITEMS.length);
const short = C.FAQ_ITEMS.filter(x => x.a.length < 40);
check("每条答案都有实质内容（≥ 40 字）", short.length === 0, short.length ? `太短的：${short.map(x => x.q).join("、")}` : "全部合格");

rmSync(TMP_DIR, { recursive: true, force: true });

console.log(`\n结果：${pass}/${pass + fails.length} 通过${fails.length ? "\n未通过：\n  - " + fails.join("\n  - ") : ""}`);
process.exit(fails.length ? 1 : 0);
