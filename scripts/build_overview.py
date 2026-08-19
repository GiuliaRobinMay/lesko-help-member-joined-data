#!/usr/bin/env python3
"""Build the Lesko Help member overview (cohorts, leavers, churn) from the
daily snapshots.

Reads every data/snapshots/YYYY-MM-DD.csv (one per day, written by
fetch_members.py) and produces:

* docs/data.json      - all computed data, for anything downstream
* docs/index.html     - the self-contained overview app (Netlify / Pages)
* docs/artifact.html  - the same app in Claude-artifact form (no doc shell)

The page has three tabs:

Cohorts - one row per join-day (rolling 12 months), grouped and collapsible
year -> month -> week, with members / not joined / cumulative by-week entry
columns. A member has "entered" once last_visited is filled; the first
snapshot carrying last_visited data (2026-08-19) is the signal baseline, and
cohorts whose whole 10-week window closed before it get blank week cells.

Leavers - one row per day since tracking began, counting members who
disappeared from the member list that day, with tenure buckets: left in
month 1..12 of membership, or after 12+ months (30-day months).

Churn - opening balance at the first snapshot, then one row per month:
joined, left, net, members at end (ties exactly to the snapshot counts),
and churn % (left / members at month start). Months are attributed by the
day a change was observed, at most one day after the fact. Everything older
than tracking is one opening-balance record.

A member who vanishes mid-history but is present in the latest snapshot is
treated as never having left (rejoined or a data blip). Standard library
only.
"""

import csv
import datetime as dt
import glob
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_DIR = os.path.join(REPO_ROOT, "data", "snapshots")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

LOOKBACK_DAYS = 365  # rolling window of daily cohort rows shown on the page
WEEKS = 10           # cohort week columns
TENURE_BUCKETS = 13  # leaver tenure columns: month 1..12, then 12+


def parse_date(s):
    return dt.date.fromisoformat(s)


def load_snapshots():
    files = sorted(glob.glob(os.path.join(SNAPSHOT_DIR, "*.csv")))
    snapshots = []
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            snap_date = parse_date(name)
        except ValueError:
            continue
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        snapshots.append((snap_date, rows))
    return snapshots


def fold_members(snapshots):
    """Collapse the daily snapshots into one record per member.

    Returns (members, signal_baseline). Each member record carries join,
    entry (first observed last_visited), first_seen / last_seen snapshot
    dates, and left (the snapshot date on which they were first missing,
    None while they are still in the community).
    """
    members = {}
    first_signal_snap = None
    for snap_date, rows in snapshots:
        if any(r.get("last_visited", "").strip() for r in rows):
            first_signal_snap = snap_date
            break
    snap_dates = [d for d, _ in snapshots]
    for idx, (snap_date, rows) in enumerate(snapshots):
        for row in rows:
            mid = row.get("member_id", "").strip()
            if not mid:
                continue
            m = members.setdefault(mid, {"join": None, "entry": None,
                                         "first_seen": snap_date, "last_idx": idx})
            m["last_idx"] = idx
            jd = row.get("join_date", "").strip()
            if jd:
                m["join"] = parse_date(jd)
            lv = row.get("last_visited", "").strip()
            if lv and m["entry"] is None:
                entry = parse_date(lv)
                if m["join"] and entry < m["join"]:
                    entry = m["join"]
                m["entry"] = entry
    last = len(snap_dates) - 1
    for m in members.values():
        m["left"] = snap_dates[m["last_idx"] + 1] if m["last_idx"] < last else None
        del m["last_idx"]
    return {mid: m for mid, m in members.items() if m["join"]}, first_signal_snap


