#!/usr/bin/env python3
"""Radar fetcher: retrieve, score, dedupe and write data/latest.json.

    python fetch.py                    run every enabled source
    python fetch.py --only arxiv,hn    run a subset (others keep their stored items)
    python fetch.py --verify-feeds     discover and verify institutional feeds,
                                       rewriting the orgs block of config/feeds.yaml

Dependencies: feedparser and PyYAML. Everything else is the standard library.
No API keys. CONTACT_EMAIL is optional and unset by default; when present it is
sent to OpenAlex and Crossref to join their polite pools, and nowhere else.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from pathlib import Path

import feedparser
import yaml

ROOT = Path(__file__).resolve().parent
FEEDS_PATH = ROOT / "config" / "feeds.yaml"
RELEVANCE_PATH = ROOT / "config" / "relevance.yaml"
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
ARCHIVE_DIR = DATA_DIR / "archive"

USER_AGENT = (
    "Radar/1.0 (personal daily research feed; single-user, low volume; "
    "python-urllib)"
)
TIMEOUT = 30
TIERS = ("peer_reviewed", "preprint", "institutional", "news", "regulatory")
# Which copy wins when two records are the same work: a journal version
# outranks its preprint, and so on down.
TIER_RANK = {"peer_reviewed": 5, "regulatory": 4, "preprint": 3, "institutional": 2, "news": 1}
TODAY = dt.datetime.now(dt.timezone.utc).date()


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class HTTPFailure(Exception):
    pass


_last_hit: dict[str, float] = {}


def _throttle(host: str, min_interval: float) -> None:
    wait = _last_hit.get(host, 0.0) + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_hit[host] = time.monotonic()


def http(url: str, *, method: str = "GET", min_interval: float = 1.0,
         headers: dict | None = None, retries: int = 2, max_bytes: int = 20_000_000):
    """Return (status, headers, body bytes). Raises HTTPFailure on final failure."""
    host = urllib.parse.urlsplit(url).netloc
    hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    hdrs.update(headers or {})
    for attempt in range(retries + 1):
        _throttle(host, min_interval)
        req = urllib.request.Request(url, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read(max_bytes) if method != "HEAD" else b""
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    body = gzip.decompress(body)
                elif enc == "deflate":
                    body = zlib.decompress(body)
                return resp.status, resp.headers, body
        except urllib.error.HTTPError as e:
            retry_after = e.headers.get("Retry-After") if e.headers else None
            transient = e.code in (429, 500, 502, 503, 504)
            try:
                ra = float(retry_after) if retry_after else 0.0
            except ValueError:
                ra = 0.0
            if transient and attempt < retries and ra <= 30:
                time.sleep(max(ra, 3.0 * (attempt + 1)))
                continue
            detail = ""
            try:
                raw = e.read(65536)
                if (e.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                ctype = (e.headers.get("Content-Type") or "").lower()
                if "json" in ctype or "text/plain" in ctype:
                    detail = raw[:300].decode("utf-8", "replace").strip().replace("\n", " ")
            except Exception:
                pass
            raise HTTPFailure(f"HTTP {e.code} from {host}" + (f": {detail[:200]}" if detail else ""))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < retries:
                time.sleep(3.0 * (attempt + 1))
                continue
            raise HTTPFailure(f"{type(e).__name__} reaching {host}: {getattr(e, 'reason', e)}")
    raise HTTPFailure(f"gave up on {host}")


def get_json(url: str, headers: dict | None = None, **kw):
    _, _, body = http(url, headers={"Accept": "application/json", **(headers or {})}, **kw)
    return json.loads(body.decode("utf-8"))


def contact_email() -> str | None:
    v = os.environ.get("CONTACT_EMAIL", "").strip()
    return v or None


# --------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "br", "div", "li", "jats:p", "jats:sec"):
            self.parts.append(" ")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    p = _Stripper()
    try:
        p.feed(s)
        p.close()
        text = "".join(p.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def clip(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:.- ") + "…"


def norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[‐-―\-_/]", " ", s)
    return re.sub(r"\s+", " ", s)


def norm_title(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def to_date(value) -> str | None:
    """Accept struct_time, ISO strings, date parts lists; return YYYY-MM-DD."""
    if value is None:
        return None
    if isinstance(value, time.struct_time):
        return dt.date(value.tm_year, value.tm_mon, value.tm_mday).isoformat()
    if isinstance(value, (list, tuple)):
        parts = list(value) + [1, 1]
        try:
            return dt.date(int(parts[0]), int(parts[1] or 1), int(parts[2] or 1)).isoformat()
        except (TypeError, ValueError):
            return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


# --------------------------------------------------------------------------
# URLs and identity
# --------------------------------------------------------------------------

TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$|cmpid$|_hs)", re.I)


def canonical_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    m = re.match(r"https?://(?:export\.)?arxiv\.org/(?:abs|pdf)/([^?#]+?)(?:v\d+)?(?:\.pdf)?/?(?:[?#].*)?$", url)
    if m:
        return f"https://arxiv.org/abs/{m.group(1)}"
    m = re.match(r"https?://(?:dx\.)?doi\.org/(.+)$", url, re.I)
    if m:
        return "https://doi.org/" + urllib.parse.unquote(m.group(1)).lower()
    parts = urllib.parse.urlsplit(url)
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    netloc = parts.netloc.lower()
    if netloc.endswith(":443") or netloc.endswith(":80"):
        netloc = netloc.rsplit(":", 1)[0]
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
         if not TRACKING.match(k)])
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def url_key(url: str) -> str:
    """Dedupe key: canonical URL without scheme and www."""
    c = canonical_url(url)
    c = re.sub(r"^https?://", "", c)
    return re.sub(r"^www\.", "", c)


def item_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()


def host_of(url: str | None) -> str:
    return urllib.parse.urlsplit(url or "").netloc.lower()


def make_item(*, title, url, tier, source, venue, published=None, authors=None,
              abstract=None, pdf_url=None, html_url=None, oa=False, embeddable=None) -> dict | None:
    title = strip_html(title)
    if not title or not url:
        return None
    url = canonical_url(url)
    pub = to_date(published) or TODAY.isoformat()
    if pub > TODAY.isoformat():  # issue dates in the future: it is online now
        pub = TODAY.isoformat()
    return {
        "id": item_id(url),
        "title": title,
        "authors": [a for a in (authors or []) if a][:50],
        "venue": strip_html(venue) or source,
        "tier": tier,
        "published": pub,
        "retrieved": TODAY.isoformat(),
        "url": url,
        "pdf_url": pdf_url or None,
        "html_url": html_url or None,
        "embeddable": embeddable,  # None means "decide later"
        "oa": bool(oa),
        "abstract": (abstract or None),
        "score": 0.0,
        "matched": [],
        "source": source,
    }


# --------------------------------------------------------------------------
# Relevance
# --------------------------------------------------------------------------

class Relevance:
    def __init__(self, cfg: dict):
        self.floor = float(cfg.get("floor", 0))
        self.require_match = bool(cfg.get("require_match", True))
        self.tiers = {k: float(v) for k, v in (cfg.get("tiers") or {}).items()}
        self.keywords = []
        for kw in cfg.get("keywords") or []:
            term = str(kw["term"])
            phrases = [term] + [str(a) for a in kw.get("aliases") or []]
            cs = bool(kw.get("case_sensitive"))
            pats = []
            for ph in phrases:
                # Only the term itself is case-sensitive; spelled-out aliases are not.
                sensitive = cs and ph == term
                body = re.escape(ph if sensitive else norm_text(ph)).replace(r"\ ", r"\s+")
                pats.append((sensitive, re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])")))
            search = kw.get("search", True)
            search_terms = [str(s) for s in (kw.get("search_terms") or phrases)] if search else []
            self.keywords.append({
                "term": term, "weight": float(kw.get("weight", 1)), "patterns": pats,
                "search_terms": search_terms, "primary": ([str(kw.get("search_terms", [term])[0])] if search else []),
            })

    def search_phrases(self, aliases: bool = True) -> list[str]:
        out: list[str] = []
        for kw in self.keywords:
            for p in (kw["search_terms"] if aliases else kw["primary"]):
                if p not in out:
                    out.append(p)
        return out

    def score(self, item: dict) -> None:
        raw = f"{item.get('title') or ''} . {item.get('abstract') or ''}"
        low = norm_text(raw)
        raw_sp = re.sub(r"[‐-―\-_/]", " ", raw)
        matched, total = [], 0.0
        for kw in self.keywords:
            for sensitive, pat in kw["patterns"]:
                if pat.search(raw_sp if sensitive else low):
                    matched.append(kw["term"])
                    total += kw["weight"]
                    break
        item["matched"] = matched
        item["score"] = round(self.tiers.get(item["tier"], 0.0) + total, 2)

    def keep(self, item: dict) -> bool:
        if self.require_match and not item["matched"]:
            return False
        return item["score"] >= self.floor


def chunks(seq, n):
    n = max(1, int(n))
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def since(days: int) -> dt.date:
    return TODAY - dt.timedelta(days=days)


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def src_arxiv(cfg, rel: Relevance, window: int) -> list[dict]:
    cats = " OR ".join(f"cat:{c}" for c in cfg["categories"])
    phrases = rel.search_phrases()
    out = []
    cutoff = since(window).isoformat()
    for group in chunks(phrases, cfg.get("terms_per_query", 8)):
        terms = " OR ".join(f'all:"{p}"' if " " in p else f"all:{p}" for p in group)
        q = f"({cats}) AND ({terms})"
        url = cfg["endpoint"] + "?" + urllib.parse.urlencode({
            "search_query": q, "start": 0, "max_results": cfg.get("max_results", 100),
            "sortBy": "submittedDate", "sortOrder": "descending"})
        _, _, body = http(url, min_interval=cfg.get("min_interval", 3.0))
        feed = feedparser.parse(body)
        if feed.bozo and not feed.entries:
            raise HTTPFailure(f"arXiv returned unparseable feed: {feed.bozo_exception}")
        for e in feed.entries:
            abs_url = canonical_url(e.get("id", ""))
            m = re.search(r"arxiv\.org/abs/(.+)$", abs_url)
            if not m:
                continue
            aid = m.group(1)
            pub = to_date(e.get("published"))
            if pub and pub < cutoff:
                continue
            cat = (e.get("arxiv_primary_category") or {}).get("term") or (e.get("tags") or [{}])[0].get("term", "")
            doi = e.get("arxiv_doi")
            it = make_item(
                title=e.get("title", "").replace("\n", " "),
                url=abs_url, tier="preprint", source="arxiv",
                venue=f"arXiv {cat}".strip(), published=pub,
                authors=[a.get("name") for a in e.get("authors", [])],
                abstract=re.sub(r"\s+", " ", e.get("summary", "")).strip() or None,
                pdf_url=f"https://arxiv.org/pdf/{aid}", oa=True, embeddable=True)
            if it:
                if doi:
                    it["_doi"] = "https://doi.org/" + doi.lower()
                out.append(it)
    return out


def _openalex_abstract(inv: dict | None) -> str | None:
    if not inv:
        return None
    pos = []
    for word, idxs in inv.items():
        for i in idxs:
            pos.append((i, word))
    pos.sort()
    return " ".join(w for _, w in pos) or None


def src_openalex(cfg, rel: Relevance, window: int) -> list[dict]:
    out = []
    email = contact_email()
    phrases = rel.search_phrases()
    for group in chunks(phrases, cfg.get("terms_per_query", 12)):
        search = " OR ".join(f'"{p}"' for p in group)
        params = {
            "search": search,
            "filter": f"from_publication_date:{since(window).isoformat()}",
            "sort": "publication_date:desc",
            "per-page": cfg.get("per_page", 50),
            "select": "id,doi,display_name,publication_date,type,authorships,primary_location,"
                      "best_oa_location,open_access,abstract_inverted_index",
        }
        if email:
            params["mailto"] = email
        data = get_json(cfg["endpoint"] + "?" + urllib.parse.urlencode(params),
                        min_interval=cfg.get("min_interval", 1.0))
        for w in data.get("results", []):
            prim = w.get("primary_location") or {}
            src = prim.get("source") or {}
            best = w.get("best_oa_location") or {}
            is_oa = bool((w.get("open_access") or {}).get("is_oa")) and bool(best)
            landing = w.get("doi") or prim.get("landing_page_url") or w.get("id")
            stype = (src.get("type") or "").lower()
            tier = "preprint" if (w.get("type") == "preprint" or stype == "repository") else "peer_reviewed"
            if w.get("type") not in ("article", "preprint", "review", "report", "book-chapter", None):
                continue
            pdf = best.get("pdf_url") if is_oa else None
            it = make_item(
                title=w.get("display_name"), url=landing, tier=tier, source="openalex",
                venue=src.get("display_name") or ("Preprint" if tier == "preprint" else "Journal"),
                published=w.get("publication_date"),
                authors=[(a.get("author") or {}).get("display_name") for a in w.get("authorships", [])],
                abstract=_openalex_abstract(w.get("abstract_inverted_index")),
                pdf_url=pdf, oa=is_oa)
            if it:
                out.append(it)
    return out


def src_crossref(cfg, rel: Relevance, window: int) -> list[dict]:
    out = []
    email = contact_email()
    headers = {}
    if email:
        headers["User-Agent"] = f"{USER_AGENT} (mailto:{email})"
    for phrase in rel.search_phrases(aliases=False):
        params = {
            "query.bibliographic": phrase,
            "filter": f"from-pub-date:{since(window).isoformat()},until-pub-date:{TODAY.isoformat()},"
                      "type:journal-article,type:proceedings-article,type:posted-content,type:report",
            "sort": "relevance", "rows": cfg.get("rows", 20),
            "select": "DOI,title,author,container-title,publisher,group-title,published,issued,abstract,link,license,type,URL",
        }
        if email:
            params["mailto"] = email
        data = get_json(cfg["endpoint"] + "?" + urllib.parse.urlencode(params),
                        min_interval=cfg.get("min_interval", 1.0), headers=headers or None)
        for w in (data.get("message") or {}).get("items", []):
            title = " ".join(w.get("title") or [])
            if not title:
                continue
            tier = "preprint" if w.get("type") == "posted-content" else "peer_reviewed"
            licenses = " ".join(l.get("URL", "") for l in w.get("license") or [])
            oa = "creativecommons.org" in licenses
            pdf = None
            if oa:
                for l in w.get("link") or []:
                    if l.get("content-type") == "application/pdf":
                        pdf = l.get("URL")
                        break
            venue = " ".join(w.get("container-title") or []) or w.get("group-title") or \
                w.get("publisher") or ("Preprint" if tier == "preprint" else "Journal")
            date = ((w.get("published") or w.get("issued") or {}).get("date-parts") or [[None]])[0]
            authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name")
                       for a in w.get("author") or []]
            it = make_item(
                title=title, url="https://doi.org/" + w["DOI"], tier=tier, source="crossref",
                venue=venue, published=date, authors=authors,
                abstract=re.sub(r"^(abstract|summary)\b[:.\s]*", "", strip_html(w.get("abstract")), flags=re.I) or None,
                pdf_url=pdf, oa=oa)
            if it:
                out.append(it)
    return out


def src_federal_register(cfg, rel: Relevance, window: int) -> list[dict]:
    out = []
    phrases = rel.search_phrases()
    for group in chunks(phrases, cfg.get("terms_per_query", 10)):
        term = " | ".join(f'"{p}"' for p in group)
        params = [
            ("conditions[term]", term),
            ("conditions[publication_date][gte]", since(window).isoformat()),
            ("order", "newest"), ("per_page", cfg.get("per_page", 50)),
        ] + [("fields[]", f) for f in ("title", "abstract", "document_number", "html_url", "pdf_url",
                                         "publication_date", "agencies", "type")]
        data = get_json(cfg["endpoint"] + "?" + urllib.parse.urlencode(params),
                        min_interval=cfg.get("min_interval", 1.0))
        for d in data.get("results") or []:
            agencies = [a.get("name") for a in d.get("agencies") or [] if a.get("name")]
            venue = "Federal Register" + (f" · {agencies[0]}" if agencies else "")
            it = make_item(
                title=d.get("title"), url=d.get("html_url"), tier="regulatory",
                source="federal_register", venue=venue, published=d.get("publication_date"),
                authors=agencies, abstract=d.get("abstract"), pdf_url=d.get("pdf_url"),
                oa=True, embeddable=bool(d.get("pdf_url")))
            if it:
                out.append(it)
    return out


def src_google_news(cfg, rel: Relevance, window: int) -> list[dict]:
    out = []
    for q in cfg.get("queries") or []:
        url = cfg["endpoint"] + "?" + urllib.parse.urlencode({
            "q": f"{q} when:{cfg.get('window', '7d')}", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        _, _, body = http(url, min_interval=cfg.get("min_interval", 2.0))
        feed = feedparser.parse(body)
        for e in feed.entries:
            publisher = (e.get("source") or {}).get("title") or "Google News"
            title = e.get("title", "")
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(" - " + publisher)]
            snippet = strip_html(e.get("summary"))
            if snippet and (norm_title(snippet).startswith(norm_title(title)) or len(snippet) < 40):
                snippet = ""  # Google News "snippets" are usually just the title again
            it = make_item(
                title=title, url=e.get("link"), tier="news", source="google_news",
                venue=publisher, published=e.get("published_parsed"),
                abstract=clip(snippet, 300) or None, oa=False, embeddable=False)
            if it:
                out.append(it)
    return out


def src_hacker_news(cfg, rel: Relevance, window: int) -> list[dict]:
    out = []
    cutoff = int(time.mktime(since(min(window, 30)).timetuple()))
    for phrase in rel.search_phrases():
        params = {"query": f'"{phrase}"' if " " in phrase else phrase, "tags": "story",
                  "numericFilters": f"created_at_i>{cutoff}",
                  "hitsPerPage": cfg.get("hits_per_page", 30)}
        data = get_json(cfg["endpoint"] + "?" + urllib.parse.urlencode(params),
                        min_interval=cfg.get("min_interval", 0.5))
        for h in data.get("hits") or []:
            hn_url = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            link = h.get("url") or hn_url
            host = re.sub(r"^www\.", "", host_of(link))
            it = make_item(
                title=h.get("title"), url=link, tier="news", source="hacker_news",
                venue="Hacker News" + (f" · {host}" if h.get("url") else ""),
                published=h.get("created_at"),
                authors=[h.get("author")] if h.get("author") else [], oa=False, embeddable=False)
            if it:
                out.append(it)
    return out


def src_institutional(cfg, rel: Relevance, window: int) -> tuple[list[dict], list[dict]]:
    """Returns (items, errors): one failing org must not fail the others."""
    out, errors = [], []
    cutoff = since(window).isoformat()
    n = int(cfg.get("summary_chars", 500))
    for org in cfg.get("orgs") or []:
        if not org.get("verified") or not org.get("feed"):
            continue
        try:
            _, _, body = http(org["feed"], min_interval=cfg.get("min_interval", 1.0))
            feed = feedparser.parse(body)
            if not feed.entries:
                raise HTTPFailure("feed returned no entries")
        except Exception as e:
            errors.append({"source": f"institutional:{org['name']}", "error": str(e)[:300]})
            log(f"  ! {org['name']}: {e}")
            continue
        for e in feed.entries:
            pub = to_date(e.get("published_parsed") or e.get("updated_parsed"))
            if pub and pub < cutoff:
                continue
            pdf = None
            for enc in (e.get("enclosures") or []) + (e.get("links") or []):
                if (enc.get("type") or "").lower() == "application/pdf" or (enc.get("href") or "").lower().endswith(".pdf"):
                    pdf = enc.get("href")
                    break
            link = e.get("link")
            if link and link.lower().endswith(".pdf"):
                pdf = pdf or link
            authors = [a.get("name") for a in e.get("authors") or [] if a.get("name")]
            it = make_item(
                title=e.get("title"), url=link, tier="institutional", source="institutional",
                venue=org["name"], published=pub, authors=authors,
                abstract=clip(strip_html(e.get("summary")), n) or None,
                pdf_url=pdf, html_url=None if (link or "").lower().endswith(".pdf") else link,
                oa=True)
            if it:
                out.append(it)
    return out, errors


SOURCES = {
    "arxiv": ("arxiv", src_arxiv),
    "openalex": ("openalex", src_openalex),
    "crossref": ("crossref", src_crossref),
    "federal_register": ("federal_register", src_federal_register),
    "google_news": ("google_news", src_google_news),
    "hacker_news": ("hacker_news", src_hacker_news),
    "institutional": ("institutional", src_institutional),
}


# --------------------------------------------------------------------------
# Embeddability
# --------------------------------------------------------------------------

ALWAYS_EMBED = ("arxiv.org", "federalregister.gov", "govinfo.gov")
_frame_cache: dict[str, bool] = {}


def frame_allowed(url: str) -> bool:
    host = host_of(url)
    if host in _frame_cache:
        return _frame_cache[host]
    ok = False
    try:
        try:
            status, h, _ = http(url, method="HEAD", min_interval=0.5, retries=0)
        except HTTPFailure as e:
            if "HTTP 405" in str(e) or "HTTP 403" in str(e):
                status, h, _ = http(url, min_interval=0.5, retries=0, headers={"Range": "bytes=0-0"},
                                    max_bytes=1024)
            else:
                raise
        xfo = (h.get("X-Frame-Options") or "").strip()
        csp = (h.get("Content-Security-Policy") or "").lower()
        fa = re.search(r"frame-ancestors([^;]*)", csp)
        blocks_csp = bool(fa) and "*" not in fa.group(1).split()
        attach = "attachment" in (h.get("Content-Disposition") or "").lower()
        ok = status < 400 and not xfo and not blocks_csp and not attach
    except Exception as e:
        log(f"    HEAD {host}: {e}")
        ok = False
    _frame_cache[host] = ok
    return ok


def decide_embeddable(item: dict) -> None:
    if item.get("embeddable") is not None:
        return
    target = item.get("html_url") or item.get("pdf_url")
    if item["tier"] == "news" or not target:
        item["embeddable"] = False
        return
    host = host_of(target)
    if any(host == h or host.endswith("." + h) for h in ALWAYS_EMBED):
        item["embeddable"] = True
        return
    item["embeddable"] = frame_allowed(target)


# --------------------------------------------------------------------------
# Dedupe and merge
# --------------------------------------------------------------------------

def _better(a: dict, b: dict) -> tuple[dict, dict]:
    """Return (keeper, other)."""
    ra, rb = TIER_RANK.get(a["tier"], 0), TIER_RANK.get(b["tier"], 0)
    if ra != rb:
        return (a, b) if ra > rb else (b, a)
    fa = bool(a.get("abstract")) + bool(a.get("pdf_url"))
    fb = bool(b.get("abstract")) + bool(b.get("pdf_url"))
    return (a, b) if fa > fb else (b, a)  # ties go to the later (fresher) record


def combine(a: dict, b: dict) -> dict:
    keep, other = _better(a, b)
    keep = dict(keep)
    for f in ("abstract", "authors"):
        if not keep.get(f) and other.get(f):
            keep[f] = other[f]
    if not keep.get("pdf_url") and other.get("pdf_url"):
        # e.g. a paywalled journal record gains its arXiv preprint's open PDF
        keep["pdf_url"] = other["pdf_url"]
        keep["oa"] = keep.get("oa") or other.get("oa")
        if other.get("embeddable") and not keep.get("html_url"):
            keep["embeddable"] = True
    keep["retrieved"] = min(a.get("retrieved") or TODAY.isoformat(), b.get("retrieved") or TODAY.isoformat())
    return keep


def dedupe(items: list[dict]) -> list[dict]:
    by_url: dict[str, dict] = {}
    for it in items:
        keys = [url_key(it["url"])]
        if it.get("_doi"):
            keys.append(url_key(it["_doi"]))
        hit = next((k for k in keys if k in by_url), None)
        if hit:
            merged = combine(by_url[hit], it)
            for k in keys + [url_key(by_url[hit]["url"])]:
                by_url[k] = merged
        else:
            for k in keys:
                by_url[k] = it
    unique = list({id(v): v for v in by_url.values()}.values())
    by_title: dict[str, dict] = {}
    rest = []
    for it in unique:
        t = norm_title(it["title"])
        if len(t) < 24:
            rest.append(it)
            continue
        by_title[t] = combine(by_title[t], it) if t in by_title else it
    out = list(by_title.values()) + rest
    for it in out:
        it["id"] = item_id(it["url"])
    return out


def finalize(item: dict) -> dict:
    item = {k: v for k, v in item.items() if not k.startswith("_")}
    order = ["id", "title", "authors", "venue", "tier", "published", "retrieved", "url",
             "pdf_url", "html_url", "embeddable", "oa", "abstract", "score", "matched", "source"]
    item["embeddable"] = bool(item.get("embeddable"))
    return {k: item.get(k) for k in order}


def sort_items(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (i["published"], i["score"], i["title"]), reverse=True)


def payload(items: list[dict], errors: list[dict], generated_at: str) -> dict:
    counts = {t: 0 for t in TIERS}
    for it in items:
        counts[it["tier"]] = counts.get(it["tier"], 0) + 1
    return {"generated_at": generated_at, "errors": errors, "counts": counts, "items": items}


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------
# Feed discovery (--verify-feeds)
# --------------------------------------------------------------------------

class _LinkFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.feeds: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        typ = a.get("type", "").lower()
        if "alternate" in rel and typ in ("application/rss+xml", "application/atom+xml", "application/feed+json") \
                and a.get("href"):
            self.feeds.append(a["href"])


def _check_feed(url: str):
    """Return (ok, detail)."""
    try:
        status, h, body = http(url, min_interval=1.0, retries=1,
                               headers={"Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5"})
    except HTTPFailure as e:
        return False, str(e)
    feed = feedparser.parse(body)
    if not feed.entries:
        ctype = h.get("Content-Type", "")
        return False, f"no feed entries (Content-Type {ctype.split(';')[0] or 'unknown'})"
    dates = sorted(filter(None, (to_date(e.get("published_parsed") or e.get("updated_parsed")) for e in feed.entries)))
    newest = dates[-1] if dates else "undated"
    return True, f"{len(feed.entries)} entries, newest {newest}"


def verify_feeds(only_orgs: set[str] | None = None) -> int:
    cfg = yaml.safe_load(FEEDS_PATH.read_text())
    orgs = cfg["institutional"]["orgs"]
    results = []
    for org in orgs:
        if only_orgs and org["name"].lower() not in only_orgs:
            results.append(org)  # keep the earlier verification record as-is
            continue
        landing = org["landing"]
        log(f"- {org['name']}: {landing}")
        # Known feed paths a site does not advertise (e.g. Stripe's /blog/feed.rss).
        # They are still fetched and checked like every other candidate.
        candidates, landing_note = list(org.get("candidates") or []), ""
        final_url = landing
        try:
            status, h, body = http(landing, min_interval=1.0, retries=1,
                                   headers={"Accept": "text/html,application/xhtml+xml"})
            finder = _LinkFinder()
            finder.feed(body.decode("utf-8", "replace"))
            candidates += [urllib.parse.urljoin(landing, f) for f in finder.feeds]
            landing_note = f"landing page {status}"
        except HTTPFailure as e:
            landing_note = f"landing page failed ({e})"
        parts = urllib.parse.urlsplit(final_url)
        origin = f"{parts.scheme}://{parts.netloc}"
        base = landing if landing.endswith("/") else landing + "/"
        for path in ("/feed", "/rss", "/feed.xml", "/blog/rss.xml"):
            candidates.append(origin + path)
        for rel_path in ("feed", "rss", "feed.xml", "rss.xml"):
            candidates.append(urllib.parse.urljoin(base, rel_path))
        seen, tried, found = set(), [], None
        for c in candidates:
            if c in seen:
                continue
            seen.add(c)
            ok, detail = _check_feed(c)
            log(f"    {'OK ' if ok else 'no '} {c}  ({detail})")
            tried.append(f"{c} -> {detail}")
            if ok:
                found = (c, detail)
                break
        rec = {"name": org["name"], "landing": landing}
        if org.get("candidates"):
            rec["candidates"] = org["candidates"]
        if found:
            rec.update(feed=found[0], verified=True, verified_on=TODAY.isoformat(),
                       reason=f"verified: {found[1]}")
        else:
            rec.update(feed=None, verified=False, verified_on=TODAY.isoformat(),
                       reason=f"no working feed: {landing_note}; tried {len(seen)} candidate URLs, none returned entries")
        results.append(rec)

    text = FEEDS_PATH.read_text()
    head, _, _ = text.partition("  orgs:\n")
    lines = ["  orgs:"]
    for r in results:
        lines.append(f"    - name: {yaml.safe_dump(r['name'], default_style=None).strip().removesuffix('...').strip()}")
        for k in ("landing", "candidates", "feed", "verified", "verified_on", "reason"):
            if k not in r:
                continue
            v = r[k]
            dumped = yaml.safe_dump(v, width=10_000, default_flow_style=True).strip()
            dumped = dumped.removesuffix("...").strip()
            lines.append(f"      {k}: {dumped}")
    FEEDS_PATH.write_text(head + "\n".join(lines) + "\n")
    ok = sum(r["verified"] for r in results)
    log(f"\n{ok} of {len(results)} organizations have a verified feed; wrote {FEEDS_PATH.relative_to(ROOT)}")
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run(only: set[str] | None) -> int:
    feeds = yaml.safe_load(FEEDS_PATH.read_text())
    rel = Relevance(yaml.safe_load(RELEVANCE_PATH.read_text()))
    window = int(feeds.get("window_days", 90))
    generated_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    run_items: list[dict] = []
    errors: list[dict] = []
    for name, (cfg_key, fn) in SOURCES.items():
        cfg = feeds.get(cfg_key) or {}
        if only and name not in only:
            continue
        if cfg.get("enabled") is False:
            log(f"[{name}] disabled")
            continue
        t0 = time.monotonic()
        log(f"[{name}] fetching…")
        try:
            got = fn(cfg, rel, window)
            if isinstance(got, tuple):
                got, errs = got
                errors += errs
            run_items += got
            log(f"[{name}] {len(got)} raw items in {time.monotonic() - t0:.1f}s")
        except Exception as e:  # one source failing never fails the run
            msg = f"{type(e).__name__}: {e}"[:400]
            errors.append({"source": name, "error": msg})
            log(f"[{name}] FAILED, keeping previous items: {msg}")

    # Score and filter this run.
    for it in run_items:
        rel.score(it)
    fresh = [it for it in run_items if rel.keep(it)]
    fresh = dedupe(fresh)
    log(f"\nthis run: {len(run_items)} raw -> {len(fresh)} after relevance floor and dedupe")

    # Merge with the stored window.
    previous, prev_errors = [], []
    if LATEST_PATH.exists():
        try:
            prev_doc = json.loads(LATEST_PATH.read_text())
            previous = prev_doc.get("items", [])
            prev_errors = prev_doc.get("errors", [])
        except (json.JSONDecodeError, OSError) as e:
            log(f"could not read previous latest.json ({e}); starting fresh")
    prev_by_id = {p["id"]: p for p in previous}
    for it in fresh:
        old = prev_by_id.get(it["id"])
        if old:
            it["retrieved"] = old.get("retrieved") or it["retrieved"]
            if it.get("embeddable") is None and old.get("embeddable") is not None:
                it["embeddable"] = old["embeddable"]
    merged = dedupe(previous + fresh) if previous else fresh
    cutoff = since(window).isoformat()
    kept = []
    for it in merged:
        rel.score(it)  # tuning relevance.yaml applies retroactively
        if (it.get("published") or it.get("retrieved") or "") < cutoff:
            continue
        if not rel.keep(it):
            continue
        decide_embeddable(it)
        kept.append(finalize(it))
    kept = sort_items(kept)
    fresh_ids = {it["id"] for it in fresh}
    run_out = sort_items([it for it in kept if it["id"] in fresh_ids])

    archive_path = ARCHIVE_DIR / f"{TODAY.isoformat()}.json"
    if only:
        # A partial run: keep other sources' recorded errors, and add to today's
        # archive rather than replacing it.
        errors = [e for e in prev_errors if e["source"].split(":")[0] not in only] + errors
        if archive_path.exists():
            try:
                earlier = json.loads(archive_path.read_text()).get("items", [])
            except (json.JSONDecodeError, OSError):
                earlier = []
            ids = {i["id"] for i in run_out}
            run_out = sort_items(run_out + [i for i in earlier if i["id"] not in ids])
    write_json(LATEST_PATH, payload(kept, errors, generated_at))
    write_json(archive_path, payload(run_out, errors, generated_at))
    c = payload(kept, errors, generated_at)["counts"]
    log(f"latest.json: {len(kept)} items {c}; archive/{TODAY.isoformat()}.json: {len(run_out)} items")
    if errors:
        log(f"{len(errors)} source error(s) recorded in latest.json:")
        for e in errors:
            log(f"  - {e['source']}: {e['error'][:160]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify-feeds", action="store_true", help="discover and verify institutional feeds")
    ap.add_argument("--org", action="append", help="with --verify-feeds: only re-check this organization (repeatable)")
    ap.add_argument("--only", help=f"comma-separated subset of: {', '.join(SOURCES)}")
    args = ap.parse_args()
    if args.verify_feeds:
        return verify_feeds({o.lower() for o in args.org} if args.org else None)
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    if only and (bad := only - set(SOURCES)):
        ap.error(f"unknown source(s): {', '.join(sorted(bad))}")
    return run(only)


if __name__ == "__main__":
    sys.exit(main())
