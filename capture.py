"""HLS live-stream capture via yt-dlp + ffmpeg.

Architecture:
  1. Ask yt-dlp for the direct stream URL (`yt-dlp -g -f bestaudio`).
  2. Pipe that into ffmpeg with `-t <duration>` → single MP3 chunk.
  3. Repeat in a loop; each iteration produces one Chunk row in DB.

Why per-chunk stream URL lookup: YouTube live URLs can rotate or expire mid-stream.
Requesting fresh URL for each chunk is cheap (~1s) and robust.
"""
import asyncio
import glob
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def find_ffmpeg() -> str:
    """Locate ffmpeg.exe: PATH first, then winget install locations."""
    if p := shutil.which("ffmpeg"):
        return p
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    for pattern in [
        r"Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-*\bin\ffmpeg.exe",
        r"Microsoft\WinGet\Packages\Gyan.FFmpeg*\bin\ffmpeg.exe",
    ]:
        hits = glob.glob(os.path.join(local_app_data, pattern))
        if hits:
            return hits[0]
    raise RuntimeError(
        "ffmpeg not found. Install: winget install Gyan.FFmpeg, then restart shell."
    )


async def _get_stream_audio_url(youtube_url: str) -> str:
    """Ask yt-dlp for the current live-stream audio URL."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-g", "-f", "bestaudio/best",
        "--no-warnings", "--quiet",
        youtube_url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp -g failed: {err.decode(errors='replace').strip()}")
    url = out.decode().strip().splitlines()[0]
    if not url:
        raise RuntimeError("yt-dlp returned empty stream URL")
    return url


async def capture_chunk(
    youtube_url: str,
    output_path: Path,
    duration_sec: int,
    stop_event: asyncio.Event | None = None,
) -> tuple[bool, float]:
    """Capture audio chunk. Returns (ok, actual_duration_sec).

    If stop_event gets set during capture, we SIGTERM ffmpeg and let it flush
    the partial MP3 cleanly — resulting file keeps the seconds it did record.
    16 kHz mono MP3, Whisper-friendly.
    """
    ffmpeg = find_ffmpeg()
    try:
        stream_url = await _get_stream_audio_url(youtube_url)
    except Exception as e:
        print(f"[capture] stream URL error: {e}", file=sys.stderr)
        return False, 0.0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-y",
        "-hide_banner", "-loglevel", "warning",
        "-i", stream_url,
        "-t", str(duration_sec),
        "-vn",                        # no video
        "-ar", "16000", "-ac", "1",   # 16kHz mono — Whisper-friendly
        "-c:a", "libmp3lame", "-b:a", "64k",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    wait_task = asyncio.create_task(proc.wait())
    tasks = [wait_task]
    stop_task = None
    if stop_event is not None:
        stop_task = asyncio.create_task(stop_event.wait())
        tasks.append(stop_task)

    import time as _time
    started = _time.monotonic()
    terminated_early = False
    try:
        done, _ = await asyncio.wait(
            tasks, timeout=duration_sec + 60,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task and stop_task in done:
            # Stop requested while ffmpeg still running. SIGTERM it so it can
            # finalize the MP3 headers instead of leaving a corrupt file.
            terminated_early = True
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
    finally:
        if stop_task and not stop_task.done():
            stop_task.cancel()
        if not wait_task.done():
            wait_task.cancel()

    elapsed = _time.monotonic() - started

    # Allow partial files on early-term. Minimum: 8 seconds of audio at 64 kbps ≈ 64 KB.
    # Below that, not worth keeping (silence, corrupt header, etc).
    MIN_BYTES = 64 * 1024
    if not output_path.exists():
        print(f"[capture] no output file: {output_path}", file=sys.stderr)
        return False, 0.0
    size = output_path.stat().st_size
    if size < MIN_BYTES:
        try: output_path.unlink()
        except OSError: pass
        print(f"[capture] output too small ({size} B): {output_path}", file=sys.stderr)
        return False, 0.0

    if terminated_early:
        print(f"[capture] partial chunk saved ({size // 1024} KB, ~{elapsed:.0f}s)", file=sys.stderr)
        return True, elapsed
    if proc.returncode not in (0, None):
        err = (await proc.stderr.read()).decode(errors="replace")
        print(f"[capture] ffmpeg exit {proc.returncode} but file ok ({size // 1024} KB): {err[:120]}", file=sys.stderr)
    return True, float(duration_sec)


async def capture_loop(
    stream_id: str,
    youtube_url: str,
    chunk_dir: Path,
    interval_min: int,
    stop_event: asyncio.Event,
    on_chunk_ready,
) -> None:
    """Run until stop_event is set, producing one chunk every interval_min minutes.

    NB: store.* are sync wrappers that internally call `asyncio.run()`.
    Calling them directly from this async context would raise RuntimeError —
    hence `asyncio.to_thread(...)` around every DB call.
    """
    from store import next_chunk_index, save_chunk
    duration_sec = interval_min * 60
    try:
        index = await asyncio.to_thread(next_chunk_index, stream_id)
    except Exception as e:
        print(f"[capture:{stream_id}] failed to init index: {e}", file=sys.stderr)
        return
    consecutive_failures = 0
    print(f"[capture:{stream_id}] loop started, interval={interval_min}min, starting at index {index}", file=sys.stderr)

    while not stop_event.is_set():
        started_at = datetime.now()
        audio_path = chunk_dir / f"{stream_id}_{index:05d}.mp3"
        try:
            ok, actual_duration = await capture_chunk(
                youtube_url, audio_path, duration_sec, stop_event=stop_event,
            )
        except Exception as e:
            print(f"[capture:{stream_id}] capture_chunk raised: {type(e).__name__}: {e}", file=sys.stderr)
            ok, actual_duration = False, 0.0

        if ok:
            consecutive_failures = 0
            try:
                chunk = await asyncio.to_thread(
                    save_chunk,
                    stream_id, index, started_at, actual_duration, str(audio_path),
                )
            except Exception as e:
                print(f"[capture:{stream_id}] save_chunk failed: {e}", file=sys.stderr)
                index += 1
                continue
            print(
                f"[capture:{stream_id}] chunk {index} ok "
                f"({audio_path.stat().st_size // 1024} KB, {actual_duration:.0f}s)",
                file=sys.stderr,
            )
            try:
                await on_chunk_ready(chunk)
            except Exception as e:
                print(f"[capture] on_chunk_ready failed: {e}", file=sys.stderr)
            index += 1
        else:
            consecutive_failures += 1
            # Exponential backoff, capped at 60s, then give up after 5 consecutive failures
            if consecutive_failures >= 5:
                print(f"[capture:{stream_id}] 5 consecutive failures — stopping", file=sys.stderr)
                break
            wait = min(5 * (2 ** (consecutive_failures - 1)), 60)
            print(f"[capture:{stream_id}] failed, retry in {wait}s", file=sys.stderr)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass  # timeout = continue loop
