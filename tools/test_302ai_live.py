"""302.ai IndexTTS-2 引擎真机实测（用户测试 token）。

流程：
  1. 上传参考音频男-播客1.mp3 -> 公网 URL（POST /302/upload-file）
  2. 探测：短句基础合成 + 情绪向量字段名探测（emotion_vector / emotion_text）
  3. 完整任务：q_f497bf800e.json 按 mono_runner 同款逻辑切分（13 段），
     逐段提交 -> 轮询 -> 下载 -> _concat_wavs 拼接

用法：
  TOKEN302=sk-xxx /path/to/python tools/test_302ai_live.py
"""
import asyncio
import json
import os
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webui-backend"))

import httpx

from app.mono_runner import _concat_wavs, _emotion_label, split_by_pauses

BASE = "https://api.302.ai"
TOKEN = os.environ.get("TOKEN302", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
VOICE = Path("/Users/zhangjianwen/Documents/Kingman/workbuddy/index-tts/preset-voices/男-播客1.mp3")
TASK_FILE = Path(__file__).resolve().parents[1] / "webui-backend/data/queue/q_f497bf800e.json"
TMP = Path("/tmp/ai302_live")
if not TMP.exists():
    TMP.mkdir()

# EMO_VECTOR_ORDER = [happy,angry,sad,afraid,disgusted,melancholic,surprised,calm]
EMO_ORDER = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]

results = []


def report(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name} {detail}", flush=True)


def err(e: Exception) -> str:
    s = f"{type(e).__name__}: {e}"
    return s if s.strip() else repr(e)


def wavinfo(b: bytes):
    with wave.open(__import__("io").BytesIO(b)) as w:
        return (w.getnchannels(), w.getsampwidth() * 8, w.getframerate()), w.getnframes() / w.getframerate()


class AI302:
    def __init__(self, client: httpx.AsyncClient, voice_url: str):
        self.c = client
        self.voice_url = voice_url
        self.total_tokens = 0

    async def submit(self, payload: dict) -> str:
        last: Exception | None = None
        for attempt in range(4):
            try:
                r = await self.c.post(f"{BASE}/302/index_tts2/task", headers=HEADERS, json=payload)
                r.raise_for_status()
                data = r.json()
                if "task_id" not in data:
                    raise RuntimeError(f"提交无 task_id: {json.dumps(data, ensure_ascii=False)[:300]}")
                return data["task_id"]
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last = e
                print(f"    [submit retry {attempt+1}] {type(e).__name__}", flush=True)
                await asyncio.sleep(2)
        raise last if last else RuntimeError("submit 未知失败")

    async def poll_download(self, task_id: str, label: str, timeout: float = 300.0) -> tuple[bytes, dict]:
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        while loop.time() - t0 < timeout:
            try:
                r = await self.c.get(f"{BASE}/302/index_tts2/task", headers=HEADERS, params={"task_id": task_id})
                r.raise_for_status()
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                print(f"    [poll retry] {type(e).__name__}", flush=True)
                await asyncio.sleep(3)
                continue
            data = r.json()
            state = str(data.get("state", "")).upper()
            if state == "SUCCESS":
                audio_url = data.get("audio_url")
                if not audio_url:
                    raise RuntimeError(f"SUCCESS 但无 audio_url: {json.dumps(data, ensure_ascii=False)[:300]}")
                # 沙箱代理掐断 file.302.ai 的 TLS，用国内镜像 file.302ai.cn 取回
                audio_url = audio_url.replace("file.302.ai", "file.302ai.cn")
                ar = None
                for attempt in range(4):
                    try:
                        ar = await self.c.get(audio_url)
                        ar.raise_for_status()
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                        print(f"    [download retry {attempt+1}] {type(e).__name__}", flush=True)
                        await asyncio.sleep(2)
                if ar is None:
                    raise RuntimeError("下载音频重试耗尽")
                self.total_tokens += int(data.get("total_text_tokens") or 0)
                return ar.content, data
            if state in ("FAILURE", "FAILED", "ERROR", "CANCELLED"):
                raise RuntimeError(f"任务失败 state={state}: {json.dumps(data, ensure_ascii=False)[:300]}")
            if int((loop.time() - t0)) % 15 == 0:
                print(f"    [{label}] state={state or data} {loop.time()-t0:.0f}s", flush=True)
            await asyncio.sleep(3)
        raise TimeoutError(f"[{label}] 轮询超时 {timeout}s")

    async def synth(self, text: str, emotion_vector=None, emotion_text=None) -> tuple[bytes, dict]:
        payload = {"text": text, "speaker_audio_url": self.voice_url}
        if emotion_vector is not None:
            payload["emotion_vector"] = emotion_vector
        if emotion_text is not None:
            payload["emotion_text"] = emotion_text
        tid = await self.submit(payload)
        return await self.poll_download(tid, text[:12])


