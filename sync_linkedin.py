#!/usr/bin/env python3
"""
LinkedIn Analytics — Daily Sync Script
Pulls latest post analytics from LinkedIn via Unipile/Voyager and upserts
into voyager_dataset.json. New posts are appended; existing posts have their
numbers refreshed. Runs silently — errors go to sync_log.txt.

Usage:
  python3 sync_linkedin.py

Credentials are read from environment variables:
  UNIPILE_API_KEY, UNIPILE_ACCOUNT_ID
On GitHub Actions, these come from repository Secrets.
"""

import json
import os
import time
import re
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────

UNIPILE_BASE        = "https://api25.unipile.com:15518"
UNIPILE_API_KEY     = os.environ["UNIPILE_API_KEY"]
UNIPILE_ACCOUNT_ID  = os.environ["UNIPILE_ACCOUNT_ID"]

VOYAGER_FEED_URL_TEMPLATE = (
    "https://www.linkedin.com/voyager/api/graphql"
    "?includeWebMetadata=true"
    "&variables=(organizationalPageFeedUseCase:ADMIN_ORGANIZATIONAL_PAGE_POSTS,"
    "organizationalPageIdOrUniversalName:(organizationalPageUUId:42165711),"
    "start:{start},count:{count})"
    "&queryId=voyagerFeedDashOrganizationalPageAdminUpdates"
    ".96fdd4f5900fb8a434c2a3286b1952c2"
)

FEED_PAGE_SIZE     = 25
DELAY              = 1.0    # seconds between API calls
MAX_PAGES          = 60     # safety ceiling (~1500 posts)

SCRIPT_DIR         = Path(__file__).resolve().parent
BASES_DIR          = SCRIPT_DIR
DATASET_FILE       = BASES_DIR / "voyager_dataset.json"
LOG_FILE           = SCRIPT_DIR / "sync_log.txt"


# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────

def log(msg, also_print=False):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    if also_print:
        print(line)


# ──────────────────────────────────────────────
# UNIPILE MAGIC ROUTE
# ──────────────────────────────────────────────

def voyager_get(voyager_url):
    payload = json.dumps({
        "account_id": UNIPILE_ACCOUNT_ID,
        "request_url": voyager_url
    }).encode("utf-8")

    req = Request(f"{UNIPILE_BASE}/api/v1/linkedin", data=payload, method="POST")
    req.add_header("X-API-KEY", UNIPILE_API_KEY)
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode() if e.fp else ""
        return {"error": f"HTTP {e.code}", "body": body[:300]}
    except (URLError, TimeoutError) as e:
        return {"error": "network", "message": str(e)}


# ──────────────────────────────────────────────
# PARSE FEED RESPONSE
# ──────────────────────────────────────────────

