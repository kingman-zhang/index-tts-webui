/** 个人中心页（/account）：登录/注册、资料编辑、积分（签到/兑换/流水）。 */
import { useCallback, useEffect, useState } from "react";
import {
  Coins, Gift, KeyRound, Loader2, LogIn, Save,
  Ticket, UserRound, Zap, History, CheckCircle2,
} from "lucide-react";
import { Button, Card, CardContent, CardHeader, CardTitle, Input, Label, Textarea, Badge } from "@/components/ui";
import { Header } from "@/components/Header";
import { ToastNode, useToast } from "@/hooks/useAppInit";
import { clearSession, initAuth, navigate, refreshUser, setSession, updateUser, useAuth } from "@/lib/auth";
import { memberApi, type PointLog } from "@/api/members";
import type { MemberUser } from "@/lib/auth";
import { cn } from "@/lib/utils";

let authInited = false;

const KIND_META: Record<PointLog["kind"], { label: string; cls: string }> = {
  earn:    { label: "获取", cls: "bg-green-100 text-green-700" },
  checkin: { label: "签到", cls: "bg-green-100 text-green-700" },
  redeem:  { label: "兑换", cls: "bg-indigo-100 text-indigo-700" },
  grant:   { label: "调整", cls: "bg-blue-100 text-blue-700" },
  refund:  { label: "退款", cls: "bg-amber-100 text-amber-700" },
  spend:   { label: "消费", cls: "bg-gray-100 text-gray-600" },
};

function fmtTime(iso?: string) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function AccountPage() {
  const { toast, showToast } = useToast();
  const { ready, token, user } = useAuth();

  useEffect(() => {
    // 本页不挂 Header（UserMenu），需自行恢复登录态
    if (!authInited) {
      authInited = true;
      void initAuth();
    }
  }, []);

  return (
    <div className="min-h-screen bg-gray-50">
      {/* 顶栏（与工作页共用，含全局导航 tab） */}
      <Header
        name=""
        onRename={() => {}}
        showNameInput={false}
        showTts={false}
        showProjectActions={false}
        ttsOnline={null}
        ttsInfo={null}
      />

      <main className="max-w-4xl mx-auto px-4 py-8">
        {!ready ? (
          <div className="flex justify-center py-20"><Loader2 className="w-6 h-6 text-gray-300 animate-spin" /></div>
        ) : !token || !user ? (
          <AuthPanel onDone={msg => showToast(msg)} />
        ) : (
          <MemberPanel showToast={showToast} />
        )}
      </main>

      <ToastNode toast={toast} />
    </div>
  );
}

// ─── 登录 / 注册 ────────────────────────────────────────────

