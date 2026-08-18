/** 精简版 shadcn 风格 UI 组件，基于 Tailwind 实现。 */
import { forwardRef, type ButtonHTMLAttributes, type InputHTMLAttributes, type TextareaHTMLAttributes, type SelectHTMLAttributes } from "react";
import type { LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";

// ─── Button ────────────────────────────────────────────────
type ButtonVariant = "default" | "outline" | "ghost" | "destructive" | "secondary";
type ButtonSize = "sm" | "md" | "lg" | "icon";

const btnVariants: Record<ButtonVariant, string> = {
  default: "bg-indigo-600 text-white hover:bg-indigo-700 shadow-sm",
  outline: "border border-gray-300 bg-white text-gray-700 hover:bg-gray-50",
  ghost: "text-gray-600 hover:bg-gray-100",
  destructive: "bg-red-500 text-white hover:bg-red-600",
  secondary: "bg-gray-100 text-gray-700 hover:bg-gray-200",
};
const btnSizes: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-xs gap-1",
  md: "h-9 px-4 text-sm gap-1.5",
  lg: "h-11 px-6 text-base gap-2",
  icon: "h-9 w-9",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  icon?: LucideIcon;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "default", size = "md", icon: Icon, children, ...props }, ref) => (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center rounded-lg font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-1 disabled:opacity-50 disabled:cursor-not-allowed",
        btnVariants[variant], btnSizes[size], className
      )}
      {...props}
    >
      {Icon && <Icon className={size === "sm" ? "w-3.5 h-3.5" : "w-4 h-4"} />}
      {children}
    </button>
  )
);
Button.displayName = "Button";

// ─── Card ──────────────────────────────────────────────────
export function Card({ className, children, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={cn("rounded-xl bg-white border border-gray-200 shadow-sm", className)} {...props}>
      {children}
    </div>
  );
}
export function CardHeader({ className, children, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-4 py-3 border-b border-gray-100", className)} {...props}>{children}</div>;
}
export function CardTitle({ className, children }: { className?: string; children: React.ReactNode }) {
  return <h3 className={cn("text-sm font-semibold text-gray-800", className)}>{children}</h3>;
}
export function CardContent({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn("p-4", className)}>{children}</div>;
}

// ─── Input ─────────────────────────────────────────────────
export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "h-9 w-full rounded-lg border border-gray-300 bg-white px-3 text-sm text-gray-800 placeholder:text-gray-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 transition-colors",
        className
      )}
      {...props}
    />
  )
);
Input.displayName = "Input";

// ─── Textarea ──────────────────────────────────────────────
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea
      ref={ref}
      className={cn(
        "w-full rounded-lg border border-gray-300 bg-white px-3 py-2 text-sm text-gray-800 placeholder:text-gray-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 transition-colors resize-y min-h-[80px]",
        className
      )}
      {...props}
    />
  )
);
Textarea.displayName = "Textarea";

// ─── Select ────────────────────────────────────────────────
export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, children, ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        "h-9 w-full rounded-lg border border-gray-300 bg-white px-3 text-sm text-gray-800 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 transition-colors cursor-pointer",
        className
      )}
      {...props}
    >
      {children}
    </select>
  )
);
Select.displayName = "Select";

// ─── Badge ─────────────────────────────────────────────────
export function Badge({ className, children, color = "gray" }: { className?: string; children: React.ReactNode; color?: "gray" | "indigo" | "green" | "red" | "amber" | "blue" }) {
  const colors: Record<string, string> = {
    gray: "bg-gray-100 text-gray-600",
    indigo: "bg-indigo-100 text-indigo-700",
    green: "bg-green-100 text-green-700",
    red: "bg-red-100 text-red-700",
    amber: "bg-amber-100 text-amber-700",
    blue: "bg-blue-100 text-blue-700",
  };
  return (
    <span className={cn("inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium", colors[color], className)}>
      {children}
    </span>
  );
}

// ─── Label ─────────────────────────────────────────────────
export function Label({ className, children }: { className?: string; children: React.ReactNode }) {
  return <label className={cn("text-xs font-medium text-gray-600 mb-1 block", className)}>{children}</label>;
}

// ─── Slider (带数值显示) ───────────────────────────────────
export function Slider({ label, value, min, max, step = 1, onChange, unit = "", className }: {
  label?: string; value: number; min: number; max: number; step?: number;
  onChange: (v: number) => void; unit?: string; className?: string;
}) {
  return (
    <div className={className}>
      {label && (
        <div className="flex justify-between items-center mb-1">
          <Label className="mb-0">{label}</Label>
          <span className="text-xs text-gray-500 tabular-nums">{value}{unit}</span>
        </div>
      )}
      <input
        type="range" min={min} max={max} step={step} value={value}
        onChange={e => onChange(parseFloat(e.target.value))}
        className="w-full"
      />
    </div>
  );
}

// ─── Switch ────────────────────────────────────────────────
export function Switch({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label?: string }) {
  return (
    <label className="inline-flex items-center gap-2 cursor-pointer">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={cn(
          "relative inline-flex h-5 w-9 items-center rounded-full transition-colors",
          checked ? "bg-indigo-600" : "bg-gray-300"
        )}
      >
        <span className={cn(
          "inline-block h-3.5 w-3.5 transform rounded-full bg-white transition-transform",
          checked ? "translate-x-5" : "translate-x-1"
        )} />
      </button>
      {label && <span className="text-xs text-gray-600">{label}</span>}
    </label>
  );
}

// ─── Empty State ───────────────────────────────────────────
export function EmptyState({ icon: Icon, title, hint }: { icon: LucideIcon; title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 text-center">
      <div className="w-12 h-12 rounded-full bg-gray-100 flex items-center justify-center mb-3">
        <Icon className="w-6 h-6 text-gray-400" />
      </div>
      <p className="text-sm font-medium text-gray-600">{title}</p>
      {hint && <p className="text-xs text-gray-400 mt-1">{hint}</p>}
    </div>
  );
}
