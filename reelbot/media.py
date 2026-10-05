"""Deterministic, credit-free media assembly for approved reels.

Only local files are accepted. The caller owns asset authorization, narrative
choices, and final human approval; this module never calls a generative API.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from typing import Sequence


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration_seconds: float | None
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    format_name: str
    video_codec: str | None = None
    audio_codec: str | None = None


@dataclass(frozen=True)
class RenderRequest:
    assets: Sequence[Path]
    narration: Path
    output: Path
    subtitles: Path | None = None
    target_seconds: float = 60.0
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    video_bitrate: str = "4M"


@dataclass(frozen=True)
class RenderResult:
    path: Path
    sha256: str
    duration_seconds: float
    width: int
    height: int


@dataclass(frozen=True)
class Shot:
    asset_index: int
    duration_seconds: float


def plan_shots(duration_seconds: float, asset_count: int) -> list[Shot]:
    """Keep visual beats short; recycle approved assets when necessary."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0 or asset_count < 1:
        raise MediaError("Invalid duration or asset count")
    pattern = (4.0, 5.0, 3.5, 5.5)
    remaining = duration_seconds
    durations: list[float] = []
    while remaining > 1e-6:
        beat = min(pattern[len(durations) % len(pattern)], remaining)
        durations.append(beat)
        remaining -= beat
    if len(durations) > 1 and durations[-1] < 2.5:
        tail = durations.pop()
        durations[-1] += tail
    return [Shot(index % asset_count, seconds) for index, seconds in enumerate(durations)]


def _run(argv: list[str], *, timeout: int = 900) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"Media command failed: {exc}") from exc
    if result.returncode:
        raise MediaError(f"Media command failed ({result.returncode}): {result.stderr[-2500:]}")
    return result


