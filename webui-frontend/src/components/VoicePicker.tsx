import { useState, useMemo, useEffect } from "react";
import { Search, X, Play, Square, User, Layers, Heart, Upload as UploadIcon, Pencil, Trash2, Library } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/api/client";
import type { VoiceFile, BreezeblueVoice } from "@/types";

interface PresetVoices {
  female: VoiceFile[];
  male: VoiceFile[];
  emotion: VoiceFile[];
}

interface VoicePickerProps {
  open: boolean;
  onClose: () => void;
  currentPath: string | null;
  voiceFiles: VoiceFile[];
  presetVoices: PresetVoices;
  onSelect: (path: string, name: string) => void;
  onPreview: (name: string) => void;
  playingName: string | null;
  onRename?: (voice: VoiceFile) => void;
  onDelete?: (voice: VoiceFile) => void;
}

type Tab = "mine" | "female" | "male" | "emotion" | "library" | "favorites";

export function VoicePicker({
  open, onClose, currentPath, voiceFiles, presetVoices,
  onSelect, onPreview, playingName, onRename, onDelete,
}: VoicePickerProps) {
  const [tab, setTab] = useState<Tab>("mine");
  const [search, setSearch] = useState("");
  const [favoritePaths, setFavoritePaths] = useState<string[]>([]);
  // ── 音色库（BreezeBlue）状态 ──
  const [bbVoices, setBbVoices] = useState<BreezeblueVoice[]>([]);
  const [bbLoading, setBbLoading] = useState(false);
  const [bbError, setBbError] = useState<string | null>(null);
  const [bbCat, setBbCat] = useState("");
  const [bbGender, setBbGender] = useState("");
  const [bbAge, setBbAge] = useState("");
  const [bbLimit, setBbLimit] = useState(30);

  const loadBbVoices = () => {
    if (bbLoading || bbVoices.length > 0) return;
    setBbLoading(true);
    setBbError(null);
    api.listBreezeblueVoices()
      .then(items => setBbVoices(items))
      .catch(e => setBbError(e.message || "加载失败"))
      .finally(() => setBbLoading(false));
  };
  const ensureBbLoaded = () => {
    if (bbVoices.length === 0 && !bbLoading && !bbError) loadBbVoices();
  };

  useEffect(() => {
    if (!open) return;
    api.listVoiceFavorites().then(result => setFavoritePaths(result.paths)).catch(() => setFavoritePaths([]));
  }, [open]);

  // 音色库条目映射为 VoiceFile（供收藏 tab 复用）
  const bbAsVoiceFiles = useMemo<VoiceFile[]>(() => bbVoices.map(v => ({
    name: v.name,
    path: v.path,
    size_kb: 0,
    source: "preset" as const,
    preview_name: v.filename,
  })), [bbVoices]);

  const allVoices = useMemo(() => {
    const voices = [...voiceFiles, ...presetVoices.female, ...presetVoices.male, ...presetVoices.emotion, ...bbAsVoiceFiles];
    return voices.filter((voice, index, list) => list.findIndex(item => item.path === voice.path) === index);
  }, [voiceFiles, presetVoices, bbAsVoiceFiles]);
  const favoriteVoices = useMemo(
    () => favoritePaths.map(path => allVoices.find(voice => voice.path === path)).filter((voice): voice is VoiceFile => Boolean(voice)),
    [favoritePaths, allVoices]
  );
  const mineCount = voiceFiles.length;

  const toggleFavorite = (voice: VoiceFile) => {
    setFavoritePaths(current => {
      const next = current.includes(voice.path)
        ? current.filter(path => path !== voice.path)
        : [...current, voice.path];
      api.saveVoiceFavorites(next).catch(() => {
        setFavoritePaths(current);
        alert("收藏保存失败，请检查 WebUI 后端服务");
      });
      return next;
    });
  };

  // 打开弹窗时始终默认选中"我的音色"
  useEffect(() => {
    if (open) {
      setTab("mine");
      setSearch("");
    }
  }, [open]);

  // 切到音色库 tab 时懒加载全量数据（api client 内部有模块级缓存）
  useEffect(() => {
    if (open && tab === "library") ensureBbLoaded();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, tab]);

  // 音色库筛选与分页
  const bbFiltered = useMemo(() => {
    const kw = search.trim().toLowerCase();
    return bbVoices.filter(v => {
      if (bbCat && v.category !== bbCat) return false;
      if (bbGender && v.gender !== bbGender) return false;
      if (bbAge && v.age !== bbAge) return false;
      if (!kw) return true;
      return (
        v.name.toLowerCase().includes(kw) ||
        v.description.toLowerCase().includes(kw) ||
        v.tones_zh.some(t => t.toLowerCase().includes(kw)) ||
        v.tones.some(t => t.toLowerCase().includes(kw)) ||
        v.category_zh.toLowerCase().includes(kw)
      );
    });
  }, [bbVoices, search, bbCat, bbGender, bbAge]);
  const bbVisible = useMemo(() => bbFiltered.slice(0, bbLimit), [bbFiltered, bbLimit]);

  const bbFacets = useMemo(() => {
    const cats = new Map<string, { code: string; name: string; count: number }>();
    const genders = new Map<string, { code: string; name: string; count: number }>();
    const ages = new Map<string, { code: string; name: string; count: number }>();
    for (const v of bbVoices) {
      if (v.category) {
        const c = cats.get(v.category) ?? { code: v.category, name: v.category_zh, count: 0 };
        c.count++; cats.set(v.category, c);
      }
      if (v.gender) {
        const g = genders.get(v.gender) ?? { code: v.gender, name: v.gender_zh, count: 0 };
        g.count++; genders.set(v.gender, g);
      }
      if (v.age) {
        const a = ages.get(v.age) ?? { code: v.age, name: v.age_zh, count: 0 };
        a.count++; ages.set(v.age, a);
      }
    }
    const byCount = (x: { count: number }, y: { count: number }) => y.count - x.count;
    return {
      cats: [...cats.values()].sort(byCount),
      genders: [...genders.values()].sort(byCount),
      ages: [...ages.values()].sort(byCount),
    };
  }, [bbVoices]);

  const allInTab: VoiceFile[] = useMemo(() => {
    let list: VoiceFile[] = [];
    if (tab === "mine") list = voiceFiles;
    else if (tab === "female") list = presetVoices.female;
    else if (tab === "male") list = presetVoices.male;
    else if (tab === "emotion") list = presetVoices.emotion;
    else list = favoriteVoices;

    if (!search.trim()) return list;
    const q = search.toLowerCase();
    return list.filter(f => f.name.toLowerCase().includes(q));
  }, [tab, search, voiceFiles, presetVoices, favoriteVoices]);

  const tabs: { key: Tab; label: string; icon: any; count: number; color: string }[] = [
    { key: "mine", label: "我的音色", icon: UploadIcon, count: mineCount, color: "indigo" },
    { key: "female", label: "预设 · 女声", icon: Heart, count: presetVoices.female.length, color: "pink" },
    { key: "male", label: "预设 · 男声", icon: User, count: presetVoices.male.length, color: "blue" },
    { key: "emotion", label: "情感参考", icon: Layers, count: presetVoices.emotion.length, color: "amber" },
    { key: "library", label: "音色库", icon: Library, count: bbVoices.length || 310, color: "teal" },
    { key: "favorites", label: "收藏", icon: Heart, count: favoriteVoices.length, color: "rose" },
  ];

  if (!open) return null;

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-xl w-full max-w-3xl mx-4 max-h-[82vh] flex flex-col"
        onClick={e => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
          <h3 className="text-base font-semibold text-gray-800">选择音色</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Tab 切换 */}
        <div className="flex gap-1 px-4 pt-3 border-b border-gray-100">
          {tabs.map(t => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={cn(
                "flex items-center gap-1.5 px-3 py-2.5 text-sm font-medium border-b-2 transition-colors whitespace-nowrap",
                tab === t.key
                  ? "border-indigo-500 text-indigo-600"
                  : "border-transparent text-gray-400 hover:text-gray-600"
              )}
            >
              <t.icon className="w-3.5 h-3.5" />
              {t.label}
              <span className="text-xs text-gray-400">({t.count})</span>
            </button>
          ))}
        </div>

        {/* 搜索框 */}
        <div className="px-4 py-2 border-b border-gray-100">
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
            <input
              type="text"
              value={search}
              onChange={e => setSearch(e.target.value)}
              placeholder={tab === "library" ? "搜索名称 / 描述 / 标签…" : "搜索音色名称..."}
              className="w-full h-10 pl-9 pr-3 rounded-lg border border-gray-200 bg-gray-50 text-sm focus:border-indigo-400 focus:bg-white focus:outline-none"
              autoFocus
            />
          </div>
          {/* 音色库筛选栏 */}
          {tab === "library" && (
            <div className="flex items-center gap-2 mt-2">
              <select
                value={bbCat} onChange={e => { setBbCat(e.target.value); setBbLimit(30); }}
                className="h-8 text-xs rounded-lg border border-gray-200 bg-gray-50 px-2 focus:border-indigo-400 focus:bg-white focus:outline-none"
              >
                <option value="">全部分类</option>
                {bbFacets.cats.map(c => <option key={c.code} value={c.code}>{c.name} ({c.count})</option>)}
              </select>
              <select
                value={bbGender} onChange={e => { setBbGender(e.target.value); setBbLimit(30); }}
                className="h-8 text-xs rounded-lg border border-gray-200 bg-gray-50 px-2 focus:border-indigo-400 focus:bg-white focus:outline-none"
              >
                <option value="">全部性别</option>
                {bbFacets.genders.map(g => <option key={g.code} value={g.code}>{g.name} ({g.count})</option>)}
              </select>
              <select
                value={bbAge} onChange={e => { setBbAge(e.target.value); setBbLimit(30); }}
                className="h-8 text-xs rounded-lg border border-gray-200 bg-gray-50 px-2 focus:border-indigo-400 focus:bg-white focus:outline-none"
              >
                <option value="">全部年龄段</option>
                {bbFacets.ages.map(a => <option key={a.code} value={a.code}>{a.name} ({a.count})</option>)}
              </select>
              <span className="ml-auto text-xs text-gray-400">{bbFiltered.length} 个</span>
            </div>
          )}
        </div>

        {/* 音色列表 */}
        <div className="flex-1 overflow-y-auto scrollbar-thin p-2" onScroll={ensureBbLoaded}>
          {tab === "library" ? (
            /* ── 音色库：卡片式列表 ── */
            bbLoading ? (
              <div className="text-center py-12"><p className="text-xs text-gray-400">音色库加载中…</p></div>
            ) : bbError ? (
              <div className="text-center py-12">
                <p className="text-xs text-red-400">{bbError}</p>
                <button onClick={() => { setBbError(null); loadBbVoices(); }} className="mt-2 text-xs text-indigo-500 hover:text-indigo-600">重试</button>
              </div>
            ) : bbVisible.length === 0 ? (
              <div className="text-center py-12"><p className="text-xs text-gray-400">没有找到匹配的音色</p></div>
            ) : (
              <div className="flex flex-col gap-2">
                {bbVisible.map(v => {
                  const isSelected = currentPath === v.path;
                  const isPlaying = playingName === v.filename;
                  const fav = favoritePaths.includes(v.path);
                  return (
                    <div
                      key={v.id}
                      onClick={() => { onSelect(v.path, v.name); onClose(); }}
                      className={cn(
                        "flex items-start gap-3 px-3 py-2.5 rounded-lg cursor-pointer transition-colors group border",
                        isSelected ? "bg-indigo-50 border-indigo-200" : "border-transparent hover:bg-gray-50"
                      )}
                    >
                      <button
                        onClick={e => { e.stopPropagation(); onPreview(v.filename); }}
                        className={cn(
                          "w-9 h-9 rounded-full flex items-center justify-center shrink-0 transition-colors mt-0.5",
                          isPlaying ? "bg-red-100 text-red-600" : "bg-indigo-50 text-indigo-400 group-hover:bg-indigo-100 group-hover:text-indigo-600"
                        )}
                        title="试听"
                        aria-label={`试听 ${v.name}`}
                      >
                        {isPlaying ? <Square className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5 ml-0.5" />}
                      </button>
                      <div className="flex-1 min-w-0">
                        <div className="flex items-baseline gap-2">
                          <span className={cn("text-sm truncate", isSelected ? "font-medium text-indigo-700" : "text-gray-700")}>{v.name}</span>
                          <span className="text-xs text-gray-400 shrink-0">
                            {v.category_zh}{v.gender_zh ? ` · ${v.gender_zh}` : ""}{v.age_zh ? ` · ${v.age_zh}` : ""}
                            {v.duration_s ? ` · ${v.duration_s}s` : ""}
                          </span>
                        </div>
                        <p className="text-xs text-gray-500 line-clamp-2 leading-relaxed mt-0.5">{v.description}</p>
                        {(v.tones_zh.length > 0) && (
                          <div className="flex flex-wrap gap-1 mt-1">
                            {v.tones_zh.slice(0, 4).map(t => (
                              <span key={t} className="text-[0.6875rem] px-1.5 py-0.5 rounded bg-teal-50 text-teal-600">{t}</span>
                            ))}
                            {v.tones_zh.length > 4 && (
                              <span className="text-[0.6875rem] text-gray-400 self-center">+{v.tones_zh.length - 4}</span>
                            )}
                          </div>
                        )}
                      </div>
                      <button
                        onClick={e => { e.stopPropagation(); toggleFavorite({ name: v.name, path: v.path, size_kb: 0, source: "preset" }); }}
                        className={cn(
                          "p-1 rounded transition-colors shrink-0",
                          fav ? "text-rose-500 hover:text-rose-600 hover:bg-rose-50" : "text-gray-300 hover:text-rose-500 hover:bg-rose-50"
                        )}
                        title={fav ? "移出收藏" : "收藏音色"}
                        aria-label={fav ? `移出收藏 ${v.name}` : `收藏 ${v.name}`}
                      >
                        <Heart className={cn("w-4 h-4", fav && "fill-current")} />
                      </button>
                      {isSelected && <span className="text-[0.6875rem] text-indigo-500 font-medium shrink-0 self-center">已选中</span>}
                    </div>
                  );
                })}
                {bbFiltered.length > bbVisible.length && (
                  <button
                    onClick={() => setBbLimit(n => n + 30)}
                    className="w-full py-2 text-xs text-indigo-500 hover:text-indigo-600 hover:bg-indigo-50 rounded-lg transition-colors"
                  >
                    加载更多（已显示 {bbVisible.length} / {bbFiltered.length}）
                  </button>
                )}
              </div>
            )
          ) : allInTab.length === 0 ? (
            <div className="text-center py-12">
              <p className="text-xs text-gray-400">
                {tab === "favorites" ? "还没有收藏音色，点击音色右侧的收藏按钮添加" : tab === "mine" ? "还没有自定义音色，点击下方上传或录制" : "没有找到匹配的音色"}
              </p>
            </div>
          ) : (
            allInTab.map(f => {
              const isSelected = currentPath === f.path;
              const previewName = f.preview_name || f.name;
              const isPlaying = playingName === previewName;
              return (
                <div
                  key={f.path}
                  onClick={() => { onSelect(f.path, f.name); onClose(); }}
                  className={cn(
                    "flex items-center gap-3 px-4 py-3 rounded-lg cursor-pointer transition-colors group",
                    isSelected ? "bg-indigo-50 border border-indigo-200" : "hover:bg-gray-50 border border-transparent"
                  )}
                >
                  {/* 试听按钮 */}
                  <button
                    onClick={e => { e.stopPropagation(); onPreview(previewName); }}
                    className={cn(
                      "w-6 h-6 rounded-full flex items-center justify-center shrink-0 transition-colors",
                      isPlaying
                        ? "bg-red-100 text-red-600"
                        : "bg-gray-100 text-gray-400 group-hover:bg-indigo-100 group-hover:text-indigo-600"
                    )}
                  >
                    {isPlaying ? <Square className="w-3 h-3" /> : <Play className="w-3 h-3 ml-0.5" />}
                  </button>

                  <div className="flex-1 min-w-0">
                    <p className={cn("text-sm truncate", isSelected ? "font-medium text-indigo-700" : "text-gray-700")}>
                      {f.name}
                    </p>
                    <p className="text-xs text-gray-400">{f.size_kb ? `${f.size_kb}KB` : ""}</p>
                  </div>

                  <span className={cn(
                    "text-[0.6875rem] px-1.5 py-0.5 rounded shrink-0",
                    tab === "mine" ? "bg-indigo-50 text-indigo-600" : "bg-gray-100 text-gray-500"
                  )}>
                    {tab === "mine" ? "我的音色" : tab === "favorites" ? (f.source === "custom" ? "我的音色" : "预设") : "预设"}
                  </span>
                  <button
                    onClick={e => { e.stopPropagation(); toggleFavorite(f); }}
                    className={cn(
                      "p-1 rounded transition-colors shrink-0",
                      favoritePaths.includes(f.path) ? "text-rose-500 hover:text-rose-600 hover:bg-rose-50" : "text-gray-300 hover:text-rose-500 hover:bg-rose-50"
                    )}
                    title={favoritePaths.includes(f.path) ? "移出收藏" : "收藏音色"}
                    aria-label={favoritePaths.includes(f.path) ? `移出收藏 ${f.name}` : `收藏 ${f.name}`}
                  >
                    <Heart className={cn("w-4 h-4", favoritePaths.includes(f.path) && "fill-current")} />
                  </button>
                  {tab === "mine" && (
                    <div className="flex items-center gap-1 shrink-0 opacity-0 group-hover:opacity-100 transition-opacity">
                      <button onClick={e => { e.stopPropagation(); onRename?.(f); }} className="p-1 text-gray-400 hover:text-indigo-600" title="重命名">
                        <Pencil className="w-3.5 h-3.5" />
                      </button>
                      <button onClick={e => { e.stopPropagation(); onDelete?.(f); }} className="p-1 text-gray-400 hover:text-red-600" title="删除音色">
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  )}
                  {isSelected && (
                    <span className="text-[0.6875rem] text-indigo-500 font-medium shrink-0">已选中</span>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
