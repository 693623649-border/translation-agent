import re
from pathlib import Path

w = Path(
    "outputs/文学理论 (耶鲁大学公开课) = Theory of Literature "
    "(Open Yale Courses) ([美] 保罗・H・弗莱 (Paul H. Fry) 著  吕黎 译) "
    "(z-library.sk, 1lib.sk, z-lib.sk)"
)
for f in sorted((w / "chapters").glob("*.md")):
    t = open(f, encoding="utf-8").read()
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_\"= \-.]{0,50}>", t):
        s, e = m.start(), m.end()
        print(f.name, "|", repr(t[max(0, s - 25):s]), "|", repr(m.group(0)))
    for ch in "<":
        for m in re.finditer(re.escape(ch), t):
            print(f.name, "| <left>", repr(t[max(0, m.start() - 20): m.start() + 20]))
