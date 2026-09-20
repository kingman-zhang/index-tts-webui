import { useState, useRef } from "react";
import { Play, Download, Loader2, CheckCircle2, AlertCircle, RotateCcw, AudioLines } from "lucide-react";
import { Button, Card, Badge } from "./ui";
import type { TaskInfo } from "@/types";
import { formatTime, cn } from "@/lib/utils";

interface OutputPanelProps {
  onGenerate: () => void;
  canGenerate: boolean;
  task: TaskInfo | null;
  generating: boolean;
  audioUrl: string | null;
  durationSec: number;
  error: string | null;
  onReset: () => void;
  /** 积分预估（MEMBER_ENFORCE=1 时由页面传入；null = 不展示） */
  pointsInfo?: { cost: number; balance: number | null } | null;
}

export function OutputPanel({ onGenerate, canGenerate, task, generating, audioUrl, durationSec, error, onReset, pointsInfo }: OutputPanelProps) {
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const togglePlay = () => {
    if (!audioRef.current || !audioUrl) return;
    if (playing) { audioRef.current.pause(); setPlaying(false); }
    else { audioRef.current.src = audioUrl; audioRef.current.play(); setPlaying(true); }
  };

  const progress = task?.progress || 0;
  const isRunning = generating && task?.status !== "completed" && task?.status !== "failed";
  const isDone = task?.status === "completed" && !!audioUrl;
  const isFailed = !!error || task?.status === "failed";

  return (
    <Card className="border-indigo-200 bg-gradient-to-b from-indigo-50/50 to-white">
      <div className="p-4 space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <AudioLines className="w-4 h-4 text-indigo-600" />
            <h3 className="text-sm font-semibold text-gray-800">生成与输出</h3>
          </div>
          {isDone && <Badge color="green"><CheckCircle2 className="w-3 h-3 mr-1" /> 完成</Badge>}
          {isFailed && <Badge color="red"><AlertCircle className="w-3 h-3 mr-1" /> 失败</Badge>}
        </div>

        {/* 生成按钮 */}
        {!generating && (
          <>
            <Button
              size="lg" icon={Play} onClick={onGenerate} disabled={!canGenerate}
              className="w-full text-base"
            >
              生成音频
            </Button>
            {pointsInfo && pointsInfo.cost > 0 && (
              <p className={cn(
                "text-xs text-center tabular-nums",
                pointsInfo.balance != null && pointsInfo.cost > pointsInfo.balance
                  ? "text-red-500"
                  : "text-gray-400"
              )}>
                本次约扣 {pointsInfo.cost} 积分
                {pointsInfo.balance != null ? `（余额 ${pointsInfo.balance}）` : ""}
                {pointsInfo.balance != null && pointsInfo.cost > pointsInfo.balance
                  ? " · 可在个人中心用兑换码充值" : ""}
              </p>
            )}
          </>
        )}

        {/* 进度展示 */}
        {isRunning && task && (
          <div className="space-y-2">
            <div className="flex justify-between text-xs text-gray-600">
              <span className="flex items-center gap-1.5">
                <Loader2 className="w-3.5 h-3.5 animate-spin text-indigo-500" />
                {task.message || "合成中..."}
              </span>
              <span className="tabular-nums">{task.current_line}/{task.total_lines} 行</span>
            </div>
            <div className="h-2 rounded-full bg-gray-200 overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-indigo-500 to-purple-500 transition-all duration-500"
                style={{ width: `${progress}%` }}
              />
            </div>
            <div className="text-right text-xs text-gray-400 tabular-nums">{progress}%</div>
          </div>
        )}

        {/* 错误提示 */}
        {isFailed && (
          <div className="rounded-lg bg-red-50 border border-red-200 p-3">
            <div className="flex items-start gap-2">
              <AlertCircle className="w-4 h-4 text-red-500 mt-0.5 shrink-0" />
              <p className="text-xs text-red-600 break-all">{error || task?.error || "生成失败"}</p>
            </div>
          </div>
        )}

        {/* 结果播放器 */}
        {isDone && (
          <div className="space-y-3">
            <audio
              ref={audioRef}
              onEnded={() => setPlaying(false)}
              className="hidden"
            />
            <div className="flex items-center gap-2">
              <Button
                variant={playing ? "secondary" : "default"}
                size="md" icon={playing ? Loader2 : Play}
                onClick={togglePlay}
                className="flex-1"
              >
                {playing ? "暂停" : "播放试听"}
              </Button>
              <Button
                variant="outline" size="md" icon={Download}
                onClick={() => { if (audioUrl) window.open(audioUrl, "_blank"); }}
              >
                下载
              </Button>
            </div>
            {durationSec > 0 && (
              <div className="flex items-center justify-center gap-3 text-xs text-gray-500">
                <span>时长 {formatTime(durationSec)}</span>
                <span>·</span>
                <span>{task?.total_lines} 段对话</span>
              </div>
            )}
            <Button variant="ghost" size="sm" icon={RotateCcw} onClick={onReset} className="w-full">
              重新生成
            </Button>
          </div>
        )}

        {/* 未就绪提示 */}
        {!generating && !isDone && !isFailed && !canGenerate && (
          <p className="text-xs text-gray-400 text-center py-1">
            请先为两位主持人设置参考音频，并添加至少一行有内容的对话
          </p>
        )}
      </div>
    </Card>
  );
}
