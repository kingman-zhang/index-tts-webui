import { useEffect, useState } from "react";
import { BookOpen, Plus, Trash2, Edit2, Check, X, User } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, Button, Input, Badge } from "./ui";
import { api, type GlossaryView, type GlossaryTermItem } from "@/api/client";

interface GlossaryPanelProps {
  collapsed: boolean;
  onToggle: () => void;
}

/** 术语词汇表面板：只维护「我的词条」。
 *
 *  内置词库（平台全局库）由管理员维护、用户看不到也无需维护，`GET /api/glossary`
 *  不再返回它 —— 合成时后端仍会把内置库叠加上去，只是这里不暴露。
 */
export function GlossaryPanel({ collapsed, onToggle }: GlossaryPanelProps) {
  const [view, setView] = useState<GlossaryView | null>(null);

  const [newOriginal, setNewOriginal] = useState("");
  const [newReplacement, setNewReplacement] = useState("");

  const [editing, setEditing] = useState<string | null>(null);
  const [editOriginal, setEditOriginal] = useState("");
  const [editReplacement, setEditReplacement] = useState("");

  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      setView(await api.getGlossary());
    } catch {}
  };
  useEffect(() => { load(); }, []);

  const mine = view?.mine ?? [];
  const loggedIn = view?.logged_in ?? false;

  /** 统一操作包装：成功后用服务端返回的最新视图刷新，失败展示错误 */
  const run = async (fn: () => Promise<GlossaryView>) => {
    setError("");
    setBusy(true);
    try {
      setView(await fn());
    } catch (e: any) {
      setError(e?.message || "操作失败");
    } finally {
      setBusy(false);
    }
  };

  const requireLogin = () => {
    setError("请先登录后再维护个人词条");
  };

  const add = async () => {
    if (!newOriginal.trim()) return;
    if (!loggedIn) return requireLogin();
    await run(() => api.addGlossaryTerm(newOriginal.trim(), newReplacement.trim()));
    setNewOriginal("");
    setNewReplacement("");
  };

  const remove = async (original: string) => {
    if (!loggedIn) return requireLogin();
    await run(() => api.deleteGlossaryTerm(original));
  };

  const saveEdit = async () => {
    if (editing === null) return;
    if (!loggedIn) return requireLogin();
    const updated = mine.map((t) =>
      t.original === editing
        ? { original: editOriginal.trim(), replacement: editReplacement.trim() }
        : { original: t.original, replacement: t.replacement }
    );
    setEditing(null);
    await run(() => api.updateGlossary(updated));
  };

  if (collapsed) {
    return (
      <Card>
        <CardHeader className="cursor-pointer" onClick={onToggle}>
          <div className="flex items-center gap-2">
            <BookOpen className="w-4 h-4 text-amber-600" />
            <CardTitle>术语词汇表</CardTitle>
            {!!view?.count && <Badge color="amber">{view.count}</Badge>}
          </div>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader className="cursor-pointer" onClick={onToggle}>
        <div className="flex items-center gap-2">
          <BookOpen className="w-4 h-4 text-amber-600" />
          <CardTitle>术语词汇表</CardTitle>
          {!!view?.count && <Badge color="amber">{view.count}</Badge>}
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        <p className="text-[0.75rem] text-gray-500">
          合成时自动替换文本中的词，解决专有名词与多音字读音问题。
        </p>

        {error && (
          <div className="px-2 py-1.5 rounded-lg bg-red-50 border border-red-100 text-[0.75rem] text-red-600">
            {error}
          </div>
        )}

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-1.5">
              <User className="w-3.5 h-3.5 text-indigo-500" />
              <span className="text-xs font-medium text-gray-700">我的词条</span>
            </div>
            <span className="text-[11px] text-gray-400">
              {loggedIn ? `${mine.length} 条` : "登录后可用"}
            </span>
          </div>

          <div className="flex gap-2 items-end">
            <div className="flex-1">
              <Input
                value={newOriginal}
                onChange={(e) => setNewOriginal(e.target.value)}
                placeholder="原词，如 游说"
                className="h-8 text-xs"
                disabled={!loggedIn}
              />
            </div>
            <div className="flex-1">
              <Input
                value={newReplacement}
                onChange={(e) => setNewReplacement(e.target.value)}
                placeholder="替换为，如 游睡"
                className="h-8 text-xs"
                disabled={!loggedIn}
              />
            </div>
            <Button
              size="sm"
              icon={Plus}
              onClick={add}
              disabled={busy || !loggedIn || !newOriginal.trim()}
            >
              添加
            </Button>
          </div>

          <div className="space-y-1.5 max-h-[260px] overflow-y-auto scrollbar-thin">
            {mine.length === 0 ? (
              <p className="text-xs text-gray-400 text-center py-3">
                {loggedIn ? "暂无个人词条" : "登录后可添加自己的词条"}
              </p>
            ) : (
              mine.map((t: GlossaryTermItem) => {
                const disabled = !t.replacement;
                return (
                  <div
                    key={t.original}
                    className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-indigo-50 border border-indigo-100"
                  >
                    {editing === t.original ? (
                      <>
                        <Input
                          value={editOriginal}
                          onChange={(e) => setEditOriginal(e.target.value)}
                          className="h-7 text-xs flex-1"
                        />
                        <span className="text-gray-400">→</span>
                        <Input
                          value={editReplacement}
                          onChange={(e) => setEditReplacement(e.target.value)}
                          className="h-7 text-xs flex-1"
                        />
                        <button onClick={saveEdit} className="text-green-600 hover:text-green-800">
                          <Check className="w-3.5 h-3.5" />
                        </button>
                        <button onClick={() => setEditing(null)} className="text-gray-400 hover:text-gray-600">
                          <X className="w-3.5 h-3.5" />
                        </button>
                      </>
                    ) : (
                      <>
                        <span className="text-xs font-mono text-gray-700 min-w-[52px]">{t.original}</span>
                        <span className="text-gray-400 text-xs">→</span>
                        <span className={`text-xs font-mono flex-1 truncate ${disabled ? "text-gray-400" : "text-indigo-600"}`}>
                          {disabled ? "（已停用）" : t.replacement}
                        </span>
                        <button
                          onClick={() => {
                            setEditing(t.original);
                            setEditOriginal(t.original);
                            setEditReplacement(t.replacement);
                          }}
                          className="text-gray-400 hover:text-indigo-600"
                        >
                          <Edit2 className="w-3 h-3" />
                        </button>
                        <button onClick={() => remove(t.original)} className="text-gray-400 hover:text-red-500">
                          <Trash2 className="w-3 h-3" />
                        </button>
                      </>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
