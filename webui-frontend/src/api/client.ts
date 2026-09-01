/** WebUI 后端 API 客户端。 */
import type { PodcastProject, TaskInfo, VoiceFile, EmotionConfig, GenerationParams } from "@/types";

const BASE = "/api";

async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const resp = await fetch(url, options);
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body.detail || body.error || detail;
    } catch {
      detail = await resp.text().catch(() => detail);
    }
    throw new Error(detail);
  }
  return resp.json();
}

export const api = {
  // 配置与健康检查
  async getConfig() {
    return fetchJSON<{
      tts_url: string;
      tts_online: boolean;
      tts_info: { model_loaded: boolean; device: string; fp16: boolean } | null;
    }>(`${BASE}/config`);
  },

  // 参考音频
  async listVoices(): Promise<{ voices: VoiceFile[]; count: number }> {
    return fetchJSON(`${BASE}/voices`);
  },

  async uploadVoice(file: File, customName?: string): Promise<{ name: string; path: string; size_kb?: number }> {
    const form = new FormData();
    form.append("file", file);
    if (customName) form.append("name", customName);
    return fetchJSON(`${BASE}/voices/upload`, { method: "POST", body: form });
  },

  async renameVoice(oldName: string, newName: string): Promise<{ name: string; path: string }> {
    return fetchJSON(`${BASE}/voices/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old_name: oldName, new_name: newName }),
    });
  },

  async deleteVoice(name: string): Promise<{ deleted: string }> {
    return fetchJSON(`${BASE}/voices/${encodeURIComponent(name)}`, { method: "DELETE" });
  },

  async listVoiceFavorites(): Promise<{ paths: string[] }> {
    return fetchJSON(`${BASE}/voice-favorites`);
  },

  async saveVoiceFavorites(paths: string[]): Promise<{ paths: string[] }> {
    return fetchJSON(`${BASE}/voice-favorites`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths }),
    });
  },

  // 单段合成（快速试听）
  async synthesize(voice: string, text: string, emotion: EmotionConfig,
    intervalSilence: number, params: GenerationParams): Promise<{ output_filename: string; duration_sec: number }> {
    return fetchJSON(`${BASE}/synthesize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ voice, text, emotion, interval_silence: intervalSilence, params }),
    });
  },

  // 双人播客合成
  async generatePodcast(project: PodcastProject): Promise<{ task_id: string; status: string; total_lines: number }> {
    const lines = project.lines.map(l => ({
      speaker: l.speaker,
      text: l.text,
      emotion: l.emotion,
      silence_after_ms: l.silence_after_ms,
    }));
    const speakerSpeeds = {
      A: project.voices.A.speed ?? project.params.speed ?? 1.0,
      B: project.voices.B.speed ?? project.params.speed ?? 1.0,
    };
    const voices: Record<string, string> = {};
    if (project.voices.A.voice_path) voices.A = project.voices.A.voice_path;
    if (project.voices.B.voice_path) voices.B = project.voices.B.voice_path;
    return fetchJSON(`${BASE}/podcast/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lines, voices, silence: project.silence, params: { ...project.params, speaker_speeds: speakerSpeeds } }),
    });
  },

  // SSE 进度监听
  subscribeStatus(taskId: string, onProgress: (t: TaskInfo) => void, onDone: (t: TaskInfo) => void, onError: (e: string) => void) {
    const es = new EventSource(`${BASE}/podcast/status/${taskId}`);
    es.onmessage = (ev) => {
      try { onProgress(JSON.parse(ev.data)); } catch {}
    };
    es.addEventListener("done", (ev) => {
      try { onDone(JSON.parse((ev as MessageEvent).data)); } catch {}
      es.close();
    });
    es.addEventListener("error", (ev) => {
      // EventSource 的 error 事件可能是网络断开
      if (es.readyState === EventSource.CLOSED) {
        onError("连接已关闭");
      }
    });
    es.onerror = () => {
      onError("连接中断");
      es.close();
    };
    return es;
  },

  async getTask(taskId: string): Promise<TaskInfo> {
    return fetchJSON(`${BASE}/podcast/task/${taskId}`);
  },

  // 音频 URL
  audioUrlByFilename(filename: string): string {
    return `${BASE}/audio/${filename}`;
  },
  audioUrlByTask(taskId: string): string {
    return `${BASE}/podcast/audio/${taskId}`;
  },

  // 项目管理
  async listProjects(): Promise<{ projects: any[]; count: number }> {
    return fetchJSON(`${BASE}/projects`);
  },
  async saveProject(project: PodcastProject): Promise<{ id: string; name: string; updated_at: string }> {
    return fetchJSON(`${BASE}/projects`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(project),
    });
  },
  async getProject(id: string): Promise<PodcastProject> {
    return fetchJSON(`${BASE}/projects/${id}`);
  },
  async deleteProject(id: string): Promise<{ deleted: string }> {
    return fetchJSON(`${BASE}/projects/${id}`, { method: "DELETE" });
  },
  async importText(text: string, speakerAName = "A", speakerBName = "B"): Promise<{ lines: any[]; count: number }> {
    return fetchJSON(`${BASE}/projects/import`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, speaker_a_name: speakerAName, speaker_b_name: speakerBName }),
    });
  },

  // ─── 预设音色 ───
  async listPresetVoices(): Promise<{
    categories: { female: VoiceFile[]; male: VoiceFile[]; emotion: VoiceFile[] };
    count: number;
  }> {
    return fetchJSON(`${BASE}/preset-voices`);
  },
  async uploadPresetToTTS(name: string): Promise<{ name: string; path: string; size_kb: number; local_only?: boolean }> {
    return fetchJSON(`${BASE}/preset-voices/upload-to-tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  },

  // ─── 术语词汇表 ───
  async getGlossary(): Promise<{ terms: { original: string; replacement: string }[]; count: number }> {
    return fetchJSON(`${BASE}/glossary`);
  },
  async addGlossaryTerm(original: string, replacement: string): Promise<{ terms: { original: string; replacement: string }[]; count: number }> {
    return fetchJSON(`${BASE}/glossary`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ original, replacement }),
    });
  },
  async deleteGlossaryTerm(original: string): Promise<{ terms: { original: string; replacement: string }[]; count: number }> {
    return fetchJSON(`${BASE}/glossary/${encodeURIComponent(original)}`, { method: "DELETE" });
  },
  async updateGlossary(terms: { original: string; replacement: string }[]): Promise<{ terms: { original: string; replacement: string }[]; count: number }> {
    return fetchJSON(`${BASE}/glossary`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ terms }),
    });
  },

  // ─── 音色预设 ───
  async listVoicePresets(): Promise<{ presets: any[]; count: number }> {
    return fetchJSON(`${BASE}/voice-presets`);
  },
  async saveVoicePreset(preset: any): Promise<{ id: string; name: string; created_at: string }> {
    return fetchJSON(`${BASE}/voice-presets`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(preset),
    });
  },
  async getVoicePreset(id: string): Promise<any> {
    return fetchJSON(`${BASE}/voice-presets/${id}`);
  },
  async renameVoicePreset(id: string, name: string): Promise<{ id: string; name: string }> {
    return fetchJSON(`${BASE}/voice-presets/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
  },
  async deleteVoicePreset(id: string): Promise<{ deleted: string }> {
    return fetchJSON(`${BASE}/voice-presets/${id}`, { method: "DELETE" });
  },

  // ─── 任务队列 ───
  async submitToQueue(req: {
    project_name: string;
    lines: any[];
    voices: Record<string, string>;
    silence: any;
    params: any;
    glossary_enabled: boolean;
  }): Promise<{ task_id: string; status: string; queue_position: number }> {
    return fetchJSON(`${BASE}/queue/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  },
  async listQueue(): Promise<{ tasks: any[]; count: number; current: string | null; queued: number; queue_order: string[] }> {
    return fetchJSON(`${BASE}/queue`);
  },
  async getQueueTask(taskId: string): Promise<any> {
    return fetchJSON(`${BASE}/queue/${taskId}`);
  },
  async updateQueueTaskName(taskId: string, projectName: string): Promise<{ task_id: string; project_name: string; status: string }> {
    return fetchJSON(`${BASE}/queue/${taskId}/name`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_name: projectName }),
    });
  },
  async updateQueueTask(taskId: string, req: {
    project_name: string;
    lines: any[];
    voices: Record<string, string>;
    silence: any;
    params: any;
    glossary_enabled: boolean;
  }): Promise<any> {
    return fetchJSON(`${BASE}/queue/${taskId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
  },
  async retryQueueTask(taskId: string): Promise<{ task_id: string; status: string; queue_position: number }> {
    return fetchJSON(`${BASE}/queue/${taskId}/retry`, { method: "POST" });
  },
  async pauseQueuedTasks(): Promise<{ paused: string[]; count: number }> {
    return fetchJSON(`${BASE}/queue/bulk-pause`, { method: "POST" });
  },
  async resumePausedTasks(): Promise<{ resumed: string[]; count: number }> {
    return fetchJSON(`${BASE}/queue/bulk-resume`, { method: "POST" });
  },
  async cancelQueueTask(taskId: string): Promise<any> {
    return fetchJSON(`${BASE}/queue/${taskId}`, { method: "DELETE" });
  },
  async clearFinishedTasks(): Promise<{ cleared: number; remaining: number }> {
    return fetchJSON(`${BASE}/queue`, { method: "DELETE" });
  },
  async reorderQueue(taskIds: string[]): Promise<{ queue_order: string[] }> {
    return fetchJSON(`${BASE}/queue/reorder`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_ids: taskIds }),
    });
  },
};
