#!/usr/bin/env python3
"""Decode JPRS WHOIS as EUC-JP and prefer Japanese registrant fields."""

from datetime import datetime
from pathlib import Path
import shutil
import sys

target = Path(sys.argv[1] if len(sys.argv) > 1 else "ip_monitor.py").resolve()
if not target.exists():
    raise SystemExit(f"Not found: {target}")

text = target.read_text(encoding="utf-8")
old_query = '        query = domain + "/e" if domain.endswith(".jp") else domain\n'
new_query = '        query = domain\n'

old_run = '''        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=12,
            check=False,
        )
        raw = result.stdout or ""
'''
new_run = '''        run_options = {
            "capture_output": True,
            "text": False,
            "timeout": 12,
            "check": False,
        }
        if domain.endswith(".jp"):
            run_options["env"] = {**os.environ, "LANG": "ja_JP.UTF-8"}
        result = subprocess.run(command, **run_options)
        encoding = "euc_jp" if domain.endswith(".jp") else "utf-8"
        raw = (result.stdout or b"").decode(encoding, errors="replace")
'''

if new_run in text:
    print("JPRS Japanese decoding is already installed.")
    raise SystemExit(0)
if old_query not in text or old_run not in text:
    raise SystemExit("Could not locate the direct WHOIS code; no changes made.")

text = text.replace(old_query, new_query, 1)
text = text.replace(old_run, new_run, 1)
compile(text, str(target), "exec")

timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = target.with_name(f"{target.name}.bak-jprs-ja-{timestamp}")
shutil.copy2(target, backup)
target.write_text(text, encoding="utf-8")

print(f"Patched: {target}")
print(f"Backup:  {backup}")
print("Japanese JPRS WHOIS fields are now preferred for .jp domains.")
