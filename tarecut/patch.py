"""音频定点替换：把错误句时间窗替换为 TTS 补丁，总时长不变。"""
from __future__ import annotations

from pathlib import Path

from .utils import find_binary, media_duration, run

MAX_TEMPO = 1.50
MIN_TEMPO = 0.85   # 放慢下限：最多拖 18%，宁多留静音也不拖沓


def detect_silences(wav: str, noise: str = "-35dB", d: float = 0.08) -> list:
    """ffmpeg silencedetect 提取静音区间 [(start, end), ...]（秒，升序）。

    用途：ASR 词起点普遍滞后真实发音（实测可达 0.37s）、终点也可能提前，
    直接按 ASR 时间窗替换音频会留下原句头/尾的残字（听感=补丁句重复）。
    静音区间给出真实语音边界，用于把替换窗口贴合到真实起音/收音。
    """
    import re

    p = run([find_binary("ffmpeg"), "-i", wav, "-af",
             f"silencedetect=noise={noise}:d={d}", "-f", "null", "-"],
            timeout=120)
    out, cur = [], None
    for m in re.finditer(r"silence_start:\s*(-?[\d.]+)|silence_end:\s*(-?[\d.]+)",
                         p.stderr):
        if m.group(1) is not None:
            cur = float(m.group(1))
        elif cur is not None:
            out.append((cur, float(m.group(2))))
            cur = None
    return out


def true_onset(silences: list, t: float) -> float | None:
    """t 之前最近的静音结束点（= 紧邻 t 的真实起音），无则 None。

    溢出预算用：补丁向后溢出最多吃到下一句的真实起音为止。
    """
    out = None
    for (a, b) in silences:
        if b <= t + 0.02:
            out = b
    return out


def replace_window(seg_wav: str, window: tuple, patch_wav: str, out_wav: str,
                   sample_rate: int = 44100,
                   budget_end: float | None = None) -> dict:
    """把 seg_wav 的 [s,e] 窗口替换为 patch_wav（时长拟合到窗口）。

    补丁比窗口长时先整体变速；仍装不下则允许向后溢出
    （e2 = s + 补丁时长，溢出部分吃掉窗口后的原音频，多为静音间隙），
    溢出右界不超过 budget_end（一般取下一条字幕起点，保护其后原声）。
    段内总音频时长始终保持不变，后续字幕/瑕疵图时间无需平移。

    返回 {s,e,e2,spill,window_dur,patch_dur,patched_dur,tempo,clamped,trimmed}
    """
    seg_dur = media_duration(seg_wav)
    patch_dur = media_duration(patch_wav)
    s = max(0.0, min(window[0], seg_dur - 0.2))
    e = max(s + 0.3, min(window[1], seg_dur))
    wd = e - s
    # 补丁可延伸的右界：显式预算（下一条字幕起点-保护带）与段尾取小
    limit = seg_dur
    if budget_end is not None:
        limit = min(limit, max(e, float(budget_end)))
    # 优先装进原句窗口 [s,e]：放慢不超过 MIN_TEMPO、加快不超过 MAX_TEMPO；
    # 变速到顶仍装不下才允许向后溢出（吃窗口后的静音间隙，且受预算保护，
    # 不会推进下一句的发音区）
    tempo_fit = patch_dur / wd
    tempo_eff = min(max(tempo_fit, MIN_TEMPO), MAX_TEMPO)
    clamped = tempo_fit > MAX_TEMPO + 1e-6   # 加速到顶仍装不下 → 必然溢出

    patched_dur = patch_dur / tempo_eff
    e2 = min(max(e, s + patched_dur), limit)   # 实际替换右界
    trimmed = e2 < s + patched_dur - 1e-3      # 预算内装不下，被迫截断
    spill = e2 - e                              # 向后溢出量

    pre = s > 0.05
    post = e2 < seg_dur - 0.05

    fmt = f"aformat=sample_rates={sample_rate}:channel_layouts=mono"
    chains = []
    parts = []

    if pre and post:
        chains.append(f"[0:a]{fmt},asplit=2[sa][sb]")
        chains.append(f"[sa]atrim=0:{s:.3f},asetpts=PTS-STARTPTS[pre]")
        chains.append(f"[sb]atrim=start={e2:.3f},asetpts=PTS-STARTPTS[post]")
        parts = ["[pre]", "[p]", "[post]"]
    elif pre:
        chains.append(f"[0:a]{fmt},atrim=0:{s:.3f},asetpts=PTS-STARTPTS[pre]")
        parts = ["[pre]", "[p]"]
    elif post:
        chains.append(f"[0:a]{fmt},atrim=start={e2:.3f},asetpts=PTS-STARTPTS[post]")
        parts = ["[p]", "[post]"]
    else:
        parts = ["[p]"]

    pchain = f"[1:a]{fmt}"
    if abs(tempo_eff - 1.0) > 1e-3:
        pchain += f",atempo={tempo_eff:.5f}"
    pchain += f",apad,atrim=0:{e2 - s:.3f},asetpts=PTS-STARTPTS[p]"
    chains.append(pchain)

    if len(parts) == 1:
        out_map = parts[0]
    else:
        chains.append("".join(parts) + f"concat=n={len(parts)}:v=0:a=1[aout]")
        out_map = "[aout]"

    fc = ";".join(chains)
    p = run([find_binary("ffmpeg"), "-y", "-i", str(seg_wav),
             "-i", str(patch_wav), "-filter_complex", fc,
             "-map", out_map, str(out_wav)], timeout=300)
    if p.returncode != 0 or not Path(out_wav).exists():
        raise RuntimeError(f"音频替换失败: {p.stderr[-500:]}")
    return {"s": round(s, 3), "e": round(e, 3), "e2": round(e2, 3),
            "spill": round(spill, 3), "window_dur": round(wd, 3),
            "patch_dur": round(patch_dur, 3),
            "patched_dur": round(patched_dur, 3),
            "tempo": round(tempo_eff, 4), "clamped": clamped,
            "trimmed": trimmed}
