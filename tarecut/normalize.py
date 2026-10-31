"""文本归一化：全半角、数字转汉字、去标点。对齐与审计共用。"""
from __future__ import annotations

import re
import unicodedata

import cn2an

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_KEEP_RE = re.compile(r"[^\u4e00-\u9fffA-Z]")


def _num_to_cn(m: re.Match) -> str:
    s = m.group(0)
    try:
        return cn2an.an2cn(s)
    except Exception:
        return s


def normalize(text: str) -> str:
    """归一化为仅含汉字与大写字母的字符串。"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = t.upper()
    t = _NUM_RE.sub(_num_to_cn, t)
    t = _KEEP_RE.sub("", t)
    return t
