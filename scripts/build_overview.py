#!/usr/bin/env python3
"""Build the Lesko Help member cohort overview from the daily snapshots.

Reads every data/snapshots/YYYY-MM-DD.csv (one per day, written by
fetch_members.py) and produces:

* docs/data.json      - the computed cohort data (flat days + grouped), for
                        anything downstream
* docs/index.html     - the self-contained overview page (Netlify / Pages)
* docs/artifact.html  - the same overview in Claude-artifact form (no
                        document shell, three-state theme support)

Cohort logic
------------
A member belongs to the cohort of their join_date (the day the automation
added them to the community). A member has "entered" once last_visited is
filled in. Mighty Networks only stores the LAST visit, so the moment someone
first entered is pinned down by watching the daily snapshots: the first
snapshot where last_visited appears dates their entry (we use the
last_visited value itself at that moment, which is at most one day off).

The first snapshot that carries last_visited data at all (2026-08-19 - the
day Mighty added the field to the Admin API) is the signal baseline. Entries
already present in the baseline are upper bounds. Cohorts whose entire
10-week window closed before the baseline get blank week cells - their
entered / not-entered totals are still exact as of today.

The page groups the daily rows year -> month -> week, each section
collapsible with its own totals row.

Standard library only.
"""

import csv
import datetime as dt
import glob
import json
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT_DIR = os.path.join(REPO_ROOT, "data", "snapshots")
DOCS_DIR = os.path.join(REPO_ROOT, "docs")

