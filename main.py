import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from brief import generate_brief, resolve_model
from extractor import Segment, Transcript, extract_video_id, extract_with_fallback
from store import find_matching_brief, get_video, log_run, save_brief, save_transcript

# Windows consoles default to cp1251/cp866 which mangles Cyrillic + emoji.
for s in (sys.stdout, sys.stderr):
    if hasattr(s, 'reconfigure'):
        s.reconfigure(encoding='utf-8')


def _safe_name(s: str, limit: int = 80) -> str:
    cleaned = ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in s)
    return cleaned.strip().replace(' ', '_')[:limit] or 'video'


def _transcript_from_db(video) -> Transcript:
    """Reconstruct a Transcript dataclass from a Prisma Video row with segments."""
    segments = [Segment(start=s.start, end=s.end, text=s.text) for s in video.segments]
    return Transcript(
        video_id=video.id,
        title=video.title or '',
        duration=video.duration or 0,
        language=video.language or '?',
        source=video.source,
        text='\n'.join(s.text for s in segments),
        segments=segments,
    )


def process_url(
    url: str,
    languages: tuple[str, ...],
    output_dir: Path,
    *,
    no_brief: bool = False,
    brief_lang: str = 'ru',
    fmt: str = 'markdown',
    model: str | None = None,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
    backend: str = 'auto',
    triggered_by: str = 'cli',
    force_refresh: bool = False,
) -> dict:
    """Run the full pipeline for a single URL. Returns a summary dict.

    Cache behaviour (force_refresh=False):
      * Transcript: if Video exists in DB with segments, reuse (no Supadata/yt-dlp call).
      * Brief: if a Brief with matching (video_id, resolved model, language, format) exists,
        return it from DB (no Claude call, cost = 0).

    force_refresh=True re-extracts the transcript and regenerates the brief, overwriting
    state in DB.

    Persists: transcript files on disk, Video+Segment+Brief+Run rows in SQLite.
    """
    started = datetime.now()
    video_id_for_log: str | None = None

    # ─── 1. Try transcript from cache ──────────────────────────────
    transcript: Transcript | None = None
    transcript_from_cache = False
    if not force_refresh:
        try:
            vid = extract_video_id(url)
            cached_video = get_video(vid, with_segments=True)
            if cached_video and cached_video.segments:
                transcript = _transcript_from_db(cached_video)
                transcript_from_cache = True
                print(
                    f"✓ Transcript from DB cache: {transcript.language}, "
                    f"{len(transcript.segments)} segments",
                    file=sys.stderr,
                )
        except ValueError:
            pass  # URL parse failed — fall through to fresh extraction

    # ─── 2. Fresh extraction if no cache ───────────────────────────
    backends_tried: list[str] = []
    if transcript is None:
        print(f"→ Extracting ({backend}): {url}", file=sys.stderr)
        try:
            transcript, backends_tried = extract_with_fallback(
                url, languages=languages,
                cookies_from_browser=cookies_from_browser,
                cookies_file=cookies_file,
                backend=backend,
            )
        except Exception as e:
            log_run(url, None, 'error', f"extract: {e}", triggered_by, started)
            raise
        path_str = backends_tried[-1] if len(backends_tried) == 1 \
                   else f"{' → '.join(backends_tried[:-1])} → {backends_tried[-1]} (fallback)"
        print(
            f"✓ via {path_str}: {transcript.language} ({transcript.source}), "
            f"{len(transcript.segments)} segments, {transcript.duration}s",
            file=sys.stderr,
        )
        save_transcript(transcript)

    video_id_for_log = transcript.video_id

    # ─── 3. Write files on disk (always — cheap and keeps .txt/.json fresh) ──
    output_dir.mkdir(parents=True, exist_ok=True)
    base = output_dir / f'{transcript.video_id}_{_safe_name(transcript.title)}'
    base.with_suffix('.txt').write_text(transcript.text, encoding='utf-8')
    base.with_suffix('.json').write_text(
        json.dumps({
            'video_id': transcript.video_id, 'title': transcript.title,
            'duration': transcript.duration, 'language': transcript.language,
            'source': transcript.source,
            'segments': [{'start': s.start, 'end': s.end, 'text': s.text} for s in transcript.segments],
        }, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    if not transcript_from_cache:
        print(f"✓ Transcript saved: {base.name}.txt / .json", file=sys.stderr)

    result = {
        'url': url, 'video_id': transcript.video_id, 'title': transcript.title,
        'duration': transcript.duration, 'transcript_path': str(base.with_suffix('.txt')),
        'brief': None, 'brief_id': None, 'cost_usd': None,
        'transcript_from_cache': transcript_from_cache,
        'backends_tried': backends_tried,
        'from_cache': False,
    }

    if no_brief:
        log_run(url, video_id_for_log, 'ok', None, triggered_by, started)
        return result
    if not os.getenv('ANTHROPIC_API_KEY'):
        print("⚠ ANTHROPIC_API_KEY not set — skipping brief.", file=sys.stderr)
        log_run(url, video_id_for_log, 'ok', 'no anthropic key', triggered_by, started)
        return result

    # ─── 4. Try brief from cache ───────────────────────────────────
    resolved_model = resolve_model(model)
    if not force_refresh:
        existing = find_matching_brief(transcript.video_id, resolved_model, brief_lang, fmt)
        if existing:
            print(
                f"✓ Brief from DB cache (#{existing.id}, "
                f"{existing.model}, {fmt}/{brief_lang}) — 0 tokens spent",
                file=sys.stderr,
            )
            result.update({
                'brief': existing.contentMd,
                'brief_id': existing.id,
                'cost_usd': 0.0,
                'from_cache': True,
                'model': existing.model,
            })
            log_run(url, video_id_for_log, 'ok', 'cached brief', triggered_by, started)
            return result

    # ─── 5. Generate fresh brief ───────────────────────────────────
    print("→ Generating brief via Claude...", file=sys.stderr)
    try:
        brief = generate_brief(transcript, language=brief_lang, fmt=fmt, model=model)
    except Exception as e:
        log_run(url, video_id_for_log, 'error', f"brief: {e}", triggered_by, started)
        raise

    brief_id, cost = save_brief(
        transcript.video_id, brief['model'], brief_lang, fmt,
        brief['content_md'], brief['content_json'], brief['usage'],
    )

    suffix = '_brief.json' if fmt == 'json' else '_brief.md'
    brief_path = base.with_name(base.name + suffix)
    content_to_write = brief['content_json'] if fmt == 'json' else brief['content_md']
    brief_path.write_text(content_to_write, encoding='utf-8')
    if fmt == 'json':
        # also dump readable md alongside
        base.with_name(base.name + '_brief.md').write_text(brief['content_md'], encoding='utf-8')

    u = brief['usage']
    print(
        f"✓ Brief #{brief_id} saved: {brief_path.name}\n"
        f"  model={brief['model']}  format={fmt}\n"
        f"  tokens: in={u['input_tokens']} out={u['output_tokens']} "
        f"cache_read={u['cache_read_input_tokens']} cache_write={u['cache_creation_input_tokens']}\n"
        f"  cost≈${cost:.4f}" if cost is not None else "",
        file=sys.stderr,
    )

    result.update({'brief': brief['content_md'], 'brief_id': brief_id, 'cost_usd': cost})
    log_run(url, video_id_for_log, 'ok', None, triggered_by, started)
    return result


def main() -> int:
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Extract YouTube transcript and generate a brief.")
    parser.add_argument('url', help='YouTube video URL')
    parser.add_argument('--lang', default='ru,en',
                        help='Subtitle languages in priority order (default: ru,en)')
    parser.add_argument('--output-dir', default='output')
    parser.add_argument('--no-brief', action='store_true')
    parser.add_argument('--brief-lang', default='ru', choices=['ru', 'en'])
    parser.add_argument('--format', dest='fmt', default='markdown', choices=['markdown', 'json'],
                        help='Brief output format (markdown=prose, json=structured+schema-validated)')
    parser.add_argument('--model', default=None,
                        help='Claude model. Aliases: opus|sonnet|haiku, or full ID like claude-opus-4-7. '
                             'Default: env CLAUDE_MODEL, else claude-sonnet-4-6.')
    parser.add_argument('--cookies-from-browser', default=None,
                        help='Read cookies from browser (chrome, firefox, edge) for yt-dlp backend')
    parser.add_argument('--cookies', default=None,
                        help='Path to cookies.txt for yt-dlp (auto-detects ./cookies.txt)')
    parser.add_argument('--backend', default='auto', choices=['auto', 'supadata', 'ytdlp'])
    parser.add_argument('--force-refresh', action='store_true',
                        help='Ignore DB cache — re-extract transcript and regenerate brief '
                             'even if matching entries exist.')
    args = parser.parse_args()

    languages = tuple(l.strip() for l in args.lang.split(',') if l.strip())
    cookies_file = args.cookies
    if not cookies_file and not args.cookies_from_browser:
        auto = Path(__file__).parent / 'cookies.txt'
        if auto.exists():
            cookies_file = str(auto)
            print(f"→ Using cookies from {auto.name}", file=sys.stderr)

    try:
        result = process_url(
            args.url, languages, Path(args.output_dir),
            no_brief=args.no_brief, brief_lang=args.brief_lang,
            fmt=args.fmt, model=args.model,
            cookies_from_browser=args.cookies_from_browser,
            cookies_file=cookies_file, backend=args.backend,
            force_refresh=args.force_refresh,
        )
    except Exception as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    if result['brief']:
        print()
        print(result['brief'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
