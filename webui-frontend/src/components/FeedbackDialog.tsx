/** 举报与反馈弹窗（顶栏用户菜单 → 「举报与反馈」）。
 *
 *  为什么让用户只填三样：类型 / 描述 / 联系方式（选填）。
 *  「哪个页面出的问题」「用的什么浏览器」这些用户说不清、也不该让他填 ——
 *  前端自动带上 `page_url` / `user_agent`，服务端再补账号、时间、IP。
 *  字段口径见后端 `app/routes/feedback.py` 的模块注释。
 *
 *  提交成功后停在「已收到」态并显示回执编号：用户能确认"东西真的发出去了"，
 *  站长按这个编号在 `db.feedback` 里定位（比"我刚才发过"好查得多）。
 */
import { useEffect, useState } from "react";
import { CheckCircle2, Loader2, Send, X } from "lucide-react";
import { Button, Input, Textarea } from "./ui";
import { memberApi } from "@/api/members";
import { cn } from "@/lib/utils";

/** 类型清单 —— key 与后端 `app/routes/feedback.py::CATEGORIES` **一一对应**，
 *  改这里必须同步改那边（后端会按 key 再校验一次：写错立刻 400，不会静默存下脏数据）。
 *  英文 key 落库，中文 label 只用于展示。 */
const CATEGORIES = [
  { key: "bug", label: "功能异常" },
  { key: "voice", label: "音色与音质" },
  { key: "points", label: "积分与订单" },
  { key: "content", label: "内容问题" },
  { key: "idea", label: "功能建议" },
  { key: "other", label: "其他" },
] as const;

const CONTENT_MIN = 5;
const CONTENT_MAX = 1000;
const CONTACT_MAX = 100;

export function FeedbackDialog({ onClose }: { onClose: () => void }) {
  const [category, setCategory] = useState<string>("bug");
  const [content, setContent] = useState("");
  const [contact, setContact] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  /** 非空 = 提交成功，值是回执编号 */
  const [ticket, setTicket] = useState("");

  // Esc 关闭（提交中不关：请求已发出，关掉只会让用户以为没发成功）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const len = content.trim().length;
  const canSubmit = !busy && len >= CONTENT_MIN && len <= CONTENT_MAX;

  const submit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setErr("");
    try {
      const r = await memberApi.submitFeedback({
        category,
        content: content.trim(),
        contact: contact.trim(),
        page_url: window.location.href,
        user_agent: navigator.userAgent,
      });
      setTicket(r.id);
    } catch (e) {
      // 后端已经把"为什么失败"写进 detail（字数不够 / 库不可用 …），原样呈现
      setErr(e instanceof Error ? e.message : "提交失败，请稍后再试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={() => { if (!busy) onClose(); }} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="举报与反馈"
        className="relative w-full max-w-md rounded-2xl bg-white shadow-xl"
      >
        <button
          type="button"
          onClick={onClose}
          disabled={busy}
          aria-label="关闭"
          className="absolute top-3 right-3 p-1 rounded-md text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors disabled:opacity-40"
        >
          <X className="w-4 h-4" />
        </button>

        {ticket ? (
          // ── 成功态 ──
          <div className="px-6 py-9 text-center">
            <CheckCircle2 className="w-10 h-10 text-emerald-500 mx-auto" />
            <h3 className="mt-3 text-[0.9375rem] font-semibold text-gray-800">已收到你的反馈</h3>
            <p className="mt-1.5 text-xs leading-6 text-gray-500">
              我们会尽快处理。如果留了联系方式，需要时会主动联系你。
            </p>
            <p className="mt-3 text-[0.6875rem] text-gray-400">
              回执编号 <span className="font-mono text-gray-600">{ticket}</span>
            </p>
            <Button size="sm" variant="outline" className="mt-5" onClick={onClose}>关闭</Button>
          </div>
        ) : (
          <>
            <div className="px-5 pt-5 pb-3 pr-12">
              <h3 className="text-[0.9375rem] font-semibold text-gray-800">举报与反馈</h3>
              <p className="mt-1 text-[0.6875rem] leading-5 text-gray-500">
                用了哪里不对劲、哪条音色有问题、想让我们加什么功能，都可以写在这里。
                会一并记录你的账号与当前页面地址，方便我们复现。
              </p>
            </div>

            <div className="px-5 pb-4 space-y-3.5">
              {/* 类型：用 chip 而不是下拉 —— 6 个选项一眼看全，少一次点击 */}
              <div>
                <label className="text-[0.6875rem] font-medium text-gray-600">反馈类型</label>
                <div className="mt-1.5 flex flex-wrap gap-1.5">
                  {CATEGORIES.map(c => (
                    <button
                      key={c.key}
                      type="button"
                      onClick={() => setCategory(c.key)}
                      className={cn(
                        "px-2.5 py-1 rounded-lg border text-xs transition-colors",
                        category === c.key
                          ? "border-indigo-300 bg-indigo-50 text-indigo-700 font-medium"
                          : "border-gray-200 text-gray-600 hover:border-indigo-200 hover:text-indigo-600"
                      )}
                    >
                      {c.label}
                    </button>
                  ))}
                </div>
              </div>

              <div>
                <div className="flex items-baseline justify-between">
                  <label className="text-[0.6875rem] font-medium text-gray-600">
                    详细描述 <span className="text-red-500">*</span>
                  </label>
                  <span className={cn(
                    "text-[0.625rem] tabular-nums",
                    len > CONTENT_MAX ? "text-red-500" : "text-gray-400"
                  )}>
                    {len}/{CONTENT_MAX}
                  </span>
                </div>
                <Textarea
                  value={content}
                  onChange={e => setContent(e.target.value)}
                  maxLength={CONTENT_MAX}
                  rows={5}
                  placeholder={
                    category === "bug"
                      ? "做什么操作时出的问题？期望是什么、实际是什么？（比如：点了「开始生成」一直停在 0%）"
                      : "写清楚一些，我们能更快处理。"
                  }
                  className="mt-1.5 min-h-[6.5rem]"
                />
                {len > 0 && len < CONTENT_MIN && (
                  <p className="mt-1 text-[0.625rem] text-red-500">再多写几个字，至少 {CONTENT_MIN} 个字</p>
                )}
              </div>

              <div>
                <label className="text-[0.6875rem] font-medium text-gray-600">
                  联系方式 <span className="text-gray-400 font-normal">（选填）</span>
                </label>
                <Input
                  value={contact}
                  onChange={e => setContact(e.target.value)}
                  maxLength={CONTACT_MAX}
                  placeholder="邮箱 / 微信，方便我们回复你"
                  className="mt-1.5 h-8 text-xs"
                />
              </div>

              {err && (
                <p className="rounded-lg bg-red-50 px-3 py-2 text-[0.6875rem] leading-5 text-red-600">
                  {err}
                </p>
              )}
            </div>

            <div className="flex items-center justify-end gap-2 border-t border-gray-100 px-5 py-3">
              <Button size="sm" variant="ghost" onClick={onClose} disabled={busy}>取消</Button>
              <Button size="sm" icon={busy ? Loader2 : Send} onClick={submit} disabled={!canSubmit}>
                {busy ? "提交中…" : "提交反馈"}
              </Button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