def extract_posts(response):
    """Parse a Voyager feed page and return a list of post dicts."""
    posts = []

    # Navigate Unipile wrapper: { data: { data: { feedDash...: { elements } } } }
    inner = response
    for _ in range(2):
        if "data" in inner and isinstance(inner["data"], dict):
            inner = inner["data"]

    feed_key = next((k for k in inner if k.startswith("feedDash")), None)
    if not feed_key:
        return posts

    for elem in inner[feed_key].get("elements", []):
        post = {}

        # IDs
        meta        = elem.get("update", {}).get("metadata", {})
        backend_urn = meta.get("backendUrn", "") if isinstance(meta.get("backendUrn"), str) else ""
        share_urn   = meta.get("shareUrn", "")   if isinstance(meta.get("shareUrn"), str)   else ""

        activity_match = re.search(r"activity:(\d+)", backend_urn)
        share_match    = re.search(r"share:(\d+)",    share_urn)

        if not activity_match:
            permalink = elem.get("permalink") or ""
            if isinstance(permalink, str):
                activity_match = re.search(r"activity:(\d+)", permalink)

        if not activity_match and not share_match:
            continue

        post["activity_id"] = activity_match.group(1) if activity_match else None
        post["share_id"]    = share_match.group(1)    if share_match    else None
        post["permalink"]   = elem.get("permalink")   if isinstance(elem.get("permalink"), str) else ""

        # Body / commentary (full post text — required for URL-direct matching)
        commentary = elem.get("update", {}).get("commentary", {})
        text_obj   = commentary.get("text", {}) if isinstance(commentary, dict) else {}
        body       = text_obj.get("text", "")   if isinstance(text_obj, dict)   else ""
        post["content"] = body if isinstance(body, str) else ""

        # Published timestamp
        if elem.get("publishedAt"):
            post["publishedAt"] = elem["publishedAt"]

        # Organic analytics
        oa = elem.get("organicAnalytics")
        if isinstance(oa, dict):
            for field in ["impressions", "clicks", "reactions", "engagements",
                          "engagementRate", "clickThroughRate", "shares",
                          "videoViews", "articleViews", "comments"]:
                post[field] = oa.get(field)

        # Social counts
        social = (elem.get("update", {})
                      .get("socialDetail", {})
                      .get("totalSocialActivityCounts", {}))
        if isinstance(social, dict):
            post["numLikes"]    = social.get("numLikes")
            post["numComments"] = social.get("numComments")
            raw_rtc = social.get("reactionTypeCounts", [])
            if isinstance(raw_rtc, list):
                post["reactionTypeCounts"] = [
                    {"type": r.get("reactionType"), "count": r.get("count")}
                    for r in raw_rtc if isinstance(r, dict)
                ]

        posts.append(post)

    return posts


# ──────────────────────────────────────────────
# UPSERT INTO DATASET
# ──────────────────────────────────────────────

ANALYTICS_FIELDS = [
    "impressions", "clicks", "reactions", "engagements",
    "engagementRate", "clickThroughRate", "shares",
    "videoViews", "articleViews", "comments",
    "numLikes", "numComments", "reactionTypeCounts", "permalink"
]

