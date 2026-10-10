/** 【临时探针】把真实的 MonoEditor 单独挂到页面上，用于「导入覆盖确认弹窗」的交互取证。
 *
 *  为什么不用静态复刻：本次要证的是**交互**（有内容时确认分章 → 先弹自绘确认框、
 *  取消/Esc/遮罩 → 不替换、点「继续导入」→ 才替换），照抄 HTML 只能证明视觉、证明不了行为。
 *  这里加载的是真组件、跑的是真状态机，只有 /api/mono/extract 由 fetch mock 喂数据。
 *
 *  验证完即删（不进任何提交）。用法见 outputs/verify-mono-import-confirm.js。
 */
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { MonoEditor } from "./components/MonoEditor";

// 隐藏真实 app（未登录时是登录门禁页），避免干扰截图
const app = document.getElementById("root");
if (app) app.style.display = "none";

/** 画布里预置一段非空文稿 —— 覆盖确认只在「已有内容」时才该弹。 */
const INITIAL = Array.from(
  { length: 40 },
  (_, i) => `原有的第 ${i + 1} 段文稿内容，用于验证替换前是否会先弹确认框。`
).join("\n");

(window as any).__monoText = INITIAL;

function Probe() {
  const [text, setText] = useState(INITIAL);
  return (
    <MonoEditor
      text={text}
      onChange={(t) => {
        (window as any).__monoText = t;
        setText(t);
      }}
      onGenerate={() => {}}
      canGenerate={false}
      generating={false}
      error={null}
      pointsEnv={null}
    />
  );
}

const host = document.createElement("div");
host.id = "probe-root";
host.style.cssText = "width:900px;padding:12px;background:#f8fafc;font-family:system-ui,sans-serif";
document.body.appendChild(host);

createRoot(host).render(<Probe />);
