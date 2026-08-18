#!/usr/bin/env python3
"""Fetch today's member snapshot for Lesko Help from the Mighty Networks API.

Runs inside the daily GitHub Action. Standard library only.

Two modes:

* Configured mode - when scripts/api_config.json exists, fetch the full member
  list and write one snapshot file: data/snapshots/YYYY-MM-DD.csv
  (columns: member_id, join_date, last_visited, checklist_completed).

* Discovery mode - while api_config.json does not exist yet, probe a short list
  of candidate API endpoints with the token and write a sanitized report to
  data/api_discovery/report.json so the exact endpoint/query can be pinned
  down from the committed output. The report never contains the token, and
  anything that looks like an e-mail address is redacted.

Privacy rule for this repository: snapshots contain only the numeric member id
and dates - never names or e-mail addresses.
"""

import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(REPO_ROOT, "scripts", "api_config.json")
SNAPSHOT_DIR = os.path.join(REPO_ROOT, "data", "snapshots")
DISCOVERY_DIR = os.path.join(REPO_ROOT, "data", "api_discovery")

NETWORK_HOST = "lesko-help-2.mn.co"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

SNAPSHOT_FIELDS = ["member_id", "join_date", "last_visited", "checklist_completed"]


def token():
    t = os.environ.get("MIGHTY_API_TOKEN", "").strip()
    if not t:
        print("ERROR: MIGHTY_API_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)
    return t


def http(url, method="GET", headers=None, body=None, timeout=30):
    """Return (status, content_type, text). Never raises on HTTP errors."""
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    else:
        data = None
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", text
    except Exception as e:  # DNS failure, timeout, TLS ...
        return 0, "", "TRANSPORT ERROR: %s" % e


def dig(obj, path):
    """Walk a dotted path ("data.network.members") through dicts."""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def norm_date(value):
    """Normalize an API date/datetime value to YYYY-MM-DD ('' when absent)."""
    if value in (None, "", False):
        return ""
    s = str(value).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    # Numeric epoch (seconds or milliseconds)
    if re.fullmatch(r"\d{10,13}", s):
        secs = int(s[:10])
        return dt.datetime.utcfromtimestamp(secs).strftime("%Y-%m-%d")
    return ""


# ---------------------------------------------------------------- configured

def fetch_configured(cfg, tok):
    kind = cfg.get("kind", "graphql")
    auth_name, auth_tpl = cfg["auth_header"]
    headers = {
        auth_name: auth_tpl.replace("{token}", tok),
        "Accept": "application/json",
        "User-Agent": "lesko-help-cohort-snapshot/1.0",
    }
    page_size = int(cfg.get("page_size", 200))
    max_pages = int(cfg.get("max_pages", 500))
    rows = []

    if cfg.get("preflight_url"):
        status, _, text = http(cfg["preflight_url"], "GET", headers)
        if status == 200:
            try:
                me = json.loads(text)
                safe = {k: me.get(k) for k in ("network_id", "role") if k in me}
            except Exception:
                safe = {}
            print("Preflight OK: token accepted%s" %
                  (" (%s)" % json.dumps(safe) if safe else ""))
        else:
            print("ERROR: preflight %s returned HTTP %s: %s\n"
                  "Check that the API token is valid and the network id in "
                  "scripts/api_config.json is correct."
                  % (cfg["preflight_url"], status, text[:300]), file=sys.stderr)
            sys.exit(1)

    if kind == "graphql":
        cursor = None
        for _ in range(max_pages):
            variables = {"first": page_size, "after": cursor}
            variables.update(cfg.get("extra_variables", {}))
            status, _, text = http(cfg["url"], "POST", headers, {"query": cfg["query"], "variables": variables})
            if status != 200:
                print("ERROR: %s returned HTTP %s: %s" % (cfg["url"], status, text[:300]), file=sys.stderr)
                sys.exit(1)
            payload = json.loads(text)
            if payload.get("errors"):
                print("ERROR: GraphQL errors: %s" % json.dumps(payload["errors"])[:500], file=sys.stderr)
                sys.exit(1)
            conn = dig(payload, cfg["connection_path"])
            if conn is None:
                print("ERROR: connection_path %r not found in response." % cfg["connection_path"], file=sys.stderr)
                sys.exit(1)
            edges = conn.get("edges") or []
            for edge in edges:
                node = edge.get("node") if isinstance(edge, dict) else None
                if node:
                    rows.append(extract_row(node, cfg["node_fields"]))
            page = conn.get("pageInfo") or {}
            if page.get("hasNextPage") and page.get("endCursor"):
                cursor = page["endCursor"]
                time.sleep(float(cfg.get("page_delay", 0.3)))
            else:
                break
    elif kind == "rest":
        url = cfg["url"].replace("{page}", "1").replace("{per_page}", str(page_size))
        logged_keys = False
        for page_no in range(1, max_pages + 1):
            status, _, text = http(url, "GET", headers)
            if status != 200:
                print("ERROR: %s returned HTTP %s: %s" % (url, status, text[:300]), file=sys.stderr)
                sys.exit(1)
            payload = json.loads(text)
            items = rest_items(payload, cfg.get("items_path"))
            if items is None:
                print("ERROR: could not locate the member list in the response. "
                      "Top-level keys: %s" % sorted(payload.keys() if isinstance(payload, dict) else []),
                      file=sys.stderr)
                sys.exit(1)
            if not items:
                break
            if not logged_keys and isinstance(items[0], dict):
                print("Member object keys: %s" % sorted(items[0].keys()))
                logged_keys = True
            for node in items:
                rows.append(extract_row(node, cfg["node_fields"]))
            next_url = dig(payload, "links.next") if isinstance(payload, dict) else None
            meta = payload.get("meta") if isinstance(payload, dict) else None
            if next_url:
                if not str(next_url).startswith("http"):
                    next_url = urllib.parse.urljoin(cfg["url"], str(next_url))
                url = next_url
            elif meta and meta.get("total_pages") and page_no >= int(meta["total_pages"]):
                break
            elif len(items) < page_size:
                break
            else:
                url = cfg["url"].replace("{page}", str(page_no + 1)).replace("{per_page}", str(page_size))
            time.sleep(float(cfg.get("page_delay", 0.3)))
    else:
        print("ERROR: unknown kind %r in api_config.json" % kind, file=sys.stderr)
        sys.exit(1)

    return rows


def rest_items(payload, items_path):
    """Find the list of member objects in a REST response, tolerantly."""
    if items_path:
        found = dig(payload, items_path)
        if isinstance(found, list):
            return found
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "data", "members", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return None


def extract_row(node, node_fields):
    """node_fields values may be a single source path or a list of candidate
    paths - the first one present in the node wins."""
    row = {}
    for out_name, src in node_fields.items():
        candidates = src if isinstance(src, list) else [src]
        value = None
        for path in candidates:
            value = dig(node, path) if "." in path else node.get(path)
            if value is not None:
                break
        row[out_name] = value
    member_id = row.get("member_id")
    if isinstance(member_id, str) and member_id.startswith("gid://"):
        member_id = member_id.rsplit("/", 1)[-1]
    checklist = row.get("checklist_completed")
    return {
        "member_id": "" if member_id is None else str(member_id),
        "join_date": norm_date(row.get("join_date")),
        "last_visited": norm_date(row.get("last_visited")),
        "checklist_completed": "" if checklist is None else str(bool(checklist)).lower(),
    }


def write_snapshot(rows):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    path = os.path.join(SNAPSHOT_DIR, "%s.csv" % today)
    seen = set()
    unique = []
    for r in rows:
        if r["member_id"] and r["member_id"] not in seen:
            seen.add(r["member_id"])
            unique.append(r)
    unique.sort(key=lambda r: (r["join_date"], r["member_id"]))
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SNAPSHOT_FIELDS)
        writer.writeheader()
        writer.writerows(unique)
    print("Wrote %s member rows to %s" % (len(unique), os.path.relpath(path, REPO_ROOT)))
    if not unique:
        print("ERROR: snapshot came back empty - refusing to keep an empty file.", file=sys.stderr)
        os.remove(path)
        sys.exit(1)


