"""从测试样例 CSV 生成各车源 文案.txt（批量流程用）。

用法:
  python make_scripts.py 728 [729 ...]   # 指定编号
  python make_scripts.py                  # 全部（input/ 下已存在的编号）
"""
import csv
import re
import sys
from pathlib import Path

CSV_PATH = Path("input/9.20/自动化剪辑测试样例-9.20 - Sheet1 (1).csv")
# 台词列可能带 "男主播【情绪提示】：" 前缀，配音不含该前缀，去掉
_PREFIX_RE = re.compile(r"^\s*(?:男主播|女主播|主播|旁白)?\s*【[^】]*】\s*[：:]\s*")


def clean(text: str) -> str:
    return _PREFIX_RE.sub("", text or "").strip()


def main():
    ids = {a.strip() for a in sys.argv[1:] if a.strip()}
    with open(CSV_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    made = 0
    for row in rows:
        no = (row.get("序号") or "").strip()
        if not no or (ids and no not in ids):
            continue
        seg_dir = Path("input") / no
        if not seg_dir.is_dir():
            continue
        lines = []
        for n in range(1, 7):
            text = clean(row.get(f"场景{n}台词") or "")
            lines.append(f"# 第{n}段")
            lines.append(text)
            lines.append("")
        out = seg_dir / "文案.txt"
        out.write_text("\n".join(lines), encoding="utf-8")
        empty = [n for n in range(1, 7)
                 if not clean(row.get(f"场景{n}台词") or "")]
        print(f"{no}: 文案.txt 写入完成"
              + (f"（空段: {empty}）" if empty else ""))
        made += 1
    if ids and made == 0:
        print(f"CSV 中未找到编号: {sorted(ids)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
