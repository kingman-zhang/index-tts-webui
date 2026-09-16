#!/usr/bin/env python3
"""Minimal AutoDL IndexTTS2 API smoke test.

The exact request-body field names are read from --body-template because AutoDL
can expose different fields for different workflow revisions. The script keeps
all provider-specific details in one JSON template and does not touch the
existing podcast-webui code.
"""
import argparse
import base64
import json
import os
import pathlib
import sys
import time
from typing import Any

import requests

DEFAULT_SUBMIT = "https://autodl.art/api/v1/comfyui/comfyui_workflow/indextts2-v1"
DEFAULT_RESULT = "https://autodl.art/api/v1/comfyui/comfyui_workflow/result/{task_id}"


def audio_data_uri(path: pathlib.Path) -> str:
    """把本地参考音频转成 data URI（autodl.art 的 prompt_simple 字段格式）。"""
    mime = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".m4a": "audio/mp4",
        ".flac": "audio/flac",
    }.get(path.suffix.lower(), "audio/wav")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def replace_values(value: Any, text: str, audio: str) -> Any:
    if isinstance(value, str):
        return value.replace("{{TEXT}}", text).replace("{{AUDIO}}", audio)
    if isinstance(value, list):
        return [replace_values(x, text, audio) for x in value]
    if isinstance(value, dict):
        return {k: replace_values(v, text, audio) for k, v in value.items()}
    return value


def save_result(session: requests.Session, result: Any, output: pathlib.Path) -> None:
    candidates = []
    if isinstance(result, list):
        candidates = result
    elif isinstance(result, dict):
        candidates = result.get("results", [])
    for item in candidates:
        url = item if isinstance(item, str) else item.get("url") if isinstance(item, dict) else None
        if url and isinstance(url, str) and url.startswith(("http://", "https://")):
            response = session.get(url, timeout=120)
            response.raise_for_status()
            output.write_bytes(response.content)
            print(f"音频已保存: {output} ({len(response.content)} bytes)")
            return
    print("任务成功，但没有识别到可下载的音频 URL。原始 results:")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="测试 AutoDL IndexTTS2 工作流")
    parser.add_argument("--audio", required=True, help="参考音频本地路径；脚本会自动转成 data URI 填入 prompt_simple")
    parser.add_argument("--text", default="哈喽，大家好，欢迎收听我们的播客。这里是慢半拍，我是半熟老哥。", help="要合成的文字")
    parser.add_argument("--body-template", required=True, help="AutoDL 在线调用页面复制出的请求体 JSON 文件")
    parser.add_argument("--token", default=os.getenv("AUTODL_API_TOKEN"), help="API Token，也可用 AUTODL_API_TOKEN")
    parser.add_argument("--output", default="autodl-indextts2-test.wav", help="输出音频文件")
    parser.add_argument("--timeout", type=int, default=600, help="最大等待秒数")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数")
    args = parser.parse_args()

    if not args.token:
        print("缺少 Token：请设置 AUTODL_API_TOKEN 或传入 --token", file=sys.stderr)
        return 2
    template_path = pathlib.Path(args.body_template)
    audio_path = pathlib.Path(args.audio)
    if not audio_path.is_file():
        print(f"参考音频不存在: {audio_path}", file=sys.stderr)
        return 2
    body = replace_values(
        json.loads(template_path.read_text(encoding="utf-8")),
        args.text,
        audio_data_uri(audio_path),
    )

    session = requests.Session()
    session.headers.update({"Authorization": args.token, "Content-Type": "application/json"})
    response = session.post(DEFAULT_SUBMIT, json=body, timeout=60)
    print("提交 HTTP 状态:", response.status_code)
    print(response.text)
    response.raise_for_status()
    submitted = response.json()
    task_id = submitted.get("data", {}).get("task_id")
    if not task_id:
        raise RuntimeError("响应中没有 data.task_id，请检查请求体或 API 返回")

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        response = session.get(DEFAULT_RESULT.format(task_id=task_id), timeout=60)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", payload)
        status = str(data.get("status", "")).upper()
        print(f"任务状态: {status}  duration={data.get('duration')}")
        if status == "SUCCESS":
            save_result(session, data.get("results", []), pathlib.Path(args.output))
            return 0
        if status == "FAILED":
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1
        time.sleep(args.interval)
    raise TimeoutError(f"任务超过 {args.timeout} 秒仍未完成，task_id={task_id}")


if __name__ == "__main__":
    raise SystemExit(main())
