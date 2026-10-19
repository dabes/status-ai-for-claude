#!/usr/bin/env python3
"""Claude Code statusLine command for AI Status: saves the plan usage limits Claude Code passes on stdin
(rate_limits.five_hour / seven_day: used_percentage, resets_at) to <config>/ai-status-usage.json and prints
"5h 6% · 7d 27%". No token and no network: it only uses what Claude Code already sends to the status line.

settings.json:  "statusLine": {"type": "command", "command": "python <path>/statusline.py"}
"""

import json
import os
import pathlib
import sys
import time

CONFIG = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude")
OUT = CONFIG / "ai-status-usage.json"


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    limits = data.get("rate_limits") or {}
    if limits.get("five_hour") or limits.get("seven_day"):
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps({"updated": time.time(), "rate_limits": limits}), encoding="utf-8")
        os.replace(tmp, OUT)   # quem lê nunca vê o arquivo pela metade
    parts = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = (limits.get(key) or {}).get("used_percentage")
        if pct is not None:
            parts.append(f"{label} {pct:.0f}%")
    sys.stdout.buffer.write(" · ".join(parts).encode("utf-8"))   # console do Windows não é UTF-8


if __name__ == "__main__":
    main()
