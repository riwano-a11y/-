#!/usr/bin/env python3
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path

SITEMAP_URL = "https://shinra-portal.com/sitemap-shops-1.xml"
BPLUS_URL = "https://www.business-plus.net/interview/"
BSTIMES_URL = "https://bs-times.com/"
TOKORO_API_URL = (
    "https://tokoro-map.com/wp-json/business-directory/v1/shops?per_page=100&page=1"
)
ISANA_API_URL = "https://isana-listing.com/wp-json/business-directory/v1/shops"
ISANA_STATE_FILE = Path(os.environ.get("ISANA_STATE_FILE", "/var/lib/shinra-listing-monitor/seen-isana.txt"))
WEBHOOK_URL = os.environ["SLACK_WEBHOOK_URL"]
MEDIA_WEBHOOK_URL = os.environ.get("MEDIA_SLACK_WEBHOOK_URL", WEBHOOK_URL)
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
TOKORO_STATE_FILE = Path(os.environ.get(
    "TOKORO_STATE_FILE",
    "/var/lib/shinra-listing-monitor/seen-tokoro.txt",
))
IDENTITY_STATE_FILE = Path(os.environ.get(
    "IDENTITY_STATE_FILE",
    "/var/lib/shinra-listing-monitor/seen-business-identities.txt",
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


def current_tokoro_items():
    data = json.loads(fetch(TOKORO_API_URL).decode("utf-8", errors="replace"))
    items = []
    for shop in data:
        url = str(shop.get("permalink") or "").strip()
        if not url:
            continue
        items.append({
            "url": url,
            "name": str(shop.get("name") or "").strip() or "名称不明",
            "phone": str(shop.get("phone") or "").strip() or "記載なし",
        })
    return items


def identity_keys(name, phone):
    keys = set()
    digits = "".join(character for character in str(phone) if character.isdigit())
    if len(digits) >= 8:
        keys.add(f"phone:{digits}")
    normalized_name = unicodedata.normalize("NFKC", str(name)).casefold()
    normalized_name = "".join(
        character for character in normalized_name if character.isalnum()
    )
    if normalized_name:
        keys.add(f"name:{normalized_name}")
    return keys


def seed_identity_baseline(tokoro_items):
    identities = set()
    for item in tokoro_items:
        identities.update(identity_keys(item["name"], item["phone"]))

    shinra_urls = current_shinra_urls()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(shinra_details, url): url for url in shinra_urls}
        for future in as_completed(futures):
            try:
                name, phone = future.result()
                identities.update(identity_keys(name, phone))
            except Exception as error:
                print(f"Identity baseline error: {error}", file=sys.stderr, flush=True)

    save_seen(IDENTITY_STATE_FILE, identities)
    print(
        f"Cross-site identity baseline saved: {len(identities)} keys",
        flush=True,
    )
    return identities


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


def post_slack(text, webhook_url=WEBHOOK_URL):
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status >= 300:
            raise RuntimeError(f"Slack returned HTTP {response.status}")


def send_shinra(name, phone, url):
    if phone != "記載なし":
        phone = "".join(character for character in phone if character.isdigit()) or "記載なし"
    post_slack(
        "新しくお店もしくは会社が掲載されました！\n"
        f"店舗・会社名：{name}\n"
        f"電話番号：{phone}\n"
        f"掲載ページ：{url}"
    )


def plain_lines(page):
    page = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", page,
                  flags=re.I | re.S)
    page = re.sub(r"<br\s*/?>|</(?:p|div|tr|td|th|h[1-6]|li|dt|dd)>",
                  "\n", page, flags=re.I)
    return [line.strip() for line in html.unescape(
        re.sub(r"<[^>]+>", "", page)).splitlines() if line.strip()]


def contact_phone(page):
    normalized = unicodedata.normalize("NFKC", page)
    # Prefer an explicitly labelled telephone; never treat FAX as a phone.
    text = "\n".join(plain_lines(normalized))
    match = re.search(r"(?:TEL(?:EPHONE)?|電話(?:番号)?)\s*[.:：]?\s*"
                      r"(0[0-9()（） \t\-‐‑–—ー−]{8,22})", text, re.I)
    if not match:
        match = re.search(r'href=["\']tel:([^"\']+)', normalized, re.I)
    if match:
        digits = re.sub(r"\D", "", match.group(1))
        if digits.startswith("81"):
            digits = "0" + digits[2:]
        if digits.startswith("0") and 10 <= len(digits) <= 11:
            return digits
    return "記載なし"


