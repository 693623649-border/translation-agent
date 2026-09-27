import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
report = json.loads(path.read_text(encoding="utf-8"))
print(f"status: {report.get('status')} | ok: {report.get('ok')}")
for check in report.get("checks", []):
    line = f"{check['id']}: {check['status']}"
    if check.get("issues"):
        codes: dict[str, int] = {}
        for issue in check["issues"]:
            codes[issue.get("code", "?")] = codes.get(issue.get("code", "?"), 0) + 1
        line += " | " + ", ".join(f"{k}x{v}" for k, v in codes.items())
    print(line)
