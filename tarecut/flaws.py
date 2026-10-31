"""瑕疵图：文件名解析（部位+瑕疵）、同义词匹配台词、叠加时间窗。"""
from __future__ import annotations

from pathlib import Path


def _find_group(stem: str, groups: dict) -> tuple:
    """在文本中找命中的词组，返回 (组名, 同义词列表, 最长命中长度)。"""
    best = ("", [], 0)
    for gname, terms in groups.items():
        for t in terms or []:
            if t and t in stem and len(t) > best[2]:
                best = (gname, terms, len(t))
    return best


def parse_image(img_path: str, syn_cfg: dict) -> dict:
    stem = Path(img_path).stem
    pg_name, pg_terms, _ = _find_group(stem, syn_cfg.get("parts", {}))
    dg_name, dg_terms, _ = _find_group(stem, syn_cfg.get("defects", {}))
    return {"path": str(img_path), "name": stem,
            "part": pg_name, "part_terms": pg_terms,
            "defect": dg_name, "defect_terms": dg_terms}


def _match_terms(text: str, terms: list) -> bool:
    return any(t in text for t in terms or [])


def plan_overlays(images: list, units5: list, seg5_dur: float,
                  syn_cfg: dict, flaw_cfg: dict) -> tuple:
    """为第 5 段字幕单元规划瑕疵图叠加时间窗。

    返回 (plan, warnings)
    plan: [{"path","s","e","unit","name"}]
    """
    plan, warnings = [], []
    if not units5:
        return plan, ["第5段没有可用台词，无法叠加瑕疵图"]

    per_unit: dict = {}  # unit_idx -> [img,...]
    DIRS = ("前", "后", "左", "右", "内", "外")
    for img in images:
        if not img["part_terms"] and not img["defect_terms"]:
            warnings.append(
                f"瑕疵图「{img['name']}」无法解析出部位/瑕疵，请按「部位+瑕疵」命名")
            continue
        cands = []
        for k, u in enumerate(units5):
            text = u["text"]
            part_ok = (not img["part_terms"]) or _match_terms(text, img["part_terms"])
            defect_ok = (not img["defect_terms"]) or _match_terms(text, img["defect_terms"])
            if part_ok and defect_ok:
                cands.append(k)
        if not cands and img["part_terms"]:
            # 部位命中但瑕疵词对不上（表述差异）→ 放宽为仅按部位
            cands = [k for k, u in enumerate(units5)
                     if _match_terms(u["text"], img["part_terms"])]
        if not cands:
            warnings.append(
                f"瑕疵图「{img['name']}」未在第5段台词中找到对应描述，未使用")
            continue
        # 方位词优先：图名含「前/后/左/右」等时，优先对到台词含同方位词的句子
        prefs = [d for d in DIRS if d in img["name"]]
        pick = -1
        if prefs:
            for k in cands:
                if (len(per_unit.get(k, [])) < 2
                        and any(d in units5[k]["text"] for d in prefs)):
                    pick = k
                    break
        if pick < 0:
            for k in cands:
                if len(per_unit.get(k, [])) < 2:
                    pick = k
                    break
        if pick < 0:
            warnings.append(
                f"台词对应图片超过 2 张，「{img['name']}」未使用")
            continue
        per_unit.setdefault(pick, []).append(img)

    min_dur = float(flaw_cfg.get("min_duration", 2.0))
    for k, imgs in per_unit.items():
        u = units5[k]
        s, e = max(0.0, u["s"]), min(u["e"], seg5_dur)
        d = max(e - s, min_dur)
        e = min(s + d, seg5_dur)
        n = len(imgs)
        for idx, img in enumerate(imgs[:2]):
            ps = s + (e - s) * idx / n
            pe = s + (e - s) * (idx + 1) / n
            pe = max(pe, ps + min(min_dur, (e - s) / n))
            pe = min(pe, seg5_dur)
            plan.append({"path": img["path"], "name": img["name"],
                         "unit": k, "s": round(ps, 3), "e": round(pe, 3)})

    # 台词提到瑕疵但没有图 → 提醒
    covered_units = set(per_unit.keys())
    for k, u in enumerate(units5):
        if k in covered_units:
            continue
        _, dg_terms, _ = _find_group(u["text"], syn_cfg.get("defects", {}))
        _, pg_terms, _ = _find_group(u["text"], syn_cfg.get("parts", {}))
        if dg_terms and pg_terms:
            warnings.append(
                f"台词「{u['text'][:24]}…」提到瑕疵但没有对应瑕疵图")

    plan.sort(key=lambda x: x["s"])
    return plan, warnings
