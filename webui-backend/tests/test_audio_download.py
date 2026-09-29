"""下载链路回归；屏蔽 .env，使用临时数据与模拟上游，不启动合成。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.TemporaryDirectory(prefix="audio-download-test-")
os.environ["DATA_DIR"] = TMP.name
_exists = Path.exists
with patch.object(Path, "exists", lambda p: False if p.name == ".env" else _exists(p)):
    from app.main import app
    from app import queue_state as qs
    from app.routes import podcast
from fastapi.testclient import TestClient
import httpx


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.saved = qs.queue_tasks.copy()
        qs.queue_tasks.clear()
        self.audio = Path(TMP.name) / "result.wav"
        self.audio.write_bytes(b"RIFF-test-audio")

    def tearDown(self):
        qs.queue_tasks.clear()
        qs.queue_tasks.update(self.saved)

    def assert_download(self, url, expected):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        header = response.headers["content-disposition"]
        self.assertTrue(header.startswith("attachment;"))
        actual = unquote(header.split("filename*=", 1)[1].split("''", 1)[1]) if "filename*=" in header else header.split('filename="', 1)[1].rstrip('"')
        self.assertEqual(actual, expected)
        self.assertEqual(response.content, b"RIFF-test-audio")

    def test_local_mono_and_podcast(self):
        for kind in ("mono", "podcast"):
            with self.subTest(kind=kind):
                qs.queue_tasks["local"] = {"project_name": "中文项目.wav", "output_path": str(self.audio)}
                self.assert_download(f"/api/{kind}/audio/local", "中文项目.wav")
                qs.queue_tasks["local"].pop("project_name")
                self.assert_download(f"/api/{kind}/audio/local", f"{kind}_local.wav")
        self.assertTrue(self.audio.exists())

    def test_historical_tts_id_and_proxy(self):
        qs.queue_tasks["queue-id"] = {"tts_task_id": "tts-id", "project_name": "双人旧项目"}
        upstream = AsyncMock(return_value=httpx.Response(200, content=b"RIFF-test-audio"))
        with patch.object(podcast.http_client, "get", upstream):
            self.assert_download("/api/podcast/audio/tts-id", "双人旧项目.wav")
            qs.queue_tasks.clear()
            self.assert_download("/api/podcast/audio/tts-id", "podcast_tts-id.wav")
        self.assertTrue(upstream.call_args.args[0].endswith("/api/task/tts-id/audio"))

    def test_historical_local_output(self):
        qs.queue_tasks["queue-id"] = {"tts_task_id": "tts-id", "project_name": "恢复项目", "output_path": str(self.audio)}
        with patch.object(podcast.http_client, "get", AsyncMock()) as upstream:
            self.assert_download("/api/podcast/audio/tts-id", "恢复项目.wav")
            upstream.assert_not_called()

    def test_sanitization(self):
        cases = [("中文/\\:*?\"<>|\r\n\x00\x7f", "中文_____________.wav"),
                 (" .. ", "audio.wav"), ("CON", "_CON.wav"),
                 ("节目.WAV.wav ", "节目.wav"), ("中文 . ", "中文.wav")]
        for name, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(qs.audio_download_name({"project_name": name}, "id", "mono"), expected)
        self.assertLessEqual(len(qs.audio_download_name({"project_name": "中" * 200}, "id", "mono").encode()), 244)

    def test_errors_and_unique_route(self):
        self.assertEqual(self.client.get("/api/mono/audio/missing").status_code, 404)
        qs.queue_tasks["missing"] = {}
        self.assertEqual(self.client.get("/api/mono/audio/missing").status_code, 404)
        with patch.object(podcast.http_client, "get", AsyncMock(return_value=httpx.Response(404, json={"detail": "不存在"}))):
            self.assertEqual(self.client.get("/api/podcast/audio/missing").status_code, 404)
        from app.routes import all_routers
        routes = [r for router in all_routers for r in router.routes if getattr(r, "path", "") == "/api/podcast/audio/{task_id}"]
        self.assertEqual(len(routes), 1)


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        TMP.cleanup()
