/** 平台统一二次确认弹窗。删除 / 清空 / 覆盖文稿等需要用户拍板的动作共用一套外观，
 *  避免多处各写一份、样式各自漂移。
 *
 *  为什么不用 `window.confirm`：要显示**具体对象**（任务名、条数、字数）才有防误删
 *  的意义，原生 confirm 只能给一段纯文本、样式也突兀，与平台整体视觉不一致。
 *
 *  tone 决定语义配色：
 *    - "destructive"（默认）：不可逆的删除类动作 —— 红底警示图标 + 红色确认按钮 + 垃圾桶图标；
 *    - "default"：覆盖 / 替换这类需要确认但不属于删除的动作 —— 靛蓝图标 + 主色确认按钮。
 *
 *  Esc = 取消：高风险动作的默认键位要落在安全的一侧（顺手一按不会删掉东西）。
 *  遮罩同理；确认按钮在右、取消在左。
 */
import { useEffect } from "react";
import { AlertTriangle, Trash2 } from "lucide-react";
import { Button } from "./ui";
import { cn } from "@/lib/utils";

export type ConfirmTone = "destructive" | "default";

export function ConfirmDialog({
  open,
  title,
  description,
  warning,
  confirmText = "确认",
  busy = false,
  tone = "destructive",
  onCancel,
  onConfirm,
}: {
  open: boolean;
  title: string;
  description: React.ReactNode;
  /** 严重性提示（如「删除后无法恢复」）；不传则不显示 */
  warning?: string;
  confirmText?: string;
  busy?: boolean;
  tone?: ConfirmTone;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  if (!open) return null;
  const danger = tone === "destructive";
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={() => { if (!busy) onCancel(); }} />
      <div className="relative w-full max-w-sm rounded-2xl bg-white shadow-xl p-5">
        <div className="flex items-start gap-2.5">
          <div className={cn(
            "w-8 h-8 shrink-0 rounded-full flex items-center justify-center",
            danger ? "bg-red-50" : "bg-indigo-50"
          )}>
            <AlertTriangle className={cn("w-4 h-4", danger ? "text-red-500" : "text-indigo-500")} />
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-gray-800">{title}</h3>
            <p className="mt-1.5 text-xs text-gray-600 leading-5 break-words">{description}</p>
            {warning && (
              <p className={cn("mt-1.5 text-xs font-medium", danger ? "text-red-500" : "text-amber-600")}>
                {warning}
              </p>
            )}
          </div>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onCancel} disabled={busy}>
            取消
          </Button>
          <Button
            variant={danger ? "destructive" : "default"}
            size="sm"
            icon={danger ? Trash2 : undefined}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? `${confirmText}中…` : confirmText}
          </Button>
        </div>
      </div>
    </div>
  );
}
