"""Bounded FFmpeg renderer. One OS lock covers every process and generation."""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import re
import subprocess
import time
from pathlib import Path

import imageio_ffmpeg
import psutil
from PIL import Image, ImageDraw, ImageFont

from ttakkari.creative.service import Studio, default_studio, digest, now

FPS = 24
log = logging.getLogger(__name__)


def ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def inspect_media(path: Path) -> dict:
    formats = {
        ".wav": "wav",
        ".mp3": "mp3",
        ".flac": "flac",
        ".ogg": "ogg",
        ".m4a": "mov",
        ".mp4": "mov",
        ".mov": "mov",
        ".webm": "matroska",
    }
    r = subprocess.run(
        [
            ffmpeg(),
            "-hide_banner",
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            formats[path.suffix.lower()],
            "-i",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=20,
    )
    text = r.stderr
    duration = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if not duration:
        raise ValueError("읽을 수 없는 미디어이거나 길이를 확인할 수 없습니다.")
    h, m, s = map(float, duration.groups())
    return {
        "duration": h * 3600 + m * 60 + s,
        "has_audio": "Audio:" in text,
        "has_video": "Video:" in text,
    }


def _require_readable_image(path: Path, scene_id: str) -> None:
    """읽을 수 없는 이미지를 FFmpeg '-loop 1'에 넣으면 제한 시간(6분)까지 멈춘다. 먼저 빨리 실패시킨다."""
    try:
        with Image.open(path) as im:
            im.verify()
    except (OSError, ValueError, SyntaxError) as e:
        raise ValueError(f"장면 {scene_id}의 이미지를 읽을 수 없습니다.") from e


def dimensions(aspect: str):
    return {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (720, 720)}[aspect]


def _wrap_text_lines(text: str, draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont,
                     max_width: float) -> list[str]:
    """Prefer whitespace boundaries; split a token into characters only when it cannot fit alone."""
    lines: list[str] = []
    for paragraph in text.replace("\r", "").split("\n"):
        line = ""
        for word in paragraph.split():
            if draw.textlength(word, font=font) > max_width:
                if line:
                    lines.append(line)
                    line = ""
                for char in word:
                    if line and draw.textlength(line + char, font=font) > max_width:
                        lines.append(line)
                        line = char
                    else:
                        line += char
                continue
            candidate = f"{line} {word}" if line else word
            if line and draw.textlength(candidate, font=font) > max_width:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
    return lines


def compile_caption_segments(
    text: str,
    duration: float,
    width: int,
    *,
    style: str = "bottom_card",
    max_lines: int = 2,
) -> list[dict]:
    """Wrap narration into safe-area cues and spread them over the estimated voice interval."""
    if not text.strip():
        return []
    if duration <= 0:
        raise ValueError("자막 구간 길이는 0보다 커야 합니다.")
    font_path = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    if not font_path.is_file():
        raise ValueError("한국어 자막 글꼴(AppleSDGothicNeo)이 없습니다.")
    font_scale, width_scale = {
        "bottom_card": (0.032, 0.84),
        "center_highlight": (0.052, 0.85),
        "title_card": (0.046, 0.82),
        "code_overlay": (0.028, 0.84),
    }.get(style, (0.032, 0.84))
    font = ImageFont.truetype(str(font_path), round(width * font_scale))
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_width = width * width_scale
    lines = _wrap_text_lines(text, measure, font, max_width)
    groups = [lines[i : i + max_lines] for i in range(0, len(lines), max_lines)]
    cue_texts = ["\n".join(group) for group in groups]
    weights = [max(1, len(cue.replace("\n", "").replace(" ", ""))) for cue in cue_texts]
    weight_total = sum(weights)
    cursor = 0.0
    cues = []
    for index, (cue, weight) in enumerate(zip(cue_texts, weights, strict=True)):
        end = duration if index == len(cue_texts) - 1 else duration * sum(weights[: index + 1]) / weight_total
        cues.append({
            "text": cue,
            "start": round(cursor, 3),
            "end": round(end, 3),
            "timing_basis": "estimated_voice_duration",
        })
        cursor = end
    return cues


def caption_image(
    path: Path, caption: str, width: int, height: int, style: str = "bottom_card"
):
    im = Image.new("RGBA", (width, height))
    if not caption:
        im.save(path)
        return

    font_path = Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")
    if not font_path.is_file():
        raise ValueError("한국어 자막 글꼴(AppleSDGothicNeo)이 없습니다.")

    d = ImageDraw.Draw(im)

    if style == "center_highlight":
        font_size = round(width * 0.052)
        font = ImageFont.truetype(str(font_path), font_size)
        lines = _wrap_text_lines(caption, d, font, width * 0.85)
        if len(lines) > 3:
            lines = lines[:3]
        line_height = round(width * 0.070)
        total_h = len(lines) * line_height
        base = (height - total_h) / 2
        d.rounded_rectangle(
            (width * 0.06, base - 20, width * 0.94, base + total_h + 16),
            18,
            fill=(10, 15, 28, 220),
            outline=(255, 215, 0, 180),
            width=2,
        )
        for i, text in enumerate(lines):
            d.text(
                (width / 2, base + i * line_height),
                text,
                font=font,
                fill=(255, 235, 120, 255),
                anchor="mt",
            )
    elif style == "title_card":
        font_size = round(width * 0.046)
        font = ImageFont.truetype(str(font_path), font_size)
        lines = _wrap_text_lines(caption, d, font, width * 0.82)
        if len(lines) > 2:
            lines = lines[:2]
        line_height = round(width * 0.065)
        total_h = len(lines) * line_height
        base = (height - total_h) / 2
        d.rounded_rectangle(
            (width * 0.08, base - 24, width * 0.92, base + total_h + 20),
            16,
            fill=(6, 10, 20, 235),
            outline=(100, 160, 255, 140),
            width=2,
        )
        for i, text in enumerate(lines):
            d.text(
                (width / 2, base + i * line_height),
                text,
                font=font,
                fill="white",
                anchor="mt",
            )
    elif style == "code_overlay":
        font_size = round(width * 0.028)
        font = ImageFont.truetype(str(font_path), font_size)
        lines = _wrap_text_lines(caption, d, font, width * 0.84)
        if len(lines) > 4:
            lines = lines[:4]
        line_height = round(width * 0.040)
        base = round(height * 0.10)
        d.rounded_rectangle(
            (width * 0.05, base - 10, width * 0.95, base + len(lines) * line_height + 12),
            12,
            fill=(15, 23, 42, 225),
            outline=(56, 189, 248, 120),
            width=1,
        )
        for i, text in enumerate(lines):
            d.text(
                (width * 0.08, base + i * line_height),
                text,
                font=font,
                fill=(226, 232, 240, 255),
                anchor="lt",
            )
    else:  # bottom_card
        font_size = round(width * 0.032)
        font = ImageFont.truetype(str(font_path), font_size)
        lines = _wrap_text_lines(caption, d, font, width * 0.84)
        if len(lines) > 4:
            raise ValueError("자막은 화면당 4줄 이하로 줄여 주세요.")
        line_height = round(width * 0.045)
        base = height - round(height * 0.09) - len(lines) * line_height
        d.rounded_rectangle(
            (
                width * 0.05,
                base - 14,
                width * 0.95,
                base + len(lines) * line_height + 12,
            ),
            14,
            fill=(8, 14, 25, 205),
        )
        for i, text in enumerate(lines):
            d.text(
                (width / 2, base + i * line_height),
                text,
                font=font,
                fill="white",
                anchor="mt",
            )
    im.save(path)


async def _command(
    args: list[str],
    *,
    cwd: Path,
    lock_fd: int,
    metrics: dict,
    progress=None,
    timeout=360,
):
    proc = await asyncio.create_subprocess_exec(
        ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(lock_fd,),
    )
    started = time.monotonic()
    reader = asyncio.create_task(proc.stderr.read())
    try:
        while proc.returncode is None:
            try:
                mem = psutil.Process(proc.pid).memory_info().rss
                metrics["peak_rss_bytes"] = max(metrics.get("peak_rss_bytes") or 0, mem)
            except (psutil.Error, OSError):
                metrics["memory_measurement"] = "unavailable in command environment"
            if time.monotonic() - started > timeout:
                raise TimeoutError("렌더 제한 시간을 초과했습니다.")
            if progress:
                progress(time.monotonic() - started)
            try:
                await asyncio.wait_for(proc.wait(), 0.3)
            except TimeoutError:
                pass
        error = (await reader).decode(errors="replace")
        if proc.returncode:
            raise ValueError("FFmpeg: " + error[-1800:])
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        if not reader.done():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


def update(studio, pid, jid, **values):
    with studio.transaction(pid) as data:
        target = next(j for j in data["render_jobs"] if j["id"] == jid)
        target.update(values)


def _stamp(t):
    ms = round(t * 1000)
    return (
        f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
    )


async def render(studio: Studio, job: dict, lock_fd: int):
    start = time.monotonic()
    p, jid = job["snapshot"], job["id"]
    pid = p["id"]
    folder = studio.path(pid)
    metrics = {"peak_rss_bytes": None}
    cached = 0
    update(studio, pid, jid, status="running", started_at=now(), error=None)
    try:
        width, height = dimensions(p["aspect"])
        assets = {a["id"]: a for a in p["assets"]}
        music = assets[p["music_asset_id"]]
        info = inspect_media(folder / music["file"])
        if not info["has_audio"] or info["duration"] + 0.1 < p["duration"]:
            raise ValueError(
                "음악이 영상보다 짧습니다. 장면 길이를 줄이거나 더 긴 음악을 선택해 주세요."
            )
        clips, srt, cursor = [], [], 0.0
        for i, scene in enumerate(p["scenes"]):
            asset = assets[scene["asset_id"]]
            key = digest(
                {
                    "scene": scene,
                    "asset": asset["sha256"],
                    "aspect": p["aspect"],
                    "renderer": 2,
                }
            )
            clip = folder / "cache" / f"{key}.mp4"
            duration = scene["duration"]
            if clip.is_file():
                cached += 1
            else:
                if asset["kind"] == "image":
                    _require_readable_image(folder / asset["file"], scene["id"])
                caption_segments = scene.get("caption_segments") or []
                captions = caption_segments or ([{"text": scene["caption"], "start": 0.0, "end": duration}] if scene["caption"] else [])
                subtitle_paths = []
                if not captions:
                    captions = [{"text": "", "start": 0.0, "end": duration}]
                for cue_index, cue in enumerate(captions):
                    subtitle = folder / "cache" / f"{key}-caption-{cue_index}.png"
                    caption_image(
                        subtitle,
                        cue["text"],
                        width,
                        height,
                        style=scene.get("caption_style", "bottom_card"),
                    )
                    subtitle_paths.append(subtitle)
                inputs = (
                    ["-loop", "1", "-framerate", str(FPS)]
                    if asset["kind"] == "image"
                    else ["-stream_loop", "-1"]
                )
                args = [
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    "image2,png_pipe,jpeg_pipe,webp_pipe,mov,matroska",
                    *inputs,
                    "-i",
                    str(folder / asset["file"]),
                ]
                for subtitle in subtitle_paths:
                    args.extend(["-loop", "1", "-framerate", str(FPS), "-i", str(subtitle)])
                frames = max(1, round(duration * FPS))
                base = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
                anim = scene.get("animation", "still")
                if anim != "still" and asset["kind"] == "image":
                    if anim == "zoom_in":
                        zoom = f"1+0.10*on/{frames}"
                        x = "(iw-iw/zoom)/2"
                        y = "(ih-ih/zoom)/2"
                    elif anim == "zoom_out":
                        zoom = f"1.10-0.10*on/{frames}"
                        x = "(iw-iw/zoom)/2"
                        y = "(ih-ih/zoom)/2"
                    elif anim == "pan":
                        zoom = "1.10"
                        x = f"(iw-iw/zoom)*on/{frames}"
                        y = "(ih-ih/zoom)/2"
                    elif anim == "pan_down":
                        zoom = "1.12"
                        x = "(iw-iw/zoom)/2"
                        y = f"(ih-ih/zoom)*on/{frames}"
                    elif anim == "pulse":
                        zoom = f"1+0.04*sin(2*PI*on/{round(FPS * 1.5)})"
                        x = "(iw-iw/zoom)/2"
                        y = "(ih-ih/zoom)/2"
                    else:
                        zoom = "1.0"
                        x = "(iw-iw/zoom)/2"
                        y = "(ih-ih/zoom)/2"
                    base += f",zoompan=z='{zoom}':x='{x}':y='{y}':d=1:s={width}x{height}:fps={FPS}"
                filters = f"[0:v]{base},fps={FPS},format=yuv420p[v]"
                current = "v"
                for cue_index, cue in enumerate(captions):
                    next_label = f"caption{cue_index}"
                    filters += (
                        f";[{current}][{cue_index + 1}:v]overlay=0:0:"
                        f"enable='between(t,{float(cue['start']):.3f},{float(cue['end']):.3f})'"
                        f"[{next_label}]"
                    )
                    current = next_label
                filters += f";[{current}]null"
                if scene["transition"] == "fade":
                    filters += f",fade=t=in:d=0.25,fade=t=out:st={max(0, duration - 0.25):.6f}:d=0.25"
                filters += "[out]"
                partial = clip.with_suffix(".partial.mp4")
                await _command(
                    [
                        *args,
                        "-filter_complex_threads",
                        "1",
                        "-filter_complex",
                        filters,
                        "-map",
                        "[out]",
                        "-an",
                        "-t",
                        str(duration),
                        "-r",
                        str(FPS),
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "21",
                        "-threads",
                        "2",
                        "-pix_fmt",
                        "yuv420p",
                        str(partial),
                    ],
                    cwd=folder,
                    lock_fd=lock_fd,
                    metrics=metrics,
                )
                partial.replace(clip)
            clips.append(clip)
            caption_segments = scene.get("caption_segments") or []
            captions = caption_segments or ([{"text": scene["caption"], "start": 0.0, "end": duration}] if scene["caption"] else [])
            for cue in captions:
                srt.append(
                    f"{len(srt) + 1}\n{_stamp(cursor + float(cue['start']))} --> "
                    f"{_stamp(cursor + float(cue['end']))}\n{cue['text']}\n"
                )
            cursor += duration
            update(
                studio,
                pid,
                jid,
                progress=round((i + 1) / len(p["scenes"]) * 85),
                cached_scenes=cached,
                **metrics,
            )
        concat = folder / "renders" / f"{jid}.concat"
        concat.write_text("\n".join(f"file '{clip}'" for clip in clips))
        output = f"renders/{jid}.mp4"
        partial = folder / "renders" / f"{jid}.partial.mp4"
        # Scene media is already encoded. Final mux copies video, encodes only audio.
        await _command(
            [
                "-protocol_whitelist",
                "file,pipe",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "wav,mp3,flac,ogg,mov",
                "-i",
                str(folder / music["file"]),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-t",
                str(cursor),
                "-movflags",
                "+faststart",
                str(partial),
            ],
            cwd=folder,
            lock_fd=lock_fd,
            metrics=metrics,
        )
        result_info = inspect_media(partial)
        if (
            not result_info["has_audio"]
            or not result_info["has_video"]
            or abs(result_info["duration"] - cursor) > 0.15
        ):
            raise ValueError("출력 길이 또는 영상·음악 트랙 검증 실패")
        partial.replace(folder / output)
        (folder / "renders" / f"{jid}.srt").write_text("\n".join(srt))
        (folder / "renders" / f"{jid}.timeline.json").write_text(
            json.dumps(p, ensure_ascii=False, indent=2)
        )
        update(
            studio,
            pid,
            jid,
            status="succeeded",
            progress=100,
            ended_at=now(),
            output=output,
            elapsed_seconds=round(time.monotonic() - start, 3),
            cached_scenes=cached,
            media=result_info,
            **metrics,
        )
    except (Exception, asyncio.CancelledError) as e:
        update(
            studio,
            pid,
            jid,
            status="failed",
            ended_at=now(),
            error=str(e) or "작업이 중단됐습니다. 재시도할 수 있습니다.",
            elapsed_seconds=round(time.monotonic() - start, 3),
            **metrics,
        )
        if isinstance(e, asyncio.CancelledError):
            raise


async def scan(studio: Studio | None = None):
    studio = studio or default_studio()
    with (studio.root / "renderer.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            for p in reversed(studio.list()):
                for job in p["render_jobs"]:
                    # Lock acquisition proves no renderer/child still owns this queue.
                    if job["status"] == "running":
                        update(
                            studio,
                            p["id"],
                            job["id"],
                            status="failed",
                            ended_at=now(),
                            error="이전 실행이 중단됐습니다. 재시도를 눌러 주세요.",
                        )
                    elif job["status"] == "queued":
                        await render(studio, job, handle.fileno())
                        return  # one bounded render per maintenance turn
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def legacy_loop():
    while True:
        try:
            await scan()
        except Exception:
            log.exception("creative renderer scan failed")
        await asyncio.sleep(2)
