"""检测批量窗口内 ASR 缓存是否串台：转写文本与该段文案的字符重叠率。"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, r"e:\work\tarecut")
from tarecut.pipeline import parse_script  # noqa: E402
from tarecut.normalize import normalize  # noqa: E402

for d in sorted(os.listdir(r"e:\work\tarecut\output")):
    out_base = os.path.join(r"e:\work\tarecut\output", d)
    m = re.match(r"^(\d{3})", d)
    if not m:
        continue
    num = m.group(1)
    script = os.path.join(r"e:\work\tarecut\input", num, "文案.txt")
    if not os.path.exists(script):
        continue
    try:
        segs = parse_script(open(script, encoding="utf-8").read())
    except Exception:
        continue
    bad = []
    for i in range(1, 7):
        f = os.path.join(out_base, "work", "cache", f"seg{i}.asr.json")
        if not os.path.exists(f) or not segs.get(i, "").strip():
            continue
        want = normalize(segs[i])
        try:
            got = normalize("".join(w["w"] for w in json.load(open(f, encoding="utf-8"))))
        except Exception:
            got = ""
        if not want or not got:
            bad.append(f"seg{i}(空)")
            continue
        common = sum((min(want.count(c), got.count(c)) for c in set(want)))
        ratio = common / max(len(want), 1)
        mark = "OK " if ratio >= 0.55 else "污染"
        if ratio < 0.55:
            bad.append(f"seg{i}({ratio:.0%})")
    tag = d[:22]
    if bad:
        print(f"{tag}  ->  需重转: {', '.join(bad)}")
    else:
        print(f"{tag}  ->  全部正常")
