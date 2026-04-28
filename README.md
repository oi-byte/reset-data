# Capital Reset — Canonical Data

Read-only canonical data shared across the team. Do not push to this repo manually.

## What's in here

- `posts.jsonl` — full CMS dump from capitalreset.com (one post per line, JSON).
- `voyager_dataset.json` — LinkedIn analytics dataset for the Capital Reset page.
- `linkedin-analytics-taxonomy.jsonl` — hook/topic taxonomy for LinkedIn posts.
- `cms_taxonomies.json` — category-ID → name lookup for `posts.jsonl`.

## How it's synced

Sérgio's Mac runs two daily launchd jobs:

- 10:45 — `sync_cms.py` updates `posts.jsonl` + `cms_taxonomies.json` and pushes.
- 11:00 — `sync_linkedin.py` updates `voyager_dataset.json` + the taxonomy and pushes (voyager file is diff-gated to avoid noise commits).

## For collaborators

Pull only. Do not commit or push from your machine. If data looks stale, ask Claude in Cowork to run `git pull` in this folder.
