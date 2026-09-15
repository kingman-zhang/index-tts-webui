#!/usr/bin/env python3
"""预下载 / 体检 IndexTTS2 启动时联网拉取的 4 个辅助模型。

为什么需要这个脚本（踩过的坑）
    IndexTTS v2.0.0 的 ``indextts/infer_v2.py`` 第 4 行硬写：

        os.environ['HF_HUB_CACHE'] = './checkpoints/hf_cache'

    这是「相对路径 + 直接赋值」，两个后果：
      1) 直接赋值 ⇒ **会覆盖**你在外面 export 的 ``HF_HOME`` / ``HF_HUB_CACHE``。
         所以「export HF_HOME=/xxx 然后 snapshot_download(...)」下到的目录，
         服务器启动时**根本不会读**，它会照旧重下一遍（表现为静默卡住）。
      2) 相对路径 ⇒ 相对启动时的 CWD，同一个模型换个目录启动就会再下一遍。
    结论：预下载必须写进「它实际会读的那个目录」，也就是 tts-server 的 CWD 下
    的 ``checkpoints/hf_cache``（用 --cache 显式指定）。

    新版合并源码（index-tts-main）已改为 ``{model_dir}/hf_cache`` + 全部
    ``local_files_only=True``，读的是另一个目录，换版本时注意 --cache 也要换。

用法
    # 体检（不联网）：看缓存目录里到底缺哪个文件
    python prefetch_aux_models.py --cache <DIR> --verify-only

    # 预下载（写入同一个缓存目录）
    HF_ENDPOINT 由 --endpoint 控制，默认 https://hf-mirror.com
    python prefetch_aux_models.py --cache <DIR>

    # 典型：v2.0.0 在 /root/index-tts-webui/tts-server 下启动
    python prefetch_aux_models.py --cache /root/index-tts-webui/tts-server/checkpoints/hf_cache

下载完成后可以把 ``HF_HUB_OFFLINE=1`` 写进启动脚本：缓存命中则完全不联网、
秒过这一段；缓存缺失会直接报错而不是静默卡死。
"""

from __future__ import annotations

import argparse
import os
import sys

DEFAULT_BIGVGAN = "nvidia/bigvgan_v2_22khz_80band_256x"
DEFAULT_ENDPOINT = "https://hf-mirror.com"

# (kind, repo_id, filename, 说明)
ITEMS = (
    ("snapshot", "facebook/w2v-bert-2.0", "", "w2v-bert-2.0 整个仓库（启动大头，~2.3GB）"),
    ("file", "amphion/MaskGCT", "semantic_codec/model.safetensors", "MaskGCT semantic codec"),
    ("file", "funasr/campplus", "campplus_cn_common.bin", "CAMPPlus 说话人编码器"),
    ("file", DEFAULT_BIGVGAN, "config.json", "BigVGAN 配置"),
    ("file", DEFAULT_BIGVGAN, "bigvgan_generator.pt", "BigVGAN 权重"),
)


# ── 缓存布局（纯 pathlib，不需要 huggingface_hub） ──────────────


def _repo_root(cache_dir: str, repo_id: str) -> str:
    return os.path.join(cache_dir, "models--" + repo_id.replace("/", "--"))


def _snapshot_dir(cache_dir: str, repo_id: str) -> str | None:
    """返回 snapshots/<hash>；优先用 refs/main 记录的 commit。"""
    root = _repo_root(cache_dir, repo_id)
    refs = os.path.join(root, "refs", "main")
    if os.path.isfile(refs):
        with open(refs) as fh:
            commit = fh.read().strip()
        snap = os.path.join(root, "snapshots", commit)
        if os.path.isdir(snap):
            return snap
    snaps = os.path.join(root, "snapshots")
    if os.path.isdir(snaps):
        subs = sorted(d for d in os.listdir(snaps) if os.path.isdir(os.path.join(snaps, d)))
        if len(subs) == 1:
            return os.path.join(snaps, subs[0])
    return None


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def _incomplete_blobs(cache_dir: str) -> list[str]:
    """列出半截下载文件（.incomplete），用于判断"卡住"还是"在慢慢下"。"""
    found: list[str] = []
    for root, _dirs, files in os.walk(cache_dir):
        for name in files:
            if name.endswith(".incomplete"):
                found.append(os.path.join(root, name))
    return found


