import { Save, FolderOpen, Radio, CheckCircle2, XCircle, Loader2 } from "lucide-react";
import { Button, Input, Badge } from "./ui";
import type { PodcastProject } from "@/types";

interface HeaderProps {
  project: PodcastProject;
  onRename: (name: string) => void;
  onSave: () => void;
  onLoadProject: () => void;
  ttsOnline: boolean | null;
  ttsInfo: { model_loaded: boolean } | null;
  saving: boolean;
}

export function Header({ project, onRename, onSave, onLoadProject, ttsOnline, ttsInfo, saving }: HeaderProps) {
  return (
    <header className="h-14 bg-white border-b border-gray-200 flex items-center justify-between px-4 shrink-0">
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center">
          <Radio className="w-4.5 h-4.5 text-white" />
        </div>
        <div>
          <h1 className="text-sm font-semibold text-gray-800 leading-none">双人播客工作室</h1>
          <p className="text-[10px] text-gray-400 mt-0.5">Podcast Studio · powered by IndexTTS2</p>
        </div>
      </div>

      <div className="flex-1 max-w-sm mx-6">
        <Input
          value={project.name}
          onChange={e => onRename(e.target.value)}
          placeholder="项目名称"
          className="text-center font-medium"
        />
      </div>

      <div className="flex items-center gap-2">
        {/* TTS 状态 */}
        <div className="flex items-center gap-1.5 mr-2">
          {ttsOnline === null ? (
            <Loader2 className="w-3.5 h-3.5 text-gray-400 animate-spin" />
          ) : ttsOnline ? (
            <CheckCircle2 className="w-3.5 h-3.5 text-green-500" />
          ) : (
            <XCircle className="w-3.5 h-3.5 text-red-400" />
          )}
          <span className="text-xs text-gray-500">
            {ttsOnline === null ? "检测中" : ttsOnline ? (ttsInfo?.model_loaded ? "TTS 就绪" : "模型加载中") : "TTS 离线"}
          </span>
        </div>

        <Badge color={ttsOnline ? "green" : "red"}>
          {ttsOnline ? "在线" : "离线"}
        </Badge>

        <Button variant="outline" size="sm" icon={FolderOpen} onClick={onLoadProject}>
          项目
        </Button>
        <Button size="sm" icon={saving ? Loader2 : Save} onClick={onSave} disabled={saving}>
          {saving ? "保存中" : "保存"}
        </Button>
      </div>
    </header>
  );
}