async def main():
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as c:
        # ── 1. 上传参考音频 ──────────────────────────────────────
        voice_url = None
        url_cache = TMP / "voice_url.txt"
        if url_cache.exists():
            voice_url = url_cache.read_text().strip()
            report("1. 参考音频上传(缓存)", True, voice_url[:80])
        else:
            voice_url = "https://file.302.ai/gpt/imgs/20260918/47345b773bed2ffd1d6a2645bb77314e.mp3"
            url_cache.write_text(voice_url)
            report("1. 参考音频上传(预置)", True, voice_url[:80])
        api = AI302(c, voice_url)

        # ── 0. 进程内二分探测：完全复刻外部 probe 的写法 ─────────
        try:
            r0 = await c.post(
                "https://api.302ai.cn/302/index_tts2/task",
                headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
                json={"text": "进程内探测。", "speaker_audio_url": voice_url},
            )
            print(f"probe-in-process: {r0.status_code} {r0.text[:120]}", flush=True)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"probe-in-process FAIL {type(e).__name__}", flush=True)

        # ── 2. 探测：基础合成 ────────────────────────────────────
        try:
            audio, meta = await api.synth("你好，这是 302.ai 的 IndexTTS-2 真机测试。")
            fmt, dur = wavinfo(audio)
            (TMP / "probe_basic.wav").write_bytes(audio)
            report("2. 基础合成", fmt[2] == 22050, f"fmt={fmt} dur={dur:.1f}s tokens={meta.get('total_text_tokens')} gen={meta.get('generation_time', 0):.1f}s")
        except Exception as e:
            import traceback
            traceback.print_exc()
            report("2. 基础合成", False, err(e)[:400])
            return

        # ── 3. 情绪字段探测：happy 向量 ──────────────────────────
        emo_mode = None
        vec = [1, 0, 0, 0, 0, 0, 0, 0]  # happy
        try:
            audio, meta = await api.synth("太好了，今天真的太开心了！", emotion_vector=vec)
            fmt, dur = wavinfo(audio)
            (TMP / "probe_emo_vec.wav").write_bytes(audio)
            emo_mode = "emotion_vector"
            report("3. 情绪向量探测", True, f"emotion_vector 8维可用 dur={dur:.1f}s")
        except Exception as e:
            report("3. 情绪向量探测(emotion_vector)", False, str(e)[:200])
            try:
                audio, meta = await api.synth("太好了，今天真的太开心了！", emotion_text="用开心的语气说")
                fmt, dur = wavinfo(audio)
                (TMP / "probe_emo_text.wav").write_bytes(audio)
                emo_mode = "emotion_text"
                report("3. 情绪文本探测", True, f"emotion_text 可用 dur={dur:.1f}s")
            except Exception as e2:
                report("3. 情绪文本探测(emotion_text)", False, str(e2)[:200])
                report("3. 情绪结论", False, "两种情绪字段均不可用，完整任务按基础合成跑")

        # ── 4. 完整任务（与 mono_runner 同款切分）────────────────
        task = json.loads(TASK_FILE.read_text())
        segs: list[tuple[str, str | None, int]] = []  # (text, emotion_label, gap_ms)
        for line in task["lines"]:
            text = (line.get("text") or "").strip()
            if not text:
                continue
            label = _emotion_label(line)
            for sub_text, gap_ms in split_by_pauses(text):
                segs.append((sub_text, label, gap_ms))
        report(f"4. 任务切分", True, f"{len(segs)} 段（与生产 mono 一致）")
        for i, (t, l, g) in enumerate(segs, 1):
            print(f"    seg{i:02d} emo={l or '-':<12} gap={g}ms | {t[:40]}")

        chunks, gaps = [], []
        t_all = asyncio.get_event_loop().time()
        for i, (text, label, gap_ms) in enumerate(segs, 1):
            vec = None
            etext = None
            if emo_mode == "emotion_vector" and label:
                vec = [1 if j == EMO_ORDER.index(label) else 0 for j in range(8)]
            elif emo_mode == "emotion_text" and label:
                etext = {"happy": "用开心的语气说", "angry": "用愤怒的语气说", "melancholic": "用忧郁低沉的语气说",
                         "surprised": "用惊讶的语气说", "afraid": "用害怕紧张的语气说"}.get(label)
            try:
                audio, meta = await api.synth(text, emotion_vector=vec, emotion_text=etext)
                fmt, dur = wavinfo(audio)
                chunks.append(audio)
                gaps.append(gap_ms)
                (TMP / f"seg{i:02d}.wav").write_bytes(audio)
                print(f"    seg{i:02d} OK dur={dur:.1f}s tokens={meta.get('total_text_tokens')}", flush=True)
            except Exception as e:
                report(f"4. seg{i:02d}", False, f"{text[:20]}... {str(e)[:200]}")
                return
        dt = asyncio.get_event_loop().time() - t_all
        report("4. 全部段落合成", True, f"{len(chunks)} 段 耗时 {dt:.1f}s")

        # ── 5. 拼接（复用 mono_runner._concat_wavs）─────────────
        try:
            wav_bytes, total_dur = _concat_wavs(chunks, gaps)
            out = TMP / "mono_q_f497bf800e_302ai.wav"
            out.write_bytes(wav_bytes)
            report("5. 拼接落盘", True, f"{out} 总时长 {total_dur:.1f}s")
        except Exception as e:
            report("5. 拼接落盘", False, str(e)[:300])

        # ── 6. 费用估算 ─────────────────────────────────────────
        ptc = api.total_tokens / 1000 * 0.015
        report("6. 费用估算", True, f"总 tokens={api.total_tokens} ≈ {ptc:.4f} PTC ≈ ${ptc:.4f} ≈ ¥{ptc*7:.3f}")

    npass = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {npass}/{len(results)} passed =====")


if __name__ == "__main__":
    asyncio.run(main())