LOOKBACK_DAYS = 365  # rolling window of daily cohorts shown on the page
WEEKS = 10           # week columns


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

    Returns (members, signal_baseline) where signal_baseline is the date of
    the first snapshot carrying any last_visited data (None if none does).
    """
    members = {}
    first_signal_snap = None
    for snap_date, rows in snapshots:
        if any(r.get("last_visited", "").strip() for r in rows):
            first_signal_snap = snap_date
            break
    for snap_date, rows in snapshots:
        for row in rows:
            mid = row.get("member_id", "").strip()
            if not mid:
                continue
            m = members.setdefault(mid, {"join": None, "entry": None})
            jd = row.get("join_date", "").strip()
            if jd:
                m["join"] = parse_date(jd)
            lv = row.get("last_visited", "").strip()
            if lv and m["entry"] is None:
                entry = parse_date(lv)
                if m["join"] and entry < m["join"]:
                    entry = m["join"]
                m["entry"] = entry
    return {mid: m for mid, m in members.items() if m["join"]}, first_signal_snap


def build_days(members, asof, baseline):
    """One row per calendar day in the lookback window, newest first."""
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
        # Whole 10-week window closed before the entry signal existed ->
        # week timing is unknowable; totals remain exact.
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


def aggregate(day_rows):
    """Totals + summed week cells for a group of day rows."""
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
        "wk_joined": sum(r["joined"] for r in usable),  # denominator for cell shading
        "weeks": cells,
    }


def group_days(day_rows):
    """Nest the (newest-first) day rows into years -> months -> weeks."""
    years = []
    for r in day_rows:
        d = parse_date(r["date"])
        yk = "y%d" % d.year
        mk = "m%s" % d.strftime("%Y-%m")
        iso_week = d.isocalendar()[1]
        wk = "w%s-%02d" % (d.strftime("%Y-%m"), iso_week)
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
                w["totals"] = aggregate(w["days"])
            m["totals"] = aggregate([d for w in m["weeks"] for d in w["days"]])
        y["totals"] = aggregate([d for m in y["months"] for w in m["weeks"] for d in w["days"]])
    return years


def main():
    snapshots = load_snapshots()
    generated = dt.datetime.now(dt.timezone.utc)
    os.makedirs(DOCS_DIR, exist_ok=True)

    if snapshots:
        asof = snapshots[-1][0]
        members, baseline = fold_members(snapshots)
        days = build_days(members, asof, baseline)
        years = group_days(days)
        totals = {
            "joined": sum(r["joined"] for r in days),
            "entered": sum(r["entered"] for r in days),
            "not_entered": sum(r["not_entered"] for r in days),
        }
        meta = {
            "generated_at": generated.isoformat(timespec="seconds"),
            "as_of": asof.isoformat(),
            "first_snapshot": snapshots[0][0].isoformat(),
            "signal_baseline": baseline.isoformat() if baseline else None,
            "snapshot_count": len(snapshots),
            "members_tracked": len(members),
            "lookback_days": LOOKBACK_DAYS,
            "weeks": WEEKS,
            "entry_signal": baseline is not None,
        }
    else:
        asof = None
        days, years = [], []
        totals = {"joined": 0, "entered": 0, "not_entered": 0}
        meta = {
            "generated_at": generated.isoformat(timespec="seconds"),
            "as_of": None,
            "first_snapshot": None,
            "signal_baseline": None,
            "snapshot_count": 0,
            "members_tracked": 0,
            "lookback_days": LOOKBACK_DAYS,
            "weeks": WEEKS,
            "entry_signal": False,
        }

    with open(os.path.join(DOCS_DIR, "data.json"), "w") as fh:
        json.dump({"meta": meta, "totals": totals, "rows": days, "groups": years}, fh, indent=1)

    with open(os.path.join(DOCS_DIR, "index.html"), "w") as fh:
        fh.write(render_page(meta, totals, years, mode="doc"))
    with open(os.path.join(DOCS_DIR, "artifact.html"), "w") as fh:
        fh.write(render_page(meta, totals, years, mode="artifact"))
    print("Built docs/index.html, docs/artifact.html and docs/data.json "
          "(%d day rows, %d snapshots)." % (len(days), meta["snapshot_count"]))


# ------------------------------------------------------------------ rendering

# Palette per the validated reference instance (dataviz method): sequential
# blue ramp for the week cells, chart chrome tokens for ink and surfaces,
# plus the green / red / yellow cells Giulia asked for.
LIGHT_RAMP = [("#cde2fb", "#0b0b0b"), ("#b7d3f6", "#0b0b0b"), ("#9ec5f4", "#0b0b0b"),
              ("#86b6ef", "#0b0b0b"), ("#6da7ec", "#0b0b0b"), ("#5598e7", "#0b0b0b"),
              ("#3987e5", "#ffffff"), ("#2a78d6", "#ffffff"), ("#256abf", "#ffffff"),
              ("#1c5cab", "#ffffff")]
DARK_RAMP = [("#0d366b", "#ffffff"), ("#104281", "#ffffff"), ("#184f95", "#ffffff"),
             ("#1c5cab", "#ffffff"), ("#256abf", "#ffffff"), ("#2a78d6", "#ffffff"),
             ("#3987e5", "#ffffff"), ("#5598e7", "#0b0b0b"), ("#6da7ec", "#0b0b0b"),
             ("#86b6ef", "#0b0b0b")]

LIGHT_TOKENS = {
    "page": "#f9f9f7", "surface": "#fcfcfb", "ink": "#0b0b0b", "ink-2": "#52514e",
    "ink-3": "#898781", "grid": "#e1e0d9", "ring": "rgba(11,11,11,0.10)",
    "accent": "#2a78d6", "good": "#006300",
    "grp": "#f1f0ec", "grp2": "#f6f5f1",
    "ramp-gradient": "linear-gradient(to right,#cde2fb,#6da7ec,#2a78d6,#1c5cab)",
    # Members column: solid green cell, white numerals.
    "joined-bg": "#006300", "joined-ink": "#ffffff",
    # "Not yet entered": red severity ramp, w1 lightest (nearly everyone in)
    # to w5 deepest (nobody entered yet); yellow the moment everyone is in.
    "w1-bg": "#fbb724", "w1-ink": "#0b0b0b",
    "w2-bg": "#f79009", "w2-ink": "#0b0b0b",
    "w3-bg": "#ef6c0a", "w3-ink": "#0b0b0b",
    "w4-bg": "#e04315", "w4-ink": "#ffffff",
    "w5-bg": "#d21f1f", "w5-ink": "#ffffff",
    "done-bg": "#ffd60a", "done-ink": "#0b0b0b",
    "wait-gradient": "linear-gradient(to right,#fbb724,#ef6c0a,#d21f1f)",
}
DARK_TOKENS = {
    "page": "#0d0d0d", "surface": "#1a1a19", "ink": "#ffffff", "ink-2": "#c3c2b7",
    "ink-3": "#898781", "grid": "#2c2c2a", "ring": "rgba(255,255,255,0.10)",
    "accent": "#3987e5", "good": "#0ca30c",
    "grp": "#232322", "grp2": "#1e1e1d",
    "ramp-gradient": "linear-gradient(to right,#0d366b,#256abf,#5598e7,#86b6ef)",
    "joined-bg": "#0ca30c", "joined-ink": "#0b0b0b",
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
body {
  margin: 0; background: var(--page); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
}
main { max-width: 1180px; margin: 0 auto; padding: 32px 20px 48px; }
header h1 { font-size: 24px; margin: 0 0 4px; text-wrap: balance; }
header p { margin: 0; color: var(--ink-2); }
.asof { display: inline-block; margin-top: 10px; padding: 2px 10px; border: 1px solid var(--ring);
        border-radius: 999px; color: var(--ink-2); font-size: 13px; background: var(--surface); }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin: 24px 0 16px; }
.tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 14px 16px; }
.tile .k { font-size: 13px; color: var(--ink-2); }
.tile .v { font-size: 28px; font-weight: 650; margin-top: 2px; }
.tile .sub { font-size: 14px; font-weight: 400; color: var(--ink-2); }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; padding: 8px; margin-top: 8px; }
.card.setup { padding: 20px 24px; max-width: 640px; }
.card.setup h2 { margin-top: 0; }
.card.setup code { background: var(--page); border: 1px solid var(--grid); border-radius: 4px; padding: 1px 5px; }
.tablewrap { overflow-x: auto; }
table { border-collapse: separate; border-spacing: 2px; width: 100%; font-variant-numeric: tabular-nums; }
thead th { font-size: 12px; font-weight: 600; color: var(--ink-3); text-align: right; padding: 6px 5px; white-space: nowrap; }
thead th:first-child { text-align: left; }
tbody th { font-weight: 500; text-align: left; padding: 4px 10px 4px 8px; white-space: nowrap; color: var(--ink); font-size: 13.5px; }
tbody td { text-align: right; padding: 4px 7px 4px 4px; border-radius: 4px; min-width: 34px; font-size: 13.5px; }
td.num { color: var(--ink); }
td.joined { font-weight: 650; background: var(--joined-bg); color: var(--joined-ink); }
td.w1 { background: var(--w1-bg); color: var(--w1-ink); }
td.w2 { background: var(--w2-bg); color: var(--w2-ink); }
td.w3 { background: var(--w3-bg); color: var(--w3-ink); }
td.w4 { background: var(--w4-bg); color: var(--w4-ink); }
td.w5 { background: var(--w5-bg); color: var(--w5-ink); }
td.done { font-weight: 650; background: var(--done-bg); color: var(--done-ink); }
td.muted { color: var(--ink-2); }
tr.empty th, tr.empty td { opacity: 0.45; }
td.c0 { color: var(--ink-3); }
__HEAT_RULES__
td.open { outline: 1.5px dashed var(--ink-3); outline-offset: -1.5px; }
tr.grp th { cursor: pointer; user-select: none; }
tr.grp th::before { content: "\\25B8"; display: inline-block; width: 14px; color: var(--ink-3); }
tr.grp[aria-expanded="true"] th::before { content: "\\25BE"; }
tr.g-year th, tr.g-year td.num { font-weight: 700; }
tr.g-year th { font-size: 14.5px; }
tr.g-year { background: var(--grp); }
tr.g-month { background: var(--grp2); }
tr.g-month th { font-weight: 650; padding-left: 20px; }
tr.g-week th { padding-left: 32px; color: var(--ink-2); }
tr.day th { padding-left: 50px; font-weight: 450; }
.legend { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 10px 8px 6px; color: var(--ink-2); font-size: 12.5px; }
.legend .ramp { width: 110px; height: 10px; border-radius: 5px; border: 1px solid var(--ring); background: var(--ramp-gradient); }
.legend .ramp.wait { background: var(--wait-gradient); }
.legend .sep { width: 1px; height: 14px; background: var(--grid); }
.legend .chip { display: inline-block; width: 14px; height: 14px; border-radius: 3px; vertical-align: -2px; }
.legend .chip.open-demo { outline: 1.5px dashed var(--ink-3); outline-offset: -1.5px; }
.legend .chip.done-demo { background: var(--done-bg); }
.legend .chip.joined-demo { background: var(--joined-bg); }
.note { color: var(--ink-2); font-size: 13.5px; max-width: 820px; }
.note.notice { background: var(--surface); border: 1px solid var(--ring); border-left: 3px solid var(--accent);
  border-radius: 8px; padding: 10px 14px; max-width: none; margin: 0 0 8px; }
footer { margin-top: 28px; color: var(--ink-3); font-size: 12.5px; }
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

  // Collapsible year / month / week sections.
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
    light = token_block(LIGHT_TOKENS, LIGHT_RAMP)
    dark = token_block(DARK_TOKENS, DARK_RAMP)
    if mode == "artifact":
        # Three viewer states: unstamped (system), data-theme=light, data-theme=dark.
        theme = (":root {\n  color-scheme: light;\n%s\n}\n"
                 "@media (prefers-color-scheme: dark) {\n"
                 "  :root:not([data-theme=\"light\"]) {\n  color-scheme: dark;\n%s\n  }\n}\n"
                 ":root[data-theme=\"dark\"] {\n  color-scheme: dark;\n%s\n}\n"
                 % (light, dark, dark))
    else:
        theme = (":root {\n  color-scheme: light;\n%s\n}\n"
                 "@media (prefers-color-scheme: dark) {\n"
                 "  :root {\n  color-scheme: dark;\n%s\n  }\n}\n" % (light, dark))
    return theme + BASE_CSS.replace("__HEAT_RULES__", HEAT_RULES)


def heat_class(count, joined):
    if joined == 0 or count == 0:
        return "c0"
    pct = count / joined
    bucket = max(1, min(10, int(pct * 10 + 0.999)))
    return "c%d" % bucket


def wait_class(entered, joined):
    """Red severity for the "Not yet entered" cell; yellow when complete."""
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


def week_cells(label, totals_or_row, joined_for_shading, entry_signal):
    """The ten by-W cells for a day row or a group totals row."""
    if not entry_signal or totals_or_row.get("wk_na"):
        return '<td class="c0"></td>' * WEEKS
    cells = []
    for i, c in enumerate(totals_or_row["weeks"]):
        if joined_for_shading == 0 or not c["started"]:
            cells.append('<td class="c0"></td>')
            continue
        cls = heat_class(c["n"], joined_for_shading)
        open_cls = "" if c["closed"] else " open"
        share = "%d%%" % round(100 * c["n"] / joined_for_shading)
        state = "" if c["closed"] else " &middot; week still running"
        tip = "%s &middot; by end of week %d: %d of %d entered (%s)%s" % (
            label, i + 1, c["n"], joined_for_shading, share, state)
        cells.append('<td class="%s%s" data-tip="%s">%d</td>' % (cls, open_cls, tip, c["n"]))
    return "".join(cells)


def stat_cells(label, joined, entered, not_entered, entry_signal):
    """Members (green), Not-joined count + %% (red/yellow), Entered, Share."""
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


def render_table_rows(years, entry_signal, asof):
    today = parse_date(asof)
    open_keys = {"y%d" % today.year, "m%s" % today.strftime("%Y-%m"),
                 "w%s-%02d" % (today.strftime("%Y-%m"), today.isocalendar()[1])}
    out = []

    def group_row(level, key, parents, label, totals):
        j, w, wp, e, s = stat_cells(label, totals["joined"], totals["entered"],
                                    totals["not_entered"], entry_signal)
        if level == "week":
            shade_n = totals["wk_joined"] if not totals.get("wk_na") else 0
            cells = week_cells(label, totals, shade_n, entry_signal)
        else:
            # Month and year rows: the summed week cells mix different time
            # windows and read strangely - show them only on weeks and days.
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
                    j, wt, wtp, e, s = stat_cells(label, r["joined"], r["entered"],
                                                  r["not_entered"], entry_signal)
                    cells = week_cells(label, r, r["joined"], entry_signal)
                    dim = " empty" if r["joined"] == 0 else ""
                    out.append('<tr class="day%s" data-parents="%s %s %s"><th scope="row">%s</th>%s%s%s%s%s%s</tr>'
                               % (dim, y["key"], m["key"], w["key"], label, j, wt, wtp, cells, e, s))
    return "\n".join(out)


def build_body(meta, totals, years):
    week_heads = "".join("<th>by W%d</th>" % w for w in range(1, WEEKS + 1))
    pct_total = ("%d%%" % round(100 * totals["entered"] / totals["joined"])) if totals["joined"] else "&ndash;"

    if meta["snapshot_count"] == 0:
        return """
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

    entry_signal = meta.get("entry_signal", False)
    if entry_signal:
        entered_tile = ('<div class="tile"><div class="k">Entered the community</div><div class="v">%s <span class="sub">%s</span></div></div>'
                        '<div class="tile"><div class="k">Not joined yet</div><div class="v">%s</div></div>'
                        % (totals["entered"], pct_total, totals["not_entered"]))
        notice = ""
        legend = """<div class="legend">
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
    </div>"""
    else:
        entered_tile = ('<div class="tile"><div class="k">Entered the community</div>'
                        '<div class="v">&ndash;</div><div class="k">no entry signal yet</div></div>'
                        '<div class="tile"><div class="k">Not yet entered</div>'
                        '<div class="v">&ndash;</div><div class="k">no entry signal yet</div></div>')
        notice = ('<p class="note notice">The joined-per-day numbers are live from the Mighty Networks API. '
                  'The <em>entered / week</em> columns are waiting on an entry signal &mdash; they fill in '
                  'automatically the day it is connected.</p>')
        legend = ""

    return """
  <section class="tiles">
    <div class="tile"><div class="k">New members &middot; last 12 months</div><div class="v">%(joined)s</div></div>
    %(entered_tile)s
    <div class="tile"><div class="k">Daily snapshots</div><div class="v">%(snaps)s <span class="sub">since %(first)s</span></div></div>
  </section>
  %(notice)s
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
    %(legend)s
  </section>

  <p class="note">Click a year, month, or week row to open it up or fold it away &mdash; each shows its own
  totals. Week columns are <strong>running totals</strong>, not per-week counts: <em>by W3</em> means
  &ldquo;this many had entered by the end of their third week&rdquo; &mdash; it already includes everyone from
  <em>by W1</em> and <em>by W2</em>, so the numbers grow to the right and are never added together.
  Week timing is tracked from %(baseline)s onward; older cohorts show their exact entered totals with the
  week cells left blank.</p>""" % {
        "joined": totals["joined"],
        "entered_tile": entered_tile,
        "notice": notice,
        "snaps": meta["snapshot_count"],
        "first": pretty(meta["first_snapshot"]),
        "week_heads": week_heads,
        "rows": render_table_rows(years, entry_signal, meta["as_of"]),
        "legend": legend,
        "baseline": pretty(meta["signal_baseline"]) if meta.get("signal_baseline") else "the first snapshot",
    }


DOC_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lesko Help Member Cohorts</title>
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
    <h1>Lesko Help &mdash; member cohort overview</h1>
    <p>How many members were added each day, and how quickly they actually enter the community.</p>
    __BADGE__
  </header>
__BODY__
  <footer>Data straight from the Mighty Networks member API &middot; rebuilt daily &middot; generated __GENERATED__ UTC</footer>
</main>
<div id="tip" role="tooltip"></div>
<script>__JS__</script>
"""


def render_page(meta, totals, years, mode):
    badge = ('<span class="asof">Data as of %s</span>' % pretty(meta["as_of"])) if meta["as_of"] else \
            '<span class="asof">Not connected yet</span>'
    content = (CONTENT_SHELL
               .replace("__BADGE__", badge)
               .replace("__BODY__", build_body(meta, totals, years))
               .replace("__GENERATED__", meta["generated_at"].replace("T", " ")[:16])
               .replace("__JS__", PAGE_JS))
    shell = ARTIFACT_SHELL if mode == "artifact" else DOC_SHELL
    return shell.replace("__CSS__", build_css(mode)).replace("__CONTENT__", content)


if __name__ == "__main__":
    main()
