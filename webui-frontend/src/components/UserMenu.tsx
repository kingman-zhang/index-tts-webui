/** 顶栏用户菜单：未登录显示「登录」，登录后显示昵称+积分下拉（两个工作页共用）。 */
import { useEffect, useRef, useState } from "react";
import { ChevronDown, Coins, LogOut, UserRound } from "lucide-react";
import { Button } from "./ui";
import { clearSession, initAuth, navigate, refreshUser, useAuth } from "@/lib/auth";
import { memberApi } from "@/api/members";
import { cn } from "@/lib/utils";

let inited = false;

export function UserMenu() {
  const { ready, token, user } = useAuth();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!inited) {
      inited = true;
      void initAuth();
    }
  }, []);

  // 登录后静默刷新余额（签到/扣费可能发生在别的页面）
  useEffect(() => {
    if (token) void refreshUser();
  }, [token]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const handleLogout = async () => {
    setBusy(true);
    try {
      await memberApi.logout();
    } catch { /* token 失效等场景忽略 */ }
    clearSession();
    setOpen(false);
    setBusy(false);
    navigate("/account");
  };

  if (!ready) return <div className="w-16" />; // 占位防抖动

  if (!token || !user) {
    return (
      <Button size="sm" variant="outline" icon={UserRound} onClick={() => navigate("/account")}>
        登录
      </Button>
    );
  }

  return (
    <div className="relative" ref={menuRef}>
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        className={cn(
          "flex items-center gap-1.5 h-9 pl-1.5 pr-2 rounded-lg border transition-colors",
          open ? "border-indigo-300 bg-indigo-50" : "border-gray-200 bg-white hover:border-indigo-300"
        )}
      >
        <span className="w-6 h-6 rounded-full bg-gradient-to-br from-indigo-500 to-purple-600 text-white text-[0.625rem] font-semibold flex items-center justify-center">
          {(user.nickname || user.username).slice(0, 2)}
        </span>
        <span className="text-xs font-medium text-gray-700 max-w-[6rem] truncate">
          {user.nickname || user.username}
        </span>
        <span className="flex items-center gap-0.5 text-[0.6875rem] text-amber-600 font-medium">
          <Coins className="w-3 h-3" />
          {user.points}
        </span>
        <ChevronDown className={cn("w-3 h-3 text-gray-400 transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1.5 z-40 w-44 rounded-xl border border-gray-200 bg-white shadow-lg p-1.5">
          <div className="px-2 py-1.5 border-b border-gray-100 mb-1">
            <p className="text-xs font-medium text-gray-700 truncate">{user.nickname || user.username}</p>
            <p className="text-[0.6875rem] text-gray-400">@{user.username}</p>
          </div>
          <button
            type="button"
            onClick={() => { setOpen(false); navigate("/account"); }}
            className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs text-gray-600 hover:bg-indigo-50/60 hover:text-indigo-700 transition-colors"
          >
            <UserRound className="w-3.5 h-3.5" /> 个人中心 / 积分
          </button>
          <button
            type="button"
            onClick={handleLogout}
            disabled={busy}
            className="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-xs text-gray-600 hover:bg-red-50 hover:text-red-600 transition-colors disabled:opacity-50"
          >
            <LogOut className="w-3.5 h-3.5" /> 退出登录
          </button>
        </div>
      )}
    </div>
  );
}
