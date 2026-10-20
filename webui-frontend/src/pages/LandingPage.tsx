/**
 * 产品落地页（`/`）。
 *
 * ## 为什么放在 `/` 而不是某个 /home
 * 首页权重是 SEO 里唯一「不花力气就能拿到」的东西：`/` 是外部链接默认指向的地址，
 * 也是站点唯一会被自然收听的入口。把落地页放在 `/`、把工作台留在 `/podcast`、`/dubbing`，
 * 是这类产品的常规做法。`/` 原本承载的「播客工作台」在导航与按钮里都有明确入口，
 * 而且未登录点进去本来也是跳登录页 —— 挪走它没有损失。
 *
 * ## 两套渲染路径（产物必须一模一样）
 *  1. 构建期静态预渲染 → `dist/landing.html`（由 `scripts/prerender.mjs` 调
 *     `src/landing/entry-server.tsx` 生成），生产由 nginx 的 `location = /` 直接吐这个文件。
 *     爬虫不执行 JS 也能读到全文，这是本页 SEO 的地基。
 *  2. 开发环境 / landing.html 缺失时的回落 → 本组件由浏览器渲染（React），内容一致。
 *
 * ⚠️ 因此本文件及其依赖**不能有模块级的浏览器 API 访问**（`window`/`localStorage`/`document`），
 *    否则 Node 侧预渲染会抛错。跳转请用 `@/lib/router`；`@/lib/auth` 已按这条约束改造
 *    （凭证读取收进 `readToken` 的函数体），所以这里可以安全 import 它来判断登录态。
 *
 * ## SEO 约定
 *  - H1 只有一个，含主词「AI 配音」；每节一个 H2，卡片用 H3，层级不跳。
 *  - 正文里的每个数字都来自 `@/landing/content`，与 JSON-LD、`<head>` 同源（防止「标记与正文不一致」被判误导）。
 *  - 图片全部给了 width/height（防 CLS）与有意义的 alt；非首屏图片 lazy。
 */
import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle, ArrowRight, BookOpen, CheckCircle2, ChevronDown, Coins, FileText, Gift,
  LayoutDashboard, Lightbulb, ListMusic, Mic, Pause, Play, Radio, Sparkles, Ticket, Users,
  Volume2, Wand2, Zap,
} from "lucide-react";
import { useAuth } from "@/lib/auth";
import { navigate } from "@/lib/router";
import { SITE_NAME, SITE_NAME_EN } from "@/lib/site";
import { cn } from "@/lib/utils";
import { usePageSeo } from "@/lib/seo";
import {
  CHECKIN_BONUS_POINTS, EMOTION_LABELS, FAQ_ITEMS, GLOSSARY_BUILTIN_COUNT, LANDING_CANONICAL_PATH,
  LANDING_DESCRIPTION, LANDING_KEYWORDS, LANDING_OG_IMAGE, LANDING_TITLE, MIN_CHARGE_POINTS,
  POINTS_PACKS, POINTS_PER_1000_CHARS, PRACTICE_CASES, REG_BONUS_CHARS, REG_BONUS_POINTS,
  VOICE_BREEZEBLUE_COUNT,
  VOICE_PRESET_COUNT, VOICE_SAMPLES, VOICE_TOTAL_COUNT, YUAN_PER_POINT, caseCost,
} from "@/landing/content";
import { landingJsonLd } from "@/landing/jsonld";
import { fmtYuan } from "@/lib/format";
import { formatChars } from "@/types";

/** 页面级 head（模块级常量：引用稳定，`usePageSeo` 不会反复执行）。 */
const PAGE_SEO = {
  title: LANDING_TITLE,
  description: LANDING_DESCRIPTION,
  canonicalPath: LANDING_CANONICAL_PATH,
  keywords: LANDING_KEYWORDS,
  image: LANDING_OG_IMAGE,
  jsonLd: landingJsonLd(),
};

const IMG = "/landing-assets";
const SHOT = "/docs-assets";

/** 注册入口：带上 `?tab=register`，落地到账户页时直接停在注册表单（少一次点击）。 */
const REGISTER_URL = "/account?tab=register";
const LOGIN_URL = "/account";
/** 已登录用户的落点：单人配音工作台（他回到首页时想做的事是「接着做」，不是「再注册一次」）。 */
const DASHBOARD_URL = "/dubbing";

/**
 * 导航主按钮：未登录的「免费注册」与已登录的「控制台」共用同一套外观与 hover 效果。
 *
 * ⚠️ hover **不能用 `scale`**：那会把整颗按钮（含文字）一起放大，视觉上像「文字在变大/发虚」。
 *    这里跟 Hero 主 CTA（`PrimaryBtn`）完全对齐 —— `transition-all` + hover 时辉光变亮 +
 *    active 轻微回缩，文字本身纹丝不动。
 * ⚠️ 两个按钮是**同一个卡位**（同一时刻只出现一个），共用一份类名才不会各自漂移。
 */
const NAV_PRIMARY =
  "inline-flex items-center gap-1.5 rounded-full bg-gradient-to-r from-emerald-500 to-teal-400 px-4 py-2 text-[0.875rem] font-semibold text-emerald-950 shadow-[0_0_40px_-8px_rgba(16,185,129,0.75)] transition-all hover:shadow-[0_0_52px_-6px_rgba(16,185,129,0.95)] active:scale-[0.985]";

// ─── 通用零件 ───────────────────────────────────────────────

function go(path: string) {
  navigate(path);
}

/** 主按钮（品牌绿：与工作台里的「生成配音」同色，点下去是同一个动作）。 */
function PrimaryBtn({ children, href, size = "md", className }: {
  children: React.ReactNode; href: string; size?: "md" | "lg"; className?: string;
}) {
  return (
    <button
      type="button"
      onClick={() => go(href)}
      className={cn(
        "group inline-flex items-center justify-center gap-2 rounded-full bg-gradient-to-r from-emerald-500 to-teal-400 font-semibold text-emerald-950 shadow-[0_0_40px_-8px_rgba(16,185,129,0.75)] transition-all hover:shadow-[0_0_52px_-6px_rgba(16,185,129,0.95)] active:scale-[0.985]",
        size === "lg" ? "px-8 py-4 text-[1.0625rem]" : "px-6 py-3 text-[0.9375rem]",
        className
      )}
    >
      {children}
      <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
    </button>
  );
}

/** 次级按钮（玻璃质感，描边） */
function GhostBtn({ children, href, size = "md", className }: {
  children: React.ReactNode; href: string; size?: "md" | "lg"; className?: string;
}) {
  return (
    <button
      type="button"
      onClick={() => go(href)}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-full border border-white/15 bg-white/[0.06] font-medium text-zinc-100 backdrop-blur transition-colors hover:border-white/30 hover:bg-white/[0.12]",
        size === "lg" ? "px-7 py-4 text-[1.0625rem]" : "px-5 py-3 text-[0.9375rem]",
        className
      )}
    >
      {children}
    </button>
  );
}

