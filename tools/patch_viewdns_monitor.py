#!/usr/bin/env python3
"""Add throttled ViewDNS reverse-IP monitoring to ip_monitor.py."""

from datetime import datetime
from pathlib import Path
import shutil
import sys

target = Path(sys.argv[1] if len(sys.argv) > 1 else "ip_monitor.py").resolve()
if not target.exists():
    raise SystemExit(f"Not found: {target}")

text = target.read_text(encoding="utf-8")
if "def get_viewdns_domains(" in text:
    print("ViewDNS monitoring is already installed.")
    raise SystemExit(0)

function = r'''
def get_viewdns_domains(ip):
    """Return reverse-IP domains, None when ViewDNS is unavailable."""
    try:
        with open("/root/ip-monitor/viewdns.key", encoding="utf-8") as key_file:
            api_key = key_file.read().strip()
        if not api_key:
            raise ValueError("ViewDNS API key is empty")
        response = requests.get(
            "https://api.viewdns.info/reverseip/",
            params={"host": ip, "apikey": api_key, "output": "json"},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("response", {}).get("domains", [])
        return sorted({
            row.get("name", "").lower().strip().removeprefix("www.")
            for row in rows
            if isinstance(row, dict) and row.get("name")
        })
    except (OSError, ValueError, requests.RequestException) as error:
        print(f"ViewDNS error for {ip}: {error}", flush=True)
        return None


'''

main_marker = "def main():\n"
if main_marker not in text:
    raise SystemExit("Could not locate main().")
text = text.replace(main_marker, function + main_marker, 1)

loop_marker = "    for ip in IPS:\n"
state_code = (
    '    if "viewdns_seen" not in state:\n'
    '        state["viewdns_seen"] = {}\n'
    '    if "viewdns_last_check" not in state:\n'
    '        state["viewdns_last_check"] = {}\n\n'
)
if loop_marker not in text:
    raise SystemExit("Could not locate the IP loop.")
text = text.replace(loop_marker, state_code + loop_marker, 1)

domains_marker = "        domains = get_resolutions(ip)\n"
domains_code = r'''        new_viewdns_domains = []
        now = int(__import__("time").time())
        last_check = int(state["viewdns_last_check"].get(ip, 0))
        if ip == "18.179.211.152" and now - last_check >= 3600:
            viewdns_domains = get_viewdns_domains(ip)
            if viewdns_domains is not None:
                previous = set(state["viewdns_seen"].get(ip, []))
                if ip in state["viewdns_seen"]:
                    new_viewdns_domains = [
                        domain for domain in viewdns_domains if domain not in previous
                    ]
                state["viewdns_seen"][ip] = viewdns_domains
                state["viewdns_last_check"][ip] = now
                save_state(state)

        domains = sorted(set(get_resolutions(ip)) | set(new_viewdns_domains))
'''
if domains_marker not in text:
    raise SystemExit("Could not locate the VirusTotal domain lookup.")
text = text.replace(domains_marker, domains_code, 1)

compile(text, str(target), "exec")
timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = target.with_name(f"{target.name}.bak-viewdns-{timestamp}")
shutil.copy2(target, backup)
target.write_text(text, encoding="utf-8")

print(f"Patched: {target}")
print(f"Backup:  {backup}")
print("ViewDNS monitoring added for 18.179.211.152 (hourly, first run is baseline).")
