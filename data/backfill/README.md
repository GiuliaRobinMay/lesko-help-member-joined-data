# Provided history (backfill)

The daily snapshots only see the community from **18 Aug 2026** onward. If you
have older numbers (for example from Mighty's own dashboard or another
system), drop them here and the Churn and Analytics tabs pick them up on the
next build — chained backward from today's real member count, so the totals
always stay anchored to reality.

## Format

Create **`data/backfill/monthly.csv`** with exactly these columns:

```csv
month,joined,left
2025-01,412,367
2025-02,388,401
...
2026-07,780,590
```

* `month` — `YYYY-MM`
* `joined` — members added to the community that month
* `left` — members who left (were removed / cancelled) that month

Rules:

* Only months **before August 2026** are used — inside the tracked period the
  snapshots are authoritative and backfill rows are ignored.
* Gaps are fine; provide the months you have.
* Commit the file (or upload it via GitHub's web UI: Add file → Upload files
  into `data/backfill/`) — the next daily build, or a manual run of the
  **daily-snapshot** workflow, folds it in automatically.
