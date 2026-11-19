#!/usr/bin/env python3
"""Download the PDFs of kept, open-access items into library/.

    python pull_library.py keeps.json

keeps.json is what Radar's "Export keeps" button saves. Only items with
oa: true and a pdf_url are downloaded; every other item is skipped with its
title and the reason. Files already in library/ are left alone, so the
script is safe to re-run. library/ is gitignored: the archive stays personal
and the repository stays clean. Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIBRARY = ROOT / "library"
USER_AGENT = "Radar/1.0 (personal research library; downloads open-access PDFs a reader kept)"
# Seconds between requests to the same host; arXiv asks for one request every 3s.
MIN_INTERVAL = {"arxiv.org": 3.0, "export.arxiv.org": 3.0}
DEFAULT_INTERVAL = 1.0
MAX_BYTES = 200 * 1024 * 1024

_last: dict[str, float] = {}


def throttle(host: str) -> None:
    gap = MIN_INTERVAL.get(host, DEFAULT_INTERVAL)
    wait = _last.get(host, 0.0) + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.monotonic()


def slug(title: str, n: int = 70) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", title or "untitled").strip("-").lower()
    return (s[:n].rstrip("-") or "untitled")


def load_keeps(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    items = doc.get("items", []) if isinstance(doc, dict) else doc
    if not isinstance(items, list):
        raise ValueError("expected a list of items or an object with an 'items' list")
    return [i for i in items if isinstance(i, dict)]


def download(url: str, dest: Path) -> str | None:
    """Fetch url into dest. Return None on success, or the reason it failed."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    throttle(host)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*;q=0.5"})
    tmp = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            first = resp.read(5)
            if first != b"%PDF-":
                ctype = (resp.headers.get("Content-Type") or "unknown").split(";")[0]
                return f"server returned {ctype}, not a PDF (often a login or cookie wall)"
            size = len(first)
            with open(tmp, "wb") as fh:
                fh.write(first)
                while chunk := resp.read(1 << 16):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError(f"larger than {MAX_BYTES // 2**20} MB")
                    fh.write(chunk)
        tmp.replace(dest)
        return None
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} from {host}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return f"{type(e).__name__}: {getattr(e, 'reason', e)}"
    finally:
        if tmp.exists():
            tmp.unlink()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        items = load_keeps(Path(argv[1]))
    except (OSError, ValueError) as e:
        print(f"cannot read {argv[1]}: {e}", file=sys.stderr)
        return 2

    LIBRARY.mkdir(exist_ok=True)
    got = skipped = present = failed = 0
    for it in items:
        title = (it.get("title") or "(untitled)").strip()
        if not it.get("oa"):
            print(f"SKIP  {title}\n      reason: not open access (oa is false), so it is not downloaded")
            skipped += 1
            continue
        pdf = it.get("pdf_url")
        if not pdf:
            print(f"SKIP  {title}\n      reason: open access but the source supplies no PDF link (pdf_url is null)")
            skipped += 1
            continue
        dest = LIBRARY / f"{(it.get('published') or '')[:10]}-{slug(title)}-{(it.get('id') or '')[:8]}.pdf".lstrip("-")
        if dest.exists():
            print(f"HAVE  {title}\n      {dest.relative_to(ROOT)}")
            present += 1
            continue
        reason = download(pdf, dest)
        if reason:
            print(f"FAIL  {title}\n      reason: {reason} ({pdf})")
            failed += 1
        else:
            print(f"GOT   {title}\n      {dest.relative_to(ROOT)} ({dest.stat().st_size // 1024} KB)")
            got += 1

    print(f"\n{got} downloaded, {present} already in library/, {skipped} skipped, {failed} failed "
          f"(of {len(items)} kept)")
    return 1 if failed and not got and not present else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
