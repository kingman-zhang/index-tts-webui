import { useState, useMemo, useEffect } from "react";
import { Search, X, Play, Square, User, Layers, Heart, Upload as UploadIcon, Pencil, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { api } from "@/api/client";
import type { VoiceFile } from "@/types";

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

type Tab = "mine" | "female" | "male" | "emotion" | "favorites";

export function VoicePicker({
  open, onClose, currentPath, voiceFiles, presetVoices,
  onSelect, onPreview, playingName, onRename, onDelete,
}: VoicePickerProps) {
  const [tab, setTab] = useState<Tab>("mine");
  const [search, setSearch] = useState("");
  const [favoritePaths, setFavoritePaths] = useState<string[]>([]);

  useEffect(() => {
    if (!open) return;
    api.listVoiceFavorites().then(result => setFavoritePaths(result.paths)).catch(() => setFavoritePaths([]));
  }, [open]);

  const allVoices = useMemo(() => {
    const voices = [...voiceFiles, ...presetVoices.female, ...presetVoices.male, ...presetVoices.emotion];
    return voices.filter((voice, index, list) => list.findIndex(item => item.path === voice.path) === index);
  }, [voiceFiles, presetVoices]);
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
              placeholder="搜索音色名称..."
              className="w-full h-10 pl-9 pr-3 rounded-lg border border-gray-200 bg-gray-50 text-sm focus:border-indigo-400 focus:bg-white focus:outline-none"
              autoFocus
            />
          </div>
        </div>

        {/* 音色列表 */}
        <div className="flex-1 overflow-y-auto scrollbar-thin p-2">
          {allInTab.length === 0 ? (
            <div className="text-center py-12">
              <p className="text-xs text-gray-400">
                {tab === "favorites" ? "还没有收藏音色，点击音色右侧的收藏按钮添加" : tab === "mine" ? "还没有自定义音色，点击下方上传或录制" : "没有找到匹配的音色"}
              </p>
            </div>
          ) : (
            allInTab.map(f => {
              const isSelected = currentPath === f.path;
              const isPlaying = playingName === f.name;
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
                    onClick={e => { e.stopPropagation(); onPreview(f.name); }}
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
                    "text-[10px] px-1.5 py-0.5 rounded shrink-0",
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
                    <span className="text-[10px] text-indigo-500 font-medium shrink-0">已选中</span>
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
