"""把 LibTV 原始导出目录整理成流水线标准结构。

用法:
  python stage_input.py <原始目录> [目标车源名]
  例: python stage_input.py input/9.20/728
  批量: python stage_input.py input/9.20

规则:
  - "N.名称.mp4" → videos/N.mp4
  - 无编号视频(如"高清.mp4") → 填补 1-6 中缺失的段号（唯一缺口时）
  - 全文音频.wav 等非视频文件原样忽略
  - 文案.txt / 瑕疵图/ 存在则一并带入
"""
import re
import shutil
import sys
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".m4v"}


def stage_one(src: Path, dst_name: str | None = None) -> Path:
    dst = Path("input") / (dst_name or src.name)
    if src.resolve() == dst.resolve():
        raise ValueError(f"源与目标相同: {dst}")
    vdst = dst / "videos"
    vdst.mkdir(parents=True, exist_ok=True)
    for f in vdst.glob("*"):
        f.unlink()

    used, unnamed = set(), []
    for f in sorted(src.iterdir()):
        if not f.is_file() or f.suffix.lower() not in VIDEO_EXT:
            continue
        m = re.match(r"^(\d+)\s*[._\-）\)]?", f.stem)
        if m and int(m.group(1)) <= 6:
            n = int(m.group(1))
            shutil.copy2(f, vdst / f"{n}.mp4")
            used.add(n)
        else:
            unnamed.append(f)

    gaps = [n for n in range(1, 7) if n not in used]
    if unnamed:
        if len(unnamed) == 1 and len(gaps) == 1:
            shutil.copy2(unnamed[0], vdst / f"{gaps[0]}.mp4")
            print(f"  {unnamed[0].name} → videos/{gaps[0]}.mp4（补缺段 {gaps[0]}）")
        else:
            print(f"  ⚠ 无法安放的无编号视频: {[f.name for f in unnamed]}，"
                  f"缺口段号: {gaps}")

    for name in ("文案.txt", "瑕疵图"):
        if (src / name).exists():
            s = src / name
            d = dst / name
            if s.is_file():
                shutil.copy2(s, d)
            else:
                if d.exists():
                    shutil.rmtree(d)
                shutil.copytree(s, d)

    got = sorted(p.stem for p in vdst.glob("*.mp4"))
    print(f"{src.name} → {dst}  段: {got}")
    if len(got) != 6:
        print(f"  ⚠ 不足 6 段，缺: {[n for n in range(1,7) if str(n) not in got]}")
    return dst


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    src = Path(sys.argv[1])
    name = sys.argv[2] if len(sys.argv) > 2 else None
    if src.is_dir() and any(p.is_dir() for p in src.iterdir()):
        for sub in sorted(p for p in src.iterdir() if p.is_dir()):
            try:
                stage_one(sub)
            except Exception as e:
                print(f"  ⚠ {sub.name}: {e}")
    else:
        stage_one(src, name)


if __name__ == "__main__":
    main()