/** 章节头（eyebrow + H2 + 导语） */
function SectionHead({ eyebrow, title, lead, id }: {
  eyebrow: string; title: React.ReactNode; lead?: React.ReactNode; id?: string;
}) {
  return (
    <div className="mx-auto max-w-3xl text-center">
      <p className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.05] px-3.5 py-1.5 text-[0.75rem] font-medium tracking-wide text-cyan-300">
        <Sparkles className="h-3.5 w-3.5" />
        {eyebrow}
      </p>
      <h2 id={id} className="mt-5 scroll-mt-24 text-[1.9rem] font-bold leading-tight tracking-tight text-white sm:text-[2.5rem]">
        {title}
      </h2>
      {lead && <p className="mx-auto mt-4 max-w-2xl text-[0.9375rem] leading-8 text-zinc-400">{lead}</p>}
    </div>
  );
}

/** 产品截图：套一层「浏览器窗口」，暗色页面上亮色截图需要边框与光晕才不像贴错图。 */
function Shot({ src, alt, caption, width = 3200, height = 2000, className, priority = false }: {
  src: string; alt: string; caption?: string; width?: number; height?: number; className?: string; priority?: boolean;
}) {
  return (
    <figure className={cn("min-w-0", className)}>
      <div className="overflow-hidden rounded-2xl border border-white/[0.12] bg-white/[0.04] p-1.5 shadow-[0_30px_80px_-30px_rgba(0,0,0,0.9)] ring-1 ring-white/5">
        <div className="flex items-center gap-1.5 px-2 pb-1.5 pt-1">
          <span className="h-2.5 w-2.5 rounded-full bg-red-400/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-amber-400/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-emerald-400/70" />
          <span className="ml-2 truncate text-[0.6875rem] text-zinc-500">{SITE_NAME} · 工作台</span>
        </div>
        <img
          src={src}
          alt={alt}
          width={width}
          height={height}
          loading={priority ? "eager" : "lazy"}
          decoding="async"
          className="block w-full rounded-xl bg-white"
        />
      </div>
      {caption && <figcaption className="mt-3 text-center text-[0.8125rem] leading-6 text-zinc-500">{caption}</figcaption>}
    </figure>
  );
}

/** 复制按钮：把案例文稿一键塞进剪贴板（落地页最重要的转化动作就是「让他去试」）。 */
function CopyButton({ text, label = "复制文稿" }: { text: string; label?: string }) {
  const [state, setState] = useState<"idle" | "ok" | "err">("idle");
  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setState("ok");
    } catch {
      setState("err");   // 非 https 或权限被拒：如实告知，不假装成功
    }
    window.setTimeout(() => setState("idle"), 2200);
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-[0.75rem] font-medium transition-colors",
        state === "ok"
          ? "border-emerald-400/40 bg-emerald-400/15 text-emerald-300"
          : state === "err"
            ? "border-amber-400/40 bg-amber-400/10 text-amber-300"
            : "border-white/15 bg-white/[0.06] text-zinc-300 hover:border-white/30 hover:text-white"
      )}
    >
      {state === "ok" ? <CheckCircle2 className="h-3.5 w-3.5" /> : <FileText className="h-3.5 w-3.5" />}
      {state === "ok" ? "已复制" : state === "err" ? "复制失败，请手动选中" : label}
    </button>
  );
}

/** 音色试听：原生 audio 藏起来，外面套一个暗色播放器（原生控件在暗色页上会突兀）。 */
function SamplePlayer({ file, label, desc, scene }: {
  file: string; label: string; desc: string; scene: string;
}) {
  const ref = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(0);

  const toggle = () => {
    const a = ref.current;
    if (!a) return;
    if (a.paused) void a.play();
    else a.pause();
  };

  // min-w-0 不能省：卡是栅格子项，默认 min-width:auto，而卡内 p.truncate 是 nowrap，
  // 它的 min-content（约 234px）会把栅格轨道顶宽、在窄屏溢出被裁掉
  return (
    <div className="flex min-w-0 items-center gap-4 rounded-2xl border border-white/10 bg-white/[0.03] p-4 transition-colors hover:border-white/20">
      <audio
        ref={ref}
        src={`${IMG}/samples/${file}`}
        preload="none"
        onPlay={() => {
          // 同时只放一条：不然四条叠在一起就是噪音
          document.querySelectorAll("audio").forEach(el => { if (el !== ref.current) el.pause(); });
          setPlaying(true);
        }}
        onPause={() => setPlaying(false)}
        onEnded={() => { setPlaying(false); setProgress(0); }}
        onTimeUpdate={e => {
          const a = e.currentTarget;
          if (a.duration) setProgress(a.currentTime / a.duration);
        }}
      />
      <button
        type="button"
        onClick={toggle}
        aria-label={`${playing ? "暂停" : "播放"}${label}`}
        className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-violet-500 to-cyan-400 text-white shadow-[0_0_30px_-6px_rgba(139,92,246,0.9)] transition-transform hover:scale-105"
      >
        {playing ? <Pause className="h-5 w-5" /> : <Play className="h-5 w-5 translate-x-[1px]" />}
      </button>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <p className="truncate text-[0.875rem] font-semibold text-zinc-100">{label}</p>
          <span className="shrink-0 rounded-full border border-cyan-400/25 bg-cyan-400/10 px-2 py-0.5 text-[0.6875rem] text-cyan-300">{scene}</span>
        </div>
        <p className="mt-0.5 truncate text-[0.8125rem] text-zinc-400">{desc}</p>
        <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/10">
          <div
            className="h-full rounded-full bg-gradient-to-r from-violet-400 to-cyan-300 transition-[width] duration-200"
            style={{ width: `${Math.round(progress * 100)}%` }}
          />
        </div>
      </div>
    </div>
  );
}

// ─── 导航 ───────────────────────────────────────────────────

const NAV_LINKS = [
  { hash: "workbench", label: "工作台" },
  { hash: "features", label: "核心能力" },
  { hash: "practice", label: "最佳实践" },
  { hash: "pricing", label: "积分与价格" },
  { hash: "faq", label: "常见问题" },
];

