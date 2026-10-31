"""生成合成样例车源 input/DEMO（无需真实素材），用于端到端冒烟测试。

运行: python tests/make_fixture.py
需要网络（edge-tts）。素材声音与 TTS 补丁音色不同（Xiaoxiao vs Yunjian），
正好可验证音频体检与补丁替换链路。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tarecut import tts                                   # noqa: E402
from tarecut.utils import find_binary, media_duration, run  # noqa: E402

SCRIPT = """# 第1段
大家好，今天给大家带来一台2021年上牌的丰田卡罗拉，表显里程4.2万公里，新车指导价11.98万，现在只要9.8万。
# 第2段
先看外观，整车原版原漆，漆面光泽度非常好，白色车漆特别保值，日常通勤非常有面子。
# 第3段
内饰方面，中控大屏功能正常，座椅没有明显磨损，空间家用完全够用，空调制冷效果也很好。
# 第4段
这台车搭载1.2T发动机，动力输出平顺，百公里油耗只要5.5升，日常代步非常省油，底盘也很扎实。
# 第5段
车况方面，这台车左前轮毂有轻微剐蹭，后保险杠有凹陷补漆，其余部位车况良好，不影响正常使用。
# 第6段
好了，这台车就给大家介绍到这里，喜欢的朋友可以私信我，价格美丽，支持第三方检测，感谢观看。
"""

IMAGES = ["左前轮毂剐蹭", "右后门锈蚀"]  # 第二张故意无对应台词，验证警告


def make_voice(text: str, out_wav: str) -> None:
    cfg = {"engine": "edge-tts", "voice": "zh-CN-XiaoxiaoNeural", "rate": "+0%"}
    tts.synth(text, out_wav, cfg)


def make_video(wav: str, out_mp4: str) -> None:
    dur = media_duration(wav) + 0.4
    p = run([find_binary("ffmpeg"), "-y",
             "-f", "lavfi", "-i",
             f"color=c=0x1d2b3a:s=720x1280:d={dur:.2f}:r=30",
             "-i", wav,
             "-c:v", "libx264", "-preset", "fast", "-crf", "20",
             "-c:a", "aac", "-shortest", out_mp4], timeout=300)
    assert p.returncode == 0, p.stderr[-400:]


def make_image(text: str, out_jpg: str) -> None:
    vf = (f"drawtext=text='{text}':fontfile=C\\\\:/Windows/Fonts/msyh.ttc"
          f":fontcolor=white:fontsize=72:x=(w-text_w)/2:y=(h-text_h)/2")
    p = run([find_binary("ffmpeg"), "-y",
             "-f", "lavfi", "-i", "color=c=0x8b3a3a:s=800x600",
             "-vf", vf, "-frames:v", "1", "-q:v", "2", out_jpg], timeout=120)
    assert p.returncode == 0, p.stderr[-400:]


def main():
    demo = ROOT / "input" / "DEMO"
    vdir = demo / "videos"
    idir = demo / "瑕疵图"
    tmp = ROOT / "tests" / "_fixture"
    for d in (vdir, idir, tmp):
        d.mkdir(parents=True, exist_ok=True)
    (demo / "文案.txt").write_text(SCRIPT, encoding="utf-8")

    for i, para in enumerate(_paras(SCRIPT), 1):
        wav = str(tmp / f"seg{i}.wav")
        print(f"[fixture] 合成第{i}段语音…")
        make_voice(para, wav)
        print(f"[fixture] 生成第{i}段视频…")
        make_video(wav, str(vdir / f"{i}.mp4"))

    for name in IMAGES:
        print(f"[fixture] 生成瑕疵图 {name}.jpg")
        make_image(name, str(idir / f"{name}.jpg"))

    print(f"[fixture] 完成 → {demo}")


def _paras(script: str):
    seg = None
    out = {}
    for line in script.splitlines():
        if line.strip().startswith("#"):
            seg = int("".join(c for c in line if c.isdigit()) or 0)
            out[seg] = []
        elif seg:
            out[seg].append(line)
    return ["".join(out[i]).strip() for i in sorted(out)]


if __name__ == "__main__":
    main()
