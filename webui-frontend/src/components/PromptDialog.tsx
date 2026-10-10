/**
 * 平台统一输入弹窗，与 `ConfirmDialog` 同族外观（白卡片 / 圆角 / 遮罩 / 取消在左）。
 *
 * 替代原生 `window.prompt`：后者样式突兀、无法带上「原始文件名」这类上下文，
 * 也不能与平台其它弹窗保持一致。用法与 ConfirmDialog 一致 —— 确认后才回调，
 * 取消 / Esc / 点遮罩都不产生副作用。
 *
 * 输入框在打开时自动聚焦并全选，回车即确认。
 */
import { useEffect, useId, useRef, useState } from "react";
import { Pencil } from "lucide-react";
import { Button, Input, Label } from "./ui";

export function PromptDialog({
  open,
  title,
  label,
  description,
  defaultValue = "",
  placeholder,
  confirmText = "确认",
  busy = false,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  title: string;
  /** 输入框上方的字段名 */
  label?: string;
  /** 标题下的补充说明（如「原始文件: xxx.mp3」） */
  description?: React.ReactNode;
  defaultValue?: string;
  placeholder?: string;
  confirmText?: string;
  busy?: boolean;
  onCancel: () => void;
  /** 仅在非空时触发（空白输入不允许确认） */
  onConfirm: (value: string) => void;
}) {
  const [value, setValue] = useState(defaultValue);
  const inputRef = useRef<HTMLInputElement>(null);
  // 同页可能同时挂几个 PromptDialog（如角色卡里上传命名 + 预设改名 + 音色改名），
  // 用 useId 保证 label 的 htmlFor 与 input 的 id 一一对应，不会串。
  const inputId = useId();

  // 每次打开都同步初值并聚焦全选（默认名本身就是最可能的输入）
  useEffect(() => {
    if (!open) return;
    setValue(defaultValue);
    const t = setTimeout(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    }, 0);
    return () => clearTimeout(t);
  }, [open, defaultValue]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, busy, onCancel]);

  if (!open) return null;
  const trimmed = value.trim();
  const submit = () => {
    if (!trimmed || busy) return;
    onConfirm(trimmed);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/40" onClick={() => { if (!busy) onCancel(); }} />
      <div className="relative w-full max-w-sm rounded-2xl bg-white shadow-xl p-5">
        <div className="flex items-start gap-2.5">
          <div className="w-8 h-8 shrink-0 rounded-full bg-indigo-50 flex items-center justify-center">
            <Pencil className="w-4 h-4 text-indigo-500" />
          </div>
          <div className="min-w-0 flex-1">
            <h3 className="text-sm font-semibold text-gray-800">{title}</h3>
            {description && (
              <p className="mt-1.5 text-xs text-gray-500 leading-5 break-words">{description}</p>
            )}
          </div>
        </div>

        <div className="mt-3">
          {label && <Label htmlFor={inputId}>{label}</Label>}
          <Input
            id={inputId}
            ref={inputRef}
            value={value}
            placeholder={placeholder}
            onChange={e => setValue(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") submit(); }}
          />
        </div>

        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={onCancel} disabled={busy}>
            取消
          </Button>
          <Button size="sm" onClick={submit} disabled={busy || !trimmed}>
            {busy ? `${confirmText}中…` : confirmText}
          </Button>
        </div>
      </div>
    </div>
  );
}