# ----------------------------------------------------------------- discovery

INTROSPECT_ROOT = "{ __schema { queryType { name fields { name } } } }"
INTROSPECT_TYPE = '{ __type(name: "%s") { name fields { name type { name kind ofType { name } } } } }'
TYPE_GUESSES = ["Member", "User", "NetworkMember", "CommunityMember", "Person"]

GRAPHQL_CANDIDATES = [
    "https://%s/api/graphql" % NETWORK_HOST,
    "https://%s/graphql" % NETWORK_HOST,
    "https://%s/api/v1/graphql" % NETWORK_HOST,
    "https://api.mightynetworks.com/graphql",
]
REST_CANDIDATES = [
    "https://%s/api/v1/members?per_page=3" % NETWORK_HOST,
    "https://%s/api/admin/members?per_page=3" % NETWORK_HOST,
    "https://api.mightynetworks.com/v1/members?per_page=3",
]
AUTH_STYLES = [
    ("bearer", "Authorization", "Bearer {token}"),
    ("token", "Authorization", "Token token={token}"),
    ("x-api-key", "X-Api-Key", "{token}"),
]


def sanitize(text, tok):
    text = text.replace(tok, "[TOKEN]")
    text = EMAIL_RE.sub("[email]", text)
    return text[:600]


def discover(tok):
    os.makedirs(DISCOVERY_DIR, exist_ok=True)
    attempts = []
    found = None

    for url in GRAPHQL_CANDIDATES:
        for style, hname, htpl in AUTH_STYLES:
            headers = {hname: htpl.replace("{token}", tok),
                       "Accept": "application/json",
                       "User-Agent": "lesko-help-cohort-snapshot/1.0"}
            status, ctype, text = http(url, "POST", headers, {"query": INTROSPECT_ROOT}, timeout=20)
            entry = {"kind": "graphql", "url": url, "auth": style, "status": status,
                     "content_type": ctype, "body": sanitize(text, tok)}
            ok = False
            if status == 200:
                try:
                    payload = json.loads(text)
                    fields = dig(payload, "data.__schema.queryType") or {}
                    names = [f["name"] for f in fields.get("fields", [])]
                    if names:
                        ok = True
                        entry["query_fields"] = names
                except Exception:
                    pass
            attempts.append(entry)
            if ok and not found:
                found = {"url": url, "auth": (hname, htpl), "style": style}
                types = {}
                for guess in TYPE_GUESSES:
                    s2, _, t2 = http(url, "POST", headers, {"query": INTROSPECT_TYPE % guess}, timeout=20)
                    if s2 == 200:
                        try:
                            info = dig(json.loads(t2), "data.__type")
                            if info:
                                types[guess] = [f["name"] for f in info.get("fields", [])]
                        except Exception:
                            pass
                entry["member_type_fields"] = types
            if found:
                break
        if found:
            break

    if not found:
        for url in REST_CANDIDATES:
            for style, hname, htpl in AUTH_STYLES:
                headers = {hname: htpl.replace("{token}", tok),
                           "Accept": "application/json",
                           "User-Agent": "lesko-help-cohort-snapshot/1.0"}
                status, ctype, text = http(url, "GET", headers, timeout=20)
                attempts.append({"kind": "rest", "url": url, "auth": style, "status": status,
                                 "content_type": ctype, "body": sanitize(text, tok)})

    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "note": "Sanitized API discovery report. Token and e-mail addresses are redacted.",
        "working_endpoint": {"url": found["url"], "auth_style": found["style"]} if found else None,
        "attempts": attempts,
    }
    with open(os.path.join(DISCOVERY_DIR, "report.json"), "w") as fh:
        json.dump(report, fh, indent=2)
    print("Discovery report written to data/api_discovery/report.json")
    if found:
        print("A GraphQL endpoint answered introspection: %s (auth: %s)" % (found["url"], found["style"]))
        print("Next step: pin the member query in scripts/api_config.json.")
    else:
        print("No candidate endpoint accepted the token yet - see the report for responses.")


def main():
    tok = token()
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as fh:
            cfg = json.load(fh)
        rows = fetch_configured(cfg, tok)
        write_snapshot(rows)
    else:
        print("scripts/api_config.json not found - running API discovery instead.")
        discover(tok)


if __name__ == "__main__":
    main()