function AuthPanel({ onDone }: { onDone: (msg: string) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [nickname, setNickname] = useState("");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [countdown, setCountdown] = useState(0);
  const [busy, setBusy] = useState(false);
  const [codeBusy, setCodeBusy] = useState(false);
  const [hint, setHint] = useState("");
  const [err, setErr] = useState("");

  useEffect(() => {
    if (countdown <= 0) return;
    const t = setTimeout(() => setCountdown(c => c - 1), 1000);
    return () => clearTimeout(t);
  }, [countdown]);

  const EMAIL_RE = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;

  const sendCode = async () => {
    setErr("");
    setHint("");
    if (!EMAIL_RE.test(email)) { setErr("请输入正确的邮箱地址"); return; }
    setCodeBusy(true);
    try {
      await memberApi.requestEmailCode(email);
      setCountdown(60);
      setHint("验证码已发送，请查收邮箱（注意垃圾箱）");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "验证码发送失败");
    } finally {
      setCodeBusy(false);
    }
  };

  const submit = async () => {
    setErr("");
    setHint("");
    setBusy(true);
    try {
      let body: { token: string; user: MemberUser };
      if (mode === "login") {
        body = await memberApi.login(username, password);
      } else {
        body = await memberApi.registerEmail(email, code, password, nickname);
      }
      setSession(body.token, body.user);
      onDone(mode === "login" ? `欢迎回来，${body.user.nickname}` : `注册成功，赠送 ${body.user.points} 积分`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = mode === "login"
    ? !!username && !!password
    : EMAIL_RE.test(email) && !!code && !!password;

  return (
    <div className="max-w-sm mx-auto">
      <Card>
        <CardHeader>
          <div className="flex gap-1 p-1 bg-gray-100 rounded-lg">
            {(["login", "register"] as const).map(m => (
              <button
                key={m}
                type="button"
                onClick={() => { setMode(m); setErr(""); }}
                className={cn(
                  "flex-1 h-8 rounded-md text-xs font-medium transition-colors",
                  mode === m ? "bg-white text-indigo-700 shadow-sm" : "text-gray-500 hover:text-gray-700"
                )}
              >
                {m === "login" ? "登录" : "注册"}
              </button>
            ))}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {mode === "login" && (
            <div>
              <Label>用户名或邮箱</Label>
              <Input value={username} onChange={e => setUsername(e.target.value)}
                placeholder="用户名或注册邮箱" onKeyDown={e => e.key === "Enter" && submit()} />
            </div>
          )}
          {mode === "register" && (
            <>
              <div>
                <Label>邮箱</Label>
                <Input type="email" value={email} onChange={e => setEmail(e.target.value)}
                  placeholder="you@example.com" onKeyDown={e => e.key === "Enter" && submit()} />
              </div>
              <div>
                <Label>验证码</Label>
                <div className="flex gap-2">
                  <Input value={code} onChange={e => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                    placeholder="6 位数字" className="flex-1" onKeyDown={e => e.key === "Enter" && submit()} />
                  <Button variant="outline" size="sm" className="shrink-0 whitespace-nowrap"
                    disabled={codeBusy || countdown > 0 || !EMAIL_RE.test(email)}
                    onClick={sendCode}>
                    {codeBusy ? "发送中" : countdown > 0 ? `${countdown}s 后重发` : "获取验证码"}
                  </Button>
                </div>
              </div>
            </>
          )}
          {mode === "register" && (
            <div>
              <Label>昵称（可选）</Label>
              <Input value={nickname} onChange={e => setNickname(e.target.value)}
                placeholder="不填则使用邮箱前缀" />
            </div>
          )}
          <div>
            <Label>密码</Label>
            <Input type="password" value={password} onChange={e => setPassword(e.target.value)}
              placeholder={mode === "register" ? "至少 6 位" : "输入密码"}
              onKeyDown={e => e.key === "Enter" && submit()} />
          </div>
          {err && <p className="text-xs text-red-500">{err}</p>}
          {!err && hint && <p className="text-xs text-green-600">{hint}</p>}
          <Button className="w-full" icon={busy ? Loader2 : LogIn} disabled={busy || !canSubmit} onClick={submit}>
            {busy ? "请稍候" : mode === "login" ? "登录" : "注册（赠送积分）"}
          </Button>
          {mode === "register" && (
            <p className="text-[0.6875rem] text-gray-400 text-center">新用户注册即送积分，可用于语音合成</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ─── 已登录：资料 / 积分 / 流水 ─────────────────────────────

function MemberPanel({ showToast }: { showToast: (msg: string) => void }) {
  const { user } = useAuth();
  const onProfileSaved = useCallback(() => { void refreshUser(); }, []);
  return (
    <div className="space-y-5">
      {/* 概览条 */}
      <Card>
        <CardContent className="flex items-center gap-4 py-4">
          <div className="w-12 h-12 rounded-full bg-gradient-to-br from-indigo-500 to-purple-600 text-white text-sm font-semibold flex items-center justify-center shrink-0">
            {(user!.nickname || user!.username).slice(0, 2)}
          </div>
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-gray-800 truncate">{user!.nickname || user!.username}</p>
            <p className="text-xs text-gray-400">@{user!.username} · 注册于 {fmtTime(user!.created_at).slice(0, 10) || "—"}</p>
          </div>
          <div className="text-right shrink-0">
            <p className="text-[0.6875rem] text-gray-400">积分余额</p>
            <p className="text-xl font-bold text-amber-600 tabular-nums flex items-center gap-1 justify-end">
              <Coins className="w-4 h-4" />{user!.points}
            </p>
          </div>
        </CardContent>
      </Card>

      <div className="grid md:grid-cols-2 gap-5">
        <ProfileCard showToast={showToast} onSaved={onProfileSaved} />
        <PointsCard showToast={showToast} />
      </div>

      <LogsCard />
    </div>
  );
}

function ProfileCard({ showToast, onSaved }: { showToast: (m: string) => void; onSaved: () => void }) {
  const { user } = useAuth();
  const [nickname, setNickname] = useState(user!.nickname || "");
  const [bio, setBio] = useState(user!.bio || "");
  const [pwdOpen, setPwdOpen] = useState(false);
  const [oldPwd, setOldPwd] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [busy, setBusy] = useState(false);

  const dirty = nickname !== (user!.nickname || "") || bio !== (user!.bio || "");

  const save = async () => {
    setBusy(true);
    try {
      const body = await memberApi.updateProfile(nickname, bio);
      updateUser(body.user);
      showToast("资料已保存");
      onSaved();
    } catch (e) {
      showToast(e instanceof Error ? e.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  const changePwd = async () => {
    setBusy(true);
    try {
      await memberApi.changePassword(oldPwd, newPwd);
      clearSession(); // 后端已吊销全部会话，本地同步退出
      navigate("/account");
      showToast("密码已修改，请重新登录");
    } catch (e) {
      showToast(e instanceof Error ? e.message : "修改失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-1.5"><UserRound className="w-4 h-4 text-indigo-500" />基础资料</CardTitle>
        {!pwdOpen && (
          <button type="button" onClick={() => setPwdOpen(true)}
            className="text-xs text-gray-400 hover:text-indigo-600 transition-colors flex items-center gap-1">
            <KeyRound className="w-3 h-3" />改密码
          </button>
        )}
      </CardHeader>
      <CardContent className="space-y-3">
        {pwdOpen ? (
          <>
            <div>
              <Label>当前密码</Label>
              <Input type="password" value={oldPwd} onChange={e => setOldPwd(e.target.value)} />
            </div>
            <div>
              <Label>新密码（至少 6 位）</Label>
              <Input type="password" value={newPwd} onChange={e => setNewPwd(e.target.value)} />
            </div>
            <div className="flex gap-2">
              <Button size="sm" icon={busy ? Loader2 : KeyRound} disabled={busy || !oldPwd || newPwd.length < 6} onClick={changePwd}>
                确认修改
              </Button>
              <Button size="sm" variant="ghost" onClick={() => { setPwdOpen(false); setOldPwd(""); setNewPwd(""); }}>
                取消
              </Button>
            </div>
            <p className="text-[0.6875rem] text-gray-400">修改密码后需要重新登录</p>
          </>
        ) : (
          <>
            <div>
              <Label>昵称</Label>
              <Input value={nickname} onChange={e => setNickname(e.target.value)} maxLength={24} />
            </div>
            <div>
              <Label>简介（最多 200 字）</Label>
              <Textarea value={bio} onChange={e => setBio(e.target.value)} maxLength={200}
                placeholder="介绍一下自己…" className="min-h-[64px]" />
            </div>
            <Button size="sm" icon={dirty ? Save : CheckCircle2} disabled={busy || !dirty} onClick={save}>
              {busy ? "保存中" : "保存资料"}
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function PointsCard({ showToast }: { showToast: (m: string) => void }) {
  const { user } = useAuth();
  const [checkinDone, setCheckinDone] = useState(false);
  const [checkinDays, setCheckinDays] = useState(0);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    memberApi.checkinStatus()
      .then(s => { setCheckinDone(s.checked_in_today); setCheckinDays(s.total_days); })
      .catch(() => {});
  }, []);

  const doCheckin = async () => {
    setBusy(true);
    try {
      const r = await memberApi.checkin();
      setCheckinDone(true);
      setCheckinDays(d => d + 1);
      await refreshUser();
      showToast(`签到成功 +${r.added} 积分`);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "签到失败");
    } finally {
      setBusy(false);
    }
  };

  const doRedeem = async () => {
    if (!code.trim()) return;
    setBusy(true);
    try {
      const r = await memberApi.redeem(code.trim());
      setCode("");
      await refreshUser();
      showToast(`兑换成功 +${r.added} 积分`);
    } catch (e) {
      showToast(e instanceof Error ? e.message : "兑换失败");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-1.5"><Zap className="w-4 h-4 text-amber-500" />积分</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="text-xs text-gray-500">
            累计签到 <span className="font-semibold text-gray-700">{checkinDays}</span> 天
          </div>
          <Button size="sm" variant={checkinDone ? "secondary" : "outline"} icon={checkinDone ? CheckCircle2 : Coins}
            disabled={busy || checkinDone} onClick={doCheckin}>
            {checkinDone ? "今日已签到" : "每日签到"}
          </Button>
        </div>
        <div className="border-t border-gray-100 pt-3">
          <Label className="flex items-center gap-1"><Ticket className="w-3 h-3" />优惠码兑换</Label>
          <div className="flex gap-2">
            <Input value={code} onChange={e => setCode(e.target.value)}
              placeholder="输入优惠码" className="uppercase"
              onKeyDown={e => e.key === "Enter" && doRedeem()} />
            <Button size="sm" icon={busy ? Loader2 : Gift} disabled={busy || !code.trim()} onClick={doRedeem}>
              兑换
            </Button>
          </div>
        </div>
        <p className="text-[0.6875rem] text-gray-400">积分可在语音合成时抵扣用量，余额 <span className="text-amber-600 font-medium">{user!.points}</span></p>
      </CardContent>
    </Card>
  );
}

function LogsCard() {
  const { user } = useAuth();
  const [logs, setLogs] = useState<PointLog[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (offset: number) => {
    setLoading(true);
    try {
      const body = await memberApi.logs(offset, 20);
      setLogs(prev => offset === 0 ? body.logs : [...prev, ...body.logs]);
      setTotal(body.total);
    } catch { /* 忽略 */ } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(0); }, [load]);

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-1.5"><History className="w-4 h-4 text-blue-500" />积分记录</CardTitle>
        <Badge color="gray">{total} 条</Badge>
      </CardHeader>
      <CardContent className="p-0">
        {!user || logs.length === 0 && !loading ? (
          <p className="text-xs text-gray-400 text-center py-8">暂无积分记录</p>
        ) : (
          <div className="divide-y divide-gray-50">
            {logs.map(log => {
              const meta = KIND_META[log.kind] ?? { label: log.kind, cls: "bg-gray-100 text-gray-600" };
              return (
                <div key={log.id} className="flex items-center gap-3 px-4 py-2.5">
                  <span className={cn("text-[0.625rem] font-medium px-1.5 py-0.5 rounded-full shrink-0", meta.cls)}>
                    {meta.label}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="text-xs text-gray-700 truncate">{log.reason}</p>
                    <p className="text-[0.625rem] text-gray-400">{fmtTime(log.created_at)}</p>
                  </div>
                  <div className="text-right shrink-0">
                    <p className={cn("text-xs font-semibold tabular-nums", log.delta > 0 ? "text-green-600" : "text-gray-500")}>
                      {log.delta > 0 ? `+${log.delta}` : log.delta}
                    </p>
                    <p className="text-[0.625rem] text-gray-400 tabular-nums">余 {log.balance_after}</p>
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {logs.length < total && (
          <div className="p-3 border-t border-gray-50 text-center">
            <Button size="sm" variant="ghost" disabled={loading} onClick={() => void load(logs.length)}>
              {loading ? "加载中…" : `加载更多（${total - logs.length}）`}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
