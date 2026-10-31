"""核心模块：字幕切分 + 文案↔ASR 字符级单调对齐。

产出：
- 每个字幕单元的精确起止时间（文字 = 文案原文）
- 对齐 opcodes（供 audit 做逐字 diff）
"""
from __future__ import annotations

import difflib
import re

from .normalize import normalize

# 长句二次切分用的标点（保留在句尾）
_SPLIT_LONG = r"(?<=[，,、：:）)])"
_SENT_RE = re.compile(r"(?<=[。！？!?；;…])")


def split_units(text: str, max_chars: int = 16) -> list:
    """把一段文案切成字幕单元（显示文本保留原始标点）。"""
    units = []
    for sent in _SENT_RE.split(text):
        sent = sent.strip()
        if not sent:
            continue
        if len(normalize(sent)) <= max_chars:
            units.append(sent)
            continue
        buf = ""
        for part in re.split(_SPLIT_LONG, sent):
            if not part:
                continue
            if buf and len(normalize(buf + part)) > max_chars:
                units.append(buf)
                buf = part
            else:
                buf += part
        if buf.strip():
            if (units and len(normalize(buf)) <= 3
                    and re.search(_SPLIT_LONG, units[-1][-1:] or "")):
                units[-1] += buf  # 过短尾片并入上一条
            else:
                units.append(buf)
    return units


def build_char_timeline(words: list) -> list:
    """ASR 词展开为字符时间轴（词时长均摊到字符）。"""
    chars = []
    for wd in words:
        txt = normalize(wd.get("w", ""))
        if not txt:
            continue
        s, e = float(wd.get("s", 0)), float(wd.get("e", 0))
        dur = max(e - s, 0.0)
        per = dur / len(txt) if len(txt) > 1 else dur
        for i, ch in enumerate(txt):
            chars.append({"c": ch, "s": s + i * per, "e": s + (i + 1) * per})
    return chars


def _map_times(B: str, chars: list, opcodes: list) -> tuple:
    """把 ASR 字符时间轴经 opcodes 映射到文案字符位置。

    返回 (times, durs)：times[i] 为文案第 i 字的时间中点（无对应为 None），
    durs 为参与映射的字符时长（用于估计单字时长）。
    """
    times = [None] * len(B)
    durs = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            ch = chars[j1 + k]
            times[i1 + k] = (ch["s"] + ch["e"]) / 2.0
            durs.append(max(ch["e"] - ch["s"], 0.03))
    return times, durs


