# -*- coding: utf-8 -*-
"""服务端健康探测：从 729 源视频提音频转写，验证结果是否真实。"""
import re
import subprocess
import sys

sys.path.insert(0, r"e:\work\tarecut")
from tarecut import asr
from tarecut.utils import find_binary

src = r"e:\work\tarecut\input\729\videos\3.mp4"
wav = r"e:\work\tarecut\probe_seg3.wav"
subprocess.run([find_binary("ffmpeg"), "-y", "-i", src, "-vn",
                "-ac", "1", "-ar", "16000", wav],
               capture_output=True, timeout=120)

cfgtxt = open(r"e:\work\tarecut\config.yaml", encoding="utf-8").read()
import yaml
cfg = yaml.safe_load(cfgtxt)
asr_cfg = dict(cfg.get("asr", {}))
words = asr.transcribe_words(wav, asr_cfg)
txt = "".join(w["w"] for w in words)
print(f"[探测] 词数={len(words)} 文本={txt[:30]}")
ok = "配置" in txt or "定速巡航" in txt
print("[探测] 服务端", "正常 ✓" if ok else "仍在故障窗口 ✗")
sys.exit(0 if ok else 2)
