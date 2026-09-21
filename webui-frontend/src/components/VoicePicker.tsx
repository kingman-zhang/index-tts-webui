import { useState, useMemo, useEffect, useRef } from "react";
import { Search, X, Play, Square, Pencil, Trash2, SlidersHorizontal, MoreVertical, Star } from "lucide-react";
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

type Tab = "library" | "mine" | "favorites";

/** 推荐音色（从音色库中人工挑选的四个方向：暖声男/优雅男/元气女/市井老者） */
const RECOMMENDED_IDS = ["voc_534z3zu7d53k", "voc_wk37myhn43qy", "voc_un8qby3rnhn6", "voc_xx6h6fxy2c26"];

/** 试试快捷词，点击直接填入搜索 */
const TRY_CHIPS = ["温柔", "磁性", "旁白", "播客", "元气", "治愈"];

/** 头像渐变色板（按性别选色板、按 id 选具体颜色） */
const GRADIENTS_FEMALE = ["from-rose-400 to-orange-300", "from-fuchsia-400 to-pink-300", "from-pink-400 to-rose-300", "from-purple-400 to-fuchsia-300"];
const GRADIENTS_MALE = ["from-sky-400 to-indigo-300", "from-teal-400 to-cyan-300", "from-indigo-400 to-blue-300", "from-cyan-400 to-sky-300"];
const GRADIENTS_NEUTRAL = ["from-gray-400 to-gray-300", "from-slate-400 to-gray-300"];

function avatarGradient(id: string, gender?: string) {
  const pool = gender === "female" ? GRADIENTS_FEMALE : gender === "male" ? GRADIENTS_MALE : GRADIENTS_NEUTRAL;
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return pool[h % pool.length];
}

/** 音色库统一视图模型：BreezeBlue + 内置预设合并后的行数据 */
interface LibVoice {
  key: string;
  path: string;
  name: string;
  gender: string;
  desc: string;
  isBb: boolean;
  sourceLabel: string;   // 来源标签（音色库 / 预设 · 女声 / 预设 · 男声 / 情感参考）
  category: string;      // 分类 code（预设无分类，为空）
  categoryLabel: string;
  age: string;
  metaTags: string[];    // 行右侧展示的标签
  extraTag?: string;     // 「+N」
  previewKey: string;
  voiceFile: VoiceFile;  // 收藏/选用用
}

