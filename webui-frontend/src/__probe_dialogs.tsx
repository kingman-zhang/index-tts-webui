/** 【临时探针】平台统一弹窗（ConfirmDialog / PromptDialog）+ 全局 toast 的渲染与交互取证。
 *
 *  为什么不用静态复刻：本次要证的是**交互**（Esc / 遮罩 / 空值禁确认 / 回车提交 /
 *  两档语义配色 / toast 三色），照抄 HTML 只能证明视觉、证明不了行为。
 *  这里加载真组件、跑真状态机，无任何 fetch 依赖。
 *
 *  验证完即删（不进任何提交）。用法见 outputs/verify-dialogs.js。
 */
import { useState } from "react";
import { createRoot } from "react-dom/client";
import { ConfirmDialog } from "./components/ConfirmDialog";
import { PromptDialog } from "./components/PromptDialog";
import { ToastNode } from "./hooks/useAppInit";
import { toast, useToastState } from "./lib/toast";

// 隐藏真实 app（未登录时是登录门禁页），避免干扰截图
const app = document.getElementById("root");
if (app) app.style.display = "none";

const BTN: React.CSSProperties = {
  padding: "6px 12px", border: "1px solid #cbd5e1", borderRadius: 8,
  background: "#fff", fontSize: 13, cursor: "pointer",
};

function Probe() {
  const [confirm, setConfirm] = useState<"destructive" | "default" | null>(null);
  const [promptOpen, setPromptOpen] = useState(false);
  const [log, setLog] = useState<string[]>([]);
  const toastState = useToastState();
  const push = (s: string) => setLog(l => [...l, s]);

  // 供 puppeteer 读取「确认/取消后到底发生了什么」
  (window as any).__probeLog = log;

  return (
    <div>
      <button style={BTN} onClick={() => setConfirm("destructive")}>打开删除确认</button>
      <button style={BTN} onClick={() => setConfirm("default")}>打开覆盖确认</button>
      <button style={BTN} onClick={() => setPromptOpen(true)}>打开输入弹窗</button>
      <button style={BTN} onClick={() => toast.info("info：普通提示")}>toast.info</button>
      <button style={BTN} onClick={() => toast.success("success：成功提示")}>toast.success</button>
      <button style={BTN} onClick={() => toast.error("error：失败提示")}>toast.error</button>

      {/* 删除语义（默认档） */}
      <ConfirmDialog
        open={confirm === "destructive"}
        title="删除这个音色？"
        description={<>将删除音色「<span className="font-medium text-gray-800">测试音色.mp3</span>」。</>}
        warning="删除后无法恢复。"
        confirmText="删除"
        onCancel={() => setConfirm(null)}
        onConfirm={() => { push("confirm:destructive"); setConfirm(null); }}
      />

      {/* 覆盖 / 替换语义（default 档，无 warning） */}
      <ConfirmDialog
        open={confirm === "default"}
        tone="default"
        title="替换当前文稿？"
        description="将替换画布中现有的 100 字文稿。"
        confirmText="继续导入"
        onCancel={() => setConfirm(null)}
        onConfirm={() => { push("confirm:default"); setConfirm(null); }}
      />

      <PromptDialog
        open={promptOpen}
        title="重命名音色"
        label="音色名称"
        description="只改显示名，不改音频文件本身。"
        defaultValue="旧名字"
        onCancel={() => setPromptOpen(false)}
        onConfirm={(v) => { push("prompt:" + v); setPromptOpen(false); }}
      />

      <ToastNode toast={toastState} />
    </div>
  );
}

const host = document.createElement("div");
host.id = "probe-root";
host.style.cssText =
  "width:900px;padding:12px;background:#f8fafc;font-family:system-ui,sans-serif;display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start";
document.body.appendChild(host);

createRoot(host).render(<Probe />);
