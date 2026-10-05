import shutil
import subprocess

import pytest

from reelbot.media import RenderRequest, render_reel
from reelbot.worker import make_cover


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg unavailable")
def test_real_short_render_and_cover(tmp_path):
    image = tmp_path / "room.jpg"
    voice = tmp_path / "voice.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=640x360:rate=1", "-frames:v", "1", str(image)], check=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=4", "-c:a", "pcm_s16le", str(voice)], check=True)
    output = tmp_path / "reel.mp4"
    result = render_reel(RenderRequest([image], voice, output, target_seconds=4))
    assert result.width == 1080 and result.height == 1920
    assert 3.9 < result.duration_seconds < 4.2
    assert len(result.sha256) == 64
    assert make_cover(output, tmp_path / "cover.jpg").is_file()
