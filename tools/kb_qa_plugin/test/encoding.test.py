"""End-to-end check of the plugin's byte-level stdin/stdout contract.

Reproduces the exact conditions the Node plugin creates: a child Python process
with the host's GBK code page, a UTF-8 JSON request (including Chinese) written
to stdin, and a UTF-8 JSON answer read back from stdout.
"""

import json
import os
import subprocess
import sys

ROOT = r"E:\Deeplearning\translation-agent"
SCRIPT = os.path.join(ROOT, "tools", "kb_qa_plugin", "kb_qa.py")

# Strip the UTF-8 hints so the child runs on the host's default (cp936) codec.
env = {k: v for k, v in os.environ.items() if k not in {"PYTHONIOENCODING", "PYTHONUTF8"}}


def call(request):
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--stdin-json"],
        input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        cwd=ROOT,
        env=env,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(f"FAIL rc={proc.returncode} stderr={proc.stderr.decode('utf-8', 'replace')[-500:]}")
    return json.loads(proc.stdout.decode("utf-8"))


cases = [
    ("ask 中文查询", {"command": "ask", "args": {"query": "柄谷行人 交换样式", "limit": 3}}),
    ("ask 繁体/异体", {"command": "ask", "args": {"query": "漱石 文学論", "limit": 2}}),
    ("verify 逐字引文", {"command": "verify-quote", "args": {"quote": "从来如此，便对么？"}}),
    ("verify 伪造引文", {"command": "verify-quote", "args": {"quote": "从来如此，便是对的了，我们必须永远如此。"}}),
    ("verify 大小写差异", {
        "command": "verify-quote",
        "args": {"quote": "交换样式有四种类型：a.赠与的互酬，b.服从与保护"},
    }),
    ("status", {"command": "status", "args": {"limit": 5}}),
]

expected_verdicts = {
    "verify 逐字引文": "verbatim",
    "verify 伪造引文": "mismatch",
    "verify 大小写差异": "verbatim_normalized",
}

failures = 0
for label, request in cases:
    payload = call(request)
    if "verdict" in payload:
        detail = f'verdict={payload["verdict"]} locations={len(payload["locations"])}'
        if label in expected_verdicts and payload["verdict"] != expected_verdicts[label]:
            failures += 1
            detail += f'  <-- expected {expected_verdicts[label]}'
    elif "hits" in payload:
        detail = f'hits={len(payload["hits"])} deep={payload["diagnostics"]["deep_books"][:1]}'
        if label.startswith("ask") and not payload["hits"]:
            failures += 1
        for hit in payload["hits"][:2]:
            detail += f'\n      {hit["stage"]:9} 《{hit["book"][:26]}》· {hit["title"][:30]}'
    else:
        detail = f'shelf={len(payload["shelf"])} workspaces={payload["global"]["workspace_count"]}'
    print(f"[ok] {label}: {detail}")

print()
print("FAILURES:", failures)
raise SystemExit(1 if failures else 0)
