"""Video-frame grabber for transcript screenshot-references.

No browser. Direct-URL frame seeking against googlevideo is throttled (ffmpeg
hangs on range requests), so instead yt-dlp downloads a small **video-only**
stream once (≤360p, ~15-60 MB), and ffmpeg then extracts every requested frame
from that local file — each cut is a ~0.1s local seek. For the "grab N frames
from one video" job this is both reliable and faster than per-frame fetching.

Reuses find_ffmpeg() from capture.py; yt-dlp is invoked as `python -m yt_dlp`
(same as capture.py), so it picks up the installed yt-dlp.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from capture import find_ffmpeg

# Prefer a small H.264/mp4 video-only stream over plain https (not HLS/DASH
# manifests) so the whole file downloads cleanly; fall back progressively.
_FORMAT = (
    "bestvideo[height<=360][vcodec^=avc1][protocol^=https]/"
    "bestvideo[height<=480][vcodec^=avc1][protocol^=https]/"
    "bestvideo[height<=480][protocol^=https]/"
    "worstvideo/worst"
)


# ─── Detection: which transcript moments deserve a screenshot ──────

_DETECT_SYSTEM = (
    "Ты анализируешь расшифровку видео. Найди моменты, где говорящий ссылается на"
    " то, что ПОКАЗАНО НА ЭКРАНE и что было бы полезным визуальным референсом для"
    " технического задания: терминал и вывод команд, интерфейс сервиса, экран"
    " настроек/конфига, дашборд, код в редакторе, схема/диаграмма, графики.\n\n"
    "НЕ отмечай: общие рассуждения, разговор без демонстрации экрана, вступления,"
    " эмоции. Отмечай только явные визуальные демонстрации.\n\n"
    "Для каждого такого момента верни номер сегмента (segment_index), краткую"
    " подпись caption (что показано, 3-6 слов) и reason (зачем это в ТЗ, 1 фраза)."
    " Если визуальных моментов нет — верни пустой список."
)

_DETECT_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment_index": {"type": "integer"},
                    "caption": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["segment_index", "caption", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["moments"],
    "additionalProperties": False,
}


def detect_reference_moments(segments: list, model: str | None = None) -> list[dict]:
    """LLM pass over transcript segments → list of visual-reference moments.

    `segments`: objects/dicts with `.index`/`.start`/`.text` (or the dict keys).
    Returns [{"segment_index", "caption", "reason"}]; empty if none. Pure w.r.t.
    frame-grabbing — this only reads text and returns which moments to shoot."""
    import anthropic
    import brief

    def _f(seg, attr):
        return seg.get(attr) if isinstance(seg, dict) else getattr(seg, attr)

    lines = []
    for seg in segments:
        idx, start, text = _f(seg, "index"), _f(seg, "start"), _f(seg, "text")
        text = (text or "").strip().replace("\n", " ")
        if text:
            lines.append(f"[{idx}] {float(start):.0f}s: {text}")
    if not lines:
        return []

    resolved = brief.resolve_model(model)
    client = anthropic.Anthropic()
    user = "Сегменты расшифровки (номер, время, текст):\n\n" + "\n".join(lines)
    import json as _json

    data = None
    # Retry once on empty/invalid output: a brand-new json_schema occasionally
    # comes back empty on its first-ever compile. A genuine "no moments" returns
    # a valid {"moments": []} and parses on the first try (no wasted retry).
    for _ in range(2):
        msg = client.messages.create(
            model=resolved,
            max_tokens=4000,
            system=[{"type": "text", "text": _DETECT_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": _DETECT_SCHEMA}},
        )
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            data = _json.loads(text)
            break
        except _json.JSONDecodeError:
            data = None
    if data is None:
        return []
    out = []
    for m in data.get("moments", []):
        if isinstance(m, dict) and "segment_index" in m:
            out.append({"segment_index": int(m["segment_index"]),
                        "caption": (m.get("caption") or "").strip(),
                        "reason": (m.get("reason") or "").strip(),
                        "model": resolved})
    return out


def download_video(youtube_url: str, dest_dir: Path, timeout: int = 300) -> Path:
    """Download a low-res video-only file for `youtube_url` into dest_dir.

    Returns the path to the downloaded file. Raises RuntimeError on failure
    (age/region-locked, deleted, or format unavailable)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(dest_dir / "src-%(id)s.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp", "-f", _FORMAT,
        "-o", out_tmpl, "--no-warnings", "--no-playlist", "--quiet",
        youtube_url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp download failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    hits = list(dest_dir.glob("src-*"))
    if not hits:
        raise RuntimeError("yt-dlp produced no file")
    return hits[0]


def grab_frame(video_file: Path, ts: float, out_path: Path, timeout: int = 30) -> bool:
    """Extract a single JPEG at timestamp `ts` (seconds) from a local video file.
    Returns True on success. Local input-seek is near-instant."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_ffmpeg()
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, ts):.3f}", "-i", str(video_file),
        "-frames:v", "1", "-q:v", "2", str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[screenshot] ffmpeg timeout at ts={ts}", file=sys.stderr)
        return False
    if proc.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        print(f"[screenshot] ffmpeg failed at ts={ts}: {proc.stderr.strip()[:200]}",
              file=sys.stderr)
        return False
    return True


def capture_frames(youtube_url: str, moments: list[dict], video_id: str) -> list[dict]:
    """Download the video once, then extract one frame per moment.

    `moments`: list of {"ts": float, ...arbitrary metadata}. Returns the same
    dicts, each augmented with "file_path" (project-relative, e.g.
    images/shot-<id>-<ts>.jpg) for frames grabbed successfully; failed grabs are
    dropped. Frames are written under images/ (same dir as NewsImage files); the
    temporary source video is always deleted."""
    with tempfile.TemporaryDirectory() as tmp:
        src = download_video(youtube_url, Path(tmp))
        out = []
        for m in moments:
            ts = float(m["ts"])
            rel = f"images/shot-{video_id}-{int(round(ts))}.jpg"
            if grab_frame(src, ts, Path(rel)):
                out.append({**m, "file_path": rel})
        return out
