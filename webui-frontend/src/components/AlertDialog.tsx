/** 平台统一「告知」弹窗：**只有确认按钮**，用户读完点一下就关。
 *
 *  为什么不复用 `ConfirmDialog`：那个组件的语义是「二选一」（取消 / 确认），Esc 和点
 *  遮罩都等于**取消**。而这里没有「取消」这个选项 —— 点哪里都只是「我知道了」。
 *  硬塞进一个组件会让"Esc 在这里到底代表什么"变成隐式约定，早晚踩坑。
 *
 *  使用边界：用于「你此刻做不了这件事，原因是 X」这类**必须让用户看清楚、且需要他
 *  明确接收**的提示（例如：该面额当前有一笔待支付的订单）。转瞬即逝的轻提示仍走 toast。
 *
 *  键位：Esc / Enter 都等于确认 —— 这个弹窗没有危险选项，不必把 Esc 绑到"安全侧"。
 */
import { useEffect } from "react";
import { Info } from "lucide-react";
import { Button } from "./ui";

export function AlertDialog({
  open,
  title,
  description,
  hint,
  confirmText = "知道了",
  onClose,
}: {
  open: boolean;
  title: string;
  description: React.ReactNode;
  /** 补一句「那我现在该怎么办」；不传则不显示 */
  hint?: string;
  confirmText?: string;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" || e.key === "Enter") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative w-full max-w-sm rounded-2xl bg-white shadow-xl p-5">
        <div className="flex items-start gap-2.5">
          <div className="w-8 h-8 shrink-0 rounded-full bg-amber-50 flex items-center justify-center">
            <Info className="w-4 h-4 text-amber-600" />
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-gray-800">{title}</h3>
            <p className="mt-1.5 text-xs text-gray-600 leading-5 break-words">{description}</p>
            {hint && <p className="mt-1.5 text-xs text-gray-500 leading-5">{hint}</p>}
          </div>
        </div>
        <div className="mt-4 flex justify-end">
          <Button size="sm" onClick={onClose}>{confirmText}</Button>
        </div>
      </div>
    </div>
  );
}
