"""字幕文件生成：SRT 与带样式 ASS（用于硬字幕烧录）。"""
from __future__ import annotations

from .utils import fmt_ass_time, fmt_srt_time


def _clean(text: str) -> str:
    return text.replace("\n", " ").replace("{", "｛").replace("}", "｝").strip()


def write_srt(entries: list, path: str) -> None:
    """entries: [(start, end, text)]"""
    lines = []
    for i, (s, e, text) in enumerate(entries, 1):
        t = _clean(text)
        if not t:
            continue
        lines.append(f"{i}\n{fmt_srt_time(s)} --> {fmt_srt_time(e)}\n{t}\n")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))


_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{fontname},{fontsize},{primary},{primary},{outline_colour},&H80000000,-1,0,0,0,100,100,0,0,1,{outline},0,2,60,60,{marginv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(entries: list, path: str, width: int, height: int, sub_cfg: dict) -> None:
    """entries: [(start, end, text)]，样式参数来自 config.subtitle。

    primary_colour/outline_colour 为 ASS 的 &HAABBGGRR 格式
    （黄字黑边 = &H0000FFFF / &H00000000）。
    """
    fontsize = max(10, round(height * float(sub_cfg.get("font_size_ratio", 0.045))))
    marginv = max(10, round(height * float(sub_cfg.get("margin_v_ratio", 0.08))))
    outline = int(sub_cfg.get("outline", 3))
    header = _ASS_HEADER.format(
        W=width, H=height, fontname=sub_cfg.get("fontname", "Microsoft YaHei"),
        fontsize=fontsize, outline=outline, marginv=marginv,
        primary=sub_cfg.get("primary_colour", "&H00FFFFFF"),
        outline_colour=sub_cfg.get("outline_colour", "&H00000000"))
    lines = [header]
    for s, e, text in entries:
        t = _clean(text)
        if not t:
            continue
        lines.append(f"Dialogue: 0,{fmt_ass_time(s)},{fmt_ass_time(e)},"
                     f"Default,,0,0,0,,{t}")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines) + "\n")
