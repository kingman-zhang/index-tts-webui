import { useState } from "react";
import { Volume2, Sliders, ChevronDown } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, Slider, Switch, Label, Input } from "./ui";
import type { SilenceConfig, GenerationParams } from "@/types";
import { cn } from "@/lib/utils";

interface ParamsPanelProps {
  silence: SilenceConfig;
  params: GenerationParams;
  onSilenceChange: (s: SilenceConfig) => void;
  onParamsChange: (p: GenerationParams) => void;
}

function Section({ icon: Icon, title, defaultOpen, children }: {
  icon: typeof Volume2; title: string; defaultOpen?: boolean; children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen ?? true);
  return (
    <Card>
      <button onClick={() => setOpen(!open)} className="w-full">
        <CardHeader className="flex flex-row items-center justify-between hover:bg-gray-50">
          <div className="flex items-center gap-2">
            <Icon className="w-3.5 h-3.5 text-indigo-500" />
            <CardTitle>{title}</CardTitle>
          </div>
          <ChevronDown className={cn("w-4 h-4 text-gray-400 transition-transform", !open && "rotate-180")} />
        </CardHeader>
      </button>
      {open && <CardContent className="space-y-3">{children}</CardContent>}
    </Card>
  );
}

export function ParamsPanel({ silence, params, onSilenceChange, onParamsChange }: ParamsPanelProps) {
  const updSilence = (p: Partial<SilenceConfig>) => onSilenceChange({ ...silence, ...p });
  const updParams = (p: Partial<GenerationParams>) => onParamsChange({ ...params, ...p });

  return (
    <div className="space-y-3">
      <Section icon={Volume2} title="静音与节奏">
        <Slider label="段内分句间静音" min={0} max={1000} step={50}
          value={silence.within_segment} unit="ms"
          onChange={v => updSilence({ within_segment: v })} />
        <Slider label="同行连续发言间隔" min={0} max={1000} step={50}
          value={silence.between_lines} unit="ms"
          onChange={v => updSilence({ between_lines: v })} />
        <Slider label="说话人切换间隔" min={0} max={1500} step={50}
          value={silence.speaker_switch} unit="ms"
          onChange={v => updSilence({ speaker_switch: v })} />
        <p className="text-[11px] text-gray-400">
          说话人切换时的静音建议稍长（400-600ms），模拟真实对话节奏。
        </p>
      </Section>

      <Section icon={Sliders} title="生成参数" defaultOpen={false}>
        <Slider label="语速" min={0.5} max={2.0} step={0.05}
          value={params.speed} unit="倍"
          onChange={v => updParams({ speed: v })} />
        <p className="text-[11px] text-gray-400 -mt-1">1.0 为正常语速；数值越大播放越快，数值越小播放越慢。</p>
        <div>
          <Label>分句最大 Token 数</Label>
          <Input type="number" min={20} max={500} step={10}
            value={params.max_text_tokens_per_segment}
            onChange={e => updParams({ max_text_tokens_per_segment: parseInt(e.target.value) || 120 })} />
          <p className="text-[11px] text-gray-400 mt-1">控制每段合成的最大长度，过短会增加段数，过长可能降低质量。</p>
        </div>

        <div className="flex items-center justify-between pt-1">
          <Label className="mb-0">do_sample（随机采样）</Label>
          <Switch checked={params.do_sample} onChange={v => updParams({ do_sample: v })} />
        </div>

        <Slider label="Temperature" min={0.1} max={2.0} step={0.1}
          value={params.temperature} onChange={v => updParams({ temperature: v })} />
        <p className="text-[11px] text-gray-400 -mt-1">建议 0.5-0.7；过高会产生异常音素（如杂音、多余 s 音）。</p>

        <Slider label="Top-P" min={0.1} max={1.0} step={0.05}
          value={params.top_p} onChange={v => updParams({ top_p: v })} />
        <p className="text-[11px] text-gray-400 -mt-1">建议 0.7-0.85；过高会降低稳定性。</p>

        <div>
          <Label>Top-K（0 = 不限制）</Label>
          <Input type="number" min={0} max={100} step={1}
            value={params.top_k}
            onChange={e => updParams({ top_k: parseInt(e.target.value) || 0 })} />
        </div>

        <div>
          <Label>Num Beams（束搜索宽度）</Label>
          <Input type="number" min={1} max={10} step={1}
            value={params.num_beams}
            onChange={e => updParams({ num_beams: parseInt(e.target.value) || 1 })} />
        </div>

        <Slider label="Repetition Penalty" min={1.0} max={20.0} step={0.5}
          value={params.repetition_penalty} onChange={v => updParams({ repetition_penalty: v })} />
        <p className="text-[11px] text-gray-400 -mt-1">建议 3-6；过高会导致模型回避常见音素，产生异常发音。</p>

        <Slider label="Length Penalty" min={-2.0} max={2.0} step={0.1}
          value={params.length_penalty} onChange={v => updParams({ length_penalty: v })} />

        <div>
          <Label>Max Mel Tokens</Label>
          <Input type="number" min={100} max={3000} step={100}
            value={params.max_mel_tokens}
            onChange={e => updParams({ max_mel_tokens: parseInt(e.target.value) || 1500 })} />
        </div>

      </Section>
    </div>
  );
}
