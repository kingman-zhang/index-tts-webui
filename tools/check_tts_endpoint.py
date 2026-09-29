#!/usr/bin/env python3
"""TTS 服务器接入自检：把一台 tts-server 接进资源池之前，先确认它"真的能用"。

为什么需要这个脚本
------------------
资源池的 local 资源把参考音频的**服务器侧绝对路径**原样发过去：
  engines/indextts_local.py 取 VoiceRef.tts_path（任务里存的原始路径）
  tts-server/server.py 用 os.path.exists(req.voice) 校验
backend **不会**上传参考音频，所以"那台服务器上有没有这个文件"决定了合成
会不会 400 —— 而这一步在「网络通了、资源池也配好了」之后仍然可能失败，
且失败发生在**提交之后**：池把异常一律包成 NonRetryable（防重复计费），
不会自动改投云端，用户看到的就是整个任务失败。

本脚本把这一步提前成一次**只读**检查：不改配置、不上传、不删除。

检查项
------
  1. /api/health   —— 地址通不通、模型加载没有、服务器音色目录在哪
  2. /api/voices   —— 服务器上现有的参考音频
  3. 音色对齐      —— 本地三类音色里，哪些在服务器上不存在（= 会 400 的）
  4. --probe-synth —— 可选：用服务器上已有的音色真跑一次合成（端到端验证）

用法
----
  python tools/check_tts_endpoint.py --tts-url http://1.2.3.4:8000
  python tools/check_tts_endpoint.py --tts-url http://1.2.3.4:8000 --probe-synth
  python tools/check_tts_endpoint.py --tts-url http://1.2.3.4:8000 --token <KEY>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "webui-backend" / "data"
AUDIO_EXT = {".wav", ".mp3", ".flac", ".ogg", ".webm", ".m4a"}

# (标签, 相对 data 的目录, 前端选中时会不会自动上传)
GROUPS = [
    ("预设音色", ["preset-voices"], True),
    ("自上传音色", ["voices"], False),
    ("BreezeBlue", ["breezeblue/audio"], False),
]


def request(url: str, token: str | None = None, payload: dict | None = None,
            timeout: float = 15.0, raw: bool = False):
    """返回 (status, body)。raw=True 时 body 是 bytes，否则是解析后的 JSON。"""
    headers = {"Accept": "application/json"}
    if token:
        headers["X-API-Key"] = token
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        if raw:
            return resp.status, data
        return resp.status, json.loads(data) if data else {}


def collect_local(data_dir: Path) -> list[tuple[str, bool, list[Path]]]:
    out = []
    for label, rel_dirs, auto_upload in GROUPS:
        found: list[Path] = []
        for rel in rel_dirs:
            d = data_dir / rel
            if not d.is_dir():
                continue
            for p in sorted(d.rglob("*")):
                if p.is_file() and p.suffix.lower() in AUDIO_EXT:
                    found.append(p)
        out.append((label, auto_upload, found))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="TTS 服务器接入自检（只读）")
    ap.add_argument("--tts-url", default=os.environ.get("TTS_URL", "http://127.0.0.1:8000"),
                    help="tts-server 地址（默认读 TTS_URL，再默认 127.0.0.1:8000）")
    ap.add_argument("--token", default=os.environ.get("TTS_SERVER_TOKEN") or None,
                    help="可选：X-API-Key（服务端启用鉴权时才需要）")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA), help="backend data 目录")
    ap.add_argument("--probe-synth", action="store_true",
                    help="额外真跑一次合成（会占用该服务器的 GPU 时间）")
    ap.add_argument("--timeout", type=float, default=15.0)
    args = ap.parse_args()

    url = args.tts_url.rstrip("/")
    problems: list[str] = []

    print("TTS 服务器接入自检")
    print(f"  目标  : {url}")
    print(f"  鉴权  : {'X-API-Key 已提供' if args.token else '无（服务端未启用鉴权）'}")
    print()

    # ── ① 连通性 ──────────────────────────────────────────────
    print("① 连通性 /api/health")
    try:
        _, health = request(f"{url}/api/health", args.token, timeout=args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"   ✗ HTTP {exc.code}：{exc.read()[:200]!r}")
        print("     → 若是 401/403：服务端启用了鉴权，请确认 --token")
        return 2
    except (urllib.error.URLError, OSError) as exc:
        print(f"   ✗ 连不上：{exc}")
        print("     → 检查：安全组/防火墙是否放行了本机出口 IP？地址端口对不对？")
        print("     → 服务端在监听吗：服务器上 curl -s localhost:8000/api/health")
        return 2

    loaded = health.get("model_loaded")
    print(f"   status={health.get('status')}  model_loaded={loaded}")
    print(f"   device={health.get('device')}  fp16={health.get('fp16')}")
    print(f"   voices_dir = {health.get('voices_dir')}")
    print(f"   output_dir = {health.get('output_dir')}")
    if not loaded:
        problems.append("模型未加载（model_loaded=false）—— 合成必定失败")
    print()

    # ── ② 服务器音色 ──────────────────────────────────────────
    print("② 服务器已有的参考音频 /api/voices")
    try:
        _, voices = request(f"{url}/api/voices", args.token, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"   ✗ 查询失败：{exc}")
        return 2
    server_voices = voices.get("voices", [])
    server_names = {v.get("name") for v in server_voices if v.get("name")}
    print(f"   共 {len(server_names)} 个")
    for v in server_voices[:5]:
        print(f"     - {v.get('name')}  ({v.get('size_kb')} KB)")
    if len(server_names) > 5:
        print(f"     … 其余 {len(server_names) - 5} 个省略")
    print()

    # ── ③ 音色对齐 ────────────────────────────────────────────
    data_dir = Path(args.data_dir)
    print("③ 音色对齐（按文件名比对本地 vs 服务器）")
    total_missing_auto, total_missing_manual = 0, 0
    for label, auto_upload, files in collect_local(data_dir):
        local_names = {p.name for p in files}
        missing = sorted(local_names - server_names)
        mark = "✅" if not missing else ("⚠️" if auto_upload else "❌")
        tail = "（选中时前端会自动上传，风险低）" if auto_upload else "（不会自动上传）"
        print(f"   {mark} {label:<12} 本地 {len(local_names):>4} 个 → 服务器缺 {len(missing):>4} 个 {tail}")
        if missing:
            for name in missing[:3]:
                print(f"        缺: {name}")
            if len(missing) > 3:
                print(f"        … 其余 {len(missing) - 3} 个省略")
        if auto_upload:
            total_missing_auto += len(missing)
        else:
            total_missing_manual += len(missing)
    print()

    # ── ④ 可选：真跑一次合成 ──────────────────────────────────
    if args.probe_synth:
        print("④ 端到端合成 /api/synthesize（会占用 GPU 时间）")
        if not server_names:
            print("   ⏭ 跳过：服务器上没有任何参考音频可用")
        else:
            voice = server_voices[0]
            voice_path = voice.get("path") or voice.get("name")
            payload = {
                "voice": voice_path,
                "text": "接入自检，一二三四五。",
                "emotion": {"mode": 0, "vector": [0.0] * 8, "weight": 0.65, "random": False},
                "params": {"speed": 1.0},
            }
            started = time.time()
            try:
                _, result = request(f"{url}/api/synthesize", args.token, payload, timeout=300.0)
                out_name = result.get("output_filename")
                if not out_name:
                    print(f"   ✗ 未返回 output_filename：{result}")
                    problems.append("合成返回体缺少 output_filename")
                else:
                    _, audio = request(f"{url}/api/audio/{out_name}", args.token, raw=True)
                    cost = time.time() - started
                    print(f"   ✅ 合成成功（{cost:.1f}s）：{out_name}  {len(audio)} 字节")
                    print(f"      音色：{voice.get('name')}")
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300]
                print(f"   ✗ HTTP {exc.code}：{detail!r}")
                problems.append(f"合成探测失败 HTTP {exc.code}")

    # ── 结论 ──────────────────────────────────────────────────
    print()
    print("结论")
    if total_missing_manual:
        problems.append(
            f"有 {total_missing_manual} 个音色在服务器上不存在，"
            "用户选中它们且任务被调度到这台 local 资源时会 400（且不会自动改投云端）"
        )
    if total_missing_auto:
        problems.append(
            f"有 {total_missing_auto} 个预设音色尚未上传（选中时前端会自动上传，"
            "但要求 backend 的 TTS_URL 指向同一台服务器）"
        )
    if not problems:
        print("   ✅ 未发现阻断项，可以接入资源池")
        print("   下一步：把该服务器写进 backend 的 TTS_RESOURCES（provider=local）")
    else:
        for p in problems:
            print(f"   ⚠️ {p}")
        print()
        print("   提示：共享挂载（同一份音色目录两边都可见）能一次性消除音色对齐问题；")
        print("         跨存储域时需要用 rsync/上传把缺失文件同步过去。")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
