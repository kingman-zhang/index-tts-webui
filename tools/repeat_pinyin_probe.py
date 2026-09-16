"""重复性探针：判定「CHONG2 拼音标注」在 IndexTTS 2.0 上是稳定生效还是随机生效。

为什么需要它
------------
`bisect_pinyin_kwargs.py` 里每个变体只生成 1 次，但同一次调用的两次运行结果不同
（`cleanroom_03_chong2` 读 chóng、`bisect_A_default` 参数完全相同却不是 chóng）
⇒ 输出不可复现。根因：`infer_v2.py:571` 把 `do_sample=True` 硬写死（传 False 无效），
且链路无 seed。故单次采样无法区分「参数决定读法」与「掷骰子」。

做法
----
同一配置连生成 `REPEATS` 次，按配置拼成 `repeat_<cfg>_ALL.wav`（段间 0.8s 静音），
一次听完即可判断：全同 = 稳定；混杂 = 随机。

另附一个无损的客观辅助量：把每段音频做「时间归一化的对数频谱」，
分别与已知读法的两个基准比距离，给出 chóng/zhòng 倾向（仅作参考，耳朵为准）。

用法（服务器上，先停 tts-server 释放 GPU）
    cd ~/index-tts-webui/tts-server
    HF_HUB_OFFLINE=1 python repeat_pinyin_probe.py
"""

from __future__ import annotations

import sys
import wave

import numpy as np

sys.path.insert(0, "/root/index-tts")
sys.path.insert(0, "/root/index-tts/indextts")

from indextts.infer_v2 import IndexTTS2  # noqa: E402

MD = "/root/index-tts/checkpoints"
OUT = "/root/autodl-tmp/index-tts/outputs"
VOICE = "/root/autodl-tmp/index-tts/voices/男-播客1.mp3"
TEXT = "那我们就CHONG2头再来一遍吧"

REPEATS = 6
GAP_SEC = 0.8

# 已知读法的基准（cleanroom 那轮生成、由人工确认）
REF_CHONG = f"{OUT}/cleanroom_03_chong2.wav"
REF_ZHONG = f"{OUT}/cleanroom_02_zhong4.wav"

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

CONFIGS = [
    ("R1_default", {}),
    ("R2_tight", {"top_p": 0.75, "top_k": 20, "temperature": 0.6}),
    ("R3_server", dict(SERVER)),
    # 近贪心：do_sample 被硬写为 True，但温度趋 0 时采样≈argmax，是唯一的「准确定性」出口。
    # 若它稳定读 chóng ⇒ 标注本身有效，是采样把读音带偏的；若仍 zhòng ⇒ 标注就是不被采纳。
    ("R4_neargreedy", {"temperature": 0.05}),
]


def read_wav(path: str):
    """读 16bit PCM wav，返回 (采样率, 单声道 float32 数组)。"""
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)
    if sampwidth != 2:
        raise ValueError(f"{path}: 只支持 16bit PCM，实际 {sampwidth * 8}bit")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return sr, data


def log_spectrum(path: str, n_bins: int = 256) -> np.ndarray:
    """时间归一化的对数幅度谱（频点 x n_bins），每帧去均值以去掉音色/音量差异。"""
    sr, x = read_wav(path)
    n_fft, hop = 1024, 256
    window = np.hanning(n_fft)
    frames = [
        np.abs(np.fft.rfft(x[i:i + n_fft] * window, n_fft))
        for i in range(0, max(1, len(x) - n_fft), hop)
    ]
    spec = np.log1p(np.array(frames).T)  # freq x time
    src = np.linspace(0, spec.shape[1] - 1, n_bins)
    spec = np.stack(
        [np.interp(src, np.arange(spec.shape[1]), row) for row in spec], axis=0
    )
    spec = spec - spec.mean(axis=0, keepdims=True)
    spec = spec / (np.linalg.norm(spec, axis=0, keepdims=True) + 1e-6)
    return spec


