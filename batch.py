"""Batch processor: run the pipeline over a list of URLs from a file.

File format: one URL per line. Blank lines and lines starting with # are ignored.

Usage:
    python batch.py urls.txt
    python batch.py urls.txt --format json --model opus --brief-lang en
"""
import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

from main import process_url

for s in (sys.stdout, sys.stderr):
    if hasattr(s, 'reconfigure'):
        s.reconfigure(encoding='utf-8')


def read_urls(path: Path) -> list[str]:
    urls: list[str] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            urls.append(line)
    return urls


def main() -> int:
    load_dotenv(override=True)

    parser = argparse.ArgumentParser(description="Batch-process YouTube URLs from a file.")
    parser.add_argument('urls_file', type=Path, help='Path to a newline-separated URL list')
    parser.add_argument('--lang', default='ru,en')
    parser.add_argument('--output-dir', default='output')
    parser.add_argument('--brief-lang', default='ru', choices=['ru', 'en'])
    parser.add_argument('--format', dest='fmt', default='markdown', choices=['markdown', 'json'])
    parser.add_argument('--model', default=None)
    parser.add_argument('--no-brief', action='store_true')
    parser.add_argument('--backend', default='auto', choices=['auto', 'supadata', 'ytdlp'])
    parser.add_argument('--stop-on-error', action='store_true',
                        help='Exit with non-zero on the first failure (default: continue)')
    args = parser.parse_args()

    if not args.urls_file.exists():
        print(f"✗ File not found: {args.urls_file}", file=sys.stderr)
        return 1

    urls = read_urls(args.urls_file)
    if not urls:
        print(f"✗ No URLs in {args.urls_file}", file=sys.stderr)
        return 1

    print(f"→ Batch run: {len(urls)} URL(s) from {args.urls_file.name}", file=sys.stderr)
    languages = tuple(l.strip() for l in args.lang.split(',') if l.strip())
    ok = err = 0
    total_cost = 0.0

    for i, url in enumerate(urls, 1):
        print(f"\n── [{i}/{len(urls)}] {url} ──", file=sys.stderr)
        try:
            result = process_url(
                url, languages, Path(args.output_dir),
                no_brief=args.no_brief, brief_lang=args.brief_lang,
                fmt=args.fmt, model=args.model, backend=args.backend,
                triggered_by='batch',
            )
            ok += 1
            if result.get('cost_usd'):
                total_cost += result['cost_usd']
        except Exception as e:
            err += 1
            print(f"✗ {type(e).__name__}: {e}", file=sys.stderr)
            if args.stop_on_error:
                break

    print(
        f"\n── done: ok={ok} err={err} "
        f"total_cost≈${total_cost:.3f} ──",
        file=sys.stderr,
    )
    return 0 if err == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
