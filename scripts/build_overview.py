#!/usr/bin/env python3
"""Build the Lesko Help member cohort overview from the daily snapshots.

Reads every data/snapshots/YYYY-MM-DD.csv (one per day, written by
fetch_members.py) and produces:

* docs/data.json      - the computed cohort matrix, for anything downstream
* docs/index.html     - the self-contained overview page (GitHub Pages)
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

Members who had already entered before the very first snapshot ever taken are
attributed to the week of their last_visited value at that time - an upper
bound, so their week attribution is approximate (never earlier than reality
... the true first visit can only be earlier, i.e. in an earlier week).
Their "entered vs. not entered" status is exact either way.

While the Admin API exposes no last-visit field (verified 2026-08-18), no
member has an entry observation, and the page renders dashes plus a notice
for the entered/week columns instead of a misleading zero.

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

LOOKBACK_DAYS = 70   # rows on the page: the last ~2 months of daily cohorts
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
    """Collapse the daily snapshots into one record per member."""
    members = {}
    # The signal baseline is the first snapshot that carries any last_visited
    # data (the field appeared in the API on 2026-08-19, after the first
    # snapshot day) - entries first observed in that baseline were not watched
    # happening, so their week attribution is an upper bound.
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
            m = members.setdefault(mid, {"join": None, "entry": None, "approx": False})
            jd = row.get("join_date", "").strip()
            if jd:
                m["join"] = parse_date(jd)
            lv = row.get("last_visited", "").strip()
            if lv and m["entry"] is None:
                entry = parse_date(lv)
                if m["join"] and entry < m["join"]:
                    entry = m["join"]
                m["entry"] = entry
                # Already entered when the signal baseline was taken, and the
                # bound is loose enough to cross a week boundary -> mark it.
                if (snap_date == first_signal_snap and m["join"]
                        and (entry - m["join"]).days > 6):
                    m["approx"] = True
    return {mid: m for mid, m in members.items() if m["join"]}


def build_matrix(members, asof):
    start = asof - dt.timedelta(days=LOOKBACK_DAYS - 1)
    by_day = {}
    for m in members.values():
        by_day.setdefault(m["join"], []).append(m)

    rows = []
    day = asof
    while day >= start:
        cohort = by_day.get(day, [])
        joined = len(cohort)
        entered = [m for m in cohort if m["entry"]]
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
        rows.append({
            "date": day.isoformat(),
            "joined": joined,
            "entered": len(entered),
            "not_entered": joined - len(entered),
            "approx": any(m["approx"] for m in cohort),
            "weeks": cells,
        })
        day -= dt.timedelta(days=1)

    totals = {
        "joined": sum(r["joined"] for r in rows),
        "entered": sum(r["entered"] for r in rows),
        "not_entered": sum(r["not_entered"] for r in rows),
    }
    return rows, totals


def main():
    snapshots = load_snapshots()
    generated = dt.datetime.now(dt.timezone.utc)
    os.makedirs(DOCS_DIR, exist_ok=True)

    if snapshots:
        asof = snapshots[-1][0]
        members = fold_members(snapshots)
        rows, totals = build_matrix(members, asof)
        meta = {
            "generated_at": generated.isoformat(timespec="seconds"),
            "as_of": asof.isoformat(),
            "first_snapshot": snapshots[0][0].isoformat(),
            "snapshot_count": len(snapshots),
            "members_tracked": len(members),
            "lookback_days": LOOKBACK_DAYS,
            "weeks": WEEKS,
            # True once at least one member has an observed entry. The Admin
            # API exposes no last-visit field today, so until an entry source
            # exists the page must say "no data" rather than "0 entered".
            "entry_signal": any(m["entry"] for m in members.values()),
        }
    else:
        asof = None
        rows, totals = [], {"joined": 0, "entered": 0, "not_entered": 0}
        meta = {
            "generated_at": generated.isoformat(timespec="seconds"),
            "as_of": None,
            "first_snapshot": None,
            "snapshot_count": 0,
            "members_tracked": 0,
            "lookback_days": LOOKBACK_DAYS,
            "weeks": WEEKS,
            "entry_signal": False,
        }

    with open(os.path.join(DOCS_DIR, "data.json"), "w") as fh:
        json.dump({"meta": meta, "totals": totals, "rows": rows}, fh, indent=1)

    with open(os.path.join(DOCS_DIR, "index.html"), "w") as fh:
        fh.write(render_page(meta, totals, rows, mode="doc"))
    with open(os.path.join(DOCS_DIR, "artifact.html"), "w") as fh:
        fh.write(render_page(meta, totals, rows, mode="artifact"))
    print("Built docs/index.html, docs/artifact.html and docs/data.json "
          "(%d cohort rows, %d snapshots)." % (len(rows), meta["snapshot_count"]))


# ------------------------------------------------------------------ rendering

# Palette per the validated reference instance (dataviz method): sequential
# blue ramp for cell shading, chart chrome tokens for ink and surfaces.
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
    "accent": "#2a78d6",
    "ramp-gradient": "linear-gradient(to right,#cde2fb,#6da7ec,#2a78d6,#1c5cab)",
}
DARK_TOKENS = {
    "page": "#0d0d0d", "surface": "#1a1a19", "ink": "#ffffff", "ink-2": "#c3c2b7",
    "ink-3": "#898781", "grid": "#2c2c2a", "ring": "rgba(255,255,255,0.10)",
    "accent": "#3987e5",
    "ramp-gradient": "linear-gradient(to right,#0d366b,#256abf,#5598e7,#86b6ef)",
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
main { max-width: 1080px; margin: 0 auto; padding: 32px 20px 48px; }
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
thead th { font-size: 12px; font-weight: 600; color: var(--ink-3); text-align: center; padding: 6px 8px; white-space: nowrap; }
thead th:first-child { text-align: left; }
tbody th { font-weight: 500; text-align: left; padding: 4px 10px 4px 8px; white-space: nowrap; color: var(--ink); font-size: 13.5px; }
tbody td { text-align: center; padding: 4px 6px; border-radius: 4px; min-width: 40px; font-size: 13.5px; }
td.num { color: var(--ink); }
td.joined { font-weight: 650; }
td.muted { color: var(--ink-2); }
tr.empty th, tr.empty td { opacity: 0.45; }
td.c0 { color: var(--ink-3); }
__HEAT_RULES__
td.open { outline: 1.5px dashed var(--ink-3); outline-offset: -1.5px; }
.approx { color: var(--ink-3); }
.legend { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding: 10px 8px 6px; color: var(--ink-2); font-size: 12.5px; }
.legend .ramp { width: 120px; height: 10px; border-radius: 5px; border: 1px solid var(--ring); background: var(--ramp-gradient); }
.legend .sep { width: 1px; height: 14px; background: var(--grid); }
.legend .chip.open-demo { display: inline-block; width: 14px; height: 14px; border-radius: 3px;
  outline: 1.5px dashed var(--ink-3); outline-offset: -1.5px; vertical-align: -2px; }
.note { color: var(--ink-2); font-size: 13.5px; max-width: 760px; }
.note.notice { background: var(--surface); border: 1px solid var(--ring); border-left: 3px solid var(--accent);
  border-radius: 8px; padding: 10px 14px; max-width: none; margin: 0 0 8px; }
footer { margin-top: 28px; color: var(--ink-3); font-size: 12.5px; }
#tip { position: fixed; display: none; max-width: 280px; padding: 6px 10px; background: var(--ink); color: var(--page);
  border-radius: 6px; font-size: 12.5px; pointer-events: none; z-index: 10; }
@media (prefers-reduced-motion: no-preference) { .tile, .card { transition: border-color 120ms ease; } }
"""

