#!/usr/bin/env node
/**
 * 站点域名一致性自检 —— 在 `npm run build` 之前跑。
 *
 * ## 为什么需要这个脚本
 *
 * 换域名时，同一个域名散落在 **4 个文件、6 个位置**，而且它们是**字面量、互相 import 不到**：
 *
 *     1. src/lib/site.ts             SITE_URL            ← 唯一真源（canonical / og:url / JSON-LD 运行时用）
 *     2. index.html                  og:image            ← 社交分享卡片
 *     3. index.html                  JSON-LD Organization.url
 *     4. index.html                  JSON-LD WebSite.url
 *     5. public/robots.txt           Sitemap:            ← 爬虫沿它找站点地图
 *     6. public/sitemap.xml          <loc> × 2           ← 爬虫按它收录
 *
 * 漏改的后果是**静默的**，而且分两种：
 *   - 漏 index.html  ⇒ canonical 已指向新域名，但 og:image 还在旧域名 ⇒ 分享卡片裂图，
 *                       两段 JSON-LD 的 url 与 canonical 不一致 ⇒ 被判「误导性标记」。
 *   - 漏 robots/sitemap ⇒ 新域名已上线，但搜索引擎被引到旧域名抓取，新站收录迟迟不开始。
 *   两种都不会报错、页面也正常打开，所以只能靠机器比对。
 *
 * ## 什么不该报错（刻意白名单）
 *
 *   - `mpay.lizigege.com` 是**支付网关自己的站点**，与产品站不同域。跟着改成 voice
 *     会直接打不出二维码 —— 这是最容易犯的"顺手全局替换"错误。
 *   - `docs.demo@lizigege.com` 之类邮箱不是 URL（没有 http:// 前缀），正则天然不匹配。
 *
 * ## 用法
 *
 *     npm run check:site-url     # 手动
 *     npm run build              # 已挂在此命令之前，失败即中止构建
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SITE_TS = join(FRONTEND_ROOT, "src/lib/site.ts");

/** 参与扫描的范围：构建产物只来自这些地方。 */
const SCAN_DIRS = ["src", "public", "scripts"];
const SCAN_FILES = ["index.html", "vite.config.ts"];
const SKIP_DIRS = new Set(["node_modules", "dist", ".git", ".vite"]);
const TEXT_EXT = new Set([".ts", ".tsx", ".js", ".mjs", ".jsx", ".json", ".html", ".xml", ".txt", ".css", ".md"]);

/**
 * 与本站同属 lizigege.com、但**不是**产品站域名的子域 —— 必须显式登记。
 *
 * 为什么不做成"凡是 lizigege.com 子域都放过"：那样反而失去意义了。
 * 登记制的好处是**新增子域时被迫想一次**「它是不是该跟着产品站一起换」，
 * 而最容易犯的错正是"顺手全局替换" —— 比如把支付网关改成产品站域名，二维码就打不出来了。
 */
const FOREIGN_HOSTS = new Set([
  "mpay.lizigege.com",  // 支付网关站点（改它会打不出二维码）
  "stats.lizigege.com", // umami 自建统计（与产品站分开部署，见 .env.example）
]);

// ─── 1. 取唯一真源 ────────────────────────────────────────────

let siteTsSource;
try {
  siteTsSource = readFileSync(SITE_TS, "utf8");
} catch {
  fail([
    `找不到 ${rel(SITE_TS)} —— 这是 SITE_URL 的唯一真源，脚本无法判断"正确值"是什么。`,
    "",
    "如果你把 SITE_URL 挪到了别的文件，请同步改本脚本的 SITE_TS 常量。",
  ]);
}

const siteUrlMatch = siteTsSource.match(/export\s+const\s+SITE_URL\s*=\s*["']([^"']+)["']/);
if (!siteUrlMatch) {
  fail([
    `没能从 ${rel(SITE_TS)} 里读到 SITE_URL。`,
    "",
    "期望写成：export const SITE_URL = \"https://voice.lizigege.com\";",
    "（脚本按字面量匹配；改成计算表达式或 import 来的值就无法校验了）",
  ]);
}