def tenure_bucket(days):
    """Month 1..12 of membership, or bucket 13 for 12+ months (30-day months)."""
    if days < 0:
        days = 0
    return min(days // 30 + 1, TENURE_BUCKETS)


# ------------------------------------------------------------- cohort maths

def build_cohort_days(members, asof, baseline):
    start = asof - dt.timedelta(days=LOOKBACK_DAYS - 1)
    by_day = {}
    for m in members.values():
        by_day.setdefault(m["join"], []).append(m)

    days = []
    day = asof
    while day >= start:
        cohort = by_day.get(day, [])
        joined = len(cohort)
        entered = [m for m in cohort if m["entry"]]
        wk_na = baseline is None or (day + dt.timedelta(days=7 * WEEKS - 1)) < baseline
        cells = []
        for w in range(1, WEEKS + 1):
            week_start = day + dt.timedelta(days=7 * (w - 1))
            week_end = day + dt.timedelta(days=7 * w - 1)
            count = sum(1 for m in entered if m["entry"] <= min(week_end, asof))
            cells.append({
                "n": count,
                "closed": week_end <= asof,
                "started": week_start <= asof,
            })
        days.append({
            "date": day.isoformat(),
            "joined": joined,
            "entered": len(entered),
            "not_entered": joined - len(entered),
            "wk_na": wk_na,
            "weeks": cells,
        })
        day -= dt.timedelta(days=1)
    return days


def aggregate_cohort(day_rows):
    joined = sum(r["joined"] for r in day_rows)
    entered = sum(r["entered"] for r in day_rows)
    usable = [r for r in day_rows if not r["wk_na"] and r["joined"] > 0]
    cells = []
    for i in range(WEEKS):
        if usable:
            cells.append({
                "n": sum(r["weeks"][i]["n"] for r in usable),
                "closed": all(r["weeks"][i]["closed"] for r in usable),
                "started": any(r["weeks"][i]["started"] for r in usable),
            })
        else:
            cells.append({"n": 0, "closed": False, "started": False})
    return {
        "joined": joined,
        "entered": entered,
        "not_entered": joined - entered,
        "wk_na": not usable,
        "wk_joined": sum(r["joined"] for r in usable),
        "weeks": cells,
    }


# ------------------------------------------------------------- leaver maths

def build_leaver_days(members, asof, tracking_start):
    """One row per day from the first observable leave through today."""
    leavers_by_day = {}
    for m in members.values():
        if m["left"]:
            leavers_by_day.setdefault(m["left"], []).append(m)

    first = tracking_start + dt.timedelta(days=1)
    days = []
    day = asof
    while day >= first:
        gone = leavers_by_day.get(day, [])
        buckets = [0] * TENURE_BUCKETS
        for m in gone:
            buckets[tenure_bucket((day - m["join"]).days) - 1] += 1
        days.append({"date": day.isoformat(), "left": len(gone), "buckets": buckets})
        day -= dt.timedelta(days=1)
    return days


def aggregate_leavers(day_rows):
    return {
        "left": sum(r["left"] for r in day_rows),
        "buckets": [sum(r["buckets"][i] for r in day_rows) for i in range(TENURE_BUCKETS)],
    }


# -------------------------------------------------------------- churn maths

def build_churn(members, snapshots):
    """Opening balance + one row per month, attributed by observation day."""
    if not snapshots:
        return None
    snap_dates = [d for d, _ in snapshots]
    tracking_start = snap_dates[0]
    opening = len({r["member_id"] for r in snapshots[0][1] if r.get("member_id")})

    monthly = {}
    for m in members.values():
        if m["first_seen"] > tracking_start:
            monthly.setdefault(m["first_seen"].strftime("%Y-%m"), [0, 0])[0] += 1
        if m["left"]:
            monthly.setdefault(m["left"].strftime("%Y-%m"), [0, 0])[1] += 1

    asof = snap_dates[-1]
    rows = []
    total = opening
    cursor = tracking_start.replace(day=1)
    while cursor <= asof:
        key = cursor.strftime("%Y-%m")
        joined, left = monthly.get(key, [0, 0])
        start_total = total
        total = total + joined - left
        label = cursor.strftime("%B %Y")
        if cursor.month == tracking_start.month and cursor.year == tracking_start.year:
            label += " (from %s)" % tracking_start.strftime("%e %b").strip()
        if cursor.month == asof.month and cursor.year == asof.year:
            label += " &middot; to date"
        rows.append({
            "key": key,
            "label": label,
            "joined": joined,
            "left": left,
            "net": joined - left,
            "end_total": total,
            "churn_pct": (100.0 * left / start_total) if start_total else 0.0,
        })
        cursor = (cursor.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    rows.reverse()
    return {
        "opening": {"date": tracking_start.isoformat(), "members": opening},
        "months": rows,
        "current_total": total,
    }


# ----------------------------------------------------------------- grouping

def group_days(day_rows, agg_fn, key_prefix=""):
    """Nest (newest-first) day rows into years -> months -> weeks."""
    years = []
    for r in day_rows:
        d = parse_date(r["date"])
        yk = "%sy%d" % (key_prefix, d.year)
        mk = "%sm%s" % (key_prefix, d.strftime("%Y-%m"))
        iso_week = d.isocalendar()[1]
        wk = "%sw%s-%02d" % (key_prefix, d.strftime("%Y-%m"), iso_week)
        if not years or years[-1]["key"] != yk:
            years.append({"key": yk, "label": str(d.year), "months": []})
        y = years[-1]
        if not y["months"] or y["months"][-1]["key"] != mk:
            y["months"].append({"key": mk, "label": d.strftime("%B %Y"), "weeks": []})
        m = y["months"][-1]
        if not m["weeks"] or m["weeks"][-1]["key"] != wk:
            monday = d - dt.timedelta(days=d.weekday())
            sunday = monday + dt.timedelta(days=6)
            if monday.month == sunday.month:
                span = "%s&ndash;%s %s" % (monday.day, sunday.day, sunday.strftime("%b"))
            else:
                span = "%s %s&ndash;%s %s" % (monday.day, monday.strftime("%b"),
                                              sunday.day, sunday.strftime("%b"))
            label = "Week %d &middot; %s" % (iso_week, span)
            m["weeks"].append({"key": wk, "label": label, "days": []})
        m["weeks"][-1]["days"].append(r)

    for y in years:
        for m in y["months"]:
            for w in m["weeks"]:
                w["totals"] = agg_fn(w["days"])
            m["totals"] = agg_fn([d for w in m["weeks"] for d in w["days"]])
        y["totals"] = agg_fn([d for m in y["months"] for w in m["weeks"] for d in w["days"]])
    return years


def main():
    snapshots = load_snapshots()
    generated = dt.datetime.now(dt.timezone.utc)
    os.makedirs(DOCS_DIR, exist_ok=True)

    if snapshots:
        asof = snapshots[-1][0]
        tracking_start = snapshots[0][0]
        members, baseline = fold_members(snapshots)
        cohort_days = build_cohort_days(members, asof, baseline)
        cohort_groups = group_days(cohort_days, aggregate_cohort)
        leaver_days = build_leaver_days(members, asof, tracking_start)
        leaver_groups = group_days(leaver_days, aggregate_leavers, key_prefix="L")
        churn = build_churn(members, snapshots)
        totals = {
            "joined": sum(r["joined"] for r in cohort_days),
            "entered": sum(r["entered"] for r in cohort_days),
            "not_entered": sum(r["not_entered"] for r in cohort_days),
        }
        leaver_totals = aggregate_leavers(leaver_days)
        meta = {
            "generated_at": generated.isoformat(timespec="seconds"),
            "as_of": asof.isoformat(),
            "first_snapshot": tracking_start.isoformat(),
            "signal_baseline": baseline.isoformat() if baseline else None,
            "snapshot_count": len(snapshots),
            "members_tracked": len(members),
            "lookback_days": LOOKBACK_DAYS,
            "weeks": WEEKS,
            "entry_signal": baseline is not None,
        }
    else:
        asof = None
        cohort_days, cohort_groups, leaver_days, leaver_groups = [], [], [], []
        churn = None
        totals = {"joined": 0, "entered": 0, "not_entered": 0}
        leaver_totals = {"left": 0, "buckets": [0] * TENURE_BUCKETS}
        meta = {
            "generated_at": generated.isoformat(timespec="seconds"),
            "as_of": None, "first_snapshot": None, "signal_baseline": None,
            "snapshot_count": 0, "members_tracked": 0,
            "lookback_days": LOOKBACK_DAYS, "weeks": WEEKS, "entry_signal": False,
        }

    with open(os.path.join(DOCS_DIR, "data.json"), "w") as fh:
        json.dump({"meta": meta, "totals": totals, "rows": cohort_days,
                   "groups": cohort_groups, "leavers": leaver_days,
                   "leaver_groups": leaver_groups, "leaver_totals": leaver_totals,
                   "churn": churn}, fh, indent=1)

    ctx = {"meta": meta, "totals": totals, "cohort_groups": cohort_groups,
           "leaver_groups": leaver_groups, "leaver_totals": leaver_totals,
           "churn": churn}
    with open(os.path.join(DOCS_DIR, "index.html"), "w") as fh:
        fh.write(render_page(ctx, mode="doc"))
    with open(os.path.join(DOCS_DIR, "artifact.html"), "w") as fh:
        fh.write(render_page(ctx, mode="artifact"))
    print("Built docs/ app: %d cohort days, %d leaver days, %d churn months, %d snapshots."
          % (len(cohort_days), len(leaver_days),
             len(churn["months"]) if churn else 0, meta["snapshot_count"]))


# ------------------------------------------------------------------ rendering

LIGHT_RAMP = [("#cde2fb", "#0b0b0b"), ("#b7d3f6", "#0b0b0b"), ("#9ec5f4", "#0b0b0b"),
              ("#86b6ef", "#0b0b0b"), ("#6da7ec", "#0b0b0b"), ("#5598e7", "#0b0b0b"),
              ("#3987e5", "#ffffff"), ("#2a78d6", "#ffffff"), ("#256abf", "#ffffff"),
              ("#1c5cab", "#ffffff")]

# Lesko Help brand chrome (cream paper, navy ink, red + yellow accents).
LIGHT_TOKENS = {
    "page": "#faf6ec", "surface": "#fffefa", "ink": "#16223f", "ink-2": "#4a5570",
    "ink-3": "#8a93a8", "grid": "rgba(22,34,63,0.14)", "ring": "rgba(22,34,63,0.22)",
    "accent": "#e8453c", "good": "#2e9e5b", "brand-yellow": "#f6c544",
    "grp": "#f1ead8", "grp2": "#f6f0e1",
    "ramp-gradient": "linear-gradient(to right,#cde2fb,#6da7ec,#2a78d6,#1c5cab)",
    "joined-bg": "#2e9e5b", "joined-ink": "#ffffff",
    "left-bg": "#e8453c", "left-ink": "#ffffff",
    "w1-bg": "#fbb724", "w1-ink": "#0b0b0b",
    "w2-bg": "#f79009", "w2-ink": "#0b0b0b",
    "w3-bg": "#ef6c0a", "w3-ink": "#0b0b0b",
    "w4-bg": "#e04315", "w4-ink": "#ffffff",
    "w5-bg": "#d21f1f", "w5-ink": "#ffffff",
    "done-bg": "#ffd60a", "done-ink": "#0b0b0b",
    "wait-gradient": "linear-gradient(to right,#fbb724,#ef6c0a,#d21f1f)",
}


def token_block(tokens, ramp):
    lines = ["  --%s: %s;" % (k, v) for k, v in tokens.items()]
    for i, (bg, ink) in enumerate(ramp, start=1):
        lines.append("  --h%d-bg: %s; --h%d-ink: %s;" % (i, bg, i, ink))
    return "\n".join(lines)


BASE_CSS = """
* { box-sizing: border-box; }
:root {
  --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 1180px; margin: 0 auto; padding: 32px 20px 48px; }
header h1 { font-family: var(--serif); font-size: 27px; margin: 0 0 4px; text-wrap: balance; }
header p { margin: 0; color: var(--ink-2); font-family: var(--serif); font-style: italic; }
.asof { display: inline-block; margin-top: 10px; padding: 3px 12px;
        border-radius: 999px; color: var(--ink); font-size: 13px; font-weight: 600;
        background: var(--brand-yellow); }
.tabs { display: flex; flex-wrap: wrap; gap: 8px; margin: 22px 0 2px; }
.tab { font-family: var(--serif); font-size: 15px; padding: 7px 20px; border-radius: 999px;
  border: 1px solid var(--ring); background: var(--surface); color: var(--ink); cursor: pointer; }
.tab.active { background: var(--ink); color: var(--page); border-color: var(--ink); }
.panel { display: none; }
.panel.active { display: block; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin: 20px 0 16px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 14px 16px; }
.tile .k { font-family: var(--mono); font-size: 11px; text-transform: uppercase;
  letter-spacing: 0.05em; color: var(--ink-2); }
.tile .v { font-family: var(--serif); font-size: 30px; font-weight: 650; margin-top: 3px; }
.tile .sub { font-size: 14px; font-weight: 400; color: var(--ink-2); }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 8px; margin-top: 8px; }
.card.setup { padding: 20px 24px; max-width: 640px; }
.card.setup h2 { margin-top: 0; }
.card.setup code { background: var(--page); border: 1px solid var(--grid); border-radius: 4px; padding: 1px 5px; }
.tablewrap { overflow-x: auto; }
table { border-collapse: separate; border-spacing: 2px; width: 100%; font-variant-numeric: tabular-nums; }
thead th { font-family: var(--mono); font-size: 10.5px; font-weight: 600; color: var(--ink-2);
  text-transform: uppercase; letter-spacing: 0.06em; text-align: right; padding: 6px 5px; white-space: nowrap; }
thead th:first-child { text-align: left; }
tbody th { font-weight: 500; text-align: left; padding: 4px 10px 4px 8px; white-space: nowrap; color: var(--ink); font-size: 13.5px; }
tbody td { text-align: right; padding: 4px 7px 4px 4px; border-radius: 4px; min-width: 34px; font-size: 13.5px; }
td.num { color: var(--ink); }
td.joined { font-weight: 650; background: var(--joined-bg); color: var(--joined-ink); }
td.leftc { font-weight: 650; background: var(--left-bg); color: var(--left-ink); }
td.w1 { background: var(--w1-bg); color: var(--w1-ink); }
td.w2 { background: var(--w2-bg); color: var(--w2-ink); }
td.w3 { background: var(--w3-bg); color: var(--w3-ink); }
td.w4 { background: var(--w4-bg); color: var(--w4-ink); }
td.w5 { background: var(--w5-bg); color: var(--w5-ink); }
td.done { font-weight: 650; background: var(--done-bg); color: var(--done-ink); }
td.muted { color: var(--ink-2); }
td.pos { color: var(--good); font-weight: 650; }
td.neg { color: var(--accent); font-weight: 650; }
tr.empty th, tr.empty td { opacity: 0.45; }
td.c0 { color: var(--ink-3); }
__HEAT_RULES__
td.open { outline: 1.5px dashed var(--ink-3); outline-offset: -1.5px; }
tr.grp th { cursor: pointer; user-select: none; }
tr.grp th::before { content: "\\25B8"; display: inline-block; width: 14px; color: var(--ink-3); }
tr.grp[aria-expanded="true"] th::before { content: "\\25BE"; }
tr.g-year th, tr.g-year td.num { font-weight: 700; }
tr.g-year th { font-family: var(--serif); font-size: 15.5px; }
tr.g-year { background: var(--grp); }
tr.g-month { background: var(--grp2); }
tr.g-month th { font-family: var(--serif); font-weight: 650; padding-left: 20px; }
tr.g-week th { padding-left: 32px; color: var(--ink-2); }
tr.day th { padding-left: 50px; font-weight: 450; }
tr.opening { background: var(--grp); }
tr.opening th { font-family: var(--serif); font-weight: 650; }
.legend { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 10px 8px 6px; color: var(--ink-2); font-size: 12.5px; }
.legend .ramp { width: 110px; height: 10px; border-radius: 5px; border: 1px solid var(--ring); background: var(--ramp-gradient); }
.legend .ramp.wait { background: var(--wait-gradient); }
.legend .sep { width: 1px; height: 14px; background: var(--grid); }
.legend .chip { display: inline-block; width: 14px; height: 14px; border-radius: 3px; vertical-align: -2px; }
.legend .chip.open-demo { outline: 1.5px dashed var(--ink-3); outline-offset: -1.5px; }
.legend .chip.done-demo { background: var(--done-bg); }
.legend .chip.joined-demo { background: var(--joined-bg); }
.legend .chip.left-demo { background: var(--left-bg); }
.note { color: var(--ink-2); font-size: 13.5px; max-width: 820px; }
.note.notice { background: var(--surface); border: 1px solid var(--ring); border-left: 3px solid var(--accent);
  border-radius: 8px; padding: 10px 14px; max-width: none; margin: 0 0 8px; }
footer { margin-top: 28px; color: var(--ink-3); font-size: 11.5px; font-family: var(--mono); }
#tip { position: fixed; display: none; max-width: 280px; padding: 6px 10px; background: var(--ink); color: var(--page);
  border-radius: 6px; font-size: 12.5px; pointer-events: none; z-index: 10; }
"""

HEAT_RULES = "\n".join(
    "td.c%d { background: var(--h%d-bg); color: var(--h%d-ink); }" % (i, i, i)
    for i in range(1, 11))

PAGE_JS = """
(function () {
  var tip = document.getElementById('tip');
  document.addEventListener('mouseover', function (e) {
    var el = e.target.closest('[data-tip]');
    if (!el) { tip.style.display = 'none'; return; }
    tip.innerHTML = el.getAttribute('data-tip');
    tip.style.display = 'block';
  });
  document.addEventListener('mousemove', function (e) {
    if (tip.style.display !== 'block') return;
    var x = Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    var y = e.clientY + 16;
    if (y + tip.offsetHeight > window.innerHeight - 8) y = e.clientY - tip.offsetHeight - 10;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  });

  // Tabs.
  function showTab(name) {
    document.querySelectorAll('.tab').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-tab') === name);
    });
    document.querySelectorAll('.panel').forEach(function (p) {
      p.classList.toggle('active', p.id === 'panel-' + name);
    });
  }
  document.querySelectorAll('.tab').forEach(function (b) {
    b.addEventListener('click', function () {
      showTab(b.getAttribute('data-tab'));
      if (history.replaceState) history.replaceState(null, '', '#' + b.getAttribute('data-tab'));
    });
  });
  var hash = (location.hash || '').replace('#', '');
  if (hash && document.getElementById('panel-' + hash)) showTab(hash);

  // Collapsible year / month / week sections (shared across tables).
  var expanded = {};
  document.querySelectorAll('tr.grp[data-open="1"]').forEach(function (tr) {
    expanded[tr.getAttribute('data-key')] = true;
  });
  function apply() {
    document.querySelectorAll('tbody tr').forEach(function (tr) {
      var parents = tr.getAttribute('data-parents');
      var show = true;
      if (parents) {
        parents.split(' ').forEach(function (p) { if (!expanded[p]) show = false; });
      }
      tr.style.display = show ? '' : 'none';
      if (tr.classList.contains('grp')) {
        tr.setAttribute('aria-expanded', expanded[tr.getAttribute('data-key')] ? 'true' : 'false');
      }
    });
  }
  document.querySelectorAll('tr.grp th').forEach(function (th) {
    th.addEventListener('click', function () {
      var key = th.parentElement.getAttribute('data-key');
      expanded[key] = !expanded[key];
      apply();
    });
  });
  apply();
})();
"""


def build_css(mode):
    theme = ":root {\n  color-scheme: light;\n%s\n}\n" % token_block(LIGHT_TOKENS, LIGHT_RAMP)
    return theme + BASE_CSS.replace("__HEAT_RULES__", HEAT_RULES)


def heat_class(count, denom):
    if denom == 0 or count == 0:
        return "c0"
    pct = count / denom
    bucket = max(1, min(10, int(pct * 10 + 0.999)))
    return "c%d" % bucket


def wait_class(entered, joined):
    if joined == 0:
        return "c0"
    if entered >= joined:
        return "done"
    share = entered / joined
    if share >= 0.75:
        return "w1"
    if share >= 0.5:
        return "w2"
    if share >= 0.25:
        return "w3"
    if share > 0:
        return "w4"
    return "w5"


def pretty(date_iso):
    d = parse_date(date_iso)
    return d.strftime("%a %e %b").replace("  ", " ")


# ----------------------------------------------------------- cohort renderer

def cohort_week_cells(label, row_or_totals, denom, entry_signal):
    if not entry_signal or row_or_totals.get("wk_na"):
        return '<td class="c0"></td>' * WEEKS
    cells = []
    for i, c in enumerate(row_or_totals["weeks"]):
        if denom == 0 or not c["started"]:
            cells.append('<td class="c0"></td>')
            continue
        cls = heat_class(c["n"], denom)
        open_cls = "" if c["closed"] else " open"
        share = "%d%%" % round(100 * c["n"] / denom)
        state = "" if c["closed"] else " &middot; week still running"
        tip = "%s &middot; by end of week %d: %d of %d entered (%s)%s" % (
            label, i + 1, c["n"], denom, share, state)
        cells.append('<td class="%s%s" data-tip="%s">%d</td>' % (cls, open_cls, tip, c["n"]))
    return "".join(cells)


def cohort_stat_cells(label, joined, entered, not_entered, entry_signal):
    joined_cell = '<td class="num joined">%d</td>' % joined if joined else '<td class="num muted">0</td>'
    dash = '<td class="num muted">&ndash;</td>'
    if not entry_signal:
        return (joined_cell, dash, dash, dash, dash)
    if joined == 0:
        return (joined_cell, '<td class="c0"></td>', '<td class="c0"></td>',
                '<td class="num muted">0</td>', dash)
    cls = wait_class(entered, joined)
    share = round(100 * entered / joined)
    missing_pct = round(100 * not_entered / joined)
    tip = "%s &middot; %d of %d still not joined (%d%%)" % (label, not_entered, joined, missing_pct)
    waiting = '<td class="%s" data-tip="%s">%d</td>' % (cls, tip, not_entered)
    waiting_pct = '<td class="num muted">%d%%</td>' % missing_pct
    return (joined_cell, waiting, waiting_pct, '<td class="num">%d</td>' % entered,
            '<td class="num muted">%d%%</td>' % share)


def render_cohort_rows(years, entry_signal, asof):
    today = parse_date(asof)
    open_keys = {"y%d" % today.year, "m%s" % today.strftime("%Y-%m"),
                 "w%s-%02d" % (today.strftime("%Y-%m"), today.isocalendar()[1])}
    out = []

    def group_row(level, key, parents, label, totals):
        j, w, wp, e, s = cohort_stat_cells(label, totals["joined"], totals["entered"],
                                           totals["not_entered"], entry_signal)
        if level == "week":
            denom = totals["wk_joined"] if not totals.get("wk_na") else 0
            cells = cohort_week_cells(label, totals, denom, entry_signal)
        else:
            cells = '<td class="c0"></td>' * WEEKS
        opened = ' data-open="1"' if key in open_keys else ""
        out.append('<tr class="grp g-%s" data-key="%s"%s%s><th scope="row">%s</th>%s%s%s%s%s%s</tr>'
                   % (level, key,
                      (' data-parents="%s"' % " ".join(parents)) if parents else "",
                      opened, label, j, w, wp, cells, e, s))

    for y in years:
        group_row("year", y["key"], [], y["label"], y["totals"])
        for m in y["months"]:
            group_row("month", m["key"], [y["key"]], m["label"], m["totals"])
            for w in m["weeks"]:
                group_row("week", w["key"], [y["key"], m["key"]], w["label"], w["totals"])
                for r in w["days"]:
                    label = pretty(r["date"])
                    j, wt, wtp, e, s = cohort_stat_cells(label, r["joined"], r["entered"],
                                                         r["not_entered"], entry_signal)
                    cells = cohort_week_cells(label, r, r["joined"], entry_signal)
                    dim = " empty" if r["joined"] == 0 else ""
                    out.append('<tr class="day%s" data-parents="%s %s %s"><th scope="row">%s</th>%s%s%s%s%s%s</tr>'
                               % (dim, y["key"], m["key"], w["key"], label, j, wt, wtp, cells, e, s))
    return "\n".join(out)


def cohort_panel(meta, totals, years):
    week_heads = "".join("<th>by W%d</th>" % w for w in range(1, WEEKS + 1))
    pct_total = ("%d%%" % round(100 * totals["entered"] / totals["joined"])) if totals["joined"] else "&ndash;"
    entry_signal = meta.get("entry_signal", False)

    return """
  <section class="tiles">
    <div class="tile"><div class="k">New members &middot; last 12 months</div><div class="v">%(joined)s</div></div>
    <div class="tile"><div class="k">Entered the community</div><div class="v">%(entered)s <span class="sub">%(pct)s</span></div></div>
    <div class="tile"><div class="k">Not joined yet</div><div class="v">%(not_entered)s</div></div>
    <div class="tile"><div class="k">Daily snapshots</div><div class="v">%(snaps)s <span class="sub">since %(first)s</span></div></div>
  </section>
  <section class="card">
    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Joined on</th>
            <th scope="col">Members</th>
            <th scope="col">Not joined</th>
            <th scope="col">%%</th>
            %(week_heads)s
            <th scope="col">Entered</th>
            <th scope="col">Share</th>
          </tr>
        </thead>
        <tbody>
          %(rows)s
        </tbody>
      </table>
    </div>
    <div class="legend">
      <span class="chip joined-demo"></span><span>members added that day</span>
      <span class="sep"></span>
      <span class="ramp wait" aria-hidden="true"></span>
      <span>still missing &mdash; red = few entered, warms to orange as they come in</span>
      <span class="sep"></span>
      <span><span class="chip done-demo"></span> everyone entered</span>
      <span class="sep"></span>
      <span class="ramp" aria-hidden="true"></span>
      <span>share entered by that week</span>
      <span class="sep"></span>
      <span><span class="chip open-demo"></span> week still running</span>
    </div>
  </section>

  <p class="note">Click a year, month, or week row to open it up or fold it away &mdash; each shows its own
  totals. Week columns are <strong>running totals</strong>, not per-week counts: <em>by W3</em> means
  &ldquo;this many had entered by the end of their third week&rdquo; &mdash; it already includes everyone from
  <em>by W1</em> and <em>by W2</em>, so the numbers grow to the right and are never added together.
  Week timing is tracked from %(baseline)s onward; older cohorts show their exact entered totals with the
  week cells left blank.</p>""" % {
        "joined": totals["joined"],
        "entered": totals["entered"],
        "pct": pct_total,
        "not_entered": totals["not_entered"],
        "snaps": meta["snapshot_count"],
        "first": pretty(meta["first_snapshot"]),
        "week_heads": week_heads,
        "rows": render_cohort_rows(years, entry_signal, meta["as_of"]),
        "baseline": pretty(meta["signal_baseline"]) if meta.get("signal_baseline") else "the first snapshot",
    }


# ----------------------------------------------------------- leaver renderer

def tenure_heads():
    heads = "".join("<th>M%d</th>" % i for i in range(1, TENURE_BUCKETS))
    return heads + "<th>12m+</th>"


def leaver_cells(label, buckets, left_total):
    names = ["month %d" % i for i in range(1, TENURE_BUCKETS)] + ["12+ months"]
    cells = []
    for i, n in enumerate(buckets):
        if left_total == 0 or n == 0:
            cells.append('<td class="c0"></td>' if left_total == 0 else '<td class="c0">0</td>')
            continue
        cls = heat_class(n, left_total)
        tip = "%s &middot; %d of %d leavers were a member for %s" % (label, n, left_total, names[i])
        cells.append('<td class="%s" data-tip="%s">%d</td>' % (cls, tip, n))
    return "".join(cells)


def render_leaver_rows(years, asof):
    today = parse_date(asof)
    open_keys = {"Ly%d" % today.year, "Lm%s" % today.strftime("%Y-%m"),
                 "Lw%s-%02d" % (today.strftime("%Y-%m"), today.isocalendar()[1])}
    out = []

    def row_cells(label, totals):
        left = totals["left"]
        left_cell = ('<td class="num leftc">%d</td>' % left) if left else '<td class="num muted">0</td>'
        return left_cell + leaver_cells(label, totals["buckets"], left)

    for y in years:
        opened = ' data-open="1"' if y["key"] in open_keys else ""
        out.append('<tr class="grp g-year" data-key="%s"%s><th scope="row">%s</th>%s</tr>'
                   % (y["key"], opened, y["label"], row_cells(y["label"], y["totals"])))
        for m in y["months"]:
            opened = ' data-open="1"' if m["key"] in open_keys else ""
            out.append('<tr class="grp g-month" data-key="%s" data-parents="%s"%s><th scope="row">%s</th>%s</tr>'
                       % (m["key"], y["key"], opened, m["label"], row_cells(m["label"], m["totals"])))
            for w in m["weeks"]:
                opened = ' data-open="1"' if w["key"] in open_keys else ""
                out.append('<tr class="grp g-week" data-key="%s" data-parents="%s %s"%s><th scope="row">%s</th>%s</tr>'
                           % (w["key"], y["key"], m["key"], opened, w["label"], row_cells(w["label"], w["totals"])))
                for r in w["days"]:
                    label = pretty(r["date"])
                    dim = " empty" if r["left"] == 0 else ""
                    out.append('<tr class="day%s" data-parents="%s %s %s"><th scope="row">%s</th>%s</tr>'
                               % (dim, y["key"], m["key"], w["key"], label,
                                  row_cells(label, {"left": r["left"], "buckets": r["buckets"]})))
    return "\n".join(out)


def leaver_panel(meta, leaver_groups, leaver_totals):
    early = leaver_totals["buckets"][0] if leaver_totals["buckets"] else 0
    longterm = leaver_totals["buckets"][-1] if leaver_totals["buckets"] else 0
    return """
  <section class="tiles">
    <div class="tile"><div class="k">Members left &middot; since %(first)s</div><div class="v">%(left)s</div></div>
    <div class="tile"><div class="k">Left within month 1</div><div class="v">%(early)s</div></div>
    <div class="tile"><div class="k">Left after 12+ months</div><div class="v">%(longterm)s</div></div>
    <div class="tile"><div class="k">Days tracked</div><div class="v">%(days)s</div></div>
  </section>
  <section class="card">
    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Left on</th>
            <th scope="col">Left</th>
            %(tenure_heads)s
          </tr>
        </thead>
        <tbody>
          %(rows)s
        </tbody>
      </table>
    </div>
    <div class="legend">
      <span class="chip left-demo"></span><span>members who left that day</span>
      <span class="sep"></span>
      <span class="ramp" aria-hidden="true"></span>
      <span>share of that day&rsquo;s leavers per tenure column</span>
    </div>
  </section>
  <p class="note"><strong>M1</strong> = left in their first month of membership, <strong>M2</strong> in their
  second, and so on; <strong>12m+</strong> = they were a member for more than twelve months (counted in
  30-day months from their join date). Leaving is detected by comparing the daily snapshots &mdash; a member
  present yesterday and missing today left. Tracking runs from %(first)s; who left before that is not
  visible anywhere and is covered by the opening balance on the Churn tab.</p>""" % {
        "first": pretty(meta["first_snapshot"]),
        "left": leaver_totals["left"],
        "early": early,
        "longterm": longterm,
        "days": max(meta["snapshot_count"] - 1, 0),
        "tenure_heads": tenure_heads(),
        "rows": render_leaver_rows(leaver_groups, meta["as_of"]),
    }


# ------------------------------------------------------------ churn renderer

def churn_panel(meta, churn):
    rows = []
    for r in churn["months"]:
        net_cls = "pos" if r["net"] > 0 else ("neg" if r["net"] < 0 else "muted num")
        net_txt = ("+%d" % r["net"]) if r["net"] > 0 else str(r["net"])
        joined_cell = ('<td class="num joined">%d</td>' % r["joined"]) if r["joined"] else '<td class="num muted">0</td>'
        left_cell = ('<td class="num leftc">%d</td>' % r["left"]) if r["left"] else '<td class="num muted">0</td>'
        rows.append('<tr><th scope="row">%s</th>%s%s<td class="%s">%s</td>'
                    '<td class="num"><strong>%s</strong></td><td class="num muted">%.2f%%</td></tr>'
                    % (r["label"], joined_cell, left_cell, net_cls, net_txt,
                       "{:,}".format(r["end_total"]), r["churn_pct"]))
    opening_row = ('<tr class="opening"><th scope="row">Before tracking &middot; everything up to %s</th>'
                   '<td class="num muted" colspan="3">one opening record</td>'
                   '<td class="num"><strong>%s</strong></td><td class="num muted">&ndash;</td></tr>'
                   % (pretty(churn["opening"]["date"]), "{:,}".format(churn["opening"]["members"])))
    return """
  <section class="tiles">
    <div class="tile"><div class="k">Members in the community</div><div class="v">%(total)s</div></div>
    <div class="tile"><div class="k">Joined since %(first)s</div><div class="v">%(joined)s</div></div>
    <div class="tile"><div class="k">Left since %(first)s</div><div class="v">%(left)s</div></div>
    <div class="tile"><div class="k">Net growth</div><div class="v">%(net)s</div></div>
  </section>
  <section class="card">
    <div class="tablewrap">
      <table>
        <thead>
          <tr>
            <th scope="col">Month</th>
            <th scope="col">Joined</th>
            <th scope="col">Left</th>
            <th scope="col">Net</th>
            <th scope="col">Members at end</th>
            <th scope="col">Churn</th>
          </tr>
        </thead>
        <tbody>
          %(rows)s
          %(opening)s
        </tbody>
      </table>
    </div>
  </section>
  <p class="note">Joined and left are counted on the day the daily snapshot observed them (at most one day
  after the fact), so <em>members at end</em> always matches the real member count. <em>Churn</em> is the
  share of members at the start of the month who left during it. Everything older than the first snapshot
  lives in the one opening record &mdash; month-by-month history before %(first)s does not exist in any
  source we can reach.</p>""" % {
        "total": "{:,}".format(churn["current_total"]),
        "first": pretty(churn["opening"]["date"]),
        "joined": sum(r["joined"] for r in churn["months"]),
        "left": sum(r["left"] for r in churn["months"]),
        "net": "%+d" % sum(r["net"] for r in churn["months"]),
        "rows": "\n".join(rows),
        "opening": opening_row,
    }


# ------------------------------------------------------------------ assembly

SETUP_BODY = """
  <section class="card setup">
    <h2>Waiting for the first snapshot</h2>
    <p>This page fills itself in automatically once the daily connection to
    Mighty Networks is switched on. One thing is needed for that:</p>
    <ol>
      <li>Create an API token in the Mighty Networks admin (Admin &rarr; Settings &rarr; API Keys).</li>
      <li>Add it to this repository as a secret named <code>MIGHTY_API_TOKEN</code> &mdash; Settings &rarr; Secrets and variables &rarr; Actions.</li>
    </ol>
    <p>From then on a snapshot is taken every morning and this overview rebuilds
    itself &mdash; new data every day, no exports, no spreadsheets.</p>
  </section>"""


def build_body(ctx):
    meta = ctx["meta"]
    if meta["snapshot_count"] == 0:
        return SETUP_BODY
    return """
  <nav class="tabs" aria-label="Views">
    <button class="tab active" data-tab="cohorts">Cohorts</button>
    <button class="tab" data-tab="leavers">Leavers</button>
    <button class="tab" data-tab="churn">Churn</button>
  </nav>
  <div class="panel active" id="panel-cohorts">%s
  </div>
  <div class="panel" id="panel-leavers">%s
  </div>
  <div class="panel" id="panel-churn">%s
  </div>""" % (cohort_panel(meta, ctx["totals"], ctx["cohort_groups"]),
               leaver_panel(meta, ctx["leaver_groups"], ctx["leaver_totals"]),
               churn_panel(meta, ctx["churn"]))


DOC_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lesko Help Member Cohorts</title>
<link rel="manifest" href="manifest.json">
<meta name="theme-color" content="#16223f">
<link rel="icon" href="icon-192.png">
<link rel="apple-touch-icon" href="icon-192.png">
<style>__CSS__</style>
</head>
<body>
__CONTENT__
</body>
</html>
"""

ARTIFACT_SHELL = """<title>Lesko Help Member Cohorts</title>
<style>__CSS__</style>
__CONTENT__
"""

CONTENT_SHELL = """<main>
  <header>
    <h1>Lesko Help &mdash; member overview</h1>
    <p>Who joins, who actually enters the community, who leaves &mdash; day by day.</p>
    __BADGE__
  </header>
__BODY__
  <footer>Data straight from the Mighty Networks member API &middot; rebuilt daily &middot; generated __GENERATED__ UTC</footer>
</main>
<div id="tip" role="tooltip"></div>
<script>__JS__</script>
"""


def render_page(ctx, mode):
    meta = ctx["meta"]
    badge = ('<span class="asof">Data as of %s</span>' % pretty(meta["as_of"])) if meta["as_of"] else \
            '<span class="asof">Not connected yet</span>'
    content = (CONTENT_SHELL
               .replace("__BADGE__", badge)
               .replace("__BODY__", build_body(ctx))
               .replace("__GENERATED__", meta["generated_at"].replace("T", " ")[:16])
               .replace("__JS__", PAGE_JS))
    shell = ARTIFACT_SHELL if mode == "artifact" else DOC_SHELL
    return shell.replace("__CSS__", build_css(mode)).replace("__CONTENT__", content)


if __name__ == "__main__":
    main()
