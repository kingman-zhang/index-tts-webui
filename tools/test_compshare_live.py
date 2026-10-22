"""CompShare / Modelverse 的 IndexTTS-2 —— 零样本接口实测（计费口径已锁定）。

## 一句话结论

`POST /v1/audio/infer`（multipart）**每次直接带参考音频**，不需要预先上传音色 ——
形态和我们 art 的引擎完全一样，**没有 7 天过期问题**。这才是该接的接口。

## 两个接口的分工（2026-10-22 全部实测通过）

| | `/v1/audio/infer` ★ | `/v1/audio/speech` |
|---|---|---|
| 请求体 | **multipart/form-data** | JSON |
| 音色 | `spk_audio_file` 直接传参考音频 | `voice` = 内置名 / `uspeech:xxx`（要先上传） |
| 预上传 | **不需要** | 需要，且 **7 天自动清理** |
| 情感 | 向量 / 文本 / 情感音频 / 无 | 仅少量扩展字段 |
| 正文上限 | 600 字符 | 600 字符 |
| 返回 | WAV 字节流 | WAV 字节流（但 Content-Type 谎报 audio/mpeg） |

## infer 的 payload 字段（以文档参数表为准，文档示例里的 emo_weight 是过时的）

`input`(≤600字) / `sample_rate`(22050) / `speed`(0.25~4.0) / `gain`(1) /
`emo_control_method`(0=无情感 1=情感音频 2=情感向量 3=情感文本) / `emo_alpha`(1，融合权重) /
`emo_vec`(8 维，和 ≤1.5，仅 method=2) / `emo_text`(仅 method=3) / `use_random`(false) /
`interval_silence`(200ms)

emo_vec 的 8 维顺序与 IndexTTS 一致：**高兴、愤怒、悲伤、恐惧、厌恶、低落、惊讶、平静**
（正好等于我们 `_LABEL_TO_FIELD` 那 8 个标签 ⇒ neutral 走 method=0，其余走 method=2）。

## 计费（已用控制台调用日志锁定，不用再猜）

后台日志：56 字 → 总 Tokens 56、365 字 → 365 ⇒ **1 token = 1 个字符（标点也计费）**，
与我们 `count_billable_chars` 同口径。
单价 **40 元/百万 token** ⇒ **¥0.40/万字**、**¥0.68/音频小时**（实测 284 字/分）⇒ 售价 ¥5/万字时毛利 92%。
⚠️ 文档「注意事项」还写着「TTS 限时免费」——若真免费，那是**额外的**，规划仍按 ¥0.40/万字。

## 用法

  MODELVERSE_API_KEY=xxx python3 tools/test_compshare_live.py
  MODELVERSE_API_KEY=xxx python3 tools/test_compshare_live.py --ref ../preset-voices/女-嚣张小姐.mp3  # 测 3.2 秒短样本
  MODELVERSE_API_KEY=xxx python3 tools/test_compshare_live.py --emo                     # 顺带测情感三档
  MODELVERSE_API_KEY=xxx python3 tools/test_compshare_live.py --dry-run                 # 只查价，不合成

产物：/tmp/compshare_probe/{report.json, infer_short.wav, infer_long.wav, emo_*.wav}
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
import uuid
import wave
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

BASE = "https://api.modelverse.cn"
OUT = Path("/tmp/compshare_probe")
MODEL = "IndexTeam/IndexTTS-2"
MAX_CHARS = 600          # 平台硬上限（art 是 2048 ⇒ 接引擎时 chunker 要参数化）
REF_MIN_S, REF_MAX_S = 5.0, 30.0   # 文档要求；实测 3.2 秒也能过，见 --ref

# 我们的换算基准
YUAN_PER_MTOKEN = Decimal("40")          # 官方 Unit 字段：input 40 CNY / 百万 Token
OUR_YUAN_PER_WANZI = Decimal("5.00")     # 售价 50 积分/千字 × ¥0.01
ART_YUAN_PER_WANZI = Decimal("1.84")     # art ¥0.001/s 按输出秒
BENCH_CHARS_PER_MIN = Decimal("326")     # 仓库基准（本地 2.0）；托管版实测 ~284

DEFAULT_REF = Path(__file__).resolve().parents[2] / "preset-voices" / "女-傲娇御姐.mp3"

TEXT_SHORT = (
    "很多人以为，读书是为了记住更多的知识。其实真正改变一个人的，"
    "不是他记住了多少，而是他在某个瞬间突然想通了一件事。"
)
TEXT_LONG = (
    "我们总在等一个完美的时机，等自己准备充分了再开始。可实际上，"
    "准备这件事是没有尽头的。你越是等，越会觉得自己不够好。"
    "真正让人发生变化的，往往是那些不够体面的开始：写得歪歪扭扭的第一篇稿子，"
    "剪得乱七八糟的第一条视频，说得结结巴巴的第一次分享。"
    "这些东西拿不出手，但它们有一个共同点，就是它们真的发生了。"
    "而完美计划永远停在纸上。我后来给自己定了一个很笨的规矩："
    "任何想法，如果四十八小时内没有动手，就默认它不重要，直接删掉。"
    "这个规矩救了我很多时间。因为它逼着我承认，那些迟迟不动的事，"
    "不是因为没时间，而是因为我其实没那么想做。"
    "承认这一点并不舒服，但比自我欺骗要轻松得多。"
    "所以如果你现在手上有一件想了很久的事，我的建议不是让你去想得更清楚，"
    "而是先做一个最粗糙的版本出来，哪怕只有三十分。"
    "因为三十分的东西可以被修改，而零分的东西永远只能被想象。"
)
TEXT_EMO = "今天股票涨停了，真的好激动啊，我从来没有这么开心过。"

# 8 维情感顺序（与 IndexTTS / 我们的情感标签一致）
EMO_ORDER = ["happy", "angry", "sad", "afraid", "disgusted", "melancholic", "surprised", "calm"]


def wav_seconds(blob: bytes) -> float:
    if blob[:4] == b"RIFF":
        with wave.open(io.BytesIO(blob)) as w:
            return w.getnframes() / w.getframerate()
    tmp = OUT / "_probe.bin"
    tmp.write_bytes(blob)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(tmp)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out or 0)
    except Exception:
        return 0.0


def wav_fmt(blob: bytes) -> str:
    if blob[:4] != b"RIFF":
        return "非 RIFF"
    with wave.open(io.BytesIO(blob)) as w:
        return f"{w.getframerate()}Hz/{w.getnchannels()}ch/{w.getsampwidth()*8}bit"


def media_seconds(path: Path) -> float:
    if path.suffix.lower() == ".wav":
        return wav_seconds(path.read_bytes())
    try:
        return float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip() or 0)
    except Exception:
        return 0.0


class MV:
    def __init__(self, key: str, use_proxy: bool = False):
        self.key = key
        self.opener = build_opener() if use_proxy else build_opener(ProxyHandler({}))

    def _open(self, req: Request, timeout: int = 300) -> bytes:
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.read()
        except HTTPError as e:
            raise RuntimeError(
                f"HTTP {e.code}  {req.full_url}\n{e.read().decode('utf-8', 'ignore')[:700]}") from None

    def get(self, path: str) -> dict:
        req = Request(BASE + path, headers={"Authorization": f"Bearer {self.key}"})
        return json.loads(self._open(req, 60), parse_float=Decimal)

    def infer(self, text: str, ref: Path, emo_file: Path | None = None,
              payload_extra: dict | None = None) -> bytes:
        """★ 主路径：multipart + spk_audio_file，直接带参考音频，不用预上传。"""
        payload = {"input": text, "sample_rate": 22050, "interval_silence": 200}
        payload.update(payload_extra or {})
        boundary = "----mv" + uuid.uuid4().hex
        chunks: list[bytes] = []

        def field(name: str, value: str) -> None:
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n".encode())

        def file_field(name: str, path: Path) -> None:
            chunks.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                f"filename=\"{path.name}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
                + path.read_bytes() + b"\r\n")

        field("model", MODEL)
        field("payload", json.dumps(payload, ensure_ascii=False))
        file_field("spk_audio_file", ref)
        if emo_file:
            file_field("emo_audio_file", emo_file)
        chunks.append(f"--{boundary}--\r\n".encode())

        req = Request(BASE + "/v1/audio/infer", data=b"".join(chunks),
                      headers={"Authorization": f"Bearer {self.key}",
                               "Content-Type": f"multipart/form-data; boundary={boundary}"},
                      method="POST")
        return self._open(req)


def gen(api: MV, text: str, ref: Path, tag: str, extra: dict | None = None,
        emo_file: Path | None = None) -> dict:
    t0 = time.time()
    blob = api.infer(text, ref, emo_file=emo_file, payload_extra=extra)
    wall = time.time() - t0
    dur = wav_seconds(blob)
    path = OUT / f"{tag}.wav"
    path.write_bytes(blob)
    row = {"tag": tag, "chars": len(text), "audio_s": round(dur, 2), "fmt": wav_fmt(blob),
           "bytes": len(blob), "wall_s": round(wall, 2), "path": str(path),
           "payload_extra": extra or {}}
    if dur:
        row["chars_per_min"] = round(len(text) / (dur / 60))
        row["rtf_wall"] = round(wall / dur, 2)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Modelverse IndexTTS-2 零样本接口实测")
    ap.add_argument("--ref", default=str(DEFAULT_REF), help="参考音频（默认用 preset-voices 里 7.2 秒那个）")
    ap.add_argument("--emo", action="store_true", help="顺带测情感三档（向量 / 文本 / 情感音频）")
    ap.add_argument("--dry-run", action="store_true", help="只查单价与模型，不合成")
    ap.add_argument("--proxy", action="store_true", help="走环境代理（默认直连）")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    key = (os.environ.get("MODELVERSE_API_KEY") or os.environ.get("COMPSHARE_API_KEY") or "").strip()
    if not key:
        print("❌ 缺少 MODELVERSE_API_KEY")
        return 2
    api = MV(key, use_proxy=args.proxy)
    ref = Path(args.ref).expanduser().resolve()
    report: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "model": MODEL,
                    "ref": ref.name, "ref_s": round(media_seconds(ref), 2)}

    # ── 1. 单价（官方 Unit 字段）─────────────────────────────
    print("\n[1] 单价", flush=True)
    info = api.get(f"/v1/models/{MODEL}")
    for p in info.get("pricing") or []:
        for r in p.get("Rates") or []:
            print(f"   {r.get('ChargeItem'):<20} {r.get('Price')} {r.get('Currency')} / {r.get('Unit')}")
    print(f"   参考音频 {ref.name} = {report['ref_s']}s"
          f"（文档要求 {REF_MIN_S:.0f}~{REF_MAX_S:.0f}s；实测 3.2s 也能过）")
    if args.dry_run:
        print("\n(--dry-run：没合成)")
        return 0

    # ── 2. 零样本合成：短句 + 长句 ──────────────────────────
    print("\n[2] /v1/audio/infer（直接带参考音频，不预上传）", flush=True)
    short = gen(api, TEXT_SHORT, ref, "infer_short")
    long_ = gen(api, TEXT_LONG, ref, "infer_long")
    report["short"], report["long"] = short, long_
    for r in (short, long_):
        print(f"   ✓ {r['tag']:<12} {r['chars']:>3} 字 → {r['audio_s']:>6.2f}s {r['fmt']} "
              f"墙钟 {r['wall_s']:>5.1f}s  {r['chars_per_min']:>3} 字/分  RTF {r['rtf_wall']}×")
    if long_["chars"] > MAX_CHARS:
        print(f"   ⚠️ 长句 {long_['chars']} 字超过 {MAX_CHARS} 上限")

    # ── 3. 情感三档（可选）──────────────────────────────────
    if args.emo:
        print("\n[3] 情感控制三档（emo_control_method）", flush=True)
        cases = [
            ("emo_none", {"emo_control_method": 0}, None),
            ("emo_vec", {"emo_control_method": 2, "emo_vec": [0.8, 0, 0, 0, 0, 0, 0, 0],
                         "emo_alpha": 1.0}, None),
            ("emo_text", {"emo_control_method": 3, "emo_text": "愉快、激动"}, None),
        ]
        report["emo"] = []
        for tag, extra, emo_file in cases:
            try:
                r = gen(api, TEXT_EMO, ref, tag, extra, emo_file)
                report["emo"].append(r)
                print(f"   ✓ {tag:<10} {r['audio_s']:>5.2f}s  RTF {r['rtf_wall']}×  {extra.get('emo_control_method')=}")
            except Exception as e:
                print(f"   ❌ {tag}: {str(e)[:200]}")

    # ── 4. 成本（口径已锁定：1 token = 1 字符）──────────────
    print("\n[4] 成本（口径已由控制台日志锁定：1 token = 1 字符，标点也计费）", flush=True)
    rate_min = Decimal(str(long_.get("chars_per_min") or BENCH_CHARS_PER_MIN))
    per_wan = Decimal(10000) * YUAN_PER_MTOKEN / Decimal(10**6)
    per_hour = rate_min * 60 * YUAN_PER_MTOKEN / Decimal(10**6)
    print(f"   ¥{per_wan}/万字    ¥{per_hour:.2f}/音频小时（按实测 {rate_min} 字/分）")
    print(f"   art ¥{ART_YUAN_PER_WANZI}/万字 → 这是 art 的 {per_wan/ART_YUAN_PER_WANZI:.2f}×；"
          f"售价 ¥{OUR_YUAN_PER_WANZI}/万字 ⇒ 毛利 {(1-per_wan/OUR_YUAN_PER_WANZI)*100:.0f}%")
    print(f"   ¥50 ≈ {Decimal(50)/per_wan:.0f} 万字 ≈ {Decimal(50)/per_hour:.1f} 音频小时")
    report.update({"yuan_per_wanzi": str(per_wan), "yuan_per_hour": str(per_hour.quantize(Decimal('0.01'))),
                   "chars_per_min_measured": str(rate_min)})

    # ── 5. 吞吐估算（决定 chunker 怎么配）───────────────────
    if long_.get("rtf_wall"):
        per_600 = 600 / rate_min * Decimal(str(long_["rtf_wall"])) * Decimal("60")
        print(f"\n[5] 吞吐：600 字一段 ≈ {per_600:.0f}s 墙钟；"
              f"一本 10 万字的书 ≈ {100000/600:.0f} 段 ≈ "
              f"{per_600*Decimal(100000/600)/Decimal(3600):.1f} 小时（单并发）")
        report["seg_600s_wall"] = str(per_600.quantize(Decimal("0.1")))

    rp = OUT / "report.json"
    rp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), "utf-8")
    print(f"\n报告 {rp}\n音频 {OUT}/*.wav")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except RuntimeError as e:
        print(f"\n❌ {e}")
        sys.exit(1)
