#!/usr/bin/env python3
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

SITEMAP_URL = "https://shinra-portal.com/sitemap-shops-1.xml"
BPLUS_URL = "https://www.business-plus.net/interview/"
BSTIMES_URL = "https://bs-times.com/"
WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "30"))
STATE_FILE = Path(os.environ.get(
    "STATE_FILE",
    "/var/lib/shinra-listing-monitor/seen-shops.txt",
))
BPLUS_STATE_FILE = Path(os.environ.get(
    "BPLUS_STATE_FILE",
    "/var/lib/shinra-listing-monitor/seen-bplus.txt",
))
BSTIMES_STATE_FILE = Path(os.environ.get(
    "BSTIMES_STATE_FILE",
    "/var/lib/shinra-listing-monitor/seen-bstimes.txt",
))
USER_AGENT = "ListingMonitor/2.0"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def current_shinra_urls():
    root = ET.fromstring(fetch(SITEMAP_URL))
    return [
        element.text.strip()
        for element in root.findall(
            ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
        )
        if element.text and "/shops/" in element.text
    ]


class BPlusParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.current_url = None
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href", "")
        absolute = urllib.parse.urljoin(BPLUS_URL, href)
        path = urllib.parse.urlsplit(absolute).path
        if re.fullmatch(r"/interview/\d{4}/[^/]+\.(?:html|shtml)", path):
            self.current_url = urllib.parse.urlunsplit(
                ("https", "www.business-plus.net", path, "", "")
            )
            self.current_text = []

    def handle_data(self, data):
        if self.current_url:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current_url:
            title = " ".join("".join(self.current_text).split())
            if title:
                self.items.append((self.current_url, title))
            self.current_url = None
            self.current_text = []


def current_bplus_items():
    parser = BPlusParser()
    parser.feed(fetch(BPLUS_URL).decode("utf-8", errors="replace"))
    unique = {}
    for url, title in parser.items:
        unique.setdefault(url, title)
    return list(unique.items())


def current_bstimes_items():
    home = fetch(BSTIMES_URL).decode("cp932", errors="replace")
    volumes = [int(value) for value in re.findall(r"vol(\d+)/news\.cgi", home)]
    if not volumes:
        raise RuntimeError("B.S.TIMES latest volume was not found")
    volume = max(volumes)
    issue_url = f"{BSTIMES_URL}vol{volume}/news.cgi"
    page = fetch(issue_url).decode("cp932", errors="replace")
    unique = {}
    blocks = re.findall(
        r'<table\b[^>]*class=["\']table01["\'][^>]*>.*?</table>',
        page,
        re.IGNORECASE | re.DOTALL,
    )
    for block in blocks:
        link = re.search(r'href=["\'](\d+\.html)["\']', block, re.IGNORECASE)
        heading = re.search(
            r'<span\b[^>]*class=["\']ttl01["\'][^>]*>(.*?)</span>',
            block,
            re.IGNORECASE | re.DOTALL,
        )
        if not link:
            continue
        url = urllib.parse.urljoin(issue_url, link.group(1))
        raw_title = heading.group(1) if heading else ""
        title = " ".join(re.sub(r"<[^>]+>", " ", raw_title).split())
        unique.setdefault(url, html.unescape(title) or f"B.S.TIMES Vol.{volume}")
    if not unique:
        raise RuntimeError(f"B.S.TIMES Vol.{volume} articles were not found")
    return list(unique.items())


def shinra_details(url):
    try:
        page = fetch(url).decode("utf-8", errors="replace")
        match = re.search(
            r'<script[^>]+id=["\']local-business-schema["\'][^>]*>(.*?)</script>',
            page,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            data = json.loads(html.unescape(match.group(1)))
            return (
                str(data.get("name") or "").strip()
                or url.rstrip("/").rsplit("/", 1)[-1],
                str(data.get("telephone") or "").strip() or "記載なし",
            )
    except Exception as error:
        print(f"Details error for {url}: {error}", file=sys.stderr, flush=True)
    return url.rstrip("/").rsplit("/", 1)[-1], "記載なし"


def post_slack(text):
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Slack returned HTTP {response.status}")


def send_shinra(name, phone, url):
    post_slack(
        "新しくお店もしくは会社が掲載されました！\n"
        f"店舗・会社名：{name}\n"
        f"電話番号：{phone}\n"
        f"掲載ページ：{url}"
    )


def send_bplus(title, url):
    post_slack(
        "B＋に新しい会社が掲載されました！\n"
        f"記事名：{title}\n"
        f"掲載ページ：{url}"
    )


def send_bstimes(title, url):
    post_slack(
        "B.S.TIMESに新しい会社・店舗が掲載されました！\n"
        f"記事名：{title}\n"
        f"掲載ページ：{url}"
    )


def load_seen(path):
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_seen(path, seen):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(sorted(seen)) + "\n", encoding="utf-8")
    temporary.replace(path)


def check_shinra():
    urls = current_shinra_urls()
    seen = load_seen(STATE_FILE)
    if not seen:
        save_seen(STATE_FILE, set(urls))
        print(f"Shinra baseline saved: {len(urls)} listings", flush=True)
        return
    new_urls = [url for url in urls if url not in seen]
    for url in reversed(new_urls):
        name, phone = shinra_details(url)
        send_shinra(name, phone, url)
        seen.add(url)
        save_seen(STATE_FILE, seen)
        print(f"Shinra notified: {name} ({url})", flush=True)
    print(f"Shinra check: {len(new_urls)} new", flush=True)


def check_bplus():
    items = current_bplus_items()
    seen = load_seen(BPLUS_STATE_FILE)
    current_urls = {url for url, _ in items}
    if not seen:
        save_seen(BPLUS_STATE_FILE, current_urls)
        print(f"B+ baseline saved: {len(items)} listings", flush=True)
        return
    new_items = [(url, title) for url, title in items if url not in seen]
    for url, title in reversed(new_items):
        send_bplus(title, url)
        seen.add(url)
        save_seen(BPLUS_STATE_FILE, seen)
        print(f"B+ notified: {title} ({url})", flush=True)
    print(f"B+ check: {len(new_items)} new", flush=True)


def check_bstimes():
    items = current_bstimes_items()
    seen = load_seen(BSTIMES_STATE_FILE)
    current_urls = {url for url, _ in items}
    if not seen:
        save_seen(BSTIMES_STATE_FILE, current_urls)
        print(f"B.S.TIMES baseline saved: {len(items)} listings", flush=True)
        return
    new_items = [(url, title) for url, title in items if url not in seen]
    for url, title in reversed(new_items):
        send_bstimes(title, url)
        seen.add(url)
        save_seen(BSTIMES_STATE_FILE, seen)
        print(f"B.S.TIMES notified: {title} ({url})", flush=True)
    print(f"B.S.TIMES check: {len(new_items)} new", flush=True)


def check_once():
    for name, checker in (
        ("Shinra", check_shinra),
        ("B+", check_bplus),
        ("B.S.TIMES", check_bstimes),
    ):
        try:
            checker()
        except Exception as error:
            print(f"{name} monitor error: {error}", file=sys.stderr, flush=True)


def main():
    print(
        f"Starting Shinra + B+ + B.S.TIMES monitor every {INTERVAL_SECONDS} seconds",
        flush=True,
    )
    while True:
        started = time.monotonic()
        check_once()
        elapsed = time.monotonic() - started
        time.sleep(max(1, INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