def media_details(url, source):
    encoding = "utf-8" if source == "bplus" else "cp932"
    page = fetch(url).decode(encoding, errors="replace")
    if source == "bplus":
        # Company Data is usually on the last interview page.
        pages = [int(n) for n in re.findall(r"[?&](?:amp;)?page=(\d+)", page)]
        if pages:
            page = fetch(url + "?page=" + str(max(pages))).decode(
                encoding, errors="replace")
        start = page.find("Company Data")
        if start < 0:
            return "記載なし", "記載なし", "記載なし"
        section = page[start:]
        section = re.split(r"<!--\s*/siegeTextBox\s*-->|</article>|</table>",
                           section, maxsplit=1, flags=re.I)[0]
        lines = plain_lines(section)
        lines = [line for line in lines if "Company Data" not in line]
        name = lines[0] if lines else "記載なし"
    else:
        parts = re.split(r"(?:■〓){3,}■?", page)
        section = parts[-2] if len(parts) >= 3 else ""
        lines = plain_lines(section)
        name = lines[0] if lines else "記載なし"
    prefecture = r"(?:北海道|東京都|大阪府|京都府|.{2,3}県)"
    addresses = [line for line in lines if re.match(
        r"^(?:〒\s*\d{3}[-－]?\d{4}\s*)?" + prefecture, line)]
    address = " ／ ".join(addresses) or "記載なし"
    phone = contact_phone(section)
    # B+ often omits phone numbers. Consult only the linked official site.
    if phone == "記載なし":
        links = re.findall(r'href=["\'](https?://[^"\']+)', section, re.I)
        official = next((html.unescape(link) for link in links if
                        urllib.parse.urlsplit(link).hostname and
                        not urllib.parse.urlsplit(link).hostname.endswith(
                            "business-plus.net")), None)
        if official:
            try:
                raw = fetch(official)
                official_page = raw.decode("utf-8", errors="replace")
                phone = contact_phone(official_page)
                if phone == "記載なし":
                    candidates = []
                    for href, label in re.findall(
                        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                        official_page, re.I | re.S):
                        target = urllib.parse.urljoin(official, html.unescape(href))
                        if (urllib.parse.urlsplit(target).hostname ==
                                urllib.parse.urlsplit(official).hostname and
                                re.search(r"company|about|contact|会社概要|お問い合わせ",
                                          href + label, re.I) and
                                target not in candidates):
                            candidates.append(target)
                    for target in candidates[:2]:
                        phone = contact_phone(fetch(target).decode(
                            "utf-8", errors="replace"))
                        if phone != "記載なし":
                            break
            except Exception as error:
                print(f"Official contact lookup failed for {url}: {error}",
                      file=sys.stderr, flush=True)
    return name, phone, address


def send_media(source, url):
    name, phone, address = media_details(url, source)
    label = "B＋" if source == "bplus" else "B.S.TIMES"
    post_slack(
        f"{label}に新しい会社・店舗が掲載されました！\n"
        f"会社名：{name}\n"
        f"電話番号：{phone}\n"
        f"会社住所：{address}\n"
        f"URL：{url}",
        MEDIA_WEBHOOK_URL,
    )


def send_bplus(title, url):
    send_media("bplus", url)


def send_bstimes(title, url):
    send_media("bstimes", url)