HEAT_RULES = "\n".join(
    "td.c%d { background: var(--h%d-bg); color: var(--h%d-ink); }" % (i, i, i)
    for i in range(1, 11))

TIP_JS = """
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


def pretty(date_iso):
    d = parse_date(date_iso)
    return d.strftime("%a %e %b").replace("  ", " ")


def render_rows(rows, entry_signal):
    if not rows:
        return ""
    out = []
    for r in rows:
        dim = ' class="empty"' if r["joined"] == 0 else ""
        approx = '<span class="approx" title="Joined before tracking started; week timing is approximate">&asymp;</span> ' if r["approx"] else ""
        if not entry_signal:
            cells = '<td class="c0"></td>' * len(r["weeks"])
            out.append(
                '<tr%s><th scope="row">%s%s</th><td class="num joined">%d</td>%s'
                '<td class="num muted">&ndash;</td><td class="num muted">&ndash;</td></tr>'
                % (dim, approx, pretty(r["date"]), r["joined"], cells))
            continue
        pct = ("%d%%" % round(100 * r["entered"] / r["joined"])) if r["joined"] else "&ndash;"
        cells = []
        for i, c in enumerate(r["weeks"]):
            if r["joined"] == 0 or not c["started"]:
                cells.append('<td class="c0"></td>')
                continue
            cls = heat_class(c["n"], r["joined"])
            open_cls = "" if c["closed"] else " open"
            share = "%d%%" % round(100 * c["n"] / r["joined"])
            state = "" if c["closed"] else " &middot; week still running"
            tip = "%s &middot; by end of week %d: %d of %d entered (%s)%s" % (
                pretty(r["date"]), i + 1, c["n"], r["joined"], share, state)
            cells.append('<td class="%s%s" data-tip="%s">%d</td>' % (cls, open_cls, tip, c["n"]))
        out.append(
            '<tr%s><th scope="row">%s%s</th><td class="num joined">%d</td>%s'
            '<td class="num">%d</td><td class="num muted">%s</td></tr>'
            % (dim, approx, pretty(r["date"]), r["joined"], "".join(cells), r["entered"], pct))
    return "\n".join(out)


def build_body(meta, totals, rows):
    week_heads = "".join("<th>W%d</th>" % w for w in range(1, WEEKS + 1))
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
                        '<div class="tile"><div class="k">Not yet entered</div><div class="v">%s</div></div>'
                        % (totals["entered"], pct_total, totals["not_entered"]))
        notice = ""
        legend = """<div class="legend">
      <span class="ramp" aria-hidden="true"></span>
      <span>share of the day&rsquo;s cohort that has entered &mdash; 0&thinsp;&ndash;&thinsp;100%</span>
      <span class="sep"></span>
      <span><span class="chip open-demo"></span> week still running</span>
      <span class="sep"></span>
      <span>&asymp; joined before tracking began &mdash; week timing approximate, totals exact</span>
    </div>"""
    else:
        entered_tile = ('<div class="tile"><div class="k">Entered the community</div>'
                        '<div class="v">&ndash;</div><div class="k">no entry signal yet</div></div>'
                        '<div class="tile"><div class="k">Not yet entered</div>'
                        '<div class="v">&ndash;</div><div class="k">no entry signal yet</div></div>')
        notice = ('<p class="note notice">The joined-per-day numbers are live from the Mighty Networks API. '
                  'The <em>entered / week</em> columns are waiting on an entry signal: the Admin API does not '
                  'expose a member&rsquo;s last visit yet (confirmed against the live member object). '
                  'They will fill in automatically the day that signal is connected.</p>')
        legend = ""

    return """
  <section class="tiles">
    <div class="tile"><div class="k">New members &middot; last %(days)s days</div><div class="v">%(joined)s</div></div>
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

  <p class="note">Week columns are cumulative: <em>W3</em> means &ldquo;had entered by the end of
  their third week&rdquo;. Each day&rsquo;s snapshot pins down who entered since the day before,
  so these columns get more precise every single day the tracker runs.</p>""" % {
        "days": meta["lookback_days"],
        "joined": totals["joined"],
        "entered_tile": entered_tile,
        "notice": notice,
        "snaps": meta["snapshot_count"],
        "first": pretty(meta["first_snapshot"]),
        "week_heads": week_heads,
        "rows": render_rows(rows, entry_signal),
        "legend": legend,
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


def render_page(meta, totals, rows, mode):
    badge = ('<span class="asof">Data as of %s</span>' % pretty(meta["as_of"])) if meta["as_of"] else \
            '<span class="asof">Not connected yet</span>'
    content = (CONTENT_SHELL
               .replace("__BADGE__", badge)
               .replace("__BODY__", build_body(meta, totals, rows))
               .replace("__GENERATED__", meta["generated_at"].replace("T", " ")[:16])
               .replace("__JS__", TIP_JS))
    shell = ARTIFACT_SHELL if mode == "artifact" else DOC_SHELL
    return shell.replace("__CSS__", build_css(mode)).replace("__CONTENT__", content)


if __name__ == "__main__":
    main()