export function VoicePicker({
  open, onClose, currentPath, voiceFiles, presetVoices,
  onSelect, onPreview, playingName, onRename, onDelete,
}: VoicePickerProps) {
  const [tab, setTab] = useState<Tab>("library");
  const [search, setSearch] = useState("");
  const [favoritePaths, setFavoritePaths] = useState<string[]>([]);
  const [showFilters, setShowFilters] = useState(false);
  const [menuPath, setMenuPath] = useState<string | null>(null); // ⋮ 菜单展开的行
  const menuRef = useRef<HTMLDivElement | null>(null);

  // ── 音色库状态 ──
  const [bbVoices, setBbVoices] = useState<BreezeblueVoice[]>([]);
  const [bbLoading, setBbLoading] = useState(false);
  const [bbError, setBbError] = useState<string | null>(null);
  const [libSource, setLibSource] = useState(""); // "" | "breezeblue" | "preset"
  const [libCat, setLibCat] = useState("");
  const [libGender, setLibGender] = useState("");
  const [libAge, setLibAge] = useState("");
  const [libLimit, setLibLimit] = useState(30);

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

  const presetAll = useMemo(
    () => [...presetVoices.female, ...presetVoices.male, ...presetVoices.emotion],
    [presetVoices]
  );

  // ── 统一音色库：BreezeBlue（310）+ 内置预设（65）合并 ──
  const libVoices = useMemo<LibVoice[]>(() => {
    const bbList: LibVoice[] = bbVoices.map(v => {
      const meta0 = v.gender_zh && v.age_zh ? `${v.gender_zh} · ${v.age_zh}` : v.category_zh;
      return {
        key: `bb-${v.id}`,
        path: v.path,
        name: v.name,
        gender: v.gender,
        desc: v.description,
        isBb: true,
        sourceLabel: "音色库",
        category: v.category,
        categoryLabel: v.category_zh,
        age: v.age,
        metaTags: [meta0, v.tones_zh[0]].filter(Boolean) as string[],
        extraTag: v.tones_zh.length > 1 ? String(v.tones_zh.length - 1) : undefined,
        previewKey: v.filename,
        voiceFile: { name: v.name, path: v.path, size_kb: 0, source: "preset" as const, preview_name: v.filename },
      };
    });
    const presetList: LibVoice[] = presetAll.map(f => {
      const isFemale = presetVoices.female.includes(f);
      const isMale = presetVoices.male.includes(f);
      const label = isFemale ? "预设 · 女声" : isMale ? "预设 · 男声" : "情感参考";
      const displayName = f.name.replace(/\.(mp3|wav|flac|m4a|ogg)$/i, "");
      return {
        key: `p-${f.path}`,
        path: f.path,
        name: displayName,
        gender: isFemale ? "female" : isMale ? "male" : "",
        desc: f.size_kb ? `内置预设参考音频 · ${f.size_kb}KB` : "内置预设参考音频",
        isBb: false,
        sourceLabel: label,
        category: "",
        categoryLabel: label,
        age: "",
        metaTags: [label],
        previewKey: f.preview_name || f.name,
        voiceFile: f,
      };
    });
    return [...bbList, ...presetList];
  }, [bbVoices, presetAll, presetVoices]);

  const allVoices = useMemo(() => {
    const voices = [...voiceFiles, ...libVoices.map(v => v.voiceFile)];
    return voices.filter((voice, index, list) => list.findIndex(item => item.path === voice.path) === index);
  }, [voiceFiles, libVoices]);
  const favoriteVoices = useMemo(
    () => favoritePaths.map(path => allVoices.find(voice => voice.path === path)).filter((voice): voice is VoiceFile => Boolean(voice)),
    [favoritePaths, allVoices]
  );

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

  // 打开弹窗默认「音色库」并清空搜索（筛选保留）
  useEffect(() => {
    if (open) {
      setTab("library");
      setSearch("");
      setMenuPath(null);
      ensureBbLoaded();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // ── 音色库筛选与分页（作用于合并后的统一列表） ──
  const libFiltered = useMemo(() => {
    const kw = search.trim().toLowerCase();
    return libVoices.filter(v => {
      if (libSource === "breezeblue" && !v.isBb) return false;
      if (libSource === "preset" && v.isBb) return false;
      if (libCat && v.category !== libCat) return false;
      if (libGender && v.gender !== libGender) return false;
      if (libAge && v.age !== libAge) return false;
      if (!kw) return true;
      return (
        v.name.toLowerCase().includes(kw) ||
        v.desc.toLowerCase().includes(kw) ||
        v.categoryLabel.toLowerCase().includes(kw) ||
        v.sourceLabel.toLowerCase().includes(kw) ||
        v.metaTags.some(t => t.toLowerCase().includes(kw))
      );
    });
  }, [libVoices, search, libSource, libCat, libGender, libAge]);
  const libVisible = useMemo(() => libFiltered.slice(0, libLimit), [libFiltered, libLimit]);
  const libBrowsing = search.trim() === "" && !libSource && !libCat && !libGender && !libAge;

  const recommended = useMemo(
    () => libVoices.filter(v => v.isBb && RECOMMENDED_IDS.includes(v.key.slice(3))),
    [libVoices]
  );

  const libFacets = useMemo(() => {
    const cats = new Map<string, { code: string; name: string; count: number }>();
    const genders = new Map<string, { code: string; name: string; count: number }>();
    const ages = new Map<string, { code: string; name: string; count: number }>();
    for (const v of libVoices) {
      if (v.category) {
        const c = cats.get(v.category) ?? { code: v.category, name: v.categoryLabel, count: 0 };
        c.count++; cats.set(v.category, c);
      }
      if (v.gender) {
        const g = genders.get(v.gender) ?? { code: v.gender, name: v.gender === "female" ? "女" : "男", count: 0 };
        g.count++; genders.set(v.gender, g);
      }
      if (v.age) {
        const a = ages.get(v.age) ?? { code: v.age, name: v.age === "child" ? "儿童" : v.age === "young" ? "青年" : v.age === "middle_aged" ? "中年" : "老年", count: 0 };
        a.count++; ages.set(v.age, a);
      }
    }
    const byCount = (x: { count: number }, y: { count: number }) => y.count - x.count;
    return {
      cats: [...cats.values()].sort(byCount),
      genders: [...genders.values()].sort(byCount),
      ages: [...ages.values()].sort(byCount),
    };
  }, [libVoices]);

  const allInTab: VoiceFile[] = useMemo(() => {
    let list: VoiceFile[] = [];
    if (tab === "mine") list = voiceFiles;
    else if (tab === "favorites") list = favoriteVoices;

    if (!search.trim()) return list;
    const q = search.toLowerCase();
    return list.filter(f => f.name.toLowerCase().includes(q));
  }, [tab, search, voiceFiles, favoriteVoices]);

  // 点击外部关闭 ⋮ 菜单
  useEffect(() => {
    if (!menuPath) return;
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuPath(null);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menuPath]);

  const tabs: { key: Tab; label: string; count: number }[] = [
    { key: "library", label: "音色库", count: libVoices.length },
    { key: "mine", label: "我的音色", count: voiceFiles.length },
    { key: "favorites", label: "收藏音色", count: favoriteVoices.length },
  ];

  if (!open) return null;

  /** ⋮ 菜单内容（仅"我的音色"有管理操作） */
  const renderRowMenu = (f: VoiceFile) => {
    if (tab !== "mine" || menuPath !== f.path) return null;
    return (
      <div ref={menuRef} className="absolute right-2 top-9 z-10 w-28 bg-white rounded-lg shadow-lg border border-gray-100 py-1">
        <button
          onClick={e => { e.stopPropagation(); setMenuPath(null); onRename?.(f); }}
          className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
        >
          <Pencil className="w-3 h-3" />重命名
        </button>
        <button
          onClick={e => { e.stopPropagation(); setMenuPath(null); onDelete?.(f); }}
          className="w-full flex items-center gap-2 px-3 py-1.5 text-xs text-red-500 hover:bg-red-50"
        >
          <Trash2 className="w-3 h-3" />删除音色
        </button>
      </div>
    );
  };

  /** 通用行渲染 */
  const renderRow = (opts: {
    key: string;
    path: string; name: string;
    avatarId: string; gender?: string;
    title: string;
    desc: string;
    metaTags: string[]; extraTag?: string;
    previewKey: string;
    favoriteOf: VoiceFile;
    showMenu?: boolean;
    selected?: boolean;
  }) => {
    const { key, path, name, avatarId, gender, title, desc, metaTags, extraTag, previewKey, favoriteOf, showMenu, selected } = opts;
    const fav = favoritePaths.includes(path);
    const isPlaying = playingName === previewKey;
    return (
      <div
        key={key}
        onClick={() => { if (!selected) { onSelect(path, name); onClose(); } }}
        className={cn(
          "group relative flex items-center gap-3 px-4 py-3 rounded-xl cursor-pointer transition-colors",
          selected ? "bg-indigo-50/70" : "hover:bg-gray-50"
        )}
      >
        {/* 头像：渐变底 + 首字；hover/播放时变为试听按钮 */}
        <button
          onClick={e => { e.stopPropagation(); onPreview(previewKey); }}
          title={isPlaying ? "停止试听" : "试听"}
          aria-label={`试听 ${name}`}
          className="relative w-10 h-10 rounded-full shrink-0 overflow-hidden"
        >
          <span className={cn(
            "absolute inset-0 bg-gradient-to-br flex items-center justify-center text-white text-sm font-medium transition-opacity",
            avatarGradient(avatarId, gender),
            isPlaying ? "opacity-0" : "group-hover:opacity-0"
          )}>
            {name.slice(0, 1)}
          </span>
          <span className={cn(
            "absolute inset-0 flex items-center justify-center bg-gray-900/70 text-white transition-opacity",
            isPlaying ? "opacity-100" : "opacity-0 group-hover:opacity-100"
          )}>
            {isPlaying ? <Square className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5 ml-0.5" />}
          </span>
        </button>

        {/* 名称 + 描述 */}
        <div className="flex-1 min-w-0">
          <p className={cn("text-sm truncate", selected ? "font-medium text-indigo-700" : "text-gray-800")}>{title}</p>
          {desc && <p className="text-xs text-gray-400 truncate mt-0.5">{desc}</p>}
        </div>

        {/* 右侧元信息标签 */}
        <div className="hidden md:flex items-center gap-1.5 shrink-0">
          {metaTags.filter(Boolean).slice(0, 2).map(t => (
            <span key={t} className={cn(
              "text-xs rounded px-2 py-0.5",
              t === "音色库" || t.startsWith("预设") || t === "情感参考"
                ? "bg-amber-50 text-amber-600"
                : "bg-gray-100 text-gray-500"
            )}>{t}</span>
          ))}
          {extraTag && <span className="text-xs text-gray-400">+{extraTag}</span>}
        </div>

        {/* 选择 / 已选 */}
        <button
          onClick={e => {
            e.stopPropagation();
            if (selected) return;
            onSelect(path, name); onClose();
          }}
          className={cn(
            "shrink-0 h-8 px-4 rounded-full text-xs font-medium transition-colors",
            selected
              ? "bg-indigo-600 text-white"
              : "bg-gray-900 text-white hover:bg-gray-700"
          )}
        >
          {selected ? "已选" : "选择"}
        </button>

        {/* 收藏 */}
        <button
          onClick={e => { e.stopPropagation(); toggleFavorite(favoriteOf); }}
          className={cn(
            "p-1.5 rounded-full transition-colors shrink-0",
            fav ? "text-amber-400 hover:text-amber-500" : "text-gray-300 hover:text-amber-400"
          )}
          title={fav ? "移出收藏" : "收藏音色"}
          aria-label={fav ? `移出收藏 ${name}` : `收藏 ${name}`}
        >
          <Star className={cn("w-4 h-4", fav && "fill-current")} />
        </button>

        {/* 更多操作 */}
        {showMenu && (
          <button
            onClick={e => { e.stopPropagation(); setMenuPath(menuPath === path ? null : path); }}
            className="p-1.5 rounded-full text-gray-300 hover:text-gray-600 hover:bg-gray-100 transition-colors shrink-0"
            title="更多操作"
            aria-label={`更多操作 ${name}`}
          >
            <MoreVertical className="w-4 h-4" />
          </button>
        )}
        {showMenu && renderRowMenu(favoriteOf)}
      </div>
    );
  };

  const libRow = (v: LibVoice) => renderRow({
    key: v.key,
    path: v.path,
    name: v.name,
    avatarId: v.key,
    gender: v.gender,
    title: v.name,
    desc: v.desc,
    metaTags: v.metaTags,
    extraTag: v.extraTag,
    previewKey: v.previewKey,
    favoriteOf: v.voiceFile,
    selected: currentPath === v.path,
  });

  const fileRow = (f: VoiceFile) => renderRow({
    key: f.path,
    path: f.path,
    name: f.name,
    avatarId: f.path,
    title: f.name,
    desc: f.size_kb ? `${f.size_kb}KB` : "",
    metaTags: [],
    previewKey: f.preview_name || f.name,
    favoriteOf: f,
    showMenu: tab === "mine",
    selected: currentPath === f.path,
  });

  const listEmptyText =
    tab === "favorites" ? "还没有收藏音色，点击音色右侧的星标添加"
    : tab === "mine" ? "还没有自定义音色，点击下方上传或录制"
    : "没有找到匹配的音色";

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-4xl mx-4 max-h-[85vh] flex flex-col overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        {/* 标题栏 */}
        <div className="flex items-center justify-between px-6 pt-5 pb-1">
          <h3 className="text-lg font-semibold text-gray-900">音色选择</h3>
          <button onClick={onClose} className="p-1.5 rounded-full text-gray-400 hover:text-gray-600 hover:bg-gray-100">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Tab（下划线式） */}
        <div className="flex gap-6 px-6 border-b border-gray-100">
          {tabs.map(t => (
            <button
              key={t.key}
              onClick={() => { setTab(t.key); setMenuPath(null); }}
              className={cn(
                "pb-2.5 text-sm font-medium border-b-2 -mb-px transition-colors whitespace-nowrap",
                tab === t.key
                  ? "border-gray-900 text-gray-900"
                  : "border-transparent text-gray-400 hover:text-gray-600"
              )}
            >
              {t.label}
              <span className="ml-1 text-xs text-gray-300">{t.count > 0 ? t.count : ""}</span>
            </button>
          ))}
        </div>

        {/* 搜索 + 筛选 */}
        <div className="px-6 pt-3 pb-3 border-b border-gray-50">
          <div className="flex items-center gap-2.5">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-300" />
              <input
                type="text"
                value={search}
                onChange={e => { setSearch(e.target.value); setLibLimit(30); }}
                placeholder={tab === "library" ? "搜索音色库" : "搜索音色名称"}
                className="w-full h-10 pl-9 pr-3 rounded-full border border-gray-200 bg-white text-sm focus:border-indigo-400 focus:outline-none"
              />
            </div>
            {tab === "library" && (
              <button
                onClick={() => setShowFilters(s => !s)}
                className={cn(
                  "flex items-center gap-1.5 h-10 px-4 rounded-full text-sm font-medium transition-colors shrink-0",
                  showFilters || libSource || libCat || libGender || libAge
                    ? "bg-indigo-50 text-indigo-600"
                    : "text-gray-500 hover:bg-gray-50"
                )}
              >
                <SlidersHorizontal className="w-4 h-4" />
                筛选
              </button>
            )}
          </div>

          {/* 筛选展开区（音色库） */}
          {tab === "library" && showFilters && (
            <div className="flex flex-wrap items-center gap-2 mt-2.5">
              <select
                value={libSource} onChange={e => { setLibSource(e.target.value); setLibLimit(30); }}
                className="h-8 text-xs rounded-lg border border-gray-200 bg-white px-2 focus:border-indigo-400 focus:outline-none"
              >
                <option value="">全部来源</option>
                <option value="breezeblue">BreezeBlue 精选</option>
                <option value="preset">内置预设</option>
              </select>
              <select
                value={libCat} onChange={e => { setLibCat(e.target.value); setLibLimit(30); }}
                className="h-8 text-xs rounded-lg border border-gray-200 bg-white px-2 focus:border-indigo-400 focus:outline-none"
              >
                <option value="">全部分类</option>
                {libFacets.cats.map(c => <option key={c.code} value={c.code}>{c.name} ({c.count})</option>)}
              </select>
              <select
                value={libGender} onChange={e => { setLibGender(e.target.value); setLibLimit(30); }}
                className="h-8 text-xs rounded-lg border border-gray-200 bg-white px-2 focus:border-indigo-400 focus:outline-none"
              >
                <option value="">全部性别</option>
                {libFacets.genders.map(g => <option key={g.code} value={g.code}>{g.name} ({g.count})</option>)}
              </select>
              <select
                value={libAge} onChange={e => { setLibAge(e.target.value); setLibLimit(30); }}
                className="h-8 text-xs rounded-lg border border-gray-200 bg-white px-2 focus:border-indigo-400 focus:outline-none"
              >
                <option value="">全部年龄段</option>
                {libFacets.ages.map(a => <option key={a.code} value={a.code}>{a.name} ({a.count})</option>)}
              </select>
              {(libSource || libCat || libGender || libAge) && (
                <button
                  onClick={() => { setLibSource(""); setLibCat(""); setLibGender(""); setLibAge(""); }}
                  className="text-xs text-gray-400 hover:text-gray-600 px-2"
                >
                  清除筛选
                </button>
              )}
              <span className="ml-auto text-xs text-gray-400">{libFiltered.length} 个音色</span>
            </div>
          )}

          {/* 试试快捷词（音色库、无筛选时展示） */}
          {tab === "library" && !showFilters && (
            <div className="flex flex-wrap items-center gap-2 mt-2.5">
              <span className="text-xs text-gray-400">试试：</span>
              {TRY_CHIPS.map(chip => (
                <button
                  key={chip}
                  onClick={() => { setSearch(chip); setLibLimit(30); }}
                  className={cn(
                    "text-xs px-2.5 py-1 rounded-full border transition-colors",
                    search === chip
                      ? "border-indigo-300 bg-indigo-50 text-indigo-600"
                      : "border-gray-200 text-gray-500 hover:border-gray-300 hover:bg-gray-50"
                  )}
                >
                  {chip}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* 列表 */}
        <div
          className="flex-1 overflow-y-auto scrollbar-thin px-3 py-2"
          onScroll={tab === "library" ? ensureBbLoaded : undefined}
          onClick={() => setMenuPath(null)}
        >
          {tab === "library" ? (
            /* ── 音色库（BreezeBlue + 内置预设合并） ── */
            bbLoading ? (
              <div className="text-center py-14"><p className="text-xs text-gray-400">音色库加载中…</p></div>
            ) : bbError ? (
              <div className="text-center py-14">
                <p className="text-xs text-red-400">{bbError}</p>
                <button onClick={() => { setBbError(null); loadBbVoices(); }} className="mt-2 text-xs text-indigo-500 hover:text-indigo-600">重试</button>
              </div>
            ) : (
              <>
                {/* 推荐音色：仅在无搜索无筛选时展示 */}
                {libBrowsing && recommended.length > 0 && (
                  <>
                    <p className="text-xs text-gray-400 px-4 pt-1 pb-1.5">推荐音色</p>
                    <div className="mb-3">{recommended.map(libRow)}</div>
                  </>
                )}
                <p className="text-xs text-gray-400 px-4 pb-1.5">
                  {libBrowsing ? `全部音色（${libFiltered.length}）` : `搜索结果（${libFiltered.length}）`}
                </p>
                {libVisible.length === 0 ? (
                  <div className="text-center py-12"><p className="text-xs text-gray-400">没有找到匹配的音色</p></div>
                ) : (
                  <>
                    {libVisible.map(libRow)}
                    {libFiltered.length > libVisible.length && (
                      <button
                        onClick={() => setLibLimit(n => n + 30)}
                        className="w-full py-2.5 mt-1 text-xs text-gray-500 hover:text-indigo-600 hover:bg-indigo-50/60 rounded-xl transition-colors"
                      >
                        加载更多（已显示 {libVisible.length} / {libFiltered.length}）
                      </button>
                    )}
                  </>
                )}
              </>
            )
          ) : allInTab.length === 0 ? (
            <div className="text-center py-14"><p className="text-xs text-gray-400">{listEmptyText}</p></div>
          ) : (
            allInTab.map(fileRow)
          )}
        </div>
      </div>
    </div>
  );
}
