import { useState, useEffect } from "react";
import { BookOpen, Plus, Trash2, Edit2, Check, X } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, Button, Input, Label, Badge } from "./ui";
import { api } from "@/api/client";

interface GlossaryTerm {
  original: string;
  replacement: string;
}

interface GlossaryPanelProps {
  collapsed: boolean;
  onToggle: () => void;
}

export function GlossaryPanel({ collapsed, onToggle }: GlossaryPanelProps) {
  const [terms, setTerms] = useState<GlossaryTerm[]>([]);
  const [newOriginal, setNewOriginal] = useState("");
  const [newReplacement, setNewReplacement] = useState("");
  const [editingIdx, setEditingIdx] = useState<number | null>(null);
  const [editOriginal, setEditOriginal] = useState("");
  const [editReplacement, setEditReplacement] = useState("");
  const [loading, setLoading] = useState(false);

  const load = async () => {
    try {
      const r = await api.getGlossary();
      setTerms(r.terms);
    } catch {}
  };

  useEffect(() => { load(); }, []);

  const add = async () => {
    if (!newOriginal.trim()) return;
    setLoading(true);
    try {
      const r = await api.addGlossaryTerm(newOriginal.trim(), newReplacement.trim());
      setTerms(r.terms);
      setNewOriginal("");
      setNewReplacement("");
    } finally { setLoading(false); }
  };

  const remove = async (original: string) => {
    const r = await api.deleteGlossaryTerm(original);
    setTerms(r.terms);
  };

  const saveEdit = async () => {
    if (editingIdx === null) return;
    const updated = [...terms];
    updated[editingIdx] = { original: editOriginal, replacement: editReplacement };
    const r = await api.updateGlossary(updated);
    setTerms(r.terms);
    setEditingIdx(null);
  };

  if (collapsed) {
    return (
      <Card>
        <CardHeader className="cursor-pointer" onClick={onToggle}>
          <div className="flex items-center gap-2">
            <BookOpen className="w-4 h-4 text-amber-600" />
            <CardTitle>术语词汇表</CardTitle>
            {terms.length > 0 && <Badge color="amber">{terms.length}</Badge>}
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
          {terms.length > 0 && <Badge color="amber">{terms.length}</Badge>}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-[11px] text-gray-500">
          合成时自动替换文本中的术语，解决专有名词发音问题。
        </p>

        {/* 添加新术语 */}
        <div className="flex gap-2 items-end">
          <div className="flex-1">
            <Label className="text-[11px]">原词</Label>
            <Input
              value={newOriginal}
              onChange={e => setNewOriginal(e.target.value)}
              placeholder="如 AI"
              className="h-8 text-xs"
            />
          </div>
          <div className="flex-1">
            <Label className="text-[11px]">替换为</Label>
            <Input
              value={newReplacement}
              onChange={e => setNewReplacement(e.target.value)}
              placeholder="如 A I"
              className="h-8 text-xs"
            />
          </div>
          <Button size="sm" icon={Plus} onClick={add} disabled={loading || !newOriginal.trim()}>
            添加
          </Button>
        </div>

        {/* 术语列表 */}
        <div className="space-y-1.5 max-h-[200px] overflow-y-auto scrollbar-thin">
          {terms.length === 0 ? (
            <p className="text-xs text-gray-400 text-center py-4">暂无术语</p>
          ) : (
            terms.map((t, idx) => (
              <div key={idx} className="flex items-center gap-2 px-2 py-1.5 rounded-lg bg-amber-50 border border-amber-100">
                {editingIdx === idx ? (
                  <>
                    <Input
                      value={editOriginal}
                      onChange={e => setEditOriginal(e.target.value)}
                      className="h-7 text-xs flex-1"
                    />
                    <span className="text-gray-400">→</span>
                    <Input
                      value={editReplacement}
                      onChange={e => setEditReplacement(e.target.value)}
                      className="h-7 text-xs flex-1"
                    />
                    <button onClick={saveEdit} className="text-green-600 hover:text-green-800">
                      <Check className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => setEditingIdx(null)} className="text-gray-400 hover:text-gray-600">
                      <X className="w-3.5 h-3.5" />
                    </button>
                  </>
                ) : (
                  <>
                    <span className="text-xs font-mono text-gray-700 min-w-[60px]">{t.original}</span>
                    <span className="text-gray-400 text-xs">→</span>
                    <span className="text-xs font-mono text-indigo-600 flex-1 truncate">{t.replacement || "(删除)"}</span>
                    <button
                      onClick={() => { setEditingIdx(idx); setEditOriginal(t.original); setEditReplacement(t.replacement); }}
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
            ))
          )}
        </div>
      </CardContent>
    </Card>
  );
}