function LandingNav() {
  const [open, setOpen] = useState(false);
  // 登录态读自 localStorage（`readToken` 是**同步**的）⇒ 首帧就是准的，不会先渲染
  // 「免费注册」再跳成「控制台」。构建期预渲染的 `landing.html` 里一定是未登录形态
  // （Node 没有 localStorage），所以爬虫看到的始终是注册入口 —— 这正是 SEO 想要的。
  const { token } = useAuth();
  const loggedIn = !!token;
  const toSection = (id: string) => {
    setOpen(false);
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  return (
    <header className="sticky top-0 z-40 border-b border-white/10 bg-[#06060c]/80 backdrop-blur-xl">
      <div className="mx-auto flex h-16 max-w-[1180px] items-center justify-between px-5">
        <a
          href="/"
          onClick={e => { e.preventDefault(); toSection("top"); }}
          className="flex items-center gap-2.5"
        >
          <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 via-fuchsia-500 to-cyan-400">
            <Radio className="h-[1.125rem] w-[1.125rem] text-white" />
          </span>
          <span className="text-[1.0625rem] font-bold tracking-tight text-white">
            {SITE_NAME}
            <span className="ml-1.5 hidden text-[0.75rem] font-normal tracking-normal text-zinc-500 sm:inline">{SITE_NAME_EN}</span>
          </span>
        </a>

        <nav className="hidden items-center gap-1 lg:flex">
          {NAV_LINKS.map(l => (
            <button
              key={l.hash}
              type="button"
              onClick={() => toSection(l.hash)}
              className="rounded-lg px-3.5 py-2 text-[0.875rem] text-zinc-300 transition-colors hover:bg-white/[0.06] hover:text-white"
            >
              {l.label}
            </button>
          ))}
          <a
            href="/docs"
            className="rounded-lg px-3.5 py-2 text-[0.875rem] text-zinc-300 transition-colors hover:bg-white/[0.06] hover:text-white"
          >
            使用文档
          </a>
        </nav>

        <div className="flex items-center gap-2">
          {loggedIn ? (
            <button
              type="button"
              onClick={() => go(DASHBOARD_URL)}
              className={NAV_PRIMARY}
            >
              <LayoutDashboard className="h-4 w-4" />
              控制台
            </button>
          ) : (
            <>
              <button
                type="button"
                onClick={() => go(LOGIN_URL)}
                className="hidden rounded-full px-4 py-2 text-[0.875rem] text-zinc-200 transition-colors hover:text-white sm:inline-flex"
              >
                登录
              </button>
              <button
                type="button"
                onClick={() => go(REGISTER_URL)}
                className={NAV_PRIMARY}
              >
                免费注册
              </button>
            </>
          )}
          <button
            type="button"
            aria-label="展开导航"
            onClick={() => setOpen(v => !v)}
            className="flex h-9 w-9 items-center justify-center rounded-lg border border-white/15 text-zinc-300 lg:hidden"
          >
            <ChevronDown className={cn("h-4 w-4 transition-transform", open && "rotate-180")} />
          </button>
        </div>
      </div>

      {open && (
        <div className="border-t border-white/10 bg-[#06060c]/95 px-5 py-3 lg:hidden">
          {NAV_LINKS.map(l => (
            <button
              key={l.hash}
              type="button"
              onClick={() => toSection(l.hash)}
              className="block w-full rounded-lg px-2 py-2.5 text-left text-[0.9375rem] text-zinc-300 hover:bg-white/[0.06]"
            >
              {l.label}
            </button>
          ))}
          <a href="/docs" className="block w-full rounded-lg px-2 py-2.5 text-left text-[0.9375rem] text-zinc-300 hover:bg-white/[0.06]">
            使用文档
          </a>
          {loggedIn ? (
            <button
              type="button"
              onClick={() => go(DASHBOARD_URL)}
              className="mt-1 flex w-full items-center gap-2 rounded-lg px-2 py-2.5 text-left text-[0.9375rem] font-medium text-emerald-300 hover:bg-white/[0.06]"
            >
              <LayoutDashboard className="h-4 w-4" />
              进入控制台
            </button>
          ) : (
            <button
              type="button"
              onClick={() => go(LOGIN_URL)}
              className="mt-1 block w-full rounded-lg px-2 py-2.5 text-left text-[0.9375rem] text-zinc-300 hover:bg-white/[0.06]"
            >
              登录已有账号
            </button>
          )}
        </div>
      )}
    </header>
  );
}

// ─── Hero ───────────────────────────────────────────────────

function Hero() {
  // 已登录用户看到的 CTA 不该再是「注册」（他已经注册过了）：改成直接进工作台。
  // 同步读 localStorage ⇒ 首帧就准，不会先闪「免费做第一条音频」再变。
  const { token } = useAuth();
  const loggedIn = !!token;
  return (
    <section id="top" className="relative overflow-hidden">
      {/* 主视觉：构建期预渲染的 landing.html 里已 <link rel=preload>，首屏不吃发现延迟 */}
      <img
        src={`${IMG}/hero.webp`}
        alt=""
        aria-hidden="true"
        width={1600}
        height={900}
        className="pointer-events-none absolute inset-0 h-[110%] w-full object-cover opacity-[0.55]"
      />
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(115%_80%_at_50%_-10%,rgba(124,58,237,0.45),transparent_60%)]" />
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-b from-transparent via-[#06060c]/60 to-[#06060c]" />

      <div className="relative mx-auto grid max-w-[1180px] items-center gap-14 px-5 pb-20 pt-16 lg:grid-cols-[1.15fr_1fr] lg:pb-28 lg:pt-24">
        <div className="min-w-0">
          <p className="inline-flex items-center gap-2 rounded-full border border-cyan-400/25 bg-cyan-400/10 px-3.5 py-1.5 text-[0.75rem] font-medium text-cyan-300">
            <span className="flex gap-0.5" aria-hidden="true">
              {[0, 1, 2, 3].map(i => (
                <span
                  key={i}
                  className="landing-eq-bar h-3 w-0.5 rounded-full bg-cyan-300"
                  style={{ animationDelay: `${i * 0.13}s` }}
                />
              ))}
            </span>
            AI 配音工作台 · 浏览器打开就能用
          </p>

          {/* ⚠️ 三行是刻意的：`lg` 下文字列约 600px，11 个汉字 @3.5rem 会折出
              「被听见」这样的孤行。显式断行后每行 ≤6 字，窄屏也稳定保持三段式。
              H1 只有一个，且第一行就是主词「AI 配音工具」—— 层级与关键词都不让步。 */}
          <h1 className="mt-6 text-[2.35rem] font-black leading-[1.14] tracking-tight text-white sm:text-[3.15rem] lg:text-[3.5rem]">
            AI 配音工具
            <br />
            让每一段文字
            <br />
            <span className="bg-gradient-to-r from-violet-400 via-fuchsia-400 to-cyan-300 bg-clip-text text-transparent">都能被听见</span>
          </h1>

          <p className="mt-6 max-w-xl text-[1.0625rem] leading-9 text-zinc-300">
            把文稿贴进来，挑一个音色，点一下生成 —— 直接拿到可下载的音频。
            <strong className="font-semibold text-white">{VOICE_TOTAL_COUNT} 条音色</strong>、
            {EMOTION_LABELS.length} 种情绪、整本书按章连载；
            公众号音频、有声书、双人播客，在这里一次做完。
          </p>

          <div className="mt-9 flex flex-wrap items-center gap-3">
            <PrimaryBtn href={loggedIn ? DASHBOARD_URL : REGISTER_URL} size="lg">
              {loggedIn
                ? <><LayoutDashboard className="h-5 w-5" /> 进入工作台</>
                : <><Mic className="h-5 w-5" /> 免费做第一条音频</>}
            </PrimaryBtn>
            <GhostBtn href="/docs" size="lg">
              <BookOpen className="h-5 w-5" /> 3 分钟看完怎么用
            </GhostBtn>
          </div>

          {/* ⚠️ 这行三个标签是**给未注册的人**看的（送积分 / 能免费用多少 / 注册方式），
              已登录用户看了没有意义，且与上面的「进入工作台」自相矛盾 ⇒ 登录后整行不渲染。 */}
          {!loggedIn && (
            <p className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[0.8125rem] text-zinc-400">
              <span className="inline-flex items-center gap-1.5">
                <Gift className="h-3.5 w-3.5 text-emerald-400" /> 注册即送 {REG_BONUS_POINTS} 积分
              </span>
              <span className="inline-flex items-center gap-1.5">
                <Zap className="h-3.5 w-3.5 text-amber-400" /> 约可免费合成 {formatChars(REG_BONUS_CHARS)}
              </span>
              <span className="inline-flex items-center gap-1.5">
                <CheckCircle2 className="h-3.5 w-3.5 text-cyan-400" /> 邮箱注册，开箱即用
              </span>
            </p>
          )}
        </div>

        <div className="landing-float min-w-0">
          <Shot
            src={`${SHOT}/11-dubbing-overview.webp`}
            alt="播客工坊单人配音工作台界面：左栏音色与术语词汇表，中间文稿画布，右栏任务队列"
            caption="单人配音工作台：左边挑音色、中间写文稿、右边看任务"
            priority
          />
        </div>
      </div>
    </section>
  );
}

/** 关键数字条（每条数字都能在产品里被验证，不放无法核对的「XX 万用户」） */
function StatBar() {
  const stats = [
    { n: String(VOICE_TOTAL_COUNT), unit: "条", label: `${VOICE_PRESET_COUNT} 预设 + ${VOICE_BREEZEBLUE_COUNT} 精选` },
    { n: String(EMOTION_LABELS.length), unit: "种", label: "情绪标记，可精确到句" },
    { n: String(GLOSSARY_BUILTIN_COUNT), unit: "条", label: "内置纠音词条" },
    { n: "2", unit: "万字", label: "书稿模式单章上限" },
  ];
  return (
    <section className="border-y border-white/10 bg-white/[0.02]">
      <div className="mx-auto grid max-w-[1180px] grid-cols-2 gap-x-6 gap-y-8 px-5 py-10 sm:grid-cols-4">
        {stats.map(s => (
          <div key={s.label} className="text-center">
            <p className="bg-gradient-to-b from-white to-zinc-400 bg-clip-text text-[2rem] font-black leading-none text-transparent">
              {s.n}
              <span className="ml-0.5 text-[1rem] font-bold">{s.unit}</span>
            </p>
            <p className="mt-2 text-[0.8125rem] leading-5 text-zinc-400">{s.label}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

// ─── 为什么用它 ─────────────────────────────────────────────

function WhyGrid() {
  const items = [
    { Icon: Mic, t: "不用进录音棚", d: "用自己的声音，也只需要 10 秒参考音频；不想露脸的，就从 375 条音色里挑一条。" },
    { Icon: Zap, t: "不用等别人", d: "300 字的稿子十几秒到一分钟出成品；改一句重出一遍，成本是一顿饭钱的零头。" },
    { Icon: Wand2, t: "不用装软件", d: "浏览器打开即用，稿子自动存草稿，也能存成项目存档随时切回来接着改。" },
  ];
  return (
    <section className="mx-auto max-w-[1180px] px-5 py-20">
      <div className="grid gap-5 md:grid-cols-3">
        {items.map(x => (
          <div key={x.t} className="rounded-3xl border border-white/10 bg-gradient-to-b from-white/[0.06] to-transparent p-7">
            <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-500/25 to-cyan-400/25 ring-1 ring-white/10">
              <x.Icon className="h-5 w-5 text-cyan-300" />
            </span>
            <h3 className="mt-5 text-[1.125rem] font-bold text-white">{x.t}</h3>
            <p className="mt-2.5 text-[0.9375rem] leading-7 text-zinc-400">{x.d}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

// ─── 两个工作台 ─────────────────────────────────────────────

function Workbench() {
  return (
    <section className="mx-auto max-w-[1180px] px-5 py-16">
      <SectionHead
        id="workbench"
        eyebrow="两个工作台，各自独立"
        title="一个人讲完全程，或者两个人一问一答"
        lead="内容里有几个人说话，就用哪个工作台。两边的内容互不覆盖，可以同时开两个项目。"
      />
      <div className="mt-14 grid gap-8 lg:grid-cols-2">
        <div className="rounded-3xl border border-white/10 bg-white/[0.03] p-5">
          <div className="flex items-center gap-2.5 px-2 pb-4">
            <Mic className="h-5 w-5 text-emerald-400" />
            <h3 className="text-[1.125rem] font-bold text-white">单人配音</h3>
            <span className="ml-auto rounded-full border border-white/10 bg-white/[0.06] px-2.5 py-1 text-[0.6875rem] text-zinc-400">一个音色，从头讲到尾</span>
          </div>
          <Shot
            src={`${SHOT}/11-dubbing-overview.webp`}
            alt="单人配音界面：文稿画布支持情绪标记与停顿标记，右侧是任务队列"
          />
          <ul className="mt-5 space-y-2 px-2 pb-1 text-[0.9375rem] leading-7 text-zinc-400">
            <li>· 读书分享、课程讲解、公众号音频、有声书</li>
            <li>· 长文导入后自动进入「书稿模式」，按章拆分、逐章生成</li>
            <li>· 画布只有三条规则：换行分段、【情绪】、[pause:0.5]</li>
          </ul>
        </div>

        <div className="relative overflow-hidden rounded-3xl border border-white/10 bg-white/[0.03] p-5">
          <img
            src={`${IMG}/section-duo.webp`}
            alt=""
            aria-hidden="true"
            width={900}
            height={900}
            loading="lazy"
            decoding="async"
            className="pointer-events-none absolute -right-24 -top-24 h-72 w-72 rounded-full object-cover opacity-30 blur-[2px]"
          />
          <div className="relative flex items-center gap-2.5 px-2 pb-4">
            <Users className="h-5 w-5 text-cyan-300" />
            <h3 className="text-[1.125rem] font-bold text-white">双人播客</h3>
            <span className="ml-auto rounded-full border border-white/10 bg-white/[0.06] px-2.5 py-1 text-[0.6875rem] text-zinc-400">两位主持人，一问一答</span>
          </div>
          <Shot
            src={`${SHOT}/25-podcast-overview.webp`}
            alt="双人播客界面：左栏配置主持人 A 与 B 的音色与语速，中间写对谈脚本，右栏任务队列"
          />
          <ul className="relative mt-5 space-y-2 px-2 pb-1 text-[0.9375rem] leading-7 text-zinc-400">
            <li>· A / B 各自独立音色与语速，略快略慢更像真实聊天</li>
            <li>· 每行开头标明谁在说，漏标会被拦下并告诉你是第几行</li>
            <li>· 换人之间的停顿由服务端统一控制，不用手动加</li>
          </ul>
        </div>
      </div>
    </section>
  );
}

// ─── 音色横幅 ───────────────────────────────────────────────

function VoicesBanner() {
  return (
    <section className="mx-auto max-w-[1180px] px-5 py-16">
      <div className="relative overflow-hidden rounded-[2rem] border border-white/10 bg-gradient-to-br from-violet-600/15 via-transparent to-cyan-500/15">
        <img
          src={`${IMG}/section-voices.webp`}
          alt=""
          aria-hidden="true"
          width={900}
          height={900}
          loading="lazy"
          decoding="async"
          className="pointer-events-none absolute inset-y-0 right-0 h-full w-1/2 object-cover opacity-35"
        />
        <div className="relative grid items-center gap-10 p-8 lg:grid-cols-2 lg:p-12">
          <div>
            <p className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.06] px-3 py-1 text-[0.75rem] text-violet-200">
              <Volume2 className="h-3.5 w-3.5" /> 音色库
            </p>
            <h2 className="mt-4 text-[1.75rem] font-bold leading-tight text-white sm:text-[2.125rem]">
              {VOICE_TOTAL_COUNT} 条音色，
              <br />
              一条条都写着它适合什么
            </h2>
            <p className="mt-4 text-[0.9375rem] leading-8 text-zinc-300">
              内置 {VOICE_PRESET_COUNT} 条预设，加上 {VOICE_BREEZEBLUE_COUNT} 条 BreezeBlue 精选。
              每条都带中文描述 ——「贴耳气声、语速极慢，适合冥想引导」这种，比名字靠得住得多。
              按性别 / 年龄 / 分类筛，或者直接搜关键词，常听的可以收藏。
            </p>
            <p className="mt-4 text-[0.875rem] leading-7 text-zinc-400">
              想用自己的声音，上传一段 10 秒的参考音频就行（安静环境、只有一个人在说话，像的程度最高）。
            </p>
          </div>
          <Shot
            src={`${SHOT}/12-voice-picker.webp`}
            alt="音色库弹窗：左栏按性别、年龄、分类筛选，中间是带中文描述的 375 条音色列表，可试听与收藏"
            width={2040}
            height={1724}
          />
        </div>
      </div>
    </section>
  );
}

// ─── 核心能力 ───────────────────────────────────────────────

function Features() {
  const items = [
    { Icon: Pause, t: "情绪与停顿，精确到句", d: `${EMOTION_LABELS.map(e => `【${e}】`).join("")} ${EMOTION_LABELS.length} 种情绪，从标记处生效到行尾；想只覆盖半句就插一个【/】。[pause:0.5] 能把停顿定到 0.05~5 秒 —— 留白比提速更能打动人。` },
    { Icon: FileText, t: "术语词汇表，专治读错字", d: `内置 ${GLOSSARY_BUILTIN_COUNT} 条多音字、成语与常见译名字条。原词读错就加一条同音替换，合成时临时生效，不改你的原文。` },
    { Icon: ListMusic, t: "任务队列，看得见进度", d: "排队中可拖拽排序，能批量暂停与恢复；完成后直接试听、下载。合成失败或取消，预扣积分自动退回。" },
  ];
  return (
    <section className="mx-auto max-w-[1180px] px-5 py-16">
      <SectionHead
        id="features"
        eyebrow="核心能力"
        title="别人替你念，细节你自己定"
        lead="从音色、情绪到停顿与读音，每一处都能调；调不动的地方，我们给了确定的规则而不是玄学。"
      />

      <div className="mt-14 grid gap-5 lg:grid-cols-6">
        {/* 音色库（大卡） */}
        <div className="relative overflow-hidden rounded-3xl border border-white/10 bg-white/[0.03] p-7 lg:col-span-6">
          <div className="grid gap-8 lg:grid-cols-[1.1fr_1fr] lg:items-center">
            <div>
              <h3 className="text-[1.25rem] font-bold text-white">{VOICE_TOTAL_COUNT} 条音色，挑到合适的为止</h3>
              <p className="mt-3 text-[0.9375rem] leading-8 text-zinc-400">
                {VOICE_PRESET_COUNT} 条内置预设 + {VOICE_BREEZEBLUE_COUNT} 条 BreezeBlue 精选，每条都有中文描述与试听。
                也能上传你自己的参考音频，让机器念出你的声音。
              </p>
              <div className="mt-5 flex flex-wrap gap-2 text-[0.75rem]">
                {["男声 / 女声", "青年 / 中年 / 老年", "播客 / 讲述 / 角色", "收藏常用", "搜关键词"].map(t => (
                  <span key={t} className="rounded-full border border-white/[0.12] bg-white/[0.06] px-3 py-1 text-zinc-300">{t}</span>
                ))}
              </div>
            </div>
            <Shot
              src={`${SHOT}/12-voice-picker.webp`}
              alt="音色库界面：可按性别、年龄与分类筛选，每条音色带中文描述"
              width={2040}
              height={1724}
            />
          </div>
        </div>

        {/* 书稿模式（大卡） */}
        <div className="relative overflow-hidden rounded-3xl border border-white/10 bg-white/[0.03] p-7 lg:col-span-6">
          <img
            src={`${IMG}/section-book.webp`}
            alt=""
            aria-hidden="true"
            width={900}
            height={900}
            loading="lazy"
            decoding="async"
            className="pointer-events-none absolute -bottom-28 -right-16 h-80 w-80 rounded-full object-cover opacity-30"
          />
          <div className="relative grid gap-8 lg:grid-cols-[1.1fr_1fr] lg:items-center">
            <div>
              <h3 className="text-[1.25rem] font-bold text-white">书稿模式：一整本书，按章交付</h3>
              <p className="mt-3 text-[0.9375rem] leading-8 text-zinc-400">
                导入 .docx / .pdf / .txt / .md（单文件 ≤ 20MB），自动识别「第 X 章」「# 标题」「Chapter N」切成章节。
                单章上限默认 2 万字，一章一个任务、各扣各的积分 —— 某一章读错，只重做那一章。
              </p>
              <p className="mt-3 text-[0.9375rem] leading-8 text-zinc-400">
                切章结果会先摊开给你核对（改标题、合并被误切开的短章、取消不想做的章），确认之后才开始花钱。
              </p>
            </div>
            <Shot
              src={`${SHOT}/20-book-mode.webp`}
              alt="书稿模式界面：左栏是章节目录，可勾选批量生成，画布显示当前章节"
            />
          </div>
        </div>

        {items.map(x => (
          <div key={x.t} className="rounded-3xl border border-white/10 bg-white/[0.03] p-7 lg:col-span-2">
            <span className="flex h-11 w-11 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-500/25 to-cyan-400/25 ring-1 ring-white/10">
              <x.Icon className="h-5 w-5 text-violet-200" />
            </span>
            <h3 className="mt-5 text-[1.0625rem] font-bold text-white">{x.t}</h3>
            <p className="mt-2.5 text-[0.9375rem] leading-7 text-zinc-400">{x.d}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

// ─── 试听 ───────────────────────────────────────────────────

function Samples() {
  return (
    <section className="mx-auto max-w-[1180px] px-5 py-16">
      <div className="rounded-[2rem] border border-white/10 bg-white/[0.03] p-8 lg:p-12">
        <div className="grid gap-10 lg:grid-cols-[0.85fr_1fr] lg:items-center">
          <div>
            <p className="inline-flex items-center gap-1.5 rounded-full border border-white/10 bg-white/[0.06] px-3 py-1 text-[0.75rem] text-cyan-300">
              <Volume2 className="h-3.5 w-3.5" /> 先听听声音
            </p>
            <h2 className="mt-4 text-[1.75rem] font-bold leading-tight text-white sm:text-[2.125rem]">
              与其看介绍，
              <br />
              不如先听 18 秒
            </h2>
            <p className="mt-4 text-[0.9375rem] leading-8 text-zinc-400">
              下面四条是平台自有音色的<strong className="text-zinc-200">参考音频片段</strong>（各截前 18 秒）。
              它们用来判断「这个声音像不像你要的」；最终效果还会受语速、情绪与停顿影响。
            </p>
            <p className="mt-4 text-[0.875rem] leading-7 text-zinc-500">
              正式使用时可以从 {VOICE_TOTAL_COUNT} 条里逐个试听，也能上传你自己的声音。
            </p>
          </div>
          {/* min-w-0：本列是外层栅格子项，不加则被内容的 min-content 顶宽（窄屏溢出） */}
          <div className="grid min-w-0 gap-3">
            {VOICE_SAMPLES.map(s => (
              <SamplePlayer key={s.file} file={s.file} label={s.label} desc={s.desc} scene={s.scene} />
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

// ─── 最佳实践 ───────────────────────────────────────────────

function Practice() {
  return (
    <section className="mx-auto max-w-[1180px] px-5 py-16">
      <SectionHead
        id="practice"
        eyebrow="最佳实践 · 照着做就行"
        title="跟着做一个，你就学会了"
        lead="三个案例，从最简单到最完整。每个都给了可以直接复制的文稿和确定的参数 —— 跟着点完一遍，你就拿到了一条成品音频。"
      />

      <div className="mt-14 space-y-8">
        {PRACTICE_CASES.map((c, i) => {
          const cost = caseCost(c);
          return (
            <article
              key={c.id}
              className="overflow-hidden rounded-[2rem] border border-white/10 bg-gradient-to-b from-white/[0.05] to-transparent"
            >
              <div className="flex flex-wrap items-center gap-3 border-b border-white/10 px-7 py-5">
                <span className="rounded-full bg-gradient-to-r from-violet-500 to-cyan-400 px-3 py-1 text-[0.75rem] font-semibold text-white">
                  {c.tag}
                </span>
                <h3 className="text-[1.25rem] font-bold text-white">{c.title}</h3>
                <span className="ml-auto text-[0.8125rem] text-zinc-500">案例 {i + 1} / {PRACTICE_CASES.length}</span>
              </div>

              <div className="px-7 py-6">
                <p className="max-w-3xl text-[0.9375rem] leading-8 text-zinc-300">
                  <strong className="text-white">目标：</strong>{c.goal}
                </p>

                <div className="mt-5 flex flex-wrap gap-2 text-[0.8125rem]">
                  <span className="rounded-lg border border-white/[0.12] bg-white/[0.06] px-3 py-1.5 text-zinc-300">工作台：{c.workspace}</span>
                  <span className="rounded-lg border border-white/[0.12] bg-white/[0.06] px-3 py-1.5 text-zinc-300">交付物：{c.deliverable}</span>
                  <span className="rounded-lg border border-emerald-400/30 bg-emerald-400/10 px-3 py-1.5 text-emerald-300">
                    本次花费：约 {cost.points} 积分（{cost.yuan}）· 约合 {cost.duration} 音频
                  </span>
                </div>

                <div className="mt-7 grid gap-8 lg:grid-cols-[1fr_1.05fr]">
                  {/* 左：步骤 */}
                  <div>
                    <p className="mb-4 flex items-center gap-2 text-[0.8125rem] font-semibold uppercase tracking-wider text-zinc-500">
                      <ListMusic className="h-3.5 w-3.5" /> 操作步骤
                    </p>
                    <ol className="space-y-3.5">
                      {c.steps.map((s, k) => (
                        <li key={k} className="flex gap-3">
                          <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-emerald-500 to-teal-400 text-[0.75rem] font-bold text-emerald-950">
                            {k + 1}
                          </span>
                          <span className="min-w-0 text-[0.9375rem] leading-7 text-zinc-300">{s}</span>
                        </li>
                      ))}
                    </ol>

                    <div className="mt-6 overflow-hidden rounded-2xl border border-white/10">
                      <table className="w-full border-collapse text-[0.875rem]">
                        <tbody>
                          {c.params.map(([k, v]) => (
                            <tr key={k} className="border-b border-white/[0.06] last:border-0">
                              <th scope="row" className="w-28 bg-white/[0.03] px-4 py-2.5 text-left align-top font-medium text-zinc-400">{k}</th>
                              <td className="px-4 py-2.5 align-top text-zinc-200">{v}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>

                  {/* 右：文稿模板 + 坑 */}
                  <div className="min-w-0">
                    {c.script && (
                      <div className="rounded-2xl border border-white/10 bg-[#0b0b16] p-4">
                        <div className="mb-3 flex items-center justify-between gap-3">
                          <p className="flex items-center gap-2 text-[0.8125rem] font-semibold text-zinc-300">
                            <FileText className="h-3.5 w-3.5 text-cyan-300" />
                            {c.scriptLabel}
                          </p>
                          <CopyButton text={c.script} />
                        </div>
                        <pre className="scrollbar-thin max-h-80 overflow-auto whitespace-pre-wrap break-words text-[0.8125rem] leading-7 text-zinc-300">
                          {c.script}
                        </pre>
                        <p className="mt-3 border-t border-white/10 pt-3 text-[0.75rem] leading-6 text-zinc-500">
                          计费字数 {formatChars(cost.chars)}
                          {c.costBasisNote ? `（${c.costBasisNote}）` : "（【情绪】与 [pause:N] 不计费，与编辑器显示一致）"}
                        </p>
                      </div>
                    )}

                    <div className="mt-5 rounded-2xl border border-amber-400/25 bg-amber-400/[0.07] p-4">
                      <p className="flex items-center gap-2 text-[0.8125rem] font-semibold text-amber-300">
                        <AlertTriangle className="h-3.5 w-3.5" /> 常踩的坑
                      </p>
                      <ul className="mt-2.5 space-y-2">
                        {c.pitfalls.map(p => (
                          <li key={p} className="flex gap-2 text-[0.875rem] leading-7 text-amber-100/80">
                            <span className="mt-2.5 h-1 w-1 shrink-0 rounded-full bg-amber-400/70" />
                            <span className="min-w-0">{p}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </div>
                </div>
              </div>
            </article>
          );
        })}
      </div>

      {/* 收尾自检 */}
      <div className="mt-10 rounded-[2rem] border border-indigo-400/25 bg-indigo-500/[0.08] p-7 lg:p-9">
        <h3 className="flex items-center gap-2.5 text-[1.125rem] font-bold text-white">
          <Lightbulb className="h-5 w-5 text-indigo-300" /> 做完之后的收尾自检
        </h3>
        <p className="mt-2 text-[0.9375rem] leading-7 text-indigo-100/70">
          这三条与工具无关，但决定了成品能不能用。第一次做的时候逐条过一遍：
        </p>
        <ul className="mt-5 grid gap-3 sm:grid-cols-3">
          {[
            "有没有读错的字？→ 加一条术语词条，原文不用改",
            "哪一句语气太平？→ 在行首加【情绪】标记",
            "句与句是不是太赶？→ 句尾补 [pause:0.3]",
          ].map(t => (
            <li key={t} className="rounded-2xl border border-white/10 bg-white/[0.04] p-4 text-[0.875rem] leading-7 text-indigo-100/85">
              {t}
            </li>
          ))}
        </ul>
        <div className="mt-7 flex flex-wrap gap-3">
          <PrimaryBtn href={REGISTER_URL}>
            <Mic className="h-4 w-4" /> 照着案例 A 做第一条
          </PrimaryBtn>
          <GhostBtn href="/docs">
            <BookOpen className="h-4 w-4" /> 看完整使用文档
          </GhostBtn>
        </div>
      </div>
    </section>
  );
}

// ─── 积分与价格 ─────────────────────────────────────────────

function Pricing() {
  const sampleChars = 300;
  const samplePoints = Math.max(MIN_CHARGE_POINTS, Math.ceil((sampleChars * POINTS_PER_1000_CHARS) / 1000));
  return (
    <section className="mx-auto max-w-[1180px] px-5 py-16">
      <SectionHead
        id="pricing"
        eyebrow="积分与价格"
        title="按字数付费，不订阅，不用按月交钱"
        lead={`${POINTS_PER_1000_CHARS} 积分 / 千字，1 积分 = ¥${YUAN_PER_POINT.toFixed(2)}；不足 ${MIN_CHARGE_POINTS} 积分按 ${MIN_CHARGE_POINTS} 积分收。1000 积分约可合成 2 万字、约 1 小时音频。`}
      />

      <div className="mt-14 grid gap-5 lg:grid-cols-3">
        <div className="rounded-3xl border border-emerald-400/25 bg-emerald-400/[0.07] p-7">
          <Gift className="h-6 w-6 text-emerald-300" />
          <h3 className="mt-4 text-[1.125rem] font-bold text-white">白拿的部分</h3>
          <ul className="mt-4 space-y-3 text-[0.9375rem] leading-7 text-emerald-100/85">
            <li>· 注册即送 <strong className="text-white">{REG_BONUS_POINTS} 积分</strong>，约可合成 {formatChars(REG_BONUS_CHARS)}</li>
            <li>· 每日签到 +{CHECKIN_BONUS_POINTS} 积分</li>
            <li>· 兑换码按面额入账</li>
          </ul>
        </div>

        <div className="rounded-3xl border border-white/10 bg-white/[0.03] p-7">
          <Coins className="h-6 w-6 text-cyan-300" />
          <h3 className="mt-4 text-[1.125rem] font-bold text-white">怎么估算这一条要多少钱</h3>
          <ul className="mt-4 space-y-3 text-[0.9375rem] leading-7 text-zinc-300">
            <li>· {sampleChars} 字的稿子 ≈ <strong className="text-white">{samplePoints} 积分</strong>（{fmtYuan(samplePoints)}）</li>
            <li>· 1 万字 ≈ 500 积分（¥5），约 33 分钟音频</li>
            <li>· 一段 60 秒的音频 ≈ 300 字 ≈ 15 积分（¥0.15）</li>
          </ul>
          <p className="mt-4 text-[0.8125rem] leading-6 text-zinc-500">
            画布右上角会实时显示本次字数与预估积分，提交前就能看到。
          </p>
        </div>

        <div className="rounded-3xl border border-white/10 bg-white/[0.03] p-7">
          <Ticket className="h-6 w-6 text-violet-300" />
          <h3 className="mt-4 text-[1.125rem] font-bold text-white">退费规则</h3>
          <ul className="mt-4 space-y-3 text-[0.9375rem] leading-7 text-zinc-300">
            <li>· 合成失败或被取消，预扣积分自动退回</li>
            <li>· 退款在「积分记录」里可见，状态是「退款」</li>
            <li>· 点 ↻ 重做算一次新任务，会重新扣费</li>
          </ul>
        </div>
      </div>

      <div className="mt-8 grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
        {POINTS_PACKS.map(p => (
          <div key={p.id} className="flex flex-col rounded-3xl border border-white/10 bg-gradient-to-b from-white/[0.07] to-transparent p-6">
            <p className="text-[0.8125rem] text-zinc-400">{p.name}</p>
            <p className="mt-2 text-[2rem] font-black leading-none text-white">{fmtYuan(p.priceFen)}</p>
            <p className="mt-3 text-[0.9375rem] text-zinc-300">
              {p.points} 积分
              {p.bonus > 0 && <span className="ml-2 rounded-full bg-cyan-400/15 px-2 py-0.5 text-[0.6875rem] text-cyan-300">多送 {p.bonus}</span>}
            </p>
            <p className="mt-1.5 text-[0.8125rem] leading-6 text-zinc-500">{p.note}</p>
            <button
              type="button"
              onClick={() => go(LOGIN_URL)}
              className="mt-5 w-full rounded-full border border-white/15 bg-white/[0.06] py-2.5 text-[0.875rem] font-medium text-zinc-100 transition-colors hover:border-white/30 hover:bg-white/[0.12]"
            >
              购买（登录后结算）
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}

// ─── 适合谁用 ───────────────────────────────────────────────

function Audience() {
  const items = [
    { t: "公众号 / 视频号作者", d: "把文章变成音频版，多一个分发渠道；稿子改完直接重出，不用重新约人录音。" },
    { t: "有声书 / 小说连载", d: "整本按章拆分，一章一个任务；读错的那一章单独重做，前面已经完成的不用返工。" },
    { t: "课程与知识付费", d: "课件、讲义、专栏内容批量转音频，一套音色与语速设定可以一直复用。" },
    { t: "播客主理人", d: "双人对谈不用约人、不用对时间；两位主持人的音色与语速都可以固定下来。" },
  ];
  return (
    <section className="mx-auto max-w-[1180px] px-5 py-16">
      <SectionHead
        eyebrow="适合谁用"
        title="这些人的日常，就是把文字变成声音"
      />
      <div className="mt-12 grid gap-5 sm:grid-cols-2">
        {items.map(x => (
          <div key={x.t} className="rounded-3xl border border-white/10 bg-white/[0.03] p-6">
            <h3 className="text-[1.0625rem] font-bold text-white">{x.t}</h3>
            <p className="mt-2.5 text-[0.9375rem] leading-7 text-zinc-400">{x.d}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

// ─── FAQ ────────────────────────────────────────────────────
// 用原生 <details>：不执行 JS 也能展开（静态预渲染的 HTML 里就是可用的），
// 同时保证「页面可见的问答」与 JSON-LD 的 FAQPage 逐条一致。

function Faq() {
  return (
    <section className="mx-auto max-w-[1180px] px-5 py-16">
      <SectionHead
        id="faq"
        eyebrow="常见问题"
        title="动手之前，先看这几条"
        lead="下面这些答案都能在产品里被验证；如果还有没覆盖到的，使用文档里写得更细。"
      />
      <div className="mx-auto mt-12 max-w-3xl space-y-3">
        {FAQ_ITEMS.map((f, i) => (
          <details key={f.q} className="group rounded-2xl border border-white/10 bg-white/[0.03] open:border-white/20 open:bg-white/[0.05]">
            <summary className="flex cursor-pointer list-none items-center gap-3 px-5 py-4 text-[0.9375rem] font-medium text-zinc-100 marker:hidden">
              <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded bg-white/10 text-[0.6875rem] text-cyan-300">{i + 1}</span>
              <span className="min-w-0 flex-1">{f.q}</span>
              <ChevronDown className="h-4 w-4 shrink-0 text-zinc-500 transition-transform group-open:rotate-180" />
            </summary>
            <p className="px-5 pb-5 pl-[3.25rem] text-[0.9375rem] leading-8 text-zinc-400">{f.a}</p>
          </details>
        ))}
      </div>
    </section>
  );
}

// ─── 结尾 CTA + 页脚 ────────────────────────────────────────

function FinalCta() {
  const { token } = useAuth();
  const loggedIn = !!token;
  return (
    <section className="relative mx-auto max-w-[1180px] px-5 py-20">
      <div className="relative overflow-hidden rounded-[2.5rem] border border-white/[0.12] bg-gradient-to-br from-violet-600/25 via-[#0b0b16] to-cyan-500/20 px-8 py-14 text-center lg:px-16">
        <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(70%_60%_at_50%_0%,rgba(16,185,129,0.28),transparent_70%)]" />
        <div className="relative">
          <h2 className="text-[2rem] font-black leading-tight tracking-tight text-white sm:text-[2.5rem]">
            现在去做你的第一条音频
          </h2>
          <p className="mx-auto mt-4 max-w-2xl text-[1rem] leading-8 text-zinc-300">
            {loggedIn ? (
              <>
                回到工作台接着做；忘了怎么用就翻
                <a href="/docs" className="mx-1 text-emerald-300 underline decoration-dotted underline-offset-4">
                  产品使用文档
                </a>
                ，或者从
                <button
                  type="button"
                  onClick={() => document.getElementById("practice")?.scrollIntoView({ behavior: "smooth" })}
                  className="mx-1 text-emerald-300 underline decoration-dotted underline-offset-4"
                >
                  最佳实践里的案例 A
                </button>
                再走一遍。
              </>
            ) : (
              <>
                注册只要邮箱和验证码，{REG_BONUS_POINTS} 积分到账后就能开始；照着
                <button
                  type="button"
                  onClick={() => document.getElementById("practice")?.scrollIntoView({ behavior: "smooth" })}
                  className="mx-1 text-emerald-300 underline decoration-dotted underline-offset-4"
                >
                  最佳实践里的案例 A
                </button>
                走一遍，你会拿到一条真的能发出去的音频。
              </>
            )}
          </p>
          <div className="mt-9 flex flex-wrap items-center justify-center gap-3">
            {/* 已登录时主副按钮不能再都是「进入工作台」（两颗相邻的同名按钮是坏 UI），
                所以副按钮换成登录后仍然有用的「查看使用文档」——与 Hero 的副 CTA 同款。 */}
            <PrimaryBtn href={loggedIn ? DASHBOARD_URL : REGISTER_URL} size="lg">
              {loggedIn
                ? <><LayoutDashboard className="h-5 w-5" /> 进入工作台</>
                : <><Mic className="h-5 w-5" /> 免费注册，开始第一条</>}
            </PrimaryBtn>
            <GhostBtn href={loggedIn ? "/docs" : LOGIN_URL} size="lg">
              {loggedIn ? "查看使用文档" : "我已有账号，去登录"}
            </GhostBtn>
          </div>
          {/* 这行说的是「注册之后会自动进工作台」，对已登录用户毫无意义 ⇒ 登录后不渲染。 */}
          {!loggedIn && (
            <p className="mt-5 text-[0.8125rem] text-zinc-400">已有账号登录后直接进工作台</p>
          )}
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-white/10 bg-[#05050a]">
      <div className="mx-auto grid max-w-[1180px] gap-10 px-5 py-14 sm:grid-cols-2 lg:grid-cols-4">
        <div className="lg:col-span-2">
          <div className="flex items-center gap-2.5">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-violet-500 via-fuchsia-500 to-cyan-400">
              <Radio className="h-[1.125rem] w-[1.125rem] text-white" />
            </span>
            <span className="text-[1.0625rem] font-bold text-white">{SITE_NAME}</span>
          </div>
          <p className="mt-4 max-w-md text-[0.875rem] leading-7 text-zinc-500">
            一个在浏览器里跑的 AI 配音工作台：把文稿变成可下载的音频，用来做公众号音频、有声书与双人播客。
          </p>
          <p className="mt-4 text-[0.8125rem] leading-6 text-zinc-600">
            用他人的声音素材做配音前，请先获得对方同意；文稿内容的版权由使用者自行确认。
          </p>
        </div>

        <div>
          <p className="text-[0.8125rem] font-semibold text-zinc-300">产品</p>
          <ul className="mt-3.5 space-y-2.5 text-[0.875rem] text-zinc-500">
            <li><a href="#workbench" className="transition-colors hover:text-zinc-200">两个工作台</a></li>
            <li><a href="#features" className="transition-colors hover:text-zinc-200">核心能力</a></li>
            <li><a href="#practice" className="transition-colors hover:text-zinc-200">最佳实践</a></li>
            <li><a href="#pricing" className="transition-colors hover:text-zinc-200">积分与价格</a></li>
          </ul>
        </div>

        <div>
          <p className="text-[0.8125rem] font-semibold text-zinc-300">开始使用</p>
          <ul className="mt-3.5 space-y-2.5 text-[0.875rem] text-zinc-500">
            <li><button type="button" onClick={() => go(REGISTER_URL)} className="transition-colors hover:text-zinc-200">免费注册</button></li>
            <li><button type="button" onClick={() => go(LOGIN_URL)} className="transition-colors hover:text-zinc-200">登录已有账号</button></li>
            <li><a href="/docs" className="transition-colors hover:text-zinc-200">使用文档</a></li>
            <li><a href="#faq" className="transition-colors hover:text-zinc-200">常见问题</a></li>
          </ul>
        </div>
      </div>

      <div className="border-t border-white/5">
        <div className="mx-auto flex max-w-[1180px] flex-col gap-2 px-5 py-6 text-[0.8125rem] text-zinc-600 sm:flex-row sm:items-center sm:justify-between">
          <p>© {new Date().getFullYear()} {SITE_NAME} · {SITE_NAME_EN}</p>
          {/* TODO(合规)：取得 ICP 备案号后在下面这一行补上，并按需加《用户协议》《隐私政策》链接。
              现在刻意留空而不是放占位文字 —— 页面上出现假备案号比没有更糟。 */}
          <p className="flex gap-4">
            <a href={REGISTER_URL} className="transition-colors hover:text-zinc-300">注册</a>
            <a href="/docs" className="transition-colors hover:text-zinc-300">使用文档</a>
          </p>
        </div>
      </div>
    </footer>
  );
}

// ─── 页面 ───────────────────────────────────────────────────

export default function LandingPage() {
  usePageSeo(PAGE_SEO);

  // 静态预渲染的 HTML 里没有 iOS 的滚动容器问题；这里只做一件事：
  // 从其它路由切回 `/` 时把滚动位置放回顶部（SPA 切换不会自动重置）。
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "auto" });
  }, []);

  return (
    <div className="min-h-screen overflow-x-hidden bg-[#06060c] text-zinc-100 antialiased">
      <LandingNav />
      <main>
        <Hero />
        <StatBar />
        <WhyGrid />
        <Workbench />
        <VoicesBanner />
        <Features />
        <Samples />
        <Practice />
        <Pricing />
        <Audience />
        <Faq />
        <FinalCta />
      </main>
      <Footer />
    </div>
  );
}