const SITE_URL = normalize(siteUrlMatch[1]);
if (!/^https:\/\/[a-z0-9.-]+$/.test(SITE_URL)) {
  fail([
    `SITE_URL 格式不对：${siteUrlMatch[1]}`,
    "",
    "要求：https 协议 + 纯域名，不带路径、不带结尾斜杠。",
    "因为 canonical / sitemap 是拿它直接拼路径的，多一个斜杠会拼出 //docs。",
  ]);
}

// ─── 2. 收集待扫文件 ──────────────────────────────────────────

const files = [];
for (const d of SCAN_DIRS) collect(join(FRONTEND_ROOT, d), files);
for (const f of SCAN_FILES) {
  try {
    statSync(join(FRONTEND_ROOT, f));
    files.push(join(FRONTEND_ROOT, f));
  } catch {
    /* 文件不存在就算了，不强制要求 */
  }
}

// ─── 3. 逐行比对 ─────────────────────────────────────────────

const problems = [];
/** 记录真源自己是否真的在 site.ts 里出现过（防"改错了文件但碰巧一致"）。 */
let siteUrlSeenInSource = false;

for (const file of files) {
  const text = readFileSync(file, "utf8");
  const lines = text.split(/\r?\n/);
  lines.forEach((line, i) => {
    // 只抓「带了协议的完整 URL」，因此邮箱、裸域名、注释里提到的路径都不会误报
    const re = /(https?):\/\/([a-z0-9.-]*lizigege\.com)/gi;
    let m;
    while ((m = re.exec(line)) !== null) {
      const found = normalize(`${m[1]}://${m[2]}`);
      const host = m[2].toLowerCase();
      if (FOREIGN_HOSTS.has(host)) continue; // 支付网关等，刻意不比对

      if (file === SITE_TS) siteUrlSeenInSource = true;
      if (found === SITE_URL) continue;

      problems.push({
        file: rel(file),
        line: i + 1,
        found,
        snippet: line.trim().slice(0, 100),
      });
    }
  });
}

// ─── 4. 汇总输出 ─────────────────────────────────────────────

if (problems.length === 0) {
  if (!siteUrlSeenInSource) {
    fail([
      `扫描通过，但 ${rel(SITE_TS)} 里没找到 SITE_URL 的字面量 ${SITE_URL}。`,
      "",
      "这通常意味着真源被改成了变量或函数返回值 —— 那样本脚本就失去了基准。",
      "请保持 SITE_URL 是可直接读出的字符串字面量。",
    ]);
  }
  console.log(`✓ 域名一致：${SITE_URL}（已比对 ${files.length} 个文件）`);
  process.exit(0);
}

fail([
  `域名不一致：${problems.length} 处不指向 ${SITE_URL}`,
  "",
  ...problems.flatMap(p => [
    `  ${p.file}:${p.line}`,
    `    实际    ${p.found}`,
    `    这一行  ${p.snippet}`,
    "",
  ]),
  "怎么改，看这一处是哪种情况：",
  "",
  "  A) 它本来该是产品站域名（4 个固定位置：site.ts / index.html / robots.txt / sitemap.xml）",
  `     ⇒ 改成 ${SITE_URL}，后面的路径原样保留。`,
  "",
  "  B) 它是另一个独立子域（支付网关、统计、图床……）",
  "     ⇒ 去 scripts/check-site-url.mjs 的 FOREIGN_HOSTS 里登记它，并写清用途。",
  "        不要为了「让检查通过」把它改成产品站域名 —— 那会真的把功能改坏。",
  "",
  "改完重跑 npm run build；线上还要同步 .env 的 PAY_PUBLIC_BASE_URL 与反代 301。",
]);

// ─── helpers ────────────────────────────────────────────────

function normalize(u) {
  return u.trim().toLowerCase().replace(/\/+$/, "");
}

function rel(p) {
  return relative(FRONTEND_ROOT, p) || p;
}

function collect(dir, out) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return; // 目录不存在：不报错，扫描范围里可能本来就没有
  }
  for (const e of entries) {
    const full = join(dir, e.name);
    if (e.isDirectory()) {
      if (SKIP_DIRS.has(e.name)) continue;
      collect(full, out);
    } else if (TEXT_EXT.has(e.name.slice(e.name.lastIndexOf(".")))) {
      out.push(full);
    }
  }
}

function fail(lines) {
  console.error(lines.join("\n"));
  process.exit(1);
}
