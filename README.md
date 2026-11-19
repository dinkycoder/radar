# Radar

A personal daily research feed on agentic commerce, agent payments, stablecoin infrastructure and AI-and-money. A GitHub Action fetches new papers, institutional research, regulation and news once a day and commits them as JSON. A single static page loads that JSON and does all filtering, searching and reading in the browser.

No database, no server, no build step, no API keys, no CDN. The page makes one network request, for `data/latest.json`.

```
index.html                     the entire UI (vanilla JS + CSS)
fetch.py                       retrieves, scores, dedupes, writes JSON
pull_library.py                downloads kept open-access PDFs to library/
config/feeds.yaml              sources, with feed verification results
config/relevance.yaml          keywords, weights, tier weights, floor
data/latest.json               rolling 90-day merged feed, loaded by the page
data/archive/YYYY-MM-DD.json   one file per run
library/                       gitignored; kept PDFs land here
.github/workflows/daily.yml    cron 11:00 UTC + manual dispatch, commits data/
```

## Run it locally

```bash
pip install feedparser PyYAML
```

```bash
python fetch.py
```

```bash
python -m http.server 8000
```

Then open http://localhost:8000. The page has to be served over HTTP, because browsers block `fetch()` from a `file://` page.

Useful flags:

```bash
python fetch.py --only arxiv,federal_register
```

```bash
python fetch.py --verify-feeds
```

```bash
python fetch.py --verify-feeds --org Stripe --org "a16z crypto"
```

`--only` runs a subset of sources. Items from the other sources stay in `latest.json`, and the run is added to today's archive file instead of replacing it. `--verify-feeds` re-runs discovery and rewrites the `orgs:` block of `config/feeds.yaml`, and `--org` limits that to the organizations named.

## Sources

| Source | What it supplies | Notes |
|---|---|---|
| arXiv API | preprints in cs.AI, cs.MA, cs.CR, cs.DC, econ.GN, q-fin.GN | One request every 3 s, as arXiv asks. |
| OpenAlex | journal articles and preprints, OA status via `best_oa_location` | See the OpenAlex caveat below. |
| Crossref | peer-reviewed items OpenAlex misses | Fuzzy matching, so the relevance floor does the filtering. A PDF link is kept only for Creative Commons-licensed works. |
| Federal Register API | rules, proposed rules, notices | Public domain. The reader frames the govinfo.gov PDF. |
| Institutional feeds | 6 of 14 organizations | See below. Summaries are trimmed to 500 characters, and article bodies are never stored. |
| Google News RSS | one query per topic in `feeds.yaml` | Title and publisher only. Links go through news.google.com. |
| Hacker News (Algolia) | stories matching each keyword | Title and link only. |

**Contact email.** OpenAlex and Crossref give faster service to clients that identify themselves. `fetch.py` reads `CONTACT_EMAIL` from the environment. It is unset by default, and every source works without it. To use it in Actions, add a repository variable (not a secret) named `CONTACT_EMAIL`. No address is written anywhere in this repository.

**OpenAlex caveat.** OpenAlex still answers without a key, but keyless requests now draw on a small free daily budget shared by every client on the same IP address. On shared runners or cloud IPs that budget can already be spent, and OpenAlex returns HTTP 429 until midnight UTC. When that happens, `fetch.py` records the failure in `errors`, keeps the previous items and finishes the run. OpenAlex offers free personal keys, but Radar does not use one: a key is something to rotate, which this project rules out. Crossref and arXiv cover most of the same ground.

### Institutional feeds: verified 2026-11-19

Each organization's research or blog page was fetched. The checker looked for `<link rel="alternate">` and tried `/feed`, `/rss`, `/feed.xml` and `/blog/rss.xml`, plus any `candidates:` listed for that org. A feed counts only if it actually returned entries. Only `verified: true` feeds are fetched at runtime.

| Organization | Result |
|---|---|
| Stripe | ✅ `stripe.com/blog/feed.rss` (not advertised on the page, so it is listed as a candidate) |
| a16z crypto | ✅ `a16zfrontier.com/feed.xml` (a16zcrypto.com now redirects to a16zfrontier.com) |
| Chainalysis | ✅ `www.chainalysis.com/feed` |
| TRM Labs | ✅ `www.trmlabs.com/resources/blog/rss.xml` |
| Keyrock | ✅ `keyrock.com/feed` |
| Bank for International Settlements | ✅ `www.bis.org/rss.xml` |
| BlackRock | ❌ no feed advertised and every standard path returns 404 |
| Visa | ❌ newsroom page is rendered by JavaScript, and no feed is advertised or found |
| Mastercard | ❌ newsroom returns HTTP 403 to automated clients (bot protection) |
| Coinbase | ❌ blog returns HTTP 403 to automated clients (bot protection) |
| Circle | ❌ no feed advertised and every standard path returns 404 |
| Paradigm | ❌ no feed advertised and every standard path returns 404 |
| Galaxy Research | ❌ no feed advertised and every standard path returns 404 |
| Bitcoin Policy Institute | ❌ Webflow site, with no feed advertised and every standard path returning 404 |

