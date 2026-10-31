"""诊断脚本：对给定 wav/mp4 逐一用本地 whisper 转写，打印时长/词数/内容前 60 字。

mp4 会先临时提取 16k wav（与批量同一 extract_audio 路径），用于对比
「缓存 wav」与「从输入视频新提取」的内容是否一致。
用法: python probe_files.py 路径1 路径2 ...
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
os.environ.setdefault("HF_HUB_CACHE", str(_ROOT / "hf-cache"))
sys.path.insert(0, str(_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from tarecut.asr import extract_audio, transcribe_words  # noqa: E402
from tarecut.utils import find_binary, media_duration  # noqa: E402

FF = find_binary("ffmpeg")

import yaml

_ALL = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
_CLOUD = "--cloud" in sys.argv
_ARGV = [a for a in sys.argv[1:] if not a.startswith("--")]
CFG = dict(_ALL["asr"]) if _CLOUD else {**_ALL["asr"], "backend": "local"}


def audio_streams(f: Path) -> list:
    p = subprocess.run([FF, "-i", str(f)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return [ln.strip() for ln in (p.stderr or "").splitlines()
            if " Audio: " in ln]


def main() -> int:
    for arg in _ARGV:
        f = Path(arg)
        if not f.exists():
            print(f"[缺失] {f}")
            continue
        n_words = 0
        if f.suffix.lower() == ".wav":
            wav = str(f)
        else:
            for i, ln in enumerate(audio_streams(f)):
                print(f"   [{f.name}] 音频流{i}: {ln[:120]}")
            wav = str(Path(tempfile.gettempdir()) /
                      f"probe_{f.parent.name}_{f.stem}.wav")
            extract_audio(str(f), wav)
        dur = media_duration(wav)
        try:
            words = transcribe_words(wav, CFG)
            n_words = len(words)
            text = "".join(w["w"] for w in words)[:60]
            end = words[-1]["e"] if words else 0.0
            err = ""
        except Exception as exc:  # noqa: BLE001
            text, end, err = "", 0.0, f" <ERR: {exc}>"
        print(f"== {f}\n   时长={dur:.2f}s 末词={end:.2f}s 词数={n_words}{err}\n"
              f"   内容: {text or '（空）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