def upsert(dataset, fresh_posts):
    """
    Merge fresh_posts into dataset.
    - Existing posts (matched by share_id or activity_id): analytics fields refreshed.
    - New posts: appended with minimal fields; editorial fields left empty for manual tagging.
    Returns (updated_count, added_count, new_share_ids).
    """
    # Build lookup indices
    by_share    = {p["share_id"]:    i for i, p in enumerate(dataset) if p.get("share_id")}
    by_activity = {p["activity_id"]: i for i, p in enumerate(dataset) if p.get("activity_id")}

    updated  = 0
    added    = 0
    new_ids  = set()

    for fp in fresh_posts:
        sid = fp.get("share_id")
        aid = fp.get("activity_id")

        idx = by_share.get(sid) if sid else None
        if idx is None and aid:
            idx = by_activity.get(aid)

        if idx is not None:
            # Refresh analytics on existing post
            for field in ANALYTICS_FIELDS:
                if fp.get(field) is not None:
                    dataset[idx][field] = fp[field]
            # Backfill content if it was previously empty and we now have a body
            if not dataset[idx].get("content") and fp.get("content"):
                dataset[idx]["content"] = fp["content"]
            updated += 1
        else:
            # New post — append with available data, leave editorial fields empty
            new_post = {
                "share_id":           sid,
                "activity_id":        aid,
                "publishedAt":        fp.get("publishedAt"),
                "permalink":          fp.get("permalink", ""),
                "content":            fp.get("content", ""),
                "impressions":        fp.get("impressions"),
                "clicks":             fp.get("clicks"),
                "reactions":          fp.get("reactions"),
                "engagements":        fp.get("engagements"),
                "engagementRate":     fp.get("engagementRate"),
                "clickThroughRate":   fp.get("clickThroughRate"),
                "shares":             fp.get("shares"),
                "videoViews":         fp.get("videoViews"),
                "articleViews":       fp.get("articleViews"),
                "numLikes":           fp.get("numLikes"),
                "numComments":        fp.get("numComments"),
                "reactionTypeCounts": fp.get("reactionTypeCounts", []),
                "reactorProfiles":    [],
                "totalReactors":      0,
                "matched_title":      None,
                "matched_url":        None,
                "categories":         [],
                "tags":               [],
                "match_method":       None,
                "flag":               None,
                "comments":           None,
                "totalComments":      0,
            }
            dataset.append(new_post)
            by_share[sid]    = len(dataset) - 1
            by_activity[aid] = len(dataset) - 1
            if sid:
                new_ids.add(sid)
            added += 1

    return updated, added, new_ids


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    start_time = datetime.now(timezone.utc)
    log(f"Sync started", also_print=False)

    # Load existing dataset
    if not DATASET_FILE.exists():
        log(f"ERROR: dataset not found at {DATASET_FILE}", also_print=True)
        sys.exit(1)

    with open(DATASET_FILE, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    log(f"Dataset loaded: {len(dataset)} posts")

    # Pull all pages from Voyager feed
    all_fresh = []
    start     = 0
    page      = 0
    errors    = 0
    consecutive_empty = 0

    while page < MAX_PAGES:
        url = VOYAGER_FEED_URL_TEMPLATE.format(start=start, count=FEED_PAGE_SIZE)
        response = voyager_get(url)

        if "error" in response:
            errors += 1
            log(f"API error on page {page+1}: {response}")
            if errors >= 3:
                log("ERROR: 3 consecutive API errors — aborting sync", also_print=True)
                sys.exit(1)
            time.sleep(DELAY * 3)
            continue

        errors = 0
        page_posts = extract_posts(response)

        if len(page_posts) == 0:
            consecutive_empty += 1
            if consecutive_empty >= 2:
                break  # reached end of feed
        else:
            consecutive_empty = 0
            all_fresh.extend(page_posts)

        start += FEED_PAGE_SIZE
        page  += 1
        time.sleep(DELAY)

    log(f"Feed pulled: {len(all_fresh)} posts across {page} pages")

    if not all_fresh:
        log("WARNING: No posts returned from feed — dataset unchanged")
        sys.exit(0)

    # Upsert into dataset
    updated, added, new_ids = upsert(dataset, all_fresh)
    log(f"Upsert complete: {updated} refreshed, {added} new")

    # Match new posts against the CMS canonical (posts.jsonl). Failures don't kill the sync.
    if new_ids:
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            from match_posts_to_cms import load_posts_jsonl, match_all, POSTS_FILE as MATCHER_POSTS_FILE
            by_slug = load_posts_jsonl(MATCHER_POSTS_FILE)
            stats = match_all(dataset, by_slug, only_ids=new_ids)
            log(
                f"Matcher: examined={stats['examined']} "
                f"url={stats['matched_url']} no_link={stats['no_link']} "
                f"already_matched_skipped={stats['already_matched_skipped']}"
            )
        except Exception as e:
            log(f"WARNING: matcher failed ({type(e).__name__}: {e}) — sync continues")
    else:
        log("Matcher: no new posts to examine — skipped")

    # Save updated dataset (after upsert + matching, single write)
    with open(DATASET_FILE, "w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)

    # Push to GitHub. voyager_dataset.json is diff-gated (skip if only churn).
    # Taxonomy file gets pushed unconditionally — small + stable.
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        from git_push_helper import commit_and_push, diff_gate_voyager

        files_to_push = ["linkedin-analytics-taxonomy.jsonl"]
        if diff_gate_voyager(log):
            files_to_push.append("voyager_dataset.json")

        commit_and_push(
            files_to_push,
            f"linkedin-sync {datetime.now(timezone.utc).strftime('%Y-%m-%d')}: "
            f"{updated} refreshed, {added} new",
            log,
        )
    except Exception as e:
        log(f"  [git] helper failed: {type(e).__name__}: {e}")

    elapsed = (datetime.now(timezone.utc) - start_time).seconds
    log(f"Sync complete in {elapsed}s — dataset now {len(dataset)} posts")


if __name__ == "__main__":
    main()