def verify(cache_dir: str, bigvgan_repo: str) -> bool:
    print(f"缓存目录 : {cache_dir}")
    print(f"存在     : {os.path.isdir(cache_dir)}")
    if not os.path.isdir(cache_dir):
        print("=> 目录不存在：服务器一定会重新下载（且可能静默卡住）")
        return False

    print("-" * 68)
    all_ok = True
    for kind, repo_id, filename, label in ITEMS:
        rid = bigvgan_repo if repo_id == DEFAULT_BIGVGAN else repo_id
        snap = _snapshot_dir(cache_dir, rid)
        if snap is None:
            print(f"[缺失] {label}\n       {rid}（缓存里没有这个仓库）")
            all_ok = False
            continue
        if kind == "snapshot":
            total = 0
            for root, _dirs, files in os.walk(snap):
                for name in files:
                    p = os.path.join(root, name)
                    if os.path.isfile(p):
                        total += os.path.getsize(p)
            if total == 0:
                print(f"[缺失] {label}\n       {snap} 为空")
                all_ok = False
            else:
                print(f"[就绪] {label}\n       {_human(total)}  {snap}")
        else:
            target = os.path.join(snap, *filename.split("/"))
            if os.path.isfile(target):
                print(f"[就绪] {label}\n       {_human(os.path.getsize(target))}  {filename}")
            else:
                print(f"[缺失] {label}\n       {rid} / {filename}")
                all_ok = False

    inc = _incomplete_blobs(cache_dir)
    if inc:
        print("-" * 68)
        print(f"发现 {len(inc)} 个半截下载文件（说明之前是在下、不是死锁）：")
        for p in inc:
            print(f"       {_human(os.path.getsize(p))}  {p}")

    print("-" * 68)
    print("结论      : " + ("全部就绪，启动不会再联网拉这些模型" if all_ok else "有缺失，见上面 [缺失] 行"))
    return all_ok


# ── 下载 ────────────────────────────────────────────────────


def prefetch(cache_dir: str, endpoint: str, bigvgan_repo: str) -> None:
    # 必须在 import huggingface_hub 之前设置：constants 在 import 时读取环境变量
    os.environ["HF_ENDPOINT"] = endpoint
    os.environ["HF_HUB_CACHE"] = os.path.abspath(cache_dir)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)

    from huggingface_hub import hf_hub_download, snapshot_download  # noqa: E402

    os.makedirs(cache_dir, exist_ok=True)
    print(f"镜像     : {endpoint}")
    print(f"写入     : {os.path.abspath(cache_dir)}")
    print("-" * 68)

    for kind, repo_id, filename, label in ITEMS:
        rid = bigvgan_repo if repo_id == DEFAULT_BIGVGAN else repo_id
        print(f">> {label}  <- {rid}", flush=True)
        if kind == "snapshot":
            path = snapshot_download(rid)
        else:
            path = hf_hub_download(rid, filename=filename)
        print(f"   ok: {path}", flush=True)

    print("-" * 68)
    print("下载完成，复检：")
    verify(cache_dir, bigvgan_repo)


def main() -> int:
    ap = argparse.ArgumentParser(description="IndexTTS2 辅助模型预下载 / 体检")
    ap.add_argument("--cache", required=True,
                    help="服务器实际使用的 HF 缓存目录（v2.0.0: <tts-server>/checkpoints/hf_cache）")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help=f"HF 镜像，默认 {DEFAULT_ENDPOINT}")
    ap.add_argument("--bigvgan-repo", default=DEFAULT_BIGVGAN,
                    help="BigVGAN 仓库，以 config.yaml 的 vocoder.name 为准")
    ap.add_argument("--verify-only", action="store_true", help="只体检，不联网")
    args = ap.parse_args()

    if args.verify_only:
        return 0 if verify(args.cache, args.bigvgan_repo) else 1
    prefetch(args.cache, args.endpoint, args.bigvgan_repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