News about the eight gaps still arrives through Google News queries. If one of them launches a feed, add its URL under `candidates:` and run `python fetch.py --verify-feeds --org "Name"`.

**Left out because they need keys:** none of the requested sources needed one. OpenAlex is the only borderline case (see above). Semantic Scholar, NewsAPI and similar keyed sources were deliberately not added.

## Relevance

Everything lives in `config/relevance.yaml`:

```
score = tier weight + sum of weights of matched keywords
```

An item is dropped if its score is below `floor`, or if it matches no keyword at all (`require_match`). Each keyword can have `aliases`, `case_sensitive` (for AP2 and L402) and `search: false` (scores the keyword but never sends it as a query). The `matched` list on each card shows why the item surfaced. All stored items are rescored on every run, so tuning the file applies to the whole 90-day window the next day.

Default weights put the floor at 3.5. A single core term (x402, agentic commerce, ERC-8004) carries any item past the floor. A broad term such as "stablecoin" (2) carries papers, institutional posts and regulation, but not a news story on its own.

## Data contract

`data/latest.json` and each archive file have this shape. The UI depends only on this, so either half can be rebuilt without touching the other.

```json
{
  "generated_at": "2026-11-19T10:00:00Z",
  "errors": [{ "source": "openalex", "error": "HTTP 429 …" }],
  "counts": { "peer_reviewed": 4, "preprint": 11, "institutional": 3, "news": 22, "regulatory": 1 },
  "items": [{
    "id": "sha1 of the canonical url", "title": "…", "authors": ["…"],
    "venue": "arXiv cs.AI", "tier": "preprint", "published": "2026-11-18", "retrieved": "2026-11-19",
    "url": "https://arxiv.org/abs/…", "pdf_url": "https://arxiv.org/pdf/…", "html_url": null,
    "embeddable": true, "oa": true, "abstract": "…", "score": 7.5,
    "matched": ["agent payments", "x402"], "source": "arxiv"
  }]
}
```

- `retrieved` is the date Radar first saw the item. It is preserved across runs.
- Deduplication runs on canonical URL first (tracking parameters removed, arXiv versions dropped, DOIs lowercased; an arXiv record also matches its own DOI), then on normalised title. The highest tier wins, so a journal version beats its preprint, and it inherits the preprint's open PDF if it has none of its own.
- `embeddable` is true for arXiv and Federal Register/govinfo by host. Anything else with an `html_url` or `pdf_url` gets a `HEAD` request, cached per host. The item is embeddable only if that response has no `X-Frame-Options`, no restrictive `frame-ancestors` and no `Content-Disposition: attachment`. News is never embeddable.
- `errors` lists every source that failed in the latest run. A failing source never fails the run, and its earlier items stay.

## The page

- Cards are grouped by publication date, newest first. Each shows title, first three authors, a tier badge with the venue, date, score, matched keywords and an abstract clamped to three lines.
- Rail filters: tier, source, date range (with 7d/30d/90d presets), minimum score, keeps only, unread only. The search box does plain substring matching over title, authors and abstract; every word you type must appear.
- Keeps, read state, items already seen and filter settings live in `localStorage`. Items arriving since your last visit get a **NEW** badge. Kept items that age out of the 90-day window still appear under "Keeps only".
- Keyboard: `j`/`k` move, `Enter` read, `s` keep, `/` search, `Esc` closes the reader.
- **Read.** Embeddable items open in a resizable right-hand pane; PDFs use the browser's own viewer. Other items open in a new tab, and the pane says the source refuses embedding. If a frame hasn't loaded after 8 seconds, Radar falls back the same way.
- **Download** is a plain `<a download>` pointing at `pdf_url`, so the PDF comes straight from its origin. Browsers ignore the `download` attribute across origins, so many PDFs open in a new tab rather than saving directly. It is disabled when there is no `pdf_url`.
- **Copy citation** copies `Authors (Year). Title. Venue. URL`.
- **Export keeps** saves `keeps.json` for `pull_library.py`.

## Library on disk

```bash
python pull_library.py keeps.json
```

The script downloads only items with `oa: true` and a `pdf_url` into `library/`. That folder is gitignored, as is `keeps.json`. For every other item it prints the title and why it was skipped: not open access, no PDF link, or the server returned a login page instead of a PDF. Files already in `library/` are left alone, so re-running is cheap. It respects arXiv's one-request-per-3-seconds limit.

## Deployment

1. Push to GitHub.
2. Under **Settings → Pages**, publish from the default branch, `/ (root)`.
3. Under **Settings → Actions → General → Workflow permissions**, allow read and write so the workflow can commit `data/`.
4. The `daily` workflow runs at 11:00 UTC. Use **Actions → daily → Run workflow** to run it on demand. It runs `fetch.py`, commits `data/` only if something changed, and does nothing else.

**Private repository?** GitHub Pages on private repos is a paid feature. Use Cloudflare Pages instead: connect the repo, set the build command to none and the output directory to `/`. The workflow is unchanged, and Cloudflare redeploys on each data commit.

## Out of scope, on purpose

No accounts, server, database, LLM summaries, notifications, stored article text or full-text search over the library.