def align_units(units: list, words: list, seg_duration: float,
                silences: list | None = None,
                alt_words: list | None = None) -> dict:
    """对齐一段文案的多个字幕单元与 ASR 词序列。

    silences: [(start,end),...] 该段音频的真实静音区间（silencedetect）。
    alt_words: 同一段音频的本地 whisper 转写，词级时间戳为真实声学边界。
    云端 Paraformer 的字级时间戳是按 token 数把句长均分的虚构值（实测严格
    等间隔 0.224s，见 seg*.asr.json），单元边界不可信；传入 alt_words 时
    以它的时间为对齐主数据（文本仍用 words，漏读判定不变）。

    返回 {units: [{text, norm, ci0, ci1, s, e, missing}],
          B, A, chars, opcodes, char_dur}
    """
    B = "".join(normalize(u) for u in units)
    A_chars = build_char_timeline(words)
    A = "".join(x["c"] for x in A_chars)
    sm = difflib.SequenceMatcher(None, B, A, autojunk=False)
    opcodes = sm.get_opcodes()

    times, durs = _map_times(B, A_chars, opcodes)
    char_dur = sorted(durs)[len(durs) // 2] if durs else 0.15

    mapped = [t is not None for t in times]  # 插值前快照，用于漏读检测

    # 时间主数据切换到本地 whisper：整体替换（不混用）——虚构值与真实值
    # 混在一个时间轴里会造出错位边界，未匹配处交给线性插值即可。
    alt_used = False
    if alt_words:
        alt_chars = build_char_timeline(alt_words)
        a_times, a_durs = _map_times(
            B, alt_chars,
            difflib.SequenceMatcher(
                None, B, "".join(x["c"] for x in alt_chars),
                autojunk=False).get_opcodes())
        if any(t is not None for t in a_times):
            alt_used = True
            times = a_times
            if a_durs:
                char_dur = sorted(a_durs)[len(a_durs) // 2]

    _interpolate(times)
    half = char_dur / 2.0

    out_units = []
    ptr = 0
    for u in units:
        nu = normalize(u)
        ci0, ci1 = ptr, ptr + len(nu)  # [ci0, ci1)
        ptr = ci1
        if ci1 > ci0:
            s = times[ci0] - half
            e = times[ci1 - 1] + half
            missing = not any(mapped[ci0:ci1])
        else:  # 空单元（纯标点）
            prev = out_units[-1] if out_units else None
            s = prev["e"] if prev else 0.0
            e = s + 0.5
            missing = False
        s = max(0.0, min(s, seg_duration))
        e = max(s + 0.3, min(e, seg_duration))
        out_units.append({"text": u, "norm": nu, "ci0": ci0, "ci1": ci1,
                          "s": round(s, 3), "e": round(e, 3),
                          "missing": bool(missing)})

    # 相邻单元去重叠（字幕显示用；补丁窗口单独取原始值）
    for i in range(len(out_units) - 1):
        if out_units[i]["e"] > out_units[i + 1]["s"]:
            mid = (out_units[i]["e"] + out_units[i + 1]["s"]) / 2.0
            out_units[i]["e"] = round(mid, 3)
            out_units[i + 1]["s"] = round(mid, 3)

    # 单元边界贴合真实静音间隙（v3：锚点最近间隙 + 单调地板）。
    # 时间源不可信时（云端虚构时间戳，误差 ±0.9s 且方向不定，数字词常被
    # 拉伸），邻句字符锚点本身站不住，无法用作守卫。改为：
    #   单元 s = 距首字符锚点最近的静音间隙的结束点（=本句真实起音），
    #   搜索范围见下方 back/fwd（时间源越可信收得越窄）；
    #   上一单元 e = 同一间隙的起点（=上一句真实收音），两侧同源。
    # 真实边界间隙的结束点必不晚于句内停顿的结束点（本句起音更早），
    # 故「最近」优先命中真实边界；「已占用间隙」单调地板保证后续单元
    # 不会回贴到更早的间隙（不吃上一句已裁定的语音）。宁欠勿过。
    if silences and out_units:
        sils = sorted((float(a), float(b)) for a, b in silences)
        # 贴合范围随时间源可信度收缩：真实词级时间戳边界已准（±0.1s），
        # 只需极小范围微调；虚构时间戳误差可达 ±0.9s，才需要放宽搜索。
        back, fwd = (0.35, 0.25) if alt_used else (1.0, 0.45)
        tail_back, tail_fwd = (0.45, 0.45) if alt_used else (1.5, 1.0)
        cap = 1.8 if alt_used else 1.5

        def _nearest_gap_end(t: float, back: float, fwd: float,
                             floor_idx: int):
            best, bd = None, None
            for k in range(floor_idx + 1, len(sils)):
                d = sils[k][1] - t
                if -back <= d <= fwd and (bd is None or abs(d) < bd):
                    best, bd = k, abs(d)
            return best

        floor_idx = -1
        idxs = [i for i, u in enumerate(out_units) if u["ci1"] > u["ci0"]]
        for j, i_u in enumerate(idxs):
            u = out_units[i_u]
            lo = times[u["ci0"]]
            if u["missing"]:
                # 漏读单元：只借包含锚点的间隙给上一句定收音，不占间隙
                if j > 0:
                    for k in range(floor_idx + 1, len(sils)):
                        if sils[k][0] <= lo <= sils[k][1]:
                            out_units[idxs[j - 1]]["e"] = round(sils[k][0], 3)
                            break
                continue
            g = _nearest_gap_end(lo, back, fwd, floor_idx)
            if g is not None:
                u["s"] = round(sils[g][1], 3)
                if j > 0:  # 上一单元收音 = 同一间隙的起点
                    out_units[idxs[j - 1]]["e"] = round(sils[g][0], 3)
                floor_idx = g
            if j == len(idxs) - 1:  # 末单元：向段尾找收音边界
                t = times[u["ci1"] - 1]
                best, bd = None, None
                for k in range(floor_idx + 1, len(sils)):
                    d = sils[k][0] - t
                    if -tail_back <= d <= tail_fwd and (bd is None or abs(d) < bd):
                        best, bd = sils[k][0], abs(d)
                if best is not None:
                    u["e"] = round(best, 3)

        for u in out_units:  # 异常(贴到同一间隙致倒挂等)→回退
            if u["ci1"] > u["ci0"] and u["s"] >= u["e"]:
                u["s"] = min(u["s"], max(0.0, times[u["ci0"]] - char_dur / 2.0))
                u["e"] = max(u["e"], u["s"] + 0.3)

        # 语速一致性修正（虚构时间戳下的兜底）：上面的锚点搜索偶尔会把
        # 下一句的开头划进本单元；窗口过长会让补丁连同邻句开头一起替换掉
        # （听感=整句丢失）。这里按「窗口时长应≈字符数×全局语速」把过长的
        # 窗口收缩回本句真实收音处，并把该间隙的结束点让给下一单元起点。
        # 宁欠勿过。真实时间戳下窗口本已准确，只作极端越界的最后防线。
        runs, prev = [], 0.0
        for a, b in sils:
            if a > prev + 0.02:
                runs.append((prev, a))
            prev = max(prev, b)
        if seg_duration > prev + 0.02:
            runs.append((prev, float(seg_duration)))
        gs = (sum(b - a for a, b in runs) / len(B)) if B else 0.15

        for j, i_u in enumerate(idxs):
            u = out_units[i_u]
            expect = (u["ci1"] - u["ci0"]) * gs  # 该句应有语音时长
            if u["e"] - u["s"] <= expect * cap:  # 偏差在合理范围内
                continue
            target = u["s"] + expect
            cand = [(a, b) for a, b in sils if u["s"] + 0.2 < a < u["e"]]
            if not cand:
                continue
            a, b = min(cand, key=lambda g: abs(g[0] - target))
            if abs(a - target) >= abs(u["e"] - target) - 0.05:
                continue  # 收缩无明显改善，保持原值
            u["e"] = round(a, 3)
            if j + 1 < len(idxs):  # 下一单元改从同一间隙结束处起音
                nx = out_units[idxs[j + 1]]
                if nx["s"] > b:
                    nx["s"] = round(b, 3)

    return {"units": out_units, "B": B, "A": A, "chars": A_chars,
            "opcodes": opcodes, "char_dur": char_dur, "times": times}


def _interpolate(times: list) -> None:
    """对未映射到的字符位置做线性插值（首尾用最近锚点填充）。"""
    known = [i for i, t in enumerate(times) if t is not None]
    if not known:
        return
    first, last = known[0], known[-1]
    for i in range(first):
        times[i] = times[first] - (first - i) * 0.15
    for i in range(last + 1, len(times)):
        times[i] = times[last] + (i - last) * 0.15
    for a, b in zip(known, known[1:]):
        if b - a > 1:
            t0, t1 = times[a], times[b]
            step = (t1 - t0) / (b - a)
            for i in range(a + 1, b):
                times[i] = t0 + step * (i - a)
