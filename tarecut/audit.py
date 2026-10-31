"""音频体检：对齐 opcodes → 错误清单（错读/漏读/多读/同音异字）。"""
from __future__ import annotations

import re

from pypinyin import lazy_pinyin


def _same_pinyin(s1: str, s2: str) -> bool:
    if len(s1) != len(s2) or not s1:
        return False
    try:
        return lazy_pinyin(s1) == lazy_pinyin(s2)
    except Exception:
        return False


def _unit_for_ci(units: list, ci0: int, ci1: int) -> int:
    """返回与文案字符区间重叠最大的字幕单元下标。"""
    best, best_ov = -1, 0
    for k, u in enumerate(units):
        ov = min(ci1, u["ci1"]) - max(ci0, u["ci0"])
        if ov > best_ov:
            best, best_ov = k, ov
    return best


def audit_segment(seg_no: int, align: dict, cfg: dict) -> list:
    """返回该段错误列表。"""
    B, A = align["B"], align["A"]
    chars, units, times = align["chars"], align["units"], align["times"]
    min_len = int(cfg.get("audit", {}).get("min_len", 1))
    ignore_homophone = bool(cfg.get("audit", {}).get("ignore_same_pinyin", True))
    errors = []
    for tag, i1, i2, j1, j2 in align["opcodes"]:
        if tag == "equal":
            continue
        if tag == "replace":
            script, asr = B[i1:i2], A[j1:j2]
            typ = "错读"
            if ignore_homophone and _same_pinyin(script, asr):
                typ = "同音异字"
            if j2 > j1 and j1 < len(chars):
                t0, t1 = chars[j1]["s"], chars[min(j2, len(chars)) - 1]["e"]
            else:
                t0, t1 = times[i1], times[min(i2, len(times)) - 1] if i2 > i1 else (0, 0)
                t0, t1 = (t0, t1) if i2 > i1 else (0.0, 0.0)
        elif tag == "delete":  # 文案有、音频无
            script, asr, typ = B[i1:i2], "", "漏读"
            t0, t1 = times[i1], times[min(i2, len(times)) - 1]
        elif tag == "insert":  # 音频多出
            script, asr, typ = "", A[j1:j2], "多读"
            if j1 < len(chars):
                t0 = chars[j1]["s"]
                t1 = chars[min(j2, len(chars)) - 1]["e"]
            else:
                t0, t1 = 0.0, 0.0
        else:
            continue
        # 纯字母数字、窗口 <0.15s 的单字符错漏：ASR 对字母数字串的常见幻听
        # （如 180DVVT→180DVT 丢一个 V，原声其实正确）。真实读错不可能
        # 这么短。降级为提示、不自动补丁——补丁会整句重念，TTS 读车型名
        # /字母串反而更容易读错（745 MG5 教训）。
        if (typ in ("错读", "漏读") and script
                and re.fullmatch(r"[A-Z0-9]+", script)
                and (float(t1) - float(t0)) < 0.15):
            typ = "字母数字幻听"
        if max(len(script), len(asr)) < min_len:
            continue
        errors.append({
            "seg": seg_no, "type": typ, "script": script, "asr": asr,
            "s": round(float(t0), 2), "e": round(float(t1), 2),
            "ci0": i1, "ci1": i2,
            "unit": _unit_for_ci(units, i1, i2),
        })
    return errors


def is_patchable(err: dict) -> bool:
    """可补丁的错误：有明确文案内容且定位到了字幕单元。"""
    return err["type"] in ("错读", "漏读") and err.get("unit", -1) >= 0
