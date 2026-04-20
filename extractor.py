import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    video_id: str
    title: str
    duration: int
    language: str
    source: str  # 'manual' | 'auto'
    text: str
    segments: list[Segment]


_TIMESTAMP = re.compile(r'(\d+):(\d+):(\d+)\.(\d+)\s-->\s(\d+):(\d+):(\d+)\.(\d+)')
_TAG = re.compile(r'<[^>]+>')


def extract_video_id(url: str) -> str:
    """Extract the 11-char YouTube video ID from a URL or return as-is if already an ID."""
    if re.fullmatch(r'[A-Za-z0-9_-]{11}', url):
        return url
    parsed = urlparse(url)
    if parsed.hostname in ('youtu.be',):
        vid = parsed.path.lstrip('/').split('/')[0]
        if vid:
            return vid
    if parsed.hostname and 'youtube' in parsed.hostname:
        qs = parse_qs(parsed.query)
        if 'v' in qs:
            return qs['v'][0]
        m = re.match(r'/(shorts|embed|live)/([A-Za-z0-9_-]{11})', parsed.path)
        if m:
            return m.group(2)
    raise ValueError(f"Could not extract YouTube video ID from: {url}")


def extract_via_supadata(url: str, languages: tuple[str, ...] = ('ru', 'en')) -> Transcript:
    """Fetch transcript via Supadata API. Requires SUPADATA_API_KEY env var."""
    from supadata import Supadata, SupadataError

    api_key = os.environ.get('SUPADATA_API_KEY')
    if not api_key:
        raise RuntimeError("SUPADATA_API_KEY is not set")

    video_id = extract_video_id(url)
    client = Supadata(api_key=api_key)

    # Supadata takes a single language preference. Walk our priority list and
    # stop at the first language that works; if all fail, try without lang (auto).
    last_err: Exception | None = None
    for lang in (*languages, None):
        try:
            resp = client.youtube.transcript(video_id=video_id, lang=lang) if lang \
                else client.youtube.transcript(video_id=video_id)
            break
        except SupadataError as e:
            last_err = e
            continue
    else:
        raise RuntimeError(f"Supadata failed for all languages {languages}: {last_err}")

    # Response shape: .content (str) + iterable chunks with .offset (ms), .duration (ms), .text
    raw_chunks = getattr(resp, 'content', None)
    segments: list[Segment] = []
    if isinstance(raw_chunks, list):
        for c in raw_chunks:
            offset_ms = getattr(c, 'offset', 0) or 0
            duration_ms = getattr(c, 'duration', 0) or 0
            text = (getattr(c, 'text', '') or '').strip()
            if text:
                segments.append(Segment(
                    start=offset_ms / 1000,
                    end=(offset_ms + duration_ms) / 1000,
                    text=text,
                ))
        text_joined = '\n'.join(s.text for s in segments)
    else:
        text_joined = (raw_chunks or '').strip()

    # Enrich with title. Try Supadata first; fall back to YouTube oEmbed
    # (public endpoint, no auth, rarely blocked — safer for a nice-to-have).
    title = ''
    duration = int(segments[-1].end) if segments else 0
    try:
        video = client.youtube.video(id=video_id)
        title = (getattr(video, 'title', '') or '').strip()
        duration = int(getattr(video, 'duration', 0) or duration)
    except Exception:
        pass
    if not title:
        try:
            import urllib.request
            oembed_url = f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json'
            with urllib.request.urlopen(oembed_url, timeout=10) as r:
                title = (json.loads(r.read()).get('title') or '').strip()
        except Exception:
            pass

    return Transcript(
        video_id=video_id,
        title=title,
        duration=duration,
        language=getattr(resp, 'lang', '?') or '?',
        source='supadata',
        text=text_joined,
        segments=segments,
    )


