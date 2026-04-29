#!/usr/bin/env python3
"""
sync_cms.py — incremental sync of the Reset CMS into Bases de dados/posts.jsonl.

Reads the canonical posts.jsonl, finds the most recent `modified` timestamp,
asks the WP REST API for everything changed after that watermark, and upserts
by id. New posts are appended, edited posts overwrite the old record.

Runs manually now. Will be scheduled via launchd at 10:45 AM (before
sync_linkedin.py at 11:00 AM) once validated.

Canonical file + log live in:
  /Users/sergioteixeira/Claude Reset/Projetos/Editorial/Bases de dados/

Silent on happy path. On network error: logs, exits non-zero so launchd retries.

Excludes conteudo-patrocinado and reset-in-english (same as pull_api_index.py).
"""
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error

BASE = "https://capitalreset.uol.com.br/wp-json/wp/v2"
PER_PAGE = 100
CONTENT_FIELDS = "id,date,modified,slug,link,title,excerpt,categories,tags,content"
EXCLUDE_SLUGS = {"conteudo-patrocinado", "reset-in-english"}

# Canonical data + log live next to this script (the repo root).
BASES = Path(__file__).resolve().parent
POSTS_FILE = BASES / "posts.jsonl"
TAX_CACHE = BASES / "cms_taxonomies.json"
LOG_FILE = BASES / "sync_cms_log.txt"


# ---------- helpers ----------

def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "reset-sync-cms/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()), dict(r.headers)


def strip_html(s):
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<script[\s\S]*?</script>", " ", s, flags=re.I)
    s = re.sub(r"<style[\s\S]*?</style>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- taxonomies ----------

def load_tax_cache():
    """Load the existing cache. Keys in JSON are strings -> normalize to int."""
    if not TAX_CACHE.exists():
        return {"categories": {}, "tags": {}}
    raw = json.loads(TAX_CACHE.read_text(encoding="utf-8"))
    return {
        "categories": {int(k): v for k, v in raw.get("categories", {}).items()},
        "tags": {int(k): v for k, v in raw.get("tags", {}).items()},
    }


def save_tax_cache(cache):
    # Sort keys for clean diffs
    serializable = {
        "categories": {str(k): v for k, v in sorted(cache["categories"].items())},
        "tags": {str(k): v for k, v in sorted(cache["tags"].items())},
    }
    TAX_CACHE.write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def resolve_single_taxonomy(kind, tid):
    """Fetch a single category or tag name by ID. Returns None on 404."""
    url = f"{BASE}/{kind}/{tid}?_fields=id,name"
    try:
        data, _ = http_get(url)
        return data.get("name")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def resolve_category_ids_for_exclusion():
    """Resolve slugs -> category IDs so we can filter the pull."""
    ids = set()
    for slug in EXCLUDE_SLUGS:
        url = f"{BASE}/categories?slug={slug}&_fields=id,slug"
        data, _ = http_get(url)
        if data:
            ids.add(data[0]["id"])
    return ids


def names_for_ids(ids, kind, cache):
    """
    Map a list of category/tag IDs to names using cache. Fetch any missing IDs.
    Mutates cache in place. Returns list of names.
    """
    out = []
    for tid in ids:
        if tid in cache[kind]:
            out.append(cache[kind][tid])
            continue
        name = resolve_single_taxonomy(kind, tid)
        if name is not None:
            cache[kind][tid] = name
            out.append(name)
            log(f"  [tax] new {kind[:-3]} cached: id={tid} name={name!r}")
        else:
            out.append(f"[UNKNOWN_{kind.upper()}_{tid}]")
            log(f"  [tax] WARN {kind} id={tid} not found (404)")
        time.sleep(0.1)
    return out


# ---------- canonical record ----------

def build_canonical(p, cache):
    title_raw = (p.get("title") or {}).get("rendered", "")
    content_raw = (p.get("content") or {}).get("rendered", "")
    cat_ids = p.get("categories", [])
    tag_ids = p.get("tags", [])
    cats = names_for_ids(cat_ids, "categories", cache)
    tags = names_for_ids(tag_ids, "tags", cache)
    return {
        "id": p["id"],
        "modified": p.get("modified"),
        "title": html.unescape(title_raw),
        "url": p.get("link"),
        "pub_date": p.get("date"),
        "categories": cats,
        "tags": tags,
        "content_text": strip_html(content_raw),
    }


# ---------- posts.jsonl I/O ----------

def load_posts():
    if not POSTS_FILE.exists():
        return []
    with POSTS_FILE.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_posts(records):
    records.sort(key=lambda r: (r.get("pub_date") or "", r.get("id") or 0))
    tmp = POSTS_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(POSTS_FILE)


# ---------- main ----------

def fetch_modified_since(watermark, exclude_cat_ids):
    """Paginate /posts?modified_after=<watermark>. Returns raw API records, filtered."""
    out = []
    page = 1
    while True:
        url = (
            f"{BASE}/posts"
            f"?modified_after={watermark}"
            f"&per_page={PER_PAGE}"
            f"&page={page}"
            f"&orderby=modified&order=asc"
            f"&_fields={CONTENT_FIELDS}"
        )
        try:
            batch, _ = http_get(url)
        except urllib.error.HTTPError as e:
            if e.code == 400:
                break  # past last page
            raise
        if not batch:
            break
        kept = [p for p in batch if not (set(p.get("categories", [])) & exclude_cat_ids)]
        out.extend(kept)
        log(f"  [api] page {page}: batch={len(batch)} kept={len(kept)} running={len(out)}")
        if len(batch) < PER_PAGE:
            break
        page += 1
        time.sleep(0.3)
    return out


def main():
    t0 = time.time()
    log("=" * 60)
    log("sync_cms.py START")

    try:
        records = load_posts()
        if not records:
            log("ERROR: posts.jsonl empty or missing — bootstrap required, aborting")
            sys.exit(1)

        watermark = max((r.get("modified") or "") for r in records)
        log(f"  loaded {len(records)} posts, watermark={watermark}")

        cache = load_tax_cache()
        log(f"  tax cache: cats={len(cache['categories'])} tags={len(cache['tags'])}")

        exclude_ids = resolve_category_ids_for_exclusion()
        log(f"  exclude category ids={sorted(exclude_ids)}")

        raw = fetch_modified_since(watermark, exclude_ids)
        log(f"  fetched {len(raw)} candidates from API (post-exclusion)")

        if not raw:
            log(f"  nothing to sync. elapsed={time.time()-t0:.1f}s")
            log("sync_cms.py DONE (no-op)")
            return

        by_id = {r["id"]: r for r in records}
        n_new = n_updated = 0
        for p in raw:
            rec = build_canonical(p, cache)
            if rec["id"] in by_id:
                by_id[rec["id"]] = rec
                n_updated += 1
            else:
                by_id[rec["id"]] = rec
                n_new += 1

        save_tax_cache(cache)
        write_posts(list(by_id.values()))

        log(f"  upsert: new={n_new} updated={n_updated} total_now={len(by_id)}")

        # Push canonical files to GitHub. Soft-fail.
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from git_push_helper import commit_and_push
            commit_and_push(
                ["posts.jsonl", "cms_taxonomies.json"],
                f"cms-sync {datetime.now().strftime('%Y-%m-%d')}: +{n_new} new, {n_updated} updated",
                log,
            )
        except Exception as e:
            log(f"  [git] helper failed: {type(e).__name__}: {e}")

        log(f"  elapsed={time.time()-t0:.1f}s")
        log("sync_cms.py DONE")

    except Exception as e:
        log(f"FATAL: {type(e).__name__}: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