def spec_distance(a: np.ndarray, b: np.ndarray) -> float:
    """两段频谱的平均逐帧欧氏距离（已逐帧归一化，范围 [0, ~2]）。"""
    n = min(a.shape[1], b.shape[1])
    diff = a[:, :n] - b[:, :n]
    return float(np.mean(np.linalg.norm(diff, axis=0)))


def concatenate(paths, output_path: str) -> None:
    with wave.open(paths[0], "rb") as f:
        channels, sampwidth, framerate = f.getnchannels(), f.getsampwidth(), f.getframerate()
    gap = b"\0" * channels * sampwidth * int(framerate * GAP_SEC)
    with wave.open(output_path, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sampwidth)
        out.setframerate(framerate)
        for path in paths:
            with wave.open(path, "rb") as seg:
                out.writeframes(seg.readframes(seg.getnframes()))
            out.writeframes(gap)


def main() -> None:
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
    print(">> tokens:", tokens, flush=True)
    if "CHONG2" not in tokens:
        raise SystemExit("!! CHONG2 没进 token，先把上面这行贴出来")

    # 基准频谱 + 校准：chóng 与 zhòng 两个基准之间的距离，作为「可分辨尺度」
    ref_calib = None
    try:
        sp_chong = log_spectrum(REF_CHONG)
        sp_zhong = log_spectrum(REF_ZHONG)
        ref_calib = spec_distance(sp_chong, sp_zhong)
        print(f"\n[参考] 基准间距 d(chóng基准, zhòng基准) = {ref_calib:.4f}  "
              "(新样本的组内距离若明显小于它，说明该配置是稳定的)\n")
    except Exception as exc:  # 基准文件缺失时不阻断主流程
        print(f"\n[参考] 基准文件不可用（{exc}），跳过客观辅助量\n")

    summary = []
    for cfg_index, (name, kwargs) in enumerate(CONFIGS, start=1):
        print(f"\n===== [{cfg_index}/{len(CONFIGS)}] {name}  共 {REPEATS} 次 =====", flush=True)
        print("   kwargs:", kwargs, flush=True)
        paths = []
        for run in range(1, REPEATS + 1):
            path = f"{OUT}/repeat_{name}_{run}.wav"
            tts.infer(
                spk_audio_prompt=VOICE,
                text=TEXT,
                output_path=path,
                interval_silence=200,
                verbose=False,
                **kwargs,
            )
            paths.append(path)
            print(f"   {run}/{REPEATS} -> {path}", flush=True)

        merged = f"{OUT}/repeat_{name}_ALL.wav"
        concatenate(paths, merged)

        verdicts = []
        within = []
        if ref_calib is not None:
            try:
                specs = [log_spectrum(p) for p in paths]
                for spec in specs:
                    d_chong = spec_distance(spec, sp_chong)
                    d_zhong = spec_distance(spec, sp_zhong)
                    verdicts.append("chóng" if d_chong < d_zhong else "zhòng")
                within = [
                    spec_distance(specs[i], specs[j])
                    for i in range(len(specs))
                    for j in range(i + 1, len(specs))
                ]
            except Exception as exc:
                print(f"   (客观辅助量失败: {exc})", flush=True)

        summary.append((name, merged, verdicts, within))
        print(f"   -> 合并文件 {merged}", flush=True)

    print("\n================= 汇总 =================")
    for name, merged, verdicts, within in summary:
        print(f"\n{name}")
        print(f"  合并音频: {merged}")
        if verdicts:
            print(f"  倾向判定: {verdicts}")
            print(f"  组内平均距离: {np.mean(within):.4f}  "
                  f"(基准间距 {ref_calib:.4f}；远小于它 = 该配置稳定)")
        else:
            print("  倾向判定:（不可用，请直接听合并音频）")

    print("\n听法：每段合并音频里是同配置的 6 次连续生成。"
          "\n  * 6 次读法全一致  → 该参数组合是稳定确定的；"
          "\n  * 6 次里 chóng/zhòng 混杂 → 标注只是概率倾向，靠参数调不出来，应改走换词方案。")


if __name__ == "__main__":
    main()
