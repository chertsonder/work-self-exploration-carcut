"""对齐/审计逻辑自测（不依赖模型与素材）。运行: python tests/test_align.py"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tarecut.align import split_units, align_units          # noqa: E402
from tarecut.audit import audit_segment                     # noqa: E402
from tarecut.normalize import normalize                     # noqa: E402


def fake_words(text: str, start=0.5, per=0.25, gaps=None):
    """按每字 per 秒生成词级时间戳。gaps: {字符索引: 额外停顿}"""
    words, t, gap = [], start, 0.0
    norm = normalize(text)
    for i, ch in enumerate(norm):
        gap += (gaps or {}).get(i, 0.0)
        words.append({"w": ch, "s": t + gap, "e": t + gap + per})
        t += per
    return words


def main():
    text = "大家好，今天给大家带来一台2021年的丰田卡罗拉。车况方面，左前轮毂有轻微剐蹭。"
    units = split_units(text, 16)
    assert len(units) >= 3, f"切分过少: {units}"
    words = fake_words(text)
    dur = 0.5 + len(normalize(text)) * 0.25 + 1.0
    align = align_units(units, words, dur)
    us = align["units"]
    assert all(not u["missing"] for u in us), "不应有整句漏读"
    assert all(us[i]["e"] <= us[i + 1]["s"] + 0.01 for i in range(len(us) - 1)), \
        "字幕时间应单调不重叠"
    errs = audit_segment(1, align, {})
    assert errs == [], f"完全一致时不应有差异: {errs}"

    # 错读：丰田 → 福特（拼音不同）
    words2 = fake_words(text.replace("丰田", "福特"))
    align2 = align_units(units, words2, dur)
    errs2 = audit_segment(1, align2, {})
    types2 = [e["type"] for e in errs2]
    assert "错读" in types2, f"应检出错读: {errs2}"
    assert any("福特" in e["asr"] for e in errs2), errs2

    # 同音异字：剐蹭 → 刮蹭（拼音相同，降级）
    words3 = fake_words(text.replace("剐蹭", "刮蹭"))
    align3 = align_units(units, words3, dur)
    errs3 = audit_segment(1, align3, {})
    assert any(e["type"] == "同音异字" for e in errs3), f"应检出同音异字: {errs3}"

    # 漏读：整句缺失
    words4 = fake_words(text.replace("左前轮毂有轻微剐蹭", ""))
    align4 = align_units(units, words4, dur)
    errs4 = audit_segment(1, align4, {})
    assert any(e["type"] == "漏读" for e in errs4), f"应检出漏读: {errs4}"

    # 数字归一化：文案 9.8万 vs ASR 九点八万
    t5 = "现在只要9.8万，非常划算。"
    u5 = split_units(t5, 16)
    w5 = fake_words("现在只要九点八万，非常划算。")
    a5 = align_units(u5, w5, 30)
    e5 = audit_segment(1, a5, {})
    assert all(e["type"] == "同音异字" or not e["script"] for e in e5), \
        f"数字归一化后不应报错读: {e5}"

    print("test_align: 全部通过 ✓")
    print("字幕单元示例:", [f"{u['s']:.1f}-{u['e']:.1f} {u['text']}" for u in us[:3]])


if __name__ == "__main__":
    main()
