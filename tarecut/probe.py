"""视频参数体检：读取各段参数并与第 1 段对齐基准。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .utils import ffprobe_json, parse_fps


@dataclass
class SegParams:
    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    vcodec: str = ""
    acodec: str = ""
    sample_rate: int = 0
    channels: int = 0
    issues: list = field(default_factory=list)


@dataclass
class ProbeResult:
    segments: list
    width: int
    height: int
    fps: float
    total_duration: float
    warnings: list


def probe_segment(path: str) -> SegParams:
    sp = SegParams(path=str(path))
    data = ffprobe_json(path)
    fmt = data.get("format", {})
    sp.duration = float(fmt.get("duration", 0) or 0)
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not sp.width:
            sp.width = int(st.get("width") or 0)
            sp.height = int(st.get("height") or 0)
            sp.fps = parse_fps(st.get("avg_frame_rate") or st.get("r_frame_rate"))
            sp.vcodec = st.get("codec_name", "")
        elif st.get("codec_type") == "audio" and not sp.sample_rate:
            sp.acodec = st.get("codec_name", "")
            sp.sample_rate = int(st.get("sample_rate") or 0)
            sp.channels = int(st.get("channels") or 0)
    if sp.duration <= 0:
        sp.issues.append("无法读取时长")
    if not sp.width or not sp.height:
        sp.issues.append("无法读取分辨率")
    if sp.fps <= 0:
        sp.issues.append("无法读取帧率")
    if not sp.sample_rate:
        sp.issues.append("未发现音频流")
    return sp


def probe_segments(paths: list, fps_override=None) -> ProbeResult:
    segs = [probe_segment(p) for p in paths]
    base = segs[0]
    warnings = []
    for i, s in enumerate(segs, 1):
        for msg in s.issues:
            warnings.append(f"第{i}段: {msg}")
        if s.width and base.width and (s.width, s.height) != (base.width, base.height):
            warnings.append(
                f"第{i}段分辨率 {s.width}x{s.height} 与第1段 {base.width}x{base.height} 不一致（成片保留各自原始分辨率）")
        if base.fps and abs(s.fps - base.fps) > 0.01:
            warnings.append(
                f"第{i}段帧率 {s.fps:.3f} 与第1段 {base.fps:.3f} 不一致，渲染时将统一")
        if s.duration <= 0.1:
            warnings.append(f"第{i}段时长异常({s.duration:.2f}s)")
    fps = fps_override if fps_override else base.fps
    return ProbeResult(
        segments=segs, width=base.width, height=base.height, fps=fps,
        total_duration=sum(s.duration for s in segs), warnings=warnings,
    )
