"""定位「同文本 CHONG2头，cleanroom 读对、tts-server 读错」的采样参数元凶。

背景
----
同一台机器、同一份官方权重、同一个音色：
  * cleanroom（`tts.infer()` 全默认参数）  -> CHONG2头 读 chóng ✅
  * tts-server（`GenerationParams` + `EmotionConfig`） -> 读 zhòng ❌

代码、权重、词表、`_sanitize_text`、拼音标注机制均已逐项排除。
v2.0.0 `infer()` 默认值与 tts-server 实际传值逐字对齐后，只剩 5 个采样参数有差异：

    top_p 0.8->0.75 / top_k 30->20 / temperature 0.8->0.6
    num_beams 3->2 / repetition_penalty 10.0->5.0

本脚本一次加载模型，跑「全默认 -> 完整服务端参数 -> 逐一还原」的加减矩阵，
并把所有结果拼成 `bisect_ALL.wav`，一次听完即可定位。

用法（在服务器上，需先停掉 tts-server 释放 GPU）
    cd ~/index-tts-webui/tts-server
    HF_HUB_OFFLINE=1 python bisect_pinyin_kwargs.py
"""

from __future__ import annotations

import sys
import wave

sys.path.insert(0, "/root/index-tts")
sys.path.insert(0, "/root/index-tts/indextts")

from indextts.infer_v2 import IndexTTS2  # noqa: E402
from podcast_engine import _sanitize_text  # noqa: E402

MD = "/root/index-tts/checkpoints"
OUT = "/root/autodl-tmp/index-tts/outputs"
VOICE = "/root/autodl-tmp/index-tts/voices/男-播客1.mp3"
TEXT = "那我们就CHONG2头再来一遍吧"

# tts-server 实际传给 infer() 的参数（podcast_engine.py GenerationParams / EmotionConfig）
# 注：emo_alpha=0.65 会被 infer_v2.py:420-425 强制改回 1.0，故与原默认等价、无需列入。
SERVER = {
    "max_text_tokens_per_segment": 120,
    "do_sample": True,
    "top_p": 0.75,
    "top_k": 20,
    "temperature": 0.6,
    "length_penalty": 0.0,
    "num_beams": 2,
    "repetition_penalty": 5.0,
    "max_mel_tokens": 1500,
}

CASES = [
    # 基线 + 复现
    ("A_default", {}),
    ("B_server_full", dict(SERVER)),
    # 从 B 逐一还原（减法）：哪一项还原后读回 chóng，它就是元凶
    ("C_B_but_repetition10", dict(SERVER, repetition_penalty=10.0)),
    ("D_B_but_beams3", dict(SERVER, num_beams=3)),
    ("E_B_but_sampling_std", dict(SERVER, top_p=0.8, top_k=30, temperature=0.8)),
    # 从默认逐一加（加法）：哪一项加上去读成 zhòng，它同样可疑
    ("F_only_repetition5", {"repetition_penalty": 5.0}),
    ("G_only_beams2", {"num_beams": 2}),
    ("H_only_sampling_low", {"top_p": 0.75, "top_k": 20, "temperature": 0.6}),
]


def main() -> None:
    print(">> _sanitize_text(TEXT) =", repr(_sanitize_text(TEXT)), flush=True)

    print(">> loading model ...", flush=True)
    tts = IndexTTS2(
        cfg_path=f"{MD}/config.yaml",
        model_dir=MD,
        use_fp16=True,
        device="cuda:0",
        use_cuda_kernel=True,
        use_deepspeed=True,
        use_accel=False,
        use_torch_compile=False,
    )

    tokens = tts.tokenizer.tokenize(TEXT)
    ids = tts.tokenizer.convert_tokens_to_ids(tokens)
    print("\n>> tokens:", tokens)
    print(">> ids   :", ids, flush=True)
    if "CHONG2" not in tokens:
        raise SystemExit("!! CHONG2 没进 token —— 文本层在这条路径上被改了，请把上面两行贴出来")

    results: list[tuple[str, str]] = []
    for index, (name, kwargs) in enumerate(CASES, start=1):
        path = f"{OUT}/bisect_{name}.wav"
        print(f"\n===== [{index}/{len(CASES)}] {name} =====", flush=True)
        print("   kwargs:", kwargs, flush=True)
        tts.infer(
            spk_audio_prompt=VOICE,
            text=TEXT,
            output_path=path,
            interval_silence=200,
            verbose=False,
            **kwargs,
        )
        results.append((name, path))

    # 拼成单个文件，便于一次听完（每段前留 0.8s 静音便于分辨）
    merged = f"{OUT}/bisect_ALL.wav"
    with wave.open(results[0][1], "rb") as first:
        channels = first.getnchannels()
        sampwidth = first.getsampwidth()
        framerate = first.getframerate()
    gap = b"\0" * channels * sampwidth * int(framerate * 0.8)
    with wave.open(merged, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sampwidth)
        out.setframerate(framerate)
        for _, path in results:
            with wave.open(path, "rb") as seg:
                out.writeframes(seg.readframes(seg.getnframes()))
            out.writeframes(gap)

    print("\n===== 汇总 =====")
    for index, (name, path) in enumerate(results, start=1):
        print(f"  第{index}段  {name:24s} {path}")
    print(f"\n合并文件: {merged}")
    print("听法：A 应为 chóng（基线）；B 若为 zhòng 则复现成功；"
          "C/D/E 中哪一段读回 chóng，对应参数即元凶。")


if __name__ == "__main__":
    main()
