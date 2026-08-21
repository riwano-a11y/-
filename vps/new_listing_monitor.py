#!/usr/bin/env python3
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SITEMAP_URL = "https://shinra-portal.com/sitemap-shops-1.xml"
WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "30"))
STATE_FILE = Path(os.environ.get(
    "STATE_FILE",
    "/var/lib/shinra-listing-monitor/seen-shops.txt",
))
USER_AGENT = "ShinraPortalVpsListingMonitor/1.0"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def current_urls():
    root = ET.fromstring(fetch(SITEMAP_URL))
    return [
        element.text.strip()
        for element in root.findall(
            ".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc"
        )
        if element.text and "/shops/" in element.text
    ]


def listing_details(url):
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


def send_slack(name, phone, url):
    text = (
        "新しくお店もしくは会社が掲載されました！\n"
        f"店舗・会社名：{name}\n"
        f"電話番号：{phone}\n"
        f"掲載ページ：{url}"
    )
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


def load_seen():
    if not STATE_FILE.exists():
        return set()
    return {
        line.strip()
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def save_seen(seen):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text("\n".join(sorted(seen)) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


def check_once():
    urls = current_urls()
    seen = load_seen()

    if not seen:
        save_seen(set(urls))
        print(f"Initial baseline saved: {len(urls)} listings", flush=True)
        return

    new_urls = [url for url in urls if url not in seen]
    for url in reversed(new_urls):
        name, phone = listing_details(url)
        send_slack(name, phone, url)
        seen.add(url)
        save_seen(seen)
        print(f"Notified: {name} ({url})", flush=True)

    print(f"Check complete: {len(new_urls)} new listing(s)", flush=True)


def main():
    print(f"Starting monitor every {INTERVAL_SECONDS} seconds", flush=True)
    while True:
        started = time.monotonic()
        try:
            check_once()
        except Exception as error:
            print(f"Monitor error: {error}", file=sys.stderr, flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(1, INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
