#!/usr/bin/env python3
"""Fetch the public Mighty Networks API documentation into docs/api-reference/.

Runs in a GitHub Action (the runner has open internet access). Standard
library only. It starts at the documentation root, follows links whose text or
URL looks API-related, and saves a plain-text extraction of each page so the
member query for scripts/api_config.json can be written from real docs.
"""

import html
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "docs", "api-reference")

START_URLS = [
    "https://docs.mightynetworks.com/llms-full.txt",
    "https://docs.mightynetworks.com/llms.txt",
    "https://docs.mightynetworks.com/admin-api",
    "https://docs.mightynetworks.com/admin-api/authentication",
    "https://docs.mightynetworks.com/admin-api/pagination",
]
KEYWORDS = ("admin-api", "api", "graphql", "headless", "developer", "token",
            "authentication", "member", "export", "webhook", "network")
ALLOWED_HOSTS = ("mightynetworks.com", "mn.co")
MAX_PAGES = 60
MAX_SAVE = 1_500_000  # bytes per saved page
MD_LINK_RE = re.compile(r"\((https?://[^)\s]+)\)")
UA = "Mozilla/5.0 (compatible; lesko-help-cohort-setup/1.0)"

LINK_RE = re.compile(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
ANYTAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\n{3,}")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return resp.geturl(), resp.read().decode("utf-8", "replace")
    except Exception as e:
        return url, "FETCH ERROR: %s" % e


def allowed(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS)


def to_text(page_html):
    cleaned = TAG_RE.sub(" ", page_html)
    cleaned = cleaned.replace("</p>", "\n").replace("</div>", "\n").replace("<br", "\n<br")
    cleaned = cleaned.replace("</li>", "\n").replace("</h1>", "\n").replace("</h2>", "\n")
    cleaned = cleaned.replace("</h3>", "\n").replace("</pre>", "\n").replace("</code>", " ")
    cleaned = ANYTAG_RE.sub(" ", cleaned)
    cleaned = html.unescape(cleaned)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in cleaned.splitlines()]
    return WS_RE.sub("\n\n", "\n".join(ln for ln in lines if ln))


def slug(url):
    s = re.sub(r"^https?://", "", url)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-")
    return s[:90] or "page"


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    queue = list(START_URLS)
    seen = set()
    index = []

    while queue and len(seen) < MAX_PAGES:
        url = queue.pop(0)
        if url in seen or not allowed(url):
            continue
        seen.add(url)
        final_url, body = fetch(url)
        is_plain = final_url.endswith((".txt", ".md")) or "<html" not in body[:2000].lower()
        if body.startswith("FETCH ERROR"):
            text = body
        elif is_plain:
            text = body
        else:
            text = to_text(body)
        text = text[:MAX_SAVE]
        fname = slug(final_url) + ".txt"
        with open(os.path.join(OUT_DIR, fname), "w") as fh:
            fh.write("SOURCE: %s\nFETCHED: %s\n\n%s" % (final_url, time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), text))
        index.append((final_url, fname, text[:160].replace("\n", " ")))
        print("saved %s <- %s" % (fname, final_url))

        if not body.startswith("FETCH ERROR"):
            candidates = []
            for href, label in LINK_RE.findall(body):
                candidates.append((urllib.parse.urljoin(final_url, html.unescape(href)),
                                   ANYTAG_RE.sub("", label)))
            for href in MD_LINK_RE.findall(body):
                candidates.append((urllib.parse.urljoin(final_url, href), ""))
            for target, label in candidates:
                blob = (target + " " + label).lower()
                if allowed(target) and any(k in blob for k in KEYWORDS) and target not in seen:
                    # API reference pages first, help-center articles after.
                    if "/admin-api" in target or "/headless-api" in target or "llms" in target:
                        queue.insert(0, target)
                    else:
                        queue.append(target)
        time.sleep(0.5)

    with open(os.path.join(OUT_DIR, "INDEX.md"), "w") as fh:
        fh.write("# Fetched Mighty Networks documentation pages\n\n")
        for url, fname, snippet in index:
            fh.write("- [%s](%s) - %s\n" % (fname, url, snippet))
    print("Fetched %d pages." % len(index))


if __name__ == "__main__":
    main()
