#!/usr/bin/env python3
"""Prevent overlapping ip_monitor.py runs."""

from datetime import datetime
from pathlib import Path
import shutil
import sys

target = Path(sys.argv[1] if len(sys.argv) > 1 else "ip_monitor.py").resolve()
if not target.exists():
    raise SystemExit(f"Not found: {target}")

text = target.read_text(encoding="utf-8")
marker = "_IP_MONITOR_LOCK_FILE = open(\"/tmp/ip-monitor.lock\", \"w\")"
if marker in text:
    print("Overlap lock already installed.")
    raise SystemExit(0)

lock_code = '''\n+import fcntl\n+\n+_IP_MONITOR_LOCK_FILE = open("/tmp/ip-monitor.lock", "w")\n+try:\n+    fcntl.flock(\n+        _IP_MONITOR_LOCK_FILE.fileno(),\n+        fcntl.LOCK_EX | fcntl.LOCK_NB,\n+    )\n+except BlockingIOError:\n+    raise SystemExit(0)\n+\n+'''

# Insert after a shebang when present, otherwise at the beginning.
if text.startswith("#!"):
    first_newline = text.find("\n") + 1
    text = text[:first_newline] + lock_code + text[first_newline:]
else:
    text = lock_code + text

compile(text, str(target), "exec")
timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
backup = target.with_name(f"{target.name}.bak-lock-{timestamp}")
shutil.copy2(target, backup)
target.write_text(text, encoding="utf-8")

print(f"Patched: {target}")
print(f"Backup:  {backup}")
print("Overlapping IP monitor runs are now blocked.")
