"""单段渲染（字幕烧录 + 瑕疵图叠加）与成片拼接。"""
from __future__ import annotations

import os
from pathlib import Path

from .utils import find_binary, run


def _fontsdir_arg() -> str:
    # 系统字体由 libass(fontconfig/DirectWrite) 自行解析，无需 fontsdir；
    # 且该参数的盘符冒号转义在部分 ffmpeg 构建上解析失败，故不再使用
    return ""


def _fade_chain(s: float, e: float, fade: float) -> str:
    if fade <= 0:
        return ""
    return (f",fade=t=in:st={s:.3f}:d={fade:.2f}:alpha=1"
            f",fade=t=out:st={max(s, e - fade):.3f}:d={fade:.2f}:alpha=1")


def _fullscreen_chain(images: list, width: int, height: int,
                      flaw_cfg: dict) -> list:
    """全屏瑕疵展示：背景高斯模糊(随窗口淡入淡出) + 等比放大居中图片。

    竖图上下铺满、左右留白居中；横图左右铺满、上下居中。
    字幕滤镜由调用方放在滤镜链最后（压在模糊层与图片之上）。
    """
    fade = float(flaw_cfg.get("fade", 0.25))
    sigma = float(flaw_cfg.get("blur_sigma", 20))
    fc = []
    outs = "".join(f"[bb{k}]" for k in range(len(images)))
    fc.append(f"[v0]split={len(images) + 1}[base]{outs}")
    cur = "base"
    for k, img in enumerate(images):
        s, e = img["s"], img["e"]
        # 背景模糊层：仅在窗口附近计算模糊，alpha 随窗口淡入淡出
        chain = (f"[bb{k}]gblur=sigma={sigma:.1f}"
                 f":enable='between(t,{max(0.0, s - fade):.3f},{e:.3f})',"
                 f"format=rgba" + _fade_chain(s, e, fade))
        fc.append(chain + f"[bl{k}]")
        fc.append(f"[{cur}][bl{k}]overlay=0:0[fb{k}]")
        cur = f"fb{k}"
    for k, img in enumerate(images):
        s, e = img["s"], img["e"]
        # 图片等比适配画布后居中：竖图=上下铺满左右不铺满
        chain = (f"[{1 + k}:v]scale={width}:{height}"
                 f":force_original_aspect_ratio=decrease,format=rgba"
                 + _fade_chain(s, e, fade))
        fc.append(chain + f"[im{k}]")
        fc.append(f"[{cur}][im{k}]overlay=x=(W-w)/2:y=(H-h)/2[fw{k}]")
        cur = f"fw{k}"
    return fc, cur


def _card_chain(images: list, width: int, height: int, flaw_cfg: dict) -> list:
    """旧版卡片式：左/右上角白边小卡。"""
    fade = float(flaw_cfg.get("fade", 0.25))
    wr = float(flaw_cfg.get("width_ratio", 0.30))
    xm = float(flaw_cfg.get("x_margin_ratio", 0.04))
    yr = float(flaw_cfg.get("y_ratio", 0.28))
    bd = int(flaw_cfg.get("border", 6))
    side = str(flaw_cfg.get("x_side", "left")).lower()
    if side == "right":
        x = max(0, round(width * (1 - wr - xm)) - bd * 2)
    else:
        x = round(width * xm)
    yc = round(height * yr)
    fc = []
    cur = "v0"
    for k, img in enumerate(images):
        s, e = img["s"], img["e"]
        chain = f"[{1 + k}:v]scale=w=iw*{wr:.4f}:h=-1,format=rgba"
        if bd > 0:  # 白边瑕疵展示卡
            chain += (f",pad=iw+{bd * 2}:ih+{bd * 2}:{bd}:{bd}"
                      f":color=white")
        chain += _fade_chain(s, e, fade)
        fc.append(chain + f"[im{k}]")
        fc.append(f"[{cur}][im{k}]overlay=x={x}:y={yc}-h/2:"
                  f"enable='between(t,{s:.3f},{e:.3f})'[vo{k}]")
        cur = f"vo{k}"
    return fc, cur


def render_segment(video: str, ass_name: str, images: list, audio_wav: str | None,
                   out_name: str, width: int, height: int, fps: float,
                   flaw_cfg: dict, crf: int, preset: str,
                   workdir: str, out_dur: float | None = None,
                   video_dur: float | None = None,
                   trim_start: float = 0.0) -> None:
    """video/ass_name/audio_wav/out_name 均为相对 workdir 的路径或绝对路径。

    images: [{"path","s","e"}]，path 可为绝对路径（作为 -i 输入，不进 filter）。
    音频比视频长（全文配音溢出）时冻结末帧承接差值。
    trim_start > 0 时掐掉视频开头（开头掐头功能，音频由 pipeline 侧同步掐）。
    """
    cmd = [find_binary("ffmpeg"), "-y"]
    if trim_start > 0.01:
        cmd += ["-ss", f"{trim_start:.3f}"]
    cmd += ["-i", str(video)]
    n_img = len(images)
    for img in images:
        cmd += ["-framerate", "25", "-loop", "1", "-i", str(img["path"])]
    audio_idx = None
    if audio_wav:
        cmd += ["-i", str(audio_wav)]
        audio_idx = 1 + n_img

    vhead = f"[0:v]setsar=1,fps={fps:.5f}"  # 不缩放：保留各分镜原始分辨率/清晰度
    if out_dur and video_dur and out_dur > video_dur + 0.05:
        vhead += (f",tpad=stop_mode=clone"
                  f":stop_duration={out_dur - video_dur:.3f}")
    fc = [vhead + "[v0]"]
    style = str(flaw_cfg.get("style", "fullscreen")).lower()
    if n_img and style == "fullscreen":
        flaw_fc, cur = _fullscreen_chain(images, width, height, flaw_cfg)
        fc += flaw_fc
    elif n_img:
        flaw_fc, cur = _card_chain(images, width, height, flaw_cfg)
        fc += flaw_fc
    else:
        cur = "v0"
    # 字幕最后压上：全屏瑕疵模式下字幕显示在模糊背景与图片之上
    fc.append(f"[{cur}]subtitles=filename={ass_name}{_fontsdir_arg()},"
              f"format=yuv420p[vout]")

    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]"]
    cmd += ["-map", f"{audio_idx}:a"] if audio_idx is not None else ["-map", "0:a"]
    cmd += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart"]
    # VFR 源经 fps 滤镜补帧后可能长于音频，-shortest 在 filter_complex 下不可靠，
    # 用音频时长显式封顶
    if out_dur and out_dur > 0:
        cmd += ["-t", f"{out_dur:.3f}"]
    cmd += [str(out_name)]
    p = run(cmd, cwd=workdir, timeout=1800)
    if p.returncode != 0:
        raise RuntimeError(f"渲染第 {out_name} 失败:\n{p.stderr[-800:]}")


def concat_segments(part_names: list, out_path: str, workdir: str) -> None:
    """无损拼接（各段已统一编码参数）。out_path 可为绝对路径。"""
    list_file = Path(workdir) / "concat_list.txt"
    lines = [f"file '{n}'" for n in part_names]
    list_file.write_text("\n".join(lines), encoding="utf-8")
    p = run([find_binary("ffmpeg"), "-y", "-f", "concat", "-safe", "0",
             "-i", "concat_list.txt", "-c", "copy",
             "-movflags", "+faststart", str(Path(out_path).resolve())],
            cwd=workdir, timeout=600)
    if p.returncode != 0:
        raise RuntimeError(f"拼接失败:\n{p.stderr[-800:]}")