def sha256_file(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inspect_media(path: Path, *, ffprobe: str = "ffprobe") -> MediaInfo:
    path = Path(path).resolve(strict=True)
    result = _run([ffprobe, "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_type,codec_name,width,height", "-of", "json", str(path)], timeout=60)
    try:
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        has_audio = audio is not None
        raw_duration = data.get("format", {}).get("duration")
        duration = float(raw_duration) if raw_duration is not None else None
        if duration is not None and not math.isfinite(duration):
            duration = None
    except (ValueError, TypeError, KeyError) as exc:
        raise MediaError(f"Invalid ffprobe response for {path}") from exc
    return MediaInfo(path, duration, video is not None, has_audio,
                     int(video["width"]) if video and video.get("width") else None,
                     int(video["height"]) if video and video.get("height") else None,
                     data.get("format", {}).get("format_name", ""),
                     video.get("codec_name") if video else None,
                     audio.get("codec_name") if audio else None)


def _escape_filter_path(path: Path) -> str:
    # ffmpeg filter expressions parse backslashes, drive colons and quotes.
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'").replace("[", "\\[").replace("]", "\\]")


def build_render_command(request: RenderRequest, *, asset_info: Sequence[MediaInfo], narration_info: MediaInfo) -> list[str]:
    """Build an argv list; useful for dry-run inspection without spending credits."""
    if not asset_info:
        raise MediaError("At least one visual asset is required")
    if not narration_info.has_audio:
        raise MediaError("Narration file has no audio stream")
    if request.target_seconds <= 0 or request.target_seconds > 300:
        raise MediaError("target_seconds must be between 0 and 300")
    # Never truncate the exact approved narration merely to hit 60 seconds.
    duration = max(float(request.target_seconds), narration_info.duration_seconds or 0.0)
    if duration > 300:
        raise MediaError("Narration exceeds the 5 minute safety limit")
    shots = plan_shots(duration, len(asset_info))
    command = [request.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for info in asset_info:
        if not info.has_video:
            raise MediaError(f"No image/video stream: {info.path}")
        # Still images have no duration; videos loop so short clips can fill a beat.
        if info.duration_seconds is None or info.duration_seconds < 0.1:
            command += ["-loop", "1", "-i", str(info.path)]
        else:
            command += ["-stream_loop", "-1", "-i", str(info.path)]
    command += ["-i", str(narration_info.path)]
    chains = []
    counts = [sum(shot.asset_index == index for shot in shots) for index in range(len(asset_info))]
    shot_labels: list[list[str]] = [[] for _ in asset_info]
    for shot_index, shot in enumerate(shots):
        shot_labels[shot.asset_index].append(f"[raw{shot_index}]")
    for index, labels in enumerate(shot_labels):
        # Decode a source once even when it is reused for several short beats.
        if not labels:
            continue
        chains.append(f"[{index}:v]fps=30,split={counts[index]}{''.join(labels)}")
    offsets = [0.0 for _ in asset_info]
    for shot_index, shot in enumerate(shots):
        info = asset_info[shot.asset_index]
        start = offsets[shot.asset_index] if info.duration_seconds else 0.0
        if info.duration_seconds:
            offsets[shot.asset_index] += shot.duration_seconds
        chains.append(
            f"[raw{shot_index}]trim=start={start:.6f}:duration={shot.duration_seconds:.6f},"
            f"setpts=PTS-STARTPTS,split=2[bg{shot_index}][fg{shot_index}]"
        )
        # A blurred fill makes horizontal rooms fit a portrait frame without
        # cutting walls, furniture, windows or labels from the main image.
        chains.append(
            f"[bg{shot_index}]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,boxblur=24:1,setsar=1[b{shot_index}]"
        )
        chains.append(
            f"[fg{shot_index}]scale=1040:1880:force_original_aspect_ratio=decrease,"
            f"setsar=1[f{shot_index}]"
        )
        motion = "+10*sin(t*0.65)" if info.duration_seconds is None else ""
        chains.append(
            f"[b{shot_index}][f{shot_index}]overlay=x='(W-w)/2{motion}':"
            f"y='(H-h)/2':eval=frame:shortest=1[v{shot_index}]"
        )
    labels = "".join(f"[v{i}]" for i in range(len(shots)))
    chains.append(f"{labels}concat=n={len(shots)}:v=1:a=0[vcat]")
    if request.subtitles:
        chains.append(f"[vcat]subtitles=filename='{_escape_filter_path(request.subtitles)}'[vout]")
    else:
        chains.append("[vcat]null[vout]")
    audio_index = len(asset_info)
    chains.append(f"[{audio_index}:a]apad,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[aout]")
    command += ["-filter_complex", ";".join(chains), "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-maxrate", request.video_bitrate,
                "-bufsize", "8M", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "128k",
                "-ar", "48000", "-movflags", "+faststart", "-t", f"{duration:.6f}", str(request.output)]
    return command


def verify_reel(path: Path, *, ffprobe: str = "ffprobe", min_seconds: float = 1.0) -> RenderResult:
    info = inspect_media(path, ffprobe=ffprobe)
    if (not info.has_video or not info.has_audio or info.width != 1080 or info.height != 1920
            or info.video_codec != "h264" or info.audio_codec != "aac"):
        raise MediaError("Output must contain 1080x1920 H.264 video and AAC audio")
    if not info.duration_seconds or info.duration_seconds < min_seconds:
        raise MediaError("Output duration is invalid")
    return RenderResult(info.path, sha256_file(info.path), info.duration_seconds, 1080, 1920)


def render_reel(request: RenderRequest) -> RenderResult:
    """Render only from caller-supplied local assets and narration; no AI spend."""
    if not request.assets:
        raise MediaError("At least one visual asset is required")
    if request.subtitles and not Path(request.subtitles).is_file():
        raise MediaError("Subtitle file does not exist")
    if not shutil.which(request.ffmpeg) and not Path(request.ffmpeg).is_file():
        raise MediaError(f"ffmpeg is unavailable: {request.ffmpeg}")
    infos = [inspect_media(Path(asset), ffprobe=request.ffprobe) for asset in request.assets]
    narration_info = inspect_media(Path(request.narration), ffprobe=request.ffprobe)
    output = Path(request.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + ".rendering" + output.suffix)
    staged = RenderRequest(request.assets, request.narration, temporary, request.subtitles,
                           request.target_seconds, request.ffmpeg, request.ffprobe, request.video_bitrate)
    try:
        _run(build_render_command(staged, asset_info=infos, narration_info=narration_info), timeout=3600)
        verified = verify_reel(temporary, ffprobe=request.ffprobe)
        os.replace(temporary, output)
        return RenderResult(output.resolve(), verified.sha256, verified.duration_seconds, 1080, 1920)
    finally:
        temporary.unlink(missing_ok=True)
