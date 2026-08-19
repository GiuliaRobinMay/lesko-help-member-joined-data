# Lesko Help — member cohort tracker

A small self-updating pipeline that answers one question every day:

> **How many members were added to the Lesko Help community each day — and how
> quickly do they actually enter it?**

Members are added by an automation, but they still have to sign in for the
first time themselves. Mighty Networks shows this as an empty *last visit*.
This repository takes a snapshot of the member list **every morning**, works
out per daily cohort who has entered, and rebuilds the overview page:

* **`docs/index.html`** — the overview: one row per join-day (last 70 days),
  the number of members added, and cumulative week 1 → week 10 columns showing
  how many of them had entered the community by then.
* **`docs/data.json`** — the same numbers as data, for anything downstream.

## How it works

```
every morning (05:30 UTC)
  GitHub Action  ──►  Mighty Networks member API
        │                    (join date + last visit per member)
        ▼
  data/snapshots/YYYY-MM-DD.csv        one file per day
        ▼
  scripts/build_overview.py            cohort math
        ▼
  docs/index.html + docs/data.json     the always-current overview
```

Mighty Networks only stores each member's **last** visit, not their first.
The daily snapshots are what make the week columns truthful: the day a
member's *last visit* first appears pins down when they entered. Members who
had already entered before the very first snapshot are attributed to the week
of their last visit at that moment (marked ≈ on the page — an upper bound;
their entered/not-entered status is exact either way).

## The "entered" signal — LIVE since 2026-08-19

History, for whoever reads this later:

* **18 Aug 2026** — first live snapshot. The Admin API member object had
  `created_at` (join date) but **no last-visit field**, so the page showed
  joins only, with dashes for the entered columns.
* **19 Aug 2026** — the morning snapshot suddenly carried a last-visit value
  for 97% of members: Mighty added the field to the API (the day after we
  asked about it). The pre-configured candidate field names picked it up
  automatically, and the entered/week columns lit up with no code change.
  Cross-check: 636 members with no last visit ≈ the 637 "never activated"
  the BTB warehouse counts independently.

The 19 Aug snapshot is the **signal baseline**: members already entered by
then are attributed to the week of their last visit at that moment — an
upper bound, marked ≈ on the page when it could cross a week boundary (the
true first visit can only be earlier). From 19 Aug onward, entries are
pinned day-by-day by the snapshots and the week columns are exact.

The `updated_at` experiment (day 1: 2.5% daily movement, activity-shaped
gradient by join recency) is closed as no longer needed — the column is
still captured, and remains available as a secondary activity signal.

## The one manual step: the API token

Mighty's API program was enabled for lesko-help-2 in July 2026. The Admin API
needs a Network on the Scale, Growth, or Mighty Pro plan, and (per Mighty's
quick-start) "an API access token from your Network's admin panel". To switch
the pipeline on:

1. Create the API access token in the Mighty Networks admin panel — per
   Mighty's authentication guide: log in → **Admin** → **Settings** →
   **API Keys** → **Generate New API Key**. Name it (e.g. "cohort tracker"),
   and copy the token right away — Mighty shows it only once. (If the API Keys
   section is missing, ask your Mighty contact — they enabled the API program
   for this Network in July 2026.)
2. In this repository: **Settings → Secrets and variables → Actions →
   New repository secret**, name it **`MIGHTY_API_TOKEN`**, paste the token.
3. Run the **daily-snapshot** workflow once by hand (Actions tab → daily-snapshot
   → Run workflow) — or simply wait for the next morning.

The first run with a token performs *API discovery*: it probes the known
endpoints and commits a sanitized report to `data/api_discovery/report.json`
(no token, no e-mail addresses). The exact member query is then pinned in
`scripts/api_config.json`, and every run after that takes real snapshots.

## What is stored here (privacy)

Snapshots contain **only** the numeric member id, join date, last-visit date,
and the welcome-checklist flag. **No names and no e-mail addresses, ever** —
the overview needs only counts. If this repository is ever made public-facing
beyond the team, consider switching it to private anyway (Settings → General →
Change visibility).

## Workflows

| Workflow | When | What it does |
|---|---|---|
| `daily-snapshot` | every morning + manual | fetch snapshot → rebuild overview → commit |
| `probe-api-docs` | manual only | fetches Mighty's public API docs into `docs/api-reference/` (setup aid) |

The connection details live in `scripts/api_config.json` (Mighty **Admin API**,
`https://api.mn.co/admin`, Bearer token). One daily snapshot of ~30k members at
100 per page is roughly 300 API requests per day (~9k/month) — check what your
Mighty plan includes so the volume is no surprise.

To publish the overview at a URL, enable GitHub Pages: **Settings → Pages →
Deploy from a branch → `main` / `docs`**. The page also opens fine straight
from the repository.
