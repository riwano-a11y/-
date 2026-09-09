#!/usr/bin/env python3
"""Replace blocked web WHOIS scraping with the system WHOIS client."""

from datetime import datetime
from pathlib import Path
import re
import shutil
import sys

target = Path(sys.argv[1] if len(sys.argv) > 1 else "ip_monitor.py").resolve()
if not target.exists():
    raise SystemExit(f"Not found: {target}")

text = target.read_text(encoding="utf-8")
start = text.find("def get_domain_whois_info(")
end = text.find("\ndef main()", start)
if start < 0 or end < 0:
    raise SystemExit("Could not locate get_domain_whois_info().")

function = r'''def get_domain_whois_info(domain, ip):
    """Look up public domain registrant details through WHOIS directly."""
    import subprocess

    missing = "記載なし"
    try:
        query = domain + "/e" if domain.endswith(".jp") else domain
        command = ["whois"]
        if domain.endswith(".jp"):
            command += ["-h", "whois.jprs.jp"]
        command.append(query)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            check=False,
        )
        raw = result.stdout or ""

        def values(*labels):
            found = []
            for line in raw.splitlines():
                clean = line.strip()
                lowered = clean.lower()
                for label in labels:
                    if lowered.startswith(label.lower()):
                        value = clean[len(label):].lstrip(" :\t]").strip()
                        if value and value.lower() not in {
                            "redacted for privacy", "redacted", "not disclosed",
                        }:
                            found.append(value)
                        break
            return found

        company_values = values(
            "[登録者名]", "[Registrant]", "[組織名]", "[Organization]",
            "Registrant Organization", "Registrant Name", "Organization",
        )
        phone_values = values(
            "[電話番号]", "[Phone]", "Registrant Phone", "Phone",
        )
        address_values = values(
            "[住所]", "[Postal Address]", "Registrant Street",
            "Registrant City", "Registrant State/Province",
            "Registrant Postal Code", "Registrant Country",
        )

        company = company_values[0] if company_values else missing
        raw_phone = phone_values[0] if phone_values else ""
        phone = "".join(char for char in raw_phone if char.isdigit())
        if phone.startswith("81") and len(phone) >= 10:
            phone = "0" + phone[2:]
        phone = phone or missing
        address = " ".join(dict.fromkeys(address_values)) or missing
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Direct domain WHOIS error for {domain}: {error}", flush=True)
        company = phone = address = missing

    return (
        f"会社名: {company}\n"
        f"電話番号: {phone}\n"
        f"住所: {address}"
    )


'''

text = text[:start] + function + text[end + 1:]

# The previous patch called WHOIS twice for live domains. Keep only the
# unconditional lookup immediately after is_live().
text = re.sub(
    r'(\n\s*if live_url:\s*\n)\s*whois_info\s*=\s*get_domain_whois_info\(domain, ip\)\s*\n',
    r'\1',
    text,
    count=1,
)

compile(text, str(target), "exec")
timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = target.with_name(f"{target.name}.bak-direct-whois-{timestamp}")
shutil.copy2(target, backup)
target.write_text(text, encoding="utf-8")

print(f"Patched: {target}")
print(f"Backup:  {backup}")
print("Domain WHOIS now uses the local WHOIS client directly.")
