"""从 input/9.20 素材目录 + 台词 CSV 批量准备标准车源目录。

产出 input/<车源ID>/：videos/1-6.mp4（高清.mp4 补第4段）+ 瑕疵图/ + 文案.txt。
幂等：已存在的文件跳过，可重复运行。
"""
from __future__ import annotations

import csv
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "input" / "9.20"
VIDEO_MAP = {"1.开场.mp4": "1.mp4", "2.车辆展示.mp4": "2.mp4",
             "3.内饰配置.mp4": "3.mp4", "高清.mp4": "4.mp4",
             "5.车况.mp4": "5.mp4", "6.结尾.mp4": "6.mp4"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def clean_text(t: str) -> str:
    """去掉 场景N：/男主播：/【标记】 等台词外内容，换行折空格。"""
    t = re.sub(r"场景[一二三四五六]\s*[：:]", "", t)
    t = re.sub(r"(男主播|女主播|主播)?\s*【[^】]*】\s*[：:]?", "", t)
    return re.sub(r"\s+", " ", t).strip()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    csv_path = next(SRC.glob("*.csv"))
    rows = list(csv.reader(open(csv_path, encoding="utf-8-sig")))
    head = [h.strip() for h in rows[0]]
    idc = head.index("序号") if "序号" in head else 0  # 728/729 短号在序号列
    scene_cols = [head.index(f"场景{i}台词") for i in range(1, 7)]

    made = 0
    for r in rows[1:]:
        if len(r) <= idc or not r[idc].strip().isdigit():
            continue
        cid = r[idc].strip()
        srcvid = SRC / cid
        if not srcvid.is_dir():
            print(f"[{cid}] ⚠ 无素材目录，跳过")
            continue
        dst = ROOT / "input" / cid
        vdst = dst / "videos"
        vdst.mkdir(parents=True, exist_ok=True)

        missing = []
        for s, d in VIDEO_MAP.items():
            dp = vdst / d
            if dp.exists() and dp.stat().st_size > 0:
                continue
            sp = srcvid / s
            if sp.exists():
                shutil.copy2(sp, dp)
            else:
                missing.append(s)

        imgs = 0
        agg = SRC / "9.20-AIGC素材汇总"
        flaw_src = next((p for p in agg.rglob("*")
                         if p.is_dir() and p.name.startswith(f"{cid}-")), None)
        if flaw_src:
            fdir = dst / "瑕疵图"
            if fdir.exists():  # 重建：保证去重规则始终生效
                for f in fdir.iterdir():
                    if f.suffix.lower() in IMG_EXT:
                        f.unlink()
            fdir.mkdir(exist_ok=True)
            # 相同命名的瑕疵图只放一张：去掉尾部序号 _N 后同名的视为重复，
            # 每组保留文件名排序第一张（_1 优先）
            group: dict[str, Path] = {}
            for f in sorted(flaw_src.iterdir()):
                if f.suffix.lower() in IMG_EXT:
                    group.setdefault(re.sub(r"_\d+$", "", f.stem), f)
            for f in group.values():
                shutil.copy2(f, fdir / f.name)
            imgs = len(group)

        segs = [clean_text(r[c]) if len(r) > c else "" for c in scene_cols]
        if not (dst / "文案.txt").exists():
            lines: list[str] = []
            for i, s in enumerate(segs, 1):
                lines += [f"# 第{i}段", s, ""]
            (dst / "文案.txt").write_text("\n".join(lines), encoding="utf-8")

        # 成片命名：CSV 成片命名列，空则按 序号-车源名称-商家简称-车源ID 拼接
        if not (dst / "命名.txt").exists():
            tcol = head.index("成片命名（命名视频用）") if "成片命名（命名视频用）" in head else -1
            title = (r[tcol].strip() if tcol >= 0 and len(r) > tcol else "")
            if not title:
                cols = {h: i for i, h in enumerate(head)}
                get = lambda k: (r[cols[k]].strip() if k in cols and len(r) > cols[k] else "")
                title = "-".join(x for x in (r[idc].strip(), get("车源名称"),
                                             get("商家简称"), get("车源ID")) if x)
            (dst / "命名.txt").write_text(title + "\n", encoding="utf-8")

        warn = f"，⚠缺视频: {missing}" if missing else ""
        print(f"[{cid}] 就绪: 视频{sum(1 for p in vdst.iterdir())}个"
              f"+瑕疵图{imgs}张+文案{sum(len(s) for s in segs)}字{warn}")
        made += 1
    print(f"准备完成 {made} 个车源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