def _to_sec(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _parse_vtt(content: str) -> list[Segment]:
    """Parse VTT, deduplicating YouTube's rolling-caption duplicates."""
    segments: list[Segment] = []
    last_text: str | None = None
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        m = _TIMESTAMP.match(lines[i])
        if not m:
            i += 1
            continue
        start = _to_sec(*m.group(1, 2, 3, 4))
        end = _to_sec(*m.group(5, 6, 7, 8))
        i += 1
        parts: list[str] = []
        while i < len(lines) and lines[i].strip() and not _TIMESTAMP.match(lines[i]):
            cleaned = _TAG.sub('', lines[i]).strip()
            if cleaned:
                parts.append(cleaned)
            i += 1
        text = ' '.join(parts).strip()
        if text and text != last_text:
            segments.append(Segment(start, end, text))
            last_text = text
    return segments


def _pick_backend_order(backend: str, has_cookies: bool) -> list[str]:
    """Ordered list of backends to try for a given request.

    Rationale: yt-dlp is free but fragile (YouTube PO-token, 429). Supadata is
    paid but reliable. With cookies, yt-dlp almost always works → try free first.
    Without cookies, Supadata is the safer opener.
    """
    if backend == "supadata":
        return ["supadata"]
    if backend == "ytdlp":
        return ["ytdlp"]
    has_supadata = bool(os.environ.get("SUPADATA_API_KEY"))
    if has_cookies:
        return ["ytdlp"] + (["supadata"] if has_supadata else [])
    if has_supadata:
        return ["supadata", "ytdlp"]
    return ["ytdlp"]


def extract_with_fallback(
    url: str,
    languages: tuple[str, ...] = ('ru', 'en'),
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
    backend: str = "auto",
) -> tuple[Transcript, list[str]]:
    """Try backends in smart order, return first successful transcript.

    Returns (Transcript, path_taken) where path_taken is the ordered list of
    backends that were actually attempted (e.g. ['ytdlp', 'supadata'] means
    yt-dlp failed and Supadata rescued).
    """
    has_cookies = bool(cookies_file or cookies_from_browser)
    order = _pick_backend_order(backend, has_cookies)
    taken: list[str] = []
    last_error: Exception | None = None

    for b in order:
        taken.append(b)
        try:
            if b == "supadata":
                return extract_via_supadata(url, languages=languages), taken
            return extract_transcript(
                url, languages=languages,
                cookies_from_browser=cookies_from_browser,
                cookies_file=cookies_file,
            ), taken
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(
        f"All backends failed ({' → '.join(order)}). Last error: {last_error}"
    )


def extract_transcript(
    url: str,
    languages: tuple[str, ...] = ('ru', 'en'),
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
) -> Transcript:
    """Download captions (manual > auto) via yt-dlp and parse to Transcript.

    YouTube now requires a PO Token for most caption endpoints. The reliable
    bypass is to reuse cookies from a logged-in browser profile via
    cookies_from_browser='chrome' | 'firefox' | 'edge' (etc.).
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cmd = [
            sys.executable, '-m', 'yt_dlp',
            '--skip-download',
            '--write-subs',
            '--write-auto-subs',
            '--sub-langs', ','.join(languages),
            '--sub-format', 'vtt/best',
            '--convert-subs', 'vtt',
            '--impersonate', 'chrome',
            '--retries', '5',
            '--retry-sleep', 'http:exp=1:30',
            '--dump-json',
            '--no-warnings',
            '-o', str(tmp_path / '%(id)s.%(ext)s'),
        ]
        if cookies_file:
            cmd += ['--cookies', cookies_file]
        elif cookies_from_browser:
            cmd += ['--cookies-from-browser', cookies_from_browser]
        cmd.append(url)
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        if result.returncode != 0:
            raise RuntimeError(f"yt-dlp failed:\n{result.stderr.strip()}")

        json_lines = [l for l in result.stdout.strip().splitlines() if l.startswith('{')]
        if not json_lines:
            raise RuntimeError("yt-dlp returned no metadata")
        info = json.loads(json_lines[-1])

        video_id = info['id']
        title = info.get('title', '')
        duration = info.get('duration') or 0
        manual_langs = set(info.get('subtitles') or {})
        auto_langs = set(info.get('automatic_captions') or {})

        vtt_file = None
        chosen_lang = None
        chosen_source = None

        for lang in languages:
            path = tmp_path / f'{video_id}.{lang}.vtt'
            if path.exists() and lang in manual_langs:
                vtt_file, chosen_lang, chosen_source = path, lang, 'manual'
                break

        if not vtt_file:
            for lang in languages:
                path = tmp_path / f'{video_id}.{lang}.vtt'
                if path.exists():
                    vtt_file = path
                    chosen_lang = lang
                    chosen_source = 'auto' if lang in auto_langs else 'manual'
                    break

        if not vtt_file:
            available = sorted(manual_langs | auto_langs)
            raise RuntimeError(
                f"No subtitles found in {languages}. "
                f"Available languages: {available or 'none'}. "
                f"Fallback: download audio and transcribe with Whisper."
            )

        segments = _parse_vtt(vtt_file.read_text(encoding='utf-8'))
        text = '\n'.join(s.text for s in segments)
        return Transcript(
            video_id=video_id,
            title=title,
            duration=duration,
            language=chosen_lang or '?',
            source=chosen_source or '?',
            text=text,
            segments=segments,
        )
