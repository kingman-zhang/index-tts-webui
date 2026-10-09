"""响度归一（app/audio_norm.py）单测 —— 2026-10-09 上提到池层后新增。

完全离线：不读 .env、不连网、不加载模型。需要 ffmpeg 的用例在缺失时跳过。
"""
import io
import math
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='audio-norm-test-')
_original_exists = Path.exists
with patch.object(Path, 'exists', lambda p: False if p.name == '.env' else _original_exists(p)):
    from app import audio_norm

FFMPEG = audio_norm.find_ffmpeg()


def tone(ms=1200, rate=24000, freq=220.0, amp=0.03):
    """低电平正弦（约 -34 LUFS）：模拟未经归一的引擎输出。"""
    frames = b''.join(
        struct.pack('<h', int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(rate * ms // 1000)
    )
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        w.writeframes(frames)
    return out.getvalue()


def lufs(data: bytes) -> float:
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostats", "-i", "pipe:0",
         "-af", "ebur128=peak=true", "-f", "null", "-"],
        input=data, capture_output=True,
    )
    for line in r.stderr.decode("utf-8", "ignore").splitlines():
        s = line.strip()
        if s.startswith("I:") and s.split()[1] != "-inf":
            return float(s.split()[1])
    return float("nan")


def duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as r:
        return r.getnframes() / r.getframerate()


class AudioNormTests(unittest.TestCase):
    def test_disabled_returns_input_unchanged(self):
        """PODCAST_NORM=off：逐字节原样返回，一次 ffmpeg 都不跑。"""
        raw = tone()
        with patch.object(audio_norm, 'NORM_ENABLED', False):
            self.assertEqual(audio_norm.apply_loudness(raw), raw)

    def test_no_ffmpeg_returns_input(self):
        """无 ffmpeg：返回原始数据（后处理失败不该毁掉已合成的段）。"""
        raw = tone()
        with patch.object(audio_norm, 'find_ffmpeg', return_value=None), \
             patch.object(audio_norm, '_FFMPEG_WARNED', True):
            self.assertEqual(audio_norm.apply_loudness(raw), raw)

    @unittest.skipIf(FFMPEG is None, "需要 ffmpeg")
    def test_normalizes_to_target_and_keeps_duration(self):
        raw = tone()
        self.assertLess(lufs(raw), -25, "样本本身应是低电平")
        out = audio_norm.apply_loudness(raw)
        self.assertAlmostEqual(lufs(out), audio_norm.NORM_TARGET_LUFS, delta=1.0)
        # 只调响度、不变速
        self.assertAlmostEqual(duration(out), duration(raw), places=3)
        # 输出仍是 24kHz 单声道 PCM16
        with wave.open(io.BytesIO(out)) as r:
            self.assertEqual((r.getframerate(), r.getnchannels(), r.getsampwidth()),
                             (24000, 1, 2))

    @unittest.skipIf(FFMPEG is None, "需要 ffmpeg")
    def test_idempotent_on_already_normalized_input(self):
        """已经 -16 的输入再过一次仍停在 -16（池层可能收到壳已归一的音频）。"""
        once = audio_norm.apply_loudness(tone())
        twice = audio_norm.apply_loudness(once)
        self.assertAlmostEqual(lufs(twice), audio_norm.NORM_TARGET_LUFS, delta=0.5)

    def test_silence_is_handled_without_crashing(self):
        """全静音输入必须安全：不抛异常、时长不变、输出仍是合法 24k/mono/PCM16。

        注意 ffmpeg 对全静音报的是 `I: -70.0` 而不是 `-inf`，所以走的是「抬增益」
        分支（增益被 NORM_MAX_GAIN_DB 钳住）；`measure_loudness` 返回 None 只在
        ffmpeg 失败或音频不可解析时发生。
        """
        if FFMPEG is None:
            self.skipTest("需要 ffmpeg")
        silence = tone(ms=1200, amp=0.0)
        measured = audio_norm.measure_loudness(FFMPEG, silence)
        self.assertTrue(measured is None or measured < -60)
        out = audio_norm.apply_loudness(silence)
        self.assertAlmostEqual(duration(out), duration(silence), places=3)
        with wave.open(io.BytesIO(out)) as r:
            self.assertEqual((r.getframerate(), r.getnchannels(), r.getsampwidth()),
                             (24000, 1, 2))


if __name__ == '__main__':
    unittest.main()
