#!/usr/bin/env python3
import html
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SITEMAP_URL = "https://shinra-portal.com/sitemap-shops-1.xml"
SEEN_PATH = Path(".monitor/seen-shops.txt")
NEW_PATH = Path(".monitor/new-shops.json")
USER_AGENT = "ShinraPortalNewListingMonitor/1.0 (+GitHub Actions)"


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


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
            name = str(data.get("name") or "").strip()
            phone = str(data.get("telephone") or "").strip()
            return {
                "name": name or url.rstrip("/").rsplit("/", 1)[-1],
                "phone": phone or "記載なし",
                "url": url,
            }
    except Exception as error:
        print(f"Could not read listing details for {url}: {error}")
    return {
        "name": url.rstrip("/").rsplit("/", 1)[-1],
        "phone": "記載なし",
        "url": url,
    }


root = ET.fromstring(fetch(SITEMAP_URL))
current_urls = [
    element.text.strip()
    for element in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
    if element.text and "/shops/" in element.text
]

seen = set()
if SEEN_PATH.exists():
    seen = {line.strip() for line in SEEN_PATH.read_text(encoding="utf-8").splitlines() if line.strip()}

new_urls = [url for url in current_urls if url not in seen]
new_listings = [listing_details(url) for url in new_urls]

SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
SEEN_PATH.write_text("\n".join(sorted(seen | set(current_urls))) + "\n", encoding="utf-8")
NEW_PATH.write_text(json.dumps(new_listings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

output_path = os.environ.get("GITHUB_OUTPUT")
if output_path:
    with open(output_path, "a", encoding="utf-8") as output:
        output.write(f"count={len(new_listings)}\n")

print(f"New listings: {len(new_listings)}")
