/**
 * 产品使用文档页（/docs）。
 *
 * 定位：给**第一次使用**的人看的操作手册 —— 目标不是"讲全所有功能"，而是
 * 让一个没接触过 AI 配音的人照着走一遍就能拿到成品音频。所以：
 *   - 结构按「先跑通一遍（第 1 节）→ 再逐项展开（第 3~9 节）→ 跟做案例（第 10 节）」
 *   - 每一处操作都配**真实界面截图**（webui-frontend/public/docs/，由真机操作截取）
 *   - 只写"能照着做"的话；不确定的边界一律写进第 11 节 FAQ
 *
 * 与产品的关系：本页**不依赖登录**（未登录也能读），顶栏复用全局 Header。
 * 入口见 UserMenu（「个人中心 / 积分」下一行）与 AccountPage 的积分卡片。
 */
import { createContext, useContext, useEffect, useState } from "react";
import {
  AlertTriangle, ArrowLeft, BookOpen, CheckCircle2, ChevronRight, Coins, FileText,
  Info, Lightbulb, ListVideo, Mic, Sparkles, Users, X, ZoomIn,
} from "lucide-react";
import { Header } from "@/components/Header";
import { navigate } from "@/lib/auth";
import { usePageSeo } from "@/lib/seo";
import { cn } from "@/lib/utils";
// 赠送积分的**唯一口径**在 landing/content.ts（PR 内改值时只改那一处，
// 否则文档页与落地页会各说一个数字）。这里只取来渲染。
import { REG_BONUS_CHARS, REG_BONUS_POINTS } from "@/landing/content";

/** 本页 head（模块级常量：引用稳定）。文档页是**免登录**的长尾内容页，
 *  「AI配音教程 / 怎么用」这类词的落点就在这，所以给它独立的 title 与 canonical，
 *  避免与落地页共用同一个 title 自己跟自己抢词。 */
const PAGE_SEO = {
  title: "产品使用文档 - 播客工坊 AI 配音工具（含实操截图）",
  description:
    "播客工坊完整使用文档：注册登录、单人配音、书稿模式、双人播客、音色管理与术语词汇表，每一步都配真实界面截图，照着做就能做出第一条音频。",
  canonicalPath: "/docs",
  keywords: ["AI配音教程", "文字转语音怎么用", "播客工坊使用文档", "有声书制作教程", "双人播客怎么录"],
};

/** 目录（同时也是 scrollspy 的监听目标，顺序即页面顺序） */
const SECTIONS = [
  { id: "before", label: "开始之前" },
  { id: "quickstart", label: "三分钟上手" },
  { id: "ui", label: "界面总览" },
  { id: "mono", label: "单人配音" },
  { id: "book", label: "书稿模式（整本书）" },
  { id: "podcast", label: "双人播客" },
  { id: "voices", label: "音色库与音色管理" },
  { id: "glossary", label: "读音不对怎么办" },
  { id: "points", label: "积分、充值与订单" },
  { id: "queue", label: "任务队列" },
  { id: "practice", label: "最佳实践（照着做）" },
  { id: "faq", label: "常见问题" },
];

// ⚠️ 目录名**不能叫 docs**：nginx 是 `try_files $uri $uri/ /index.html`，
// 若 public/ 下存在真实的 docs/ 目录，请求 /docs 会命中该目录 → 无 index.html → 403，
// SPA 路由就永远进不来。换一个与路由不重名的前缀即可。
const IMG = "/docs-assets";

// ─── 展示层小组件 ──────────────────────────────────────────

const ZoomCtx = createContext<(src: string) => void>(() => {});

/** 截图：默认整宽，可点开看大图（宽图在小屏上必然看不清细节）。 */
function Shot({ src, caption, className }: { src: string; caption?: string; className?: string }) {
  const zoom = useContext(ZoomCtx);
  return (
    <figure className={cn("my-4", className)}>
      <button
        type="button"
        onClick={() => zoom(src)}
        className="group relative block w-full overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm transition-shadow hover:shadow-md"
      >
        <img src={src} alt={caption ?? ""} loading="lazy" className="block w-full" />
        <span className="absolute right-2 top-2 inline-flex items-center gap-1 rounded-md bg-gray-900/70 px-2 py-1 text-[0.6875rem] text-white opacity-0 transition-opacity group-hover:opacity-100">
          <ZoomIn className="w-3 h-3" /> 点击放大
        </span>
      </button>
      {caption && <figcaption className="mt-2 text-xs leading-5 text-gray-500">{caption}</figcaption>}
    </figure>
  );
}

/** 章节标题 */
function H2({ id, n, children }: { id: string; n: string; children: React.ReactNode }) {
  return (
    <h2 id={id} className="scroll-mt-24 mt-12 mb-4 flex items-center gap-2.5 border-b border-gray-200 pb-3 text-xl font-bold text-gray-900 first:mt-0">
      <span className="inline-flex h-7 min-w-7 items-center justify-center rounded-lg bg-indigo-600 px-2 text-sm font-semibold text-white">{n}</span>
      {children}
    </h2>
  );
}

function H3({ children }: { children: React.ReactNode }) {
  return <h3 className="mt-8 mb-2.5 text-[1.0625rem] font-semibold text-gray-800">{children}</h3>;
}

function P({ children }: { children: React.ReactNode }) {
  return <p className="my-3 text-sm leading-7 text-gray-700">{children}</p>;
}

function UL({ children }: { children: React.ReactNode }) {
  return <ul className="my-3 space-y-1.5 pl-1 text-sm leading-7 text-gray-700">{children}</ul>;
}

function LI({ children }: { children: React.ReactNode }) {
  return (
    <li className="flex gap-2">
      <ChevronRight className="mt-[0.5rem] h-3.5 w-3.5 shrink-0 text-indigo-400" />
      <span className="min-w-0">{children}</span>
    </li>
  );
}

/** 编号步骤块 */
function Step({ n, title, children }: { n: number; title: string; children?: React.ReactNode }) {
  return (
    <div className="my-4 rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
      <div className="flex items-center gap-2.5">
        <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-emerald-500 text-xs font-semibold text-white">{n}</span>
        <span className="text-sm font-semibold text-gray-800">{title}</span>
      </div>
      {children && <div className="mt-2.5 pl-[2.125rem] text-sm leading-7 text-gray-700">{children}</div>}
    </div>
  );
}

const CALLOUT_STYLE = {
  tip: { box: "border-blue-200 bg-blue-50", icon: "text-blue-500", title: "text-blue-700", body: "text-blue-900/80", Icon: Info, label: "提示" },
  warn: { box: "border-amber-200 bg-amber-50", icon: "text-amber-500", title: "text-amber-700", body: "text-amber-900/80", Icon: AlertTriangle, label: "注意" },
  good: { box: "border-emerald-200 bg-emerald-50", icon: "text-emerald-600", title: "text-emerald-700", body: "text-emerald-900/80", Icon: Lightbulb, label: "小技巧" },
} as const;

