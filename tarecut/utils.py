"""通用工具：ffmpeg 查找、ffprobe、时间格式化、json 读写。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def find_binary(name: str) -> str:
    """优先项目内 tools/ffmpeg/bin，其次 PATH。"""
    local_dir = PROJECT_ROOT / "tools" / "ffmpeg" / "bin"
    local = local_dir / f"{name}.exe"
    if local.exists():
        return str(local)
    # 兼容未整理目录的解压布局
    if (PROJECT_ROOT / "tools" / "ffmpeg").exists():
        hits = sorted((PROJECT_ROOT / "tools" / "ffmpeg").rglob(f"{name}.exe"))
        if hits:
            return str(hits[0])
    w = shutil.which(name)
    if w:
        return w
    raise FileNotFoundError(
        f"未找到 {name}.exe，请将 ffmpeg 便携版放入 tools/ffmpeg/bin/，或安装后加入 PATH"
    )


def run(cmd: list[str], cwd: str | None = None, timeout: int | None = None):
    """执行外部命令，返回 CompletedProcess（捕获输出）。"""
    return subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def ffprobe_json(path: str | Path) -> dict:
    exe = find_binary("ffprobe")
    p = run([exe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)], timeout=60)
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"ffprobe 失败: {path}\n{p.stderr[:500]}")
    return json.loads(p.stdout)


def media_duration(path: str | Path) -> float:
    data = ffprobe_json(path)
    dur = data.get("format", {}).get("duration")
    if dur is not None:
        return float(dur)
    for st in data.get("streams", []):
        if st.get("duration"):
            return float(st["duration"])
    raise RuntimeError(f"无法获取时长: {path}")


def parse_fps(v: str | None) -> float:
    if not v:
        return 0.0
    if "/" in v:
        a, b = v.split("/", 1)
        try:
            den = float(b)
            return float(a) / den if den else 0.0
        except ValueError:
            return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def fmt_srt_time(sec: float) -> str:
    sec = max(sec, 0.0)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_ass_time(sec: float) -> str:
    sec = max(sec, 0.0)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def save_json(path: str | Path, obj) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                          encoding="utf-8")


def ffmpeg_convert(src: str, dst: str, rate: int = 44100, mono: bool = True,
                   timeout: int = 300) -> None:
    """统一转为 wav（默认 44100Hz 单声道）。"""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    p = run([find_binary("ffmpeg"), "-y", "-i", str(src),
             "-ar", str(rate), "-ac", "1" if mono else "2", str(dst)],
            timeout=timeout)
    if p.returncode != 0 or not Path(dst).exists():
        raise RuntimeError(f"音频转换失败: {src}\n{p.stderr[-400:]}")


def load_json(path: str | Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