def send_tokoro(name, phone, url):
    if phone != "記載なし":
        phone = "".join(character for character in phone if character.isdigit()) or "記載なし"
    post_slack(
        "トコロまっぷに新しいお店・会社が掲載されました！\n"
        f"店舗・会社名：{name}\n"
        f"電話番号：{phone}\n"
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
        identities = load_seen(IDENTITY_STATE_FILE)
        keys = identity_keys(name, phone)
        if keys and identities.intersection(keys):
            print(f"Shinra duplicate skipped: {name} ({url})", flush=True)
        else:
            send_shinra(name, phone, url)
            print(f"Shinra notified: {name} ({url})", flush=True)
        identities.update(keys)
        save_seen(IDENTITY_STATE_FILE, identities)
        seen.add(url)
        save_seen(STATE_FILE, seen)
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


def check_tokoro():
    items = current_tokoro_items()
    seen = load_seen(TOKORO_STATE_FILE)
    current_urls = {item["url"] for item in items}
    if not seen:
        save_seen(TOKORO_STATE_FILE, current_urls)
        if not IDENTITY_STATE_FILE.exists():
            seed_identity_baseline(items)
        print(f"Tokoro baseline saved: {len(items)} listings", flush=True)
        return

    identities = load_seen(IDENTITY_STATE_FILE)
    new_items = [item for item in items if item["url"] not in seen]
    for item in reversed(new_items):
        keys = identity_keys(item["name"], item["phone"])
        if keys and identities.intersection(keys):
            print(
                f"Tokoro duplicate skipped: {item['name']} ({item['url']})",
                flush=True,
            )
        else:
            send_tokoro(item["name"], item["phone"], item["url"])
            print(f"Tokoro notified: {item['name']} ({item['url']})", flush=True)
        identities.update(keys)
        save_seen(IDENTITY_STATE_FILE, identities)
        seen.add(item["url"])
        save_seen(TOKORO_STATE_FILE, seen)
    print(f"Tokoro check: {len(new_items)} new", flush=True)



def current_isana_items():
    # Read all pages so a burst larger than 100 cannot hide new listings.
    items = {}
    page = 1
    known = load_seen(ISANA_STATE_FILE)
    while True:
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        data = json.loads(fetch(ISANA_API_URL + "?" + query).decode("utf-8"))
        if not isinstance(data, list):
            raise RuntimeError("Isana returned an invalid listing response")
        added = 0
        for shop in data:
            url = str(shop.get("permalink") or "").strip()
            if not url or url in items:
                continue
            if urllib.parse.urlsplit(url).hostname != "isana-listing.com":
                raise RuntimeError("Isana returned an unexpected listing URL")
            items[url] = {
                "url": url,
                "name": html.unescape(str(shop.get("name") or "").strip()),
                "phone": str(shop.get("phone") or "").strip() or "記載なし",
                "address": "".join(str(shop.get(field) or "").strip()
                                   for field in ("stateIso", "city", "street", "street1"))
                           or "記載なし",
            }
            if not items[url]["name"]:
                raise RuntimeError("Isana listing has no business name")
            added += 1
        if len(data) < 100 or (known and all(
                str(shop.get("permalink") or "").strip() in known for shop in data)):
            return list(items.values())
        if not added:
            raise RuntimeError("Isana pagination did not advance")
        page += 1


def check_isana():
    items = current_isana_items()
    seen = load_seen(ISANA_STATE_FILE)
    identities = load_seen(IDENTITY_STATE_FILE)
    if not ISANA_STATE_FILE.exists():
        # Baseline is silent, and also prevents these existing businesses
        # being notified later from Shinra or Tokoro.
        for item in items:
            identities.update(identity_keys(item["name"], item["phone"]))
        save_seen(IDENTITY_STATE_FILE, identities)
        save_seen(ISANA_STATE_FILE, {item["url"] for item in items})
        print(f"Isana baseline saved: {len(items)} listings", flush=True)
        return
    for item in reversed(items):
        url = item["url"]
        if url in seen:
            continue
        keys = identity_keys(item["name"], item["phone"])
        if keys and identities.intersection(keys):
            print(f"Isana duplicate skipped: {item['name']} ({url})", flush=True)
        else:
            phone = re.sub(r"\D", "", unicodedata.normalize("NFKC", item["phone"])) or "記載なし"
            post_slack(
                "みせルートに新しいお店・会社が掲載されました！\n"
                f"店舗・会社名：{item['name']}\n"
                f"電話番号：{phone}\n"
                f"住所：{item['address']}\n"
                f"URL：{url}"
            )
            print(f"Isana notified: {item['name']} ({url})", flush=True)
        identities.update(keys)
        save_seen(IDENTITY_STATE_FILE, identities)
        seen.add(url)
        save_seen(ISANA_STATE_FILE, seen)


def check_once():
    for name, checker in (
        ("Shinra", check_shinra),
        ("B+", check_bplus),
        ("B.S.TIMES", check_bstimes),
        ("Tokoro", check_tokoro),
        ("Isana", check_isana),
    ):
        try:
            checker()
        except Exception as error:
            print(f"{name} monitor error: {error}", file=sys.stderr, flush=True)


def main():
    print(
        f"Starting Shinra + B+ + B.S.TIMES + Tokoro + Isana monitor every {INTERVAL_SECONDS} seconds",
        flush=True,
    )
    while True:
        started = time.monotonic()
        check_once()
        elapsed = time.monotonic() - started
        time.sleep(max(1, INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    main()