function Callout({ tone = "tip", title, children }: { tone?: keyof typeof CALLOUT_STYLE; title?: string; children: React.ReactNode }) {
  const s = CALLOUT_STYLE[tone];
  return (
    <div className={cn("my-4 flex gap-2.5 rounded-xl border p-3.5", s.box)}>
      <s.Icon className={cn("mt-0.5 h-4 w-4 shrink-0", s.icon)} />
      <div className="min-w-0 text-sm leading-6">
        <p className={cn("font-semibold", s.title)}>{title ?? s.label}</p>
        <div className={cn("mt-0.5", s.body)}>{children}</div>
      </div>
    </div>
  );
}

/** 代码/文稿示例块 */
function Code({ children, label }: { children: React.ReactNode; label?: string }) {
  return (
    <div className="my-3">
      {label && <p className="mb-1.5 text-xs font-medium text-gray-500">{label}</p>}
      <pre className="overflow-x-auto rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-[0.8125rem] leading-6 text-gray-700">{children}</pre>
    </div>
  );
}

function Table({ head, rows }: { head: string[]; rows: React.ReactNode[][] }) {
  return (
    <div className="my-4 overflow-x-auto rounded-xl border border-gray-200">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="bg-gray-50 text-left">
            {head.map(h => <th key={h} className="border-b border-gray-200 px-3.5 py-2.5 text-xs font-semibold text-gray-600">{h}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="align-top">
              {r.map((c, j) => <td key={j} className="border-b border-gray-100 px-3.5 py-2.5 text-gray-700 last:border-r-0">{c}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** 键位/按钮的内联样式 */
function K({ children }: { children: React.ReactNode }) {
  return <span className="mx-0.5 inline-flex items-center rounded-md border border-gray-300 bg-gray-50 px-1.5 py-0.5 text-[0.8125rem] font-medium text-gray-700">{children}</span>;
}

// ─── 页面 ──────────────────────────────────────────────────

export default function DocsPage() {
  const [zoom, setZoom] = useState<string | null>(null);
  const [active, setActive] = useState(SECTIONS[0].id);

  usePageSeo(PAGE_SEO);

  // 大图：ESC 关闭 + 打开期间锁滚动
  useEffect(() => {
    if (!zoom) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setZoom(null); };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [zoom]);

  // 目录高亮：谁离顶边最近就算谁
  useEffect(() => {
    const onScroll = () => {
      let cur = SECTIONS[0].id;
      for (const s of SECTIONS) {
        const el = document.getElementById(s.id);
        if (el && el.getBoundingClientRect().top <= 140) cur = s.id;
      }
      setActive(cur);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  /** 返回：本页通常是从用户菜单**新标签页**打开的，能关就关，不能关就回工作台 */
  const goBack = () => {
    if (window.opener) window.close();
    else navigate("/dubbing");
  };

  const toSection = (id: string) => {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <ZoomCtx.Provider value={setZoom}>
      <div className="min-h-screen bg-gray-50">
        <div className="sticky top-0 z-30 border-b border-gray-200 bg-white/95 backdrop-blur">
          <Header name="" onRename={() => {}} showNameInput={false} showTts={false}
            showProjectActions={false} ttsOnline={null} ttsInfo={null} />
        </div>

        <div className="mx-auto flex max-w-[1200px] gap-8 px-5 py-7">
          {/* 左：目录 */}
          <aside className="hidden w-56 shrink-0 lg:block">
            <div className="sticky top-24">
              <p className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-gray-400">
                <BookOpen className="h-3.5 w-3.5" /> 目录
              </p>
              <nav className="space-y-0.5">
                {SECTIONS.map((s, i) => (
                  <button
                    key={s.id}
                    type="button"
                    onClick={() => toSection(s.id)}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[0.8125rem] transition-colors",
                      active === s.id ? "bg-indigo-50 font-medium text-indigo-700" : "text-gray-600 hover:bg-gray-100"
                    )}
                  >
                    <span className={cn("w-4 shrink-0 text-right text-[0.6875rem] tabular-nums", active === s.id ? "text-indigo-400" : "text-gray-400")}>
                      {i + 1}
                    </span>
                    {s.label}
                  </button>
                ))}
              </nav>
              <button
                type="button"
                onClick={goBack}
                className="mt-4 inline-flex items-center gap-1.5 rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 transition-colors hover:bg-gray-50"
              >
                <ArrowLeft className="h-3.5 w-3.5" /> 返回工作台
              </button>
            </div>
          </aside>

          {/* 右：正文 */}
          <main className="min-w-0 flex-1 pb-20">
            {/* 封面 */}
            <div className="rounded-2xl border border-gray-200 bg-white p-6 shadow-sm">
              <div className="flex items-center gap-2 text-xs font-medium text-indigo-600">
                <Sparkles className="h-3.5 w-3.5" /> 产品使用文档 · v1.0
              </div>
              <h1 className="mt-2 text-2xl font-bold text-gray-900">播客工坊：把文字变成音频</h1>
              <p className="mt-3 text-sm leading-7 text-gray-600">
                这份文档写给<strong className="text-gray-800">第一次使用</strong>的人。你不必懂任何技术，
                只要会复制粘贴文字，就能做出可以发布的音频。建议按顺序读：先跟着
                <button type="button" onClick={() => toSection("quickstart")} className="mx-1 text-indigo-600 underline decoration-dotted">三分钟上手</button>
                跑通一遍，再回来看细节；最后照着
                <button type="button" onClick={() => toSection("practice")} className="mx-1 text-indigo-600 underline decoration-dotted">最佳实践</button>
                做一个完整作品。
              </p>
              <div className="mt-4 grid gap-2.5 sm:grid-cols-3">
                {[
                  { Icon: Users, t: "双人播客", d: "一问一答的对谈节目" },
                  { Icon: Mic, t: "单人配音", d: "一个人讲完全程" },
                  { Icon: ListVideo, t: "任务队列", d: "排队、试听、下载" },
                ].map(x => (
                  <div key={x.t} className="flex items-center gap-2.5 rounded-xl border border-gray-200 bg-gray-50/70 px-3 py-2.5">
                    <x.Icon className="h-4 w-4 shrink-0 text-indigo-500" />
                    <div className="min-w-0">
                      <p className="text-[0.8125rem] font-medium text-gray-800">{x.t}</p>
                      <p className="text-[0.6875rem] text-gray-500">{x.d}</p>
                    </div>
                  </div>
                ))}
              </div>
              <button
                type="button"
                onClick={goBack}
                className="mt-4 inline-flex items-center gap-1.5 text-xs text-indigo-600 hover:underline lg:hidden"
              >
                <ArrowLeft className="h-3.5 w-3.5" /> 返回工作台
              </button>
            </div>

            {/* ── 1 开始之前 ───────────────────────────── */}
            <H2 id="before" n="1">开始之前</H2>
            <P>
              播客工坊是一个「文字转语音」的工作台：你把文稿贴进去，选一个音色，点一下生成，就能拿到一条
              可以直接下载的音频 —— 不用录音、不用请人配音、不用装任何软件。
            </P>
            <P>它有两个工作区，操作逻辑完全一样，区别只在「几个人说话」：</P>
            <Table
              head={["工作区", "做的事", "什么时候用"]}
              rows={[
                ["双人播客", "两个角色一问一答的对谈音频", "聊天式节目、访谈、双主播播客"],
                ["单人配音", "一个音色从头讲到尾", "读书分享、课程讲解、公众号音频、有声书"],
              ]}
            />
            <P>另外还有一个<strong>个人中心</strong>，管理你的账号、积分和订单。</P>
            <H3>你需要准备什么</H3>
            <UL>
              <LI>一段文字 —— 自己写、让 AI 写、或者从现成稿子里复制，都行</LI>
              <LI>一台能上网的电脑（手机也能用，但宽屏体验更好）</LI>
            </UL>
            <H3>做一条音频要花多少积分</H3>
            <P>
              按字数计费：<strong>50 积分 / 1000 字</strong>，不足 5 积分按 5 积分收。
              1000 积分大约能做 2 万字，也就是约 1 小时的音频。
            </P>
            <Callout tone="good" title="先别急着充值">
              新用户注册就送 {REG_BONUS_POINTS} 积分（约 {REG_BONUS_CHARS} 字、十几分钟音频），
              够你把整条流程完整跑通一遍。
              日常再用「每日签到」和兑换码慢慢攒。
            </Callout>

            {/* ── 2 三分钟上手 ─────────────────────────── */}
            <H2 id="quickstart" n="2">三分钟上手</H2>
            <P>目标：不读任何说明，先用 3 分钟做出你的第一条音频。</P>

            <Step n={1} title="注册并登录">
              <p>打开首页 → 点右上角 <K>登录</K> → 切到顶部的 <K>注册</K> 标签。</p>
              <ul className="mt-1.5 space-y-1">
                <li>① 填邮箱 → 点 <K>获取验证码</K> → 去邮箱里拿 6 位数字（收不到就看垃圾箱）</li>
                <li>② 设一个至少 6 位的密码（昵称不填也行）</li>
                <li>③ 点 <K>注册（赠送积分）</K>，成功后自动进入单人配音工作台</li>
              </ul>
              <Shot src={`${IMG}/02-register.webp`} caption={`注册页：邮箱 + 验证码 + 密码，三格填完即可。注册成功自动送 ${REG_BONUS_POINTS} 积分。`} />
            </Step>

            <Step n={2} title="看一眼你的积分">
              <p>点右上角你的昵称 → <K>个人中心 / 积分</K>。这里能看到余额、每日签到和兑换码入口。</p>
              <Shot src={`${IMG}/04-account-top.webp`} caption="个人中心顶部：头像、昵称、积分余额一目了然。" />
            </Step>

            <Step n={3} title="写文稿 + 选音色（单人配音页）">
              <p>切到 <K>单人配音</K>。中间大白框就是画布，把文字粘进去（<strong>一行一段</strong>）；左边点 <K>选择音色</K> 挑一个，点音色头像可以试听。</p>
              <Shot src={`${IMG}/11-dubbing-overview.webp`} caption="单人配音工作台：左栏音色与术语表、中间画布、右栏任务队列。" />
            </Step>

            <Step n={4} title="点「生成配音」，然后取回音频">
              <p>点画布右下角的绿色按钮 <K>生成配音</K>，任务会出现在右侧「任务队列」里：排队中 → 合成中 → 完成。完成后点 ▶ 试听、点 ⤓ 下载。</p>
              <Shot src={`${IMG}/23-queue-success.webp`} caption="完成后：任务卡变绿，出现试听▶与下载⤓按钮，还标注了音频时长。" />
            </Step>

            <Callout tone="good" title="到这里你就已经会用了">
              后面的章节是把每一步讲细 —— 导入长文稿、加情绪、修读音、管积分。
              如果你想直接做一个完整作品，跳到最后面的
              <button type="button" onClick={() => toSection("practice")} className="mx-1 text-emerald-700 underline decoration-dotted">第 11 节 最佳实践</button>。
            </Callout>

            {/* ── 3 界面总览 ───────────────────────────── */}
            <H2 id="ui" n="3">界面总览</H2>
            <H3>顶部导航</H3>
            <Shot src={`${IMG}/28-topnav.webp`} caption="顶部一条：左边切换工作区，中间是当前项目名与存档操作，右边是登录状态与用户菜单。" />
            <UL>
              <LI><strong>双人播客 / 单人配音</strong>：两个工作区，点一下即切换（各自的内容互相独立，不会互相覆盖）</LI>
              <LI><strong>中间的项目名</strong>：默认叫「未命名_20261016」（下划线加当天日期），点右边的 ✏ 可以改名</LI>
              <LI><strong>💾（项目名右侧）</strong>：把当前内容保存成一份「项目存档」</LI>
              <LI><strong>⇅（最右）</strong>：打开已保存的项目列表，随时切回来、或删掉不要的存档</LI>
              <LI><strong>右上角</strong>：你的昵称与积分余额；点开是用户菜单</LI>
            </UL>
            <H3>用户菜单</H3>
            <Shot src={`${IMG}/03-usermenu.webp`} caption="用户菜单：个人中心 / 积分、使用文档、退出登录。" />
            <H3>三个页面各自负责什么</H3>
            <Table
              head={["页面", "怎么进", "能做什么"]}
              rows={[
                ["双人播客", "顶部第一个胶囊按钮", "配置两位主持人 → 写对话稿 → 生成对谈音频"],
                ["单人配音", "顶部第二个胶囊按钮", "选一个音色 → 写/导入文稿 → 生成音频；长文可切「书稿模式」逐章生成"],
                ["个人中心", "用户菜单 → 个人中心 / 积分", "看积分、签到、兑换码、购买积分、查订单与积分记录"],
              ]}
            />
            <H3>草稿会自己保存，但「存档」要手动点</H3>
            <P>
              你在画布里写的东西会自动存在浏览器本地，刷新页面、关掉标签页再打开，内容都还在。这叫做<strong>草稿</strong>，只有一份。
              如果你还想留几个版本，就点项目名右边的 💾，把它存成<strong>项目存档</strong>（可以有很多份，点 ⇅ 切换）。
            </P>
            <Callout tone="warn">
              草稿保存在「你这台电脑的这个浏览器」里。换电脑、换浏览器、或者清理浏览器数据，草稿就没了 ——
              重要稿子请顺手点一下 💾 存成项目存档。
            </Callout>

            {/* ── 4 单人配音 ───────────────────────────── */}
            <H2 id="mono" n="4">单人配音：把一篇文章变成音频</H2>
            <P>单人配音是使用频率最高的工作区：一个音色，从头讲到尾。整一块画布就是你的文稿。</P>

            <H3>4.1 画布只有 3 条规则</H3>
            <Table
              head={["写法", "含义", "例子"]}
              rows={[
                [<K>换行</K>, "一行 = 一段，空行会被忽略", "每行写完按一次回车"],
                [<K>【情绪】</K>, "从这一处开始用这种情绪说，到【/】或行尾结束", "【喜悦】今天很开心。 "],
                [<K>[pause:0.5]</K>, "在这里停 0.5 秒（0.05~5 秒）", "……先聊到这里。[pause:0.8]"],
              ]}
            />
            <Code label="文稿示例（可以直接复制试用）">{`【平静】大家好，欢迎来到「小柯读书」，我是小柯。
【喜悦】今天想和你聊聊一本书，叫《认知觉醒》。[pause:0.5]
【平静】很多人以为，只要足够努力，就一定能变好。但真正拉开差距的，是认知。
【惊喜】书里有三个概念让我印象最深：大脑的三重结构、舒适区边缘，还有元认知。
【平静】好了，今天先聊到这里。喜欢的话，记得点个关注，我们下期再见。`}</Code>
            <Shot src={`${IMG}/17-editor-toolbar.webp`} caption="工具条：情绪、停顿、导入都在这里；最右边实时显示字数、段数和这次要花的积分，以及「生成配音」按钮。" />
            <UL>
              <LI>写完的【情绪】和 [pause:0.5] 会显示成彩色小标签，<strong>点标签</strong>可以改内容或删掉</LI>
              <LI>工具条右侧的「330 字 · 8 段 · 约 17 积分」是实时算出来的，提交前就能知道花多少</LI>
            </UL>
            <Callout tone="tip" title="一行别写太长">
              一行（一段）太长时合成时间会明显变长。建议一行控制在
              <strong> 60~120 字</strong>，也就是一口气能读完的长度 —— 顺便也让句间停顿更自然。
            </Callout>

            <H3>4.2 导入现成的稿子</H3>
            <P>不想逐字粘贴？点工具条上的 <K>导入</K>，选文件或直接把文件拖进弹窗：</P>
            <UL>
              <LI>支持 <strong>.doc / .docx / .pdf / .txt / .md</strong>，单个文件不超过 20MB</LI>
              <LI>导入后文字会填进画布，你可以继续改</LI>
              <LI>如果这一篇特别长（比如一整本书），导入后会自动进入「书稿模式」，见下一节</LI>
            </UL>
            <Shot src={`${IMG}/18-import-modal.webp`} caption="导入弹窗：点「选择文件」或直接把文件拖进来。" />

            <H3>4.3 选音色</H3>
            <P>点音色卡上的 <K>选择音色</K>，弹出音色库，里面有三个来源：</P>
            <Table
              head={["来源", "说明"]}
              rows={[
                ["音色库", "375 条精选音色（内置预设 65 条 + BreezeBlue 310 条），可按性别 / 年龄 / 分类筛选，也能搜关键词、收藏常用音色"],
                ["我的音色", "你自己上传的参考音频（见 4.4）"],
                ["收藏", "点音色行里的 ☆，把它放进收藏，下次先筛「收藏」"],
              ]}
            />
            <Shot src={`${IMG}/12-voice-picker.webp`} caption="音色库：左栏是筛选条件，中间是音色列表（带中文音色描述），点行首▶即可试听。" />
            <Callout tone="good" title="挑不准就看描述">
              BreezeBlue 的每条音色都带中文描述，例如「贴耳气声、语速极慢，适合冥想引导」。
              比名字靠得住得多 —— 「静心向导」不点开你猜不到它是气声。
            </Callout>
            <Callout tone="warn" title="试听听到的是参考音频">
              音色试听播放的是这个音色的<strong>原始参考音频</strong>，不是合成结果。
              它的作用是让你判断「像不像你要的声音」，最终效果还会受语速、情绪、停顿影响。
            </Callout>

            <H3>4.4 上传自己的音色（用你自己的声音）</H3>
            <P>想让它念出「你自己的声音」，录一段参考音频上传就行：</P>
            <UL>
              <LI>时长 10 秒 ~ 1 分钟就够，越长越稳但没必要超过 2 分钟</LI>
              <LI>安静环境、只有一个人说话、不要背景音乐</LI>
              <LI>支持 .wav / .mp3 / .flac / .ogg / .webm</LI>
            </UL>
            <P>操作：音色卡上点 <K>上传本地音色</K> → 选文件 → 填一个名字 → <K>保存并使用</K>。上传成功后这个音色会立刻被选上。</P>
            <Shot src={`${IMG}/13-voice-upload.webp`} caption="上传后弹出的命名窗：名字只用来标识，之后随时可以改。" />
            <Callout tone="good">
              参考音频越干净，合成出来越像：没有混响（别在空房间里录）、没有笑声与停顿、没有第二个人说话。
            </Callout>

            <H3>4.5 调语速</H3>
            <P>音色卡下方是语速滑条，范围 0.5x ~ 2.0x，1.00 是正常速度。右边的数字框可以直接填数值，回车生效。</P>
            <Shot src={`${IMG}/14-speed.webp`} caption="语速滑条：1.00 在正中，向左变慢、向右变快。" />
            <Table
              head={["内容类型", "建议语速"]}
              rows={[
                ["知识讲解 / 新闻资讯", "0.95 ~ 1.05"],
                ["读书分享 / 播客对谈", "1.00 ~ 1.05"],
                ["冥想引导 / 助眠", "0.80 ~ 0.90"],
              ]}
            />

            <H3>4.6 加情绪与停顿</H3>
            <P>光标放在要说的那句话开头，点工具条的 <K>情绪</K>，选一个：</P>
            <P><strong>喜悦 / 愤怒 / 悲伤 / 低落 / 恐惧 / 厌恶 / 惊喜 / 平静 / 中性</strong>（共 9 种）</P>
            <Shot src={`${IMG}/15-emotion-popover.webp`} caption="点行首的情绪标签，可以换成别的情绪，也可以直接删除。" />
            <P>
              情绪默认管到行尾。如果你只希望前半句有情绪，就在结束的地方插一个 <K>【/】</K> 终止符：<br />
              <span className="mt-1 block rounded-lg bg-gray-50 px-3 py-2 font-mono text-[0.8125rem] text-gray-700">【恐惧】用来充实我们的大脑【/】，比如读书、看电影；</span>
            </P>
            <P>停顿：点 <K>停顿 0.5s</K> 按钮会在光标处插入一个 0.5 秒的停顿；点已插入的停顿标签可以改时长。</P>
            <Shot src={`${IMG}/16-pause-popover.webp`} caption="停顿弹窗：0.25 / 0.5 / 1 / 1.5 秒一键切换，也能自定义（0.05~5 秒）。" />
            <Callout tone="tip" title="什么时候该手动加停顿">
              机器默认按标点断句。遇到「书名号里的长标题」「数字加单位」「转折词之前」这类地方，
              手动补 0.3~0.5 秒会自然很多。留白比提速更能打动人。
            </Callout>

            <H3>4.7 生成</H3>
            <P>点右下角 <K>生成配音</K>。提交前它会替你检查三件事：有没有选音色、有没有空段、积分够不够。有问题的会直接告诉你原因。</P>
            <P>提交成功后任务出现在右侧「任务队列」，接下来就是等它跑完（见第 10 节）。</P>

            {/* ── 5 书稿模式 ───────────────────────────── */}
            <H2 id="book" n="5">书稿模式：一次做完一整本书</H2>
            <P>
              当你要处理的不是一段话，而是一整本书或一篇几万字的报告时，用「书稿模式」。
              它把长文切成章，一章一个任务 —— 不用一口气跑完，某一章读错了也只需要重做那一章。
            </P>
            <Step n={1} title="导入长文">
              <p>点 <K>导入</K> → 选文件（.docx / .pdf / .txt / .md 都行）。解析完成后会自动弹出「确认分段」。</p>
            </Step>
            <Step n={2} title="在「确认分段」里核对切章结果">
              <p>这一步不能跳过，也不该跳过：自动切章不可能永远切对，而切错了等合成完才发现代价很高（一章几十分钟、几百积分）。所以导入只做「解析 + 切分」，把结果摊开给你看，确认之后才会开始生成。</p>
            </Step>
            <Shot src={`${IMG}/19-chapter-confirm.webp`} caption="确认分段：每章标题可直接改，右侧显示各章字数、预估时长与积分；底部汇总「已选 N 章 / 共多少字 / 共多少积分」。" />
            <UL>
              <LI><strong>每章上限</strong>：调档位（1 万 / 2 万 / 3 万 / 5 万，或自定义），改完点 <K>重新切分</K>。它只是上限 —— 超过上限的章会被切碎，已经识别出来的标题章不会被合并</LI>
              <LI><strong>合并</strong>：点某一章右侧的 ⧉ 会把它并入上一章（适合被误切开的短章）</LI>
              <LI><strong>勾选</strong>：默认全选；不想做的章取消勾选即可，不勾就不会花钱</LI>
            </UL>
            <Step n={3} title="进入书稿模式">
              <p>点 <K>导入 N 章到书稿</K>。左栏出现「章节目录」，画布显示第 1 章。</p>
              <Shot src={`${IMG}/20-book-mode.webp`} caption="书稿模式：左栏是章节目录（点标题切章、勾选后批量生成），画布恒等于「当前章」。" />
            </Step>
            <Step n={4} title="逐章检查，然后生成">
              <ul className="mt-1.5 space-y-1">
                <li>点章节标题 → 画布切到那一章，可以单独编辑</li>
                <li>点某一章行尾的 ⚡ → 只生成这一章</li>
                <li>勾选多章（或点全选）→ 点底部 <K>生成选中的 N 章</K> 批量生成</li>
              </ul>
              <p className="mt-2">底部会实时汇总：已选几章、多少字、约多长、共需要多少积分；余额不够会直接标红并禁用按钮。</p>
            </Step>
            <Callout tone="warn" title="每章是一个独立任务，各扣各的积分">
              批量提交时按顺序一章一章提交。如果中途余额不足，它会在那一章停下并提示你，<strong>已经提交的章不会白做</strong>。
              想先试水，就只勾第 1 章。
            </Callout>
            <Callout tone="tip">
              想回到「普通单篇」编辑？点章节目录右上角的 <K>退出书稿</K>（画布会保留当前这一章的内容）。
            </Callout>

            {/* ── 6 双人播客 ───────────────────────────── */}
            <H2 id="podcast" n="6">双人播客：两个人一问一答</H2>
            <H3>6.1 先安排两位主持人</H3>
            <P>左栏「角色与音色」有主持人 A、主持人 B 两张卡。每张卡上可以设置：</P>
            <UL>
              <LI><strong>音色</strong>：A 和 B 必须选不同的音色，否则听不出在对谈</LI>
              <LI><strong>语速</strong>：两个人可以不一样（一方略快，会更像真实聊天）</LI>
              <LI><strong>默认情绪</strong>：没有单独标注情绪的行，就用这里的默认值</LI>
            </UL>
            <Shot src={`${IMG}/26-speaker-panel.webp`} caption="角色与音色：A/B 两张卡，各自的音色、语速与默认情绪。" />
            <H3>6.2 写对话稿</H3>
            <P>唯一的硬规则：<strong>每一行开头必须标明谁在说</strong>。<span className="text-gray-500">（没标的部分，生成前会被拦下来并告诉你是第几行）</span></P>
            <P>两种写法：</P>
            <UL>
              <LI><strong>在画布上写</strong>：光标放到行首，点工具条上的 <K>A 发言</K> 或 <K>B 发言</K></LI>
              <LI><strong>批量导入</strong>：点 <K>导入</K>，把 A:/B: 开头的纯文本贴进去 —— 没有前缀的行会自动 A/B 交替分配</LI>
            </UL>
            <Shot src={`${IMG}/27-podcast-import.webp`} caption="导入对话脚本：支持选择 .txt 或直接粘贴，格式说明就在弹窗里。" />
            <Code label="导入用的纯文本格式">{`A:哈喽，大家好，欢迎收听我们的播客。
B:大家好，我是主持人B。
A:今天咱们聊一个很现实的话题。`}</Code>
            <Code label="导入后画布里的样子">{`【A】哈喽，大家好，欢迎收听我们的播客。
【B】大家好，我是主持人B。
【A】今天咱们聊一个很现实的话题。`}</Code>
            <P>情绪标记【喜悦】与停顿 [pause:0.5] 的用法，和单人配音完全一样。</P>
            <H3>6.3 生成</H3>
            <Shot src={`${IMG}/25-podcast-overview.webp`} caption="双人播客工作台：左栏两位主持人，中间对话画布，右栏任务队列。" />
            <Callout tone="tip">
              行间的停顿（换人说话的间隔、同一人多句话之间的间隔）由服务端统一控制，不用你手动加。
              你只需要在「这个地方想停久一点」时手动插一个 [pause:1]。
            </Callout>

            {/* ── 7 音色库 ─────────────────────────────── */}
            <H2 id="voices" n="7">音色库与音色管理</H2>
            <Table
              head={["操作", "在哪做", "说明"]}
              rows={[
                ["选择音色", "音色卡 →「选择音色」", "筛性别 / 年龄 / 分类，或搜关键词"],
                ["试听", "音色卡头像悬停 / 音色库行首 ▶", "播放的是参考音频，不是合成结果"],
                ["收藏", "音色库行尾 ☆", "收藏后可用「收藏」快速筛出来"],
                ["改名", "音色库 →「我的音色」→ 行尾 ⋮", "只对你自己上传的音色有效"],
                ["删除", "音色库 →「我的音色」→ 行尾 ⋮", "删除后无法恢复；正在使用它的任务会失败"],
              ]}
            />
            <Callout tone="warn" title="预设与 BreezeBlue 音色不能改名或删除">
              它们是全平台共享的资源池。你对它们做的任何改动都会影响其他人的使用，所以系统把它们设为只读。
              想有自己的版本？下载参考音频后重新上传一份即可。
            </Callout>

            {/* ── 8 术语表 ─────────────────────────────── */}
            <H2 id="glossary" n="8">读音不对？用术语词汇表</H2>
            <P>
              机器偶尔会把多音字、专有名词、外文名读错（例如「游说」读成 yóu shuō、
              「单号」被当成数值读）。最有效的解决办法是加一条<strong>词条</strong>：
              合成前把原文里的词临时替换成读音正确的同音字。
            </P>
            <P>位置：左栏「术语词汇表」→「我的词条」→ 填「原词」和「替换为」→ 点 <K>添加</K>。</P>
            <Shot src={`${IMG}/21-glossary.webp`} caption="术语词汇表：「我的词条」由你自己维护，「内置词库」是管理员维护的公共词库（可搜索）。" />
            <Table
              head={["规则", "说明"]}
              rows={[
                ["只在合成时生效", "不会改你的原文，画布上看到的还是你写的那句"],
                ["我的词条优先", "同名时你自己的替换值覆盖内置的"],
                ["可以覆盖内置词", "在内置词库点 ✏ 编辑图标 → 把它预填到添加表单 → 改成你要的读音"],
                ["可以停用内置词", "在内置词库点 🚫 图标 → 该词只对你失效"],
              ]}
            />
            <Callout tone="good" title="不确定怎么改就先查内置词库">
              内置词库有 130 条常用词（多音字、成语、常见外文译名）。
              搜一下原词，很多情况下照抄它的替换值就行。
            </Callout>

            {/* ── 9 积分 ──────────────────────────────── */}
            <H2 id="points" n="9">积分、充值与订单</H2>
            <H3>9.1 怎么算积分</H3>
            <UL>
              <LI>按<strong>实际字数</strong>计费：50 积分 / 1000 字；不足最低消费时按 5 积分收</LI>
              <LI>换算参考：<strong>1000 积分 ≈ 2 万字 ≈ 约 1 小时音频</strong></LI>
              <LI>[pause:0.5] 这类停顿标记不计费</LI>
              <LI>提交时先按预估值扣费；生成失败或取消，积分会自动退回</LI>
            </UL>
            <H3>9.2 免费拿积分</H3>
            <Table
              head={["途径", "多少", "说明"]}
              rows={[
                ["注册赠送", "+500", "注册成功自动到账"],
                ["每日签到", "+20", "个人中心 → 积分 → 每日签到，每天一次"],
                ["兑换码", "看码面额", "个人中心 → 积分 → 优惠码兑换"],
              ]}
            />
            <Shot src={`${IMG}/05-checkin.webp`} caption="每日签到：签到成功后按钮变成「今日已签到」，累计天数会累加。" />
            <P>拿到兑换码后，填进「优惠码兑换」输入框，点 <K>兑换</K> 即可。</P>
            <Shot src={`${IMG}/06-redeem.webp`} caption="兑换成功后会弹提示，余额同步刷新。" />
            <H3>9.3 购买积分</H3>
            <P>个人中心 →「积分商城」有四个档位，买得越多、送得越多：</P>
            <Table
              head={["套餐", "价格", "到账积分"]}
              rows={[
                ["体验包", "¥10", "1000"],
                ["标准包", "¥30", "3200（含赠送 200）"],
                ["超值包", "¥50", "5750（含赠送 750）"],
                ["尊享包", "¥100", "12500（含赠送 2500）"],
              ]}
            />
            <Shot src={`${IMG}/07-shop.webp`} caption="积分商城：每张卡片上直接标了「约可合成多少字 ≈ 多少时长」，方便你估算。" />
            <P>点 <K>购买</K> → 弹出二维码 → 用微信扫码付款。</P>
            <Shot src={`${IMG}/08-pay-qr.webp`} caption="支付弹窗：二维码、金额与剩余有效时间；付款完成后页面会自动确认到账。" />
            <Callout tone="warn" title="买之前先记住这三条">
              <ul className="space-y-1">
                <li>① <strong>订单有效期 5 分钟</strong>。超时没付，订单会变成「已过期」，需要重新下单。</li>
                <li>② 同一时间、同一个金额，全平台只能有一笔待支付订单（平台按金额匹配收款）。如果提示「该面额已被占用」，按弹窗提示先把那一笔处理掉。</li>
                <li>③ 付完款如果余额没变，点订单上的 <K>去支付</K> 再查一次；仍未到账请带上订单号联系管理员。</li>
              </ul>
            </Callout>
            <H3>9.4 订单与积分流水</H3>
            <P>「最近订单」只显示最近几笔，点 <K>查看更多</K> 进入「全部订单」页看完整记录。</P>
            <Shot src={`${IMG}/09-orders.webp`} caption="全部订单：已支付 / 待支付 / 已过期 三种状态一眼可辨。" />
            <Shot src={`${IMG}/10-points-logs.webp`} caption="积分记录：每一笔加减都在这 —— 获取、签到、兑换、充值、消费、退款。" />

            {/* ── 10 队列 ─────────────────────────────── */}
            <H2 id="queue" n="10">任务队列</H2>
            <P>右侧「任务队列」是所有生成任务的中转站。一条任务会经历：</P>
            <P>
              <K>排队中</K> → <K>合成中</K> → <K>完成</K>（也可能走到 <K>失败</K> / <K>已取消</K> / <K>已中断</K> / <K>已暂停</K>）
            </P>
            <Shot src={`${IMG}/22-queue-running.webp`} caption="队列里同时有排队中与合成中：排队中的带拖拽手柄与位次，合成中的显示实时进度。" />
            <H3>常用操作一览</H3>
            <Table
              head={["想做什么", "怎么做"]}
              rows={[
                ["试听 / 下载", "完成的任务卡上有 ▶ 试听、⤓ 下载（文件名就是你的项目名）"],
                ["调整先后顺序", "按住「排队中」的任务卡拖动（只有排队中可以拖）"],
                ["让后面的先别跑", "点顶部「暂停全部」；想继续再点「全部入队」"],
                ["给任务改名", "双击任务名"],
                ["重做失败的任务", "任务卡上的 ↻（重新提交，会重新扣费）"],
                ["取消正在跑的", "任务卡上的 ■（进行中的分段跑完就停）"],
                ["删掉某条记录", "任务卡行尾的 🗑（会二次确认，删除不可恢复）"],
                ["批量清理历史", "列表底部「清空已完成任务」"],
                ["只看某一类", "顶部「成功 / 失败 / 排队 / 暂停」点一下筛选，再点一下取消筛选"],
                ["翻更早的记录", "列表底部「加载更多历史」"],
              ]}
            />
            <H3>任务失败了怎么办</H3>
            <P>任务从「合成中」变成「失败」时，队列会<strong>自动弹出报错详情</strong>；错过弹窗也能点任务卡上的 ⚠ 再看一次。</P>
            <Shot src={`${IMG}/24-queue-failed.webp`} caption="失败任务：卡片变红并显示报错摘要，点 ⚠ 打开完整报错详情。" />
            <P>报错文字都不长，对照下表看就能判断该做什么：</P>
            <Table
              head={["报错里出现", "通常是什么原因", "怎么处理"]}
              rows={[
                ["资源 xxx 合成失败", "上游那台合成机 / 那个平台临时出故障", "先点 ↻ 重做一次；连续失败就先换个音色或等几分钟，不用改文稿"],
                ["所有 TTS 资源均处于冷却中", "刚刚连续失败了几次，系统把出问题的资源临时隔离了几分钟", "等 1~2 分钟再点 ↻ 重做，不用改文稿"],
                ["参考音频文件不存在", "这个音色的参考音频丢了（自己上传的音色被删掉、或文件损坏最常见）", "换一个音色立刻就能继续；如果是自己上传的，重新上传一次"],
                ["连接中断", "网络抖动，或合成服务刚好在重启", "点 ↻ 重新提交即可（已扣的积分会先退回）"],
              ]}
            />
            <Callout tone="good" title="失败会退积分">
              合成失败或被你取消，这一单预扣的积分会自动退回，可在「积分记录」里看到一条「退款」。
              <strong>重做</strong>（↻）属于一次新的任务，会重新扣费。
            </Callout>

            {/* ── 11 最佳实践 ─────────────────────────── */}
            <H2 id="practice" n="11">最佳实践：做一条 3 分钟读书分享</H2>
            <P>
              这是给第一次使用的你的<strong>完整作业</strong>。跟完这 7 步，你会得到一条约 1 分钟的成品音频，
              而且中途会用到本产品 80% 的常用功能。做完它，剩下的功能你基本都能自己摸出来。
            </P>
            <div className="my-4 rounded-xl border border-emerald-200 bg-emerald-50 p-4">
              <p className="flex items-center gap-1.5 text-sm font-semibold text-emerald-800">
                <FileText className="h-4 w-4" /> 本次作业的目标
              </p>
              <p className="mt-1 text-sm leading-7 text-emerald-900/80">
                把一段约 330 字的读书分享，做成一条可以直接发公众号的音频。
              </p>
            </div>
            <Table
              head={["参数", "本次取值"]}
              rows={[
                ["工作区", "单人配音"],
                ["文稿长度", "约 330 字，8 段（每段都在 60 字以内）"],
                ["音色", "音色库 → 筛选「女 / 青年」→ 选一个温柔女声（示例用「女-温暖闺蜜」）"],
                ["语速", "1.00"],
                ["情绪", "整体用【平静】，开头与结尾各加一处【喜悦】"],
                ["停顿", "两处 [pause:0.5]、一处 [pause:0.8]"],
                ["预计花费", "约 17 积分（≈ 0.17 元）"],
              ]}
            />

            <Step n={1} title="先把「给谁听」想清楚，再动笔">
              <p>写不好文案的人，通常是没想清楚这三件事：给谁听、听完记住什么、最后希望他做什么。想清楚了，稿子自然短而有力。</p>
              <UL>
                <LI><strong>给谁听</strong>：例如「想读书但总没时间的上班族」</LI>
                <LI><strong>记住什么</strong>：一句话，比如「真正拉开差距的不是努力，是认知」</LI>
                <LI><strong>做什么</strong>：比如「点个关注，下期讲怎么练」</LI>
              </UL>
              <p className="mt-2">把下面这段复制进画布：</p>
              <Code>{`【平静】大家好，欢迎来到「小柯读书」，我是小柯。
【喜悦】今天想和你聊聊一本书，叫《认知觉醒》。[pause:0.5]
【平静】很多人以为，只要足够努力，就一定能变好。
【平静】但作者周岭告诉我们，大多数人的努力，只是低水平的重复。
【惊喜】真正拉开差距的，是认知。你看到的世界，决定了你能走多远。
【平静】书里有三个概念让我印象最深：大脑的三重结构、舒适区边缘，还有元认知。[pause:0.8]
【平静】简单说，就是别在舒适区里重复，也别一步跨进困难区，要站在「有点难但够得着」的地方练习。
【喜悦】如果你也常常焦虑、拖延、想改变却没方向，那这本书值得你认真读一遍。[pause:0.5]
【喜悦】好了，今天先聊到这里。喜欢的话，记得点个关注，我们下期再见。`}</Code>
              <Shot src={`${IMG}/29-practice-canvas.webp`} caption="第 1 步完成后：文稿填进画布，情绪与停顿被渲染成彩色标签。" />
            </Step>

            <Step n={2} title="检查情绪与停顿的作用范围">
              <p>
                把光标放到第 5 行「真正拉开差距的，是认知。」的开头，点工具条的 <K>情绪</K> 选「惊喜」；
                在「是认知。」后面插一个 <K>【/】</K>，让惊讶只覆盖前半句。
              </p>
              <UL>
                <LI>点任何一个情绪标签 → 可以换情绪或删除</LI>
                <LI>点任何一个停顿标签 → 改成 0.25 / 0.5 / 1 / 1.5 秒，或自定义</LI>
              </UL>
              <Shot src={`${IMG}/30-emotion-chip.webp`} caption="情绪标签是可以点的：改动即时生效，直接写回画布文本。" />
            </Step>

            <Step n={3} title="选音色并试听">
              <p>点 <K>选择音色</K> → 左栏筛「女 / 青年」→ 逐个点 ▶ 试听 → 选中一个 → 关掉弹窗。音色卡上会显示你选中的那个。</p>
              <Shot src={`${IMG}/12-voice-picker.webp`} caption="挑音色时优先看中文描述，它会告诉你这条音色适合的场景。" />
              <Callout tone="tip">
                读书分享最适合的是「温暖、稳定、不抢戏」的音色。别选情绪起伏大的角色音 ——
                那是给故事配音用的，念知识类内容会显得浮夸。
              </Callout>
            </Step>

            <Step n={4} title="定语速">
              <p>保持 <K>1.00</K>。如果你自己听着觉得赶，往下调到 0.95；觉得拖沓就调到 1.05。</p>
              <Shot src={`${IMG}/14-speed.webp`} caption="语速 1.00 在滑条正中。改完可以直接点生成，听成品再决定要不要调。" />
            </Step>

            <Step n={5} title="提交前检查积分">
              <p>看工具条右侧：「330 字 · 8 段 · 约 17 积分」。余额不足会在这里标红并阻止提交。</p>
              <Shot src={`${IMG}/31-cost-estimate.webp`} caption="工具条右侧就是本次花费的实时预估，以及「生成配音」按钮。" />
              <p>确认没问题后点 <K>生成配音</K>，任务进入右侧队列。</p>
            </Step>

            <Step n={6} title="等它跑完，试听一遍">
              <p>任务会经历 排队中 → 合成中（带百分比进度）→ 完成。完成后点 ▶ 试听。</p>
              <Shot src={`${IMG}/22-queue-running.webp`} caption="合成中：进度条会实时刷新，可以继续编辑文稿（不影响已提交的任务）。" />
              <p>试听时重点听三件事：有没有读错的字、语气是不是太平、句间是不是太赶。</p>
              <Shot src={`${IMG}/23-queue-success.webp`} caption="完成：任务卡变绿，显示音频时长，并提供试听与下载。" />
            </Step>

            <Step n={7} title="下载，然后按需微调">
              <p>点 ⤓ 下载。如果要发公众号，建议再用音频工具转成 mp3（体积能小一半以上）。</p>
              <p>如果试听发现读错了字，回到第 8 节加一条词条，重新生成 —— 只重做这一条，前面的经验都还在。</p>
            </Step>

            <div className="my-5 rounded-xl border border-indigo-200 bg-indigo-50 p-4">
              <p className="flex items-center gap-1.5 text-sm font-semibold text-indigo-800">
                <CheckCircle2 className="h-4 w-4" /> 收尾自检清单
              </p>
              <ul className="mt-2 space-y-1.5 text-sm leading-6 text-indigo-900/80">
                {[
                  "有没有读错的字？→ 加词条（第 8 节）",
                  "有没有哪一句语气太平？→ 行首加【情绪】",
                  "句与句是不是太赶？→ 句尾加 [pause:0.3]",
                  "开头 3 秒有没有「钩子」？听众为什么要继续听？",
                  "结尾有没有让人知道下一步做什么？（关注 / 收藏 / 留言）",
                  "确认积分余额还够下一次",
                ].map(t => (
                  <li key={t} className="flex gap-2">
                    <span className="mt-0.5">☐</span>{t}
                  </li>
                ))}
              </ul>
            </div>

            <Callout tone="good" title="这个案例还能怎么扩展">
              把文稿换成你自己的内容、把音色换成「我的音色」里你自己上传的参考音频，
              再点项目名右边的 💾 存成项目存档 —— 这就是一条可以持续更新的固定栏目了。
              下一期只改文稿，音色、语速、情绪全都不用重设。
            </Callout>

            {/* ── 12 FAQ ──────────────────────────────── */}
            <H2 id="faq" n="12">常见问题</H2>
            <Faq items={[
              ["提交后一直显示「排队中」，很久不动？", "说明前面还有别人的任务在跑，合成是排队进行的。可以看队列里有没有卡住的大任务；急着要成品时，点「暂停全部」把后面的先停下，让你的先跑。"],
              ["某个字 / 多音字读错了怎么办？", "加一条词条（第 8 节）：把原词替换成读音正确的同音字。这是最有效的办法，而且不会改你的原文。"],
              ["点音色试听没有声音？", "试听播放的是音色的参考音频。如果这一个没反应，先换一个音色试试；如果是你自己上传的，检查文件是否损坏（能不能在本地播放器里放）。"],
              ["提示「积分不足」？", "去个人中心签到、用兑换码，或者购买积分。书稿模式里也可以少勾几章先做。"],
              ["生成失败了，积分退给我了吗？", "退了。失败或取消的任务，预扣积分会自动退回，可以在「积分记录」里看到一条「退款」。"],
              ["音频文件在哪？可以下载到手机吗？", "在任务队列里点 ⤓ 下载即可。手机浏览器也能登录操作，但界面是为宽屏设计的，建议用电脑。"],
              ["我换了台电脑，之前写的东西不见了？", "草稿存在本机浏览器里，换设备就读不到了。需要跨设备保留，请点项目名右侧的 💾 存成「项目存档」（单人配音的存档也存在本机；双人播客的项目存在服务器上）。"],
              ["能不能不写文稿，直接让它念一个网页？", "目前不支持贴链接。请先把网页正文复制出来粘进画布，或者点「导入」上传 .txt / .docx 文件。"],
              ["任务删除了还能找回吗？", "不能。删除是不可恢复的，会连同磁盘记录一起删掉，而且会二次确认 —— 请看清再点。"],
              ["生成的音频要多久？", "和字数、排在前面的任务都有关。一段 300 字的稿子通常十几秒到一分钟；整本书请用书稿模式按章分批做。"],
              ["手机上能用吗？", "能登录、能生成、能下载，但画布与队列都是为宽屏设计的，手机上三栏会挤在一起。正式操作建议用电脑。"],
            ]} />

            <div className="mt-12 rounded-2xl border border-gray-200 bg-white p-5 text-center shadow-sm">
              <p className="text-sm font-medium text-gray-800">看完了，去做出你的第一条音频吧</p>
              <p className="mt-1 text-xs text-gray-500">注册即送 {REG_BONUS_POINTS} 积分，够跑通整条流程。</p>
              <div className="mt-3 flex items-center justify-center gap-2">
                <button
                  type="button"
                  onClick={() => { if (window.opener) window.close(); else navigate("/dubbing"); }}
                  className="inline-flex items-center gap-1.5 rounded-full bg-emerald-600 px-5 py-2.5 text-sm font-medium text-white shadow-sm transition-colors hover:bg-emerald-500"
                >
                  <Mic className="h-4 w-4" /> 去单人配音
                </button>
                <button
                  type="button"
                  onClick={() => navigate("/account")}
                  className="inline-flex items-center gap-1.5 rounded-full border border-gray-300 bg-white px-5 py-2.5 text-sm text-gray-700 transition-colors hover:bg-gray-50"
                >
                  <Coins className="h-4 w-4" /> 查看我的积分
                </button>
              </div>
            </div>
          </main>
        </div>

        {/* 大图查看：点任意截图放大，点击空白或按 ESC 关闭 */}
        {zoom && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6"
            onClick={() => setZoom(null)}
          >
            <button
              type="button"
              aria-label="关闭"
              onClick={() => setZoom(null)}
              className="absolute right-5 top-5 rounded-full bg-white/10 p-2 text-white transition-colors hover:bg-white/20"
            >
              <X className="h-5 w-5" />
            </button>
            <img
              src={zoom}
              alt=""
              onClick={e => e.stopPropagation()}
              className="max-h-[92vh] max-w-full rounded-xl bg-white object-contain shadow-2xl"
            />
          </div>
        )}
      </div>
    </ZoomCtx.Provider>
  );
}

/** FAQ 折叠项 */
function Faq({ items }: { items: [string, string][] }) {
  const [open, setOpen] = useState<number | null>(0);
  return (
    <div className="my-4 divide-y divide-gray-100 overflow-hidden rounded-xl border border-gray-200 bg-white">
      {items.map(([q, a], i) => (
        <div key={q}>
          <button
            type="button"
            onClick={() => setOpen(open === i ? null : i)}
            className="flex w-full items-center gap-2.5 px-4 py-3 text-left transition-colors hover:bg-gray-50"
          >
            <span className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded bg-indigo-50 text-[0.6875rem] font-semibold text-indigo-600">Q</span>
            <span className="flex-1 text-sm font-medium text-gray-800">{q}</span>
            <ChevronRight className={cn("h-4 w-4 shrink-0 text-gray-400 transition-transform", open === i && "rotate-90")} />
          </button>
          {open === i && (
            <div className="px-4 pb-3.5 pl-[3.375rem] text-sm leading-7 text-gray-600">{a}</div>
          )}
        </div>
      ))}
    </div>
  );
}
