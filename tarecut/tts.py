"""TTS 引擎：edge-tts 内置 / command 自定义（可接音色克隆）。"""
from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import Path

from .utils import ffmpeg_convert


def _dashscope_tts(text: str, out_path: str, cfg: dict,
                   voice_id: str | None = None,
                   speech_rate: float | None = None) -> None:
    """阿里云百炼 CosyVoice 合成（国内直连，返回 mp3 字节）。

    voice_id 传入声音复刻注册的音色 ID 时优先生效（克隆音色）；
    speech_rate 为原生语速倍率（1.0-2.0），未指定时用 cfg rate。
    """
    import os
    ds = cfg.get("dashscope", {}) or {}
    key = (ds.get("api_key") or os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "缺少 DashScope API Key（tts.dashscope.api_key 或环境变量 DASHSCOPE_API_KEY）")
    import dashscope
    from dashscope.audio.tts_v2 import SpeechSynthesizer
    dashscope.api_key = key
    model = ds.get("model", "cosyvoice-v2")
    voice = voice_id or ds.get("voice", "longcheng_v2")
    kwargs = {}
    if speech_rate and float(speech_rate) > 1.0:
        kwargs["speech_rate"] = min(float(speech_rate), 2.0)
    else:
        rate = cfg.get("rate") or "+0%"
        if rate not in ("+0%", "+0", "0%", "0"):
            try:  # "+10%" → speech_rate 1.1
                kwargs["speech_rate"] = 1.0 + float(rate.strip("%+ ")) / 100.0
            except ValueError:
                pass
    import time
    last, audio = None, None
    for attempt in range(3):  # 云端偶发 websocket 抖动/超时，重试同一句
        try:
            synth = SpeechSynthesizer(model=model, voice=voice, **kwargs)
            audio = synth.call(text)
            if audio:
                break
            last = "合成返回空音频"
        except Exception as exc:  # 连接超时等，换新连接重试
            last, audio = exc, None
        if attempt < 2:
            time.sleep(2.0 * (attempt + 1))
    if not audio:
        raise RuntimeError(f"CosyVoice 合成失败（已重试 3 次）: {last}")
    Path(out_path).write_bytes(audio)


def _public_url(path: str) -> str | None:
    """把本地音频上传到公网直链（供百炼服务器拉取），返回 URL。

    优先 uguu.se（实测 DashScope 可拉取），备用 catbox.moe。拿到 URL 后校验
    Content-Length 与本地文件一致——防止图床/缓存层返回旧上传的
    假 URL（曾导致所有转写内容串成同一段音频）。
    """
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    data = Path(path).read_bytes()

    def _ok(url: str) -> bool:
        if not url.startswith("http"):
            return False
        try:  # 校验远端文件大小与本地一致
            h = requests.head(url, headers=headers, timeout=30)
            if int(h.headers.get("Content-Length") or -1) == len(data):
                return True
        except Exception:
            return False
        return False

    try:  # uguu.se：multipart 字段 files[]（实测 DashScope 可拉取）
        r = requests.post(
            "https://uguu.se/upload", headers=headers, timeout=120,
            files={"files[]": ("audio.wav", data, "audio/wav")})
        u = ((r.json() or {}).get("files") or [{}])[0].get("url") or ""
        if _ok(u):
            return u
    except Exception:
        pass
    try:  # catbox.moe：字段 fileToUpload，纯文本直链（DashScope 常拉不动）
        r = requests.post(
            "https://catbox.moe/user/api.php", headers=headers, timeout=120,
            data={"reqtype": "fileupload"},
            files={"fileToUpload": ("audio.wav", data, "audio/wav")})
        u = r.text.strip()
        if _ok(u):
            return u
    except Exception:
        pass
    return None


def cached_voice_id(cache_dir: str) -> str | None:
    """只读缓存拿全局克隆音色 ID（未过期即有效）。

    无固定参考音（reference.path 为空）时，各车参考音本来就不同，
    音色以首次注册为准全局复用——这里不做 md5 校验。
    """
    import datetime
    import json

    cache_file = Path(cache_dir) / "voice_id.json"
    if not cache_file.exists():
        return None
    try:
        c = json.loads(cache_file.read_text(encoding="utf-8"))
        age = (datetime.date.today()
               - datetime.date.fromisoformat(c["date"])).days
        if c.get("voice_id") and age <= 25:
            return c["voice_id"]
    except Exception:
        pass
    return None


def enroll_voice(ref_wav: str, cache_dir: str, cfg: dict) -> str:
    """把参考音注册为 DashScope 声音复刻音色，返回 voice_id。

    参考音先上传公网直链，再调用百炼 CosyVoice 声音复刻注册接口。
    结果缓存到 cache_dir/voice_id.json（参考音不变可复用 25 天，
    复刻音色服务端有效期 30 天，到期自动重新注册）。
    """
    import datetime
    import hashlib
    import json
    import os

    import requests

    ds = cfg.get("dashscope", {}) or {}
    key = (ds.get("api_key") or os.environ.get("DASHSCOPE_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("缺少 DashScope API Key，无法注册克隆音色")
    model = ds.get("model", "cosyvoice-v2")

    md5 = hashlib.md5(Path(ref_wav).read_bytes()).hexdigest()
    cache_file = Path(cache_dir) / "voice_id.json"
    if cache_file.exists():
        try:
            c = json.loads(cache_file.read_text(encoding="utf-8"))
            age = (datetime.date.today()
                   - datetime.date.fromisoformat(c["date"])).days
            if c.get("ref_md5") == md5 and c.get("voice_id") and age <= 25:
                return c["voice_id"]
        except Exception:
            pass

    url = _public_url(ref_wav)
    if not url:
        raise RuntimeError("参考音上传公网直链失败，无法注册克隆音色")
    r = requests.post(
        "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        json={"model": "voice-enrollment",
              "input": {"action": "create_voice", "target_model": model,
                        "prefix": "tarecut", "url": url}},
        timeout=120)
    if r.status_code != 200:
        raise RuntimeError(
            f"声音复刻注册失败(HTTP {r.status_code})——请到百炼控制台开通"
            f"「声音复刻/CosyVoice 声音复刻」后重试: {r.text[:200]}")
    body = r.json() or {}
    out = body.get("output") or body.get("data") or {}
    vid = (out.get("voice_id") or out.get("voice_enrollment_id")
           or body.get("voice_id"))
    if not vid:
        raise RuntimeError(f"声音复刻返回异常: {r.text[:300]}")
    cache_file.write_text(json.dumps(
        {"ref_md5": md5, "date": datetime.date.today().isoformat(),
         "voice_id": vid}, ensure_ascii=False), encoding="utf-8")
    return vid


def _trim_tail_silence(wav: str) -> None:
    """裁掉 TTS 结尾静音（保留 0.1s 过渡），提高补丁窗口利用率。"""
    from .utils import find_binary, run
    tmp = wav + ".trim.wav"
    p = run([find_binary("ffmpeg"), "-y", "-i", wav, "-af",
             "areverse,silenceremove=start_periods=1:start_threshold=-45dB"
             ":start_silence=0.1,areverse", tmp], timeout=120)
    if p.returncode == 0 and Path(tmp).exists() and Path(tmp).stat().st_size > 44:
        Path(tmp).replace(wav)
    else:
        Path(tmp).unlink(missing_ok=True)


def _edge_tts(text: str, out_mp3: str, cfg: dict) -> None:
    import edge_tts

    async def _run():
        com = edge_tts.Communicate(text, cfg.get("voice", "zh-CN-YunjianNeural"),
                                   rate=cfg.get("rate", "+0%"))
        await com.save(out_mp3)

    try:
        asyncio.run(_run())
    except Exception as e:  # 网络不通等
        raise RuntimeError(
            f"edge-tts 合成失败（检查网络）：{e}") from e


def _command_tts(text: str, out_wav: str, cfg: dict, ref_wav: str | None) -> None:
    tmpl = cfg.get("command") or ""
    if not tmpl:
        raise RuntimeError("tts.engine=command 但未配置 tts.command 模板")
    cmd_str = (tmpl.replace("{text}", text)
                   .replace("{out}", str(Path(out_wav).resolve()))
                   .replace("{ref}", str(ref_wav or "")))
    parts = shlex.split(cmd_str, posix=False)
    parts = [p.strip('"') for p in parts if p.strip()]
    p = subprocess.run(parts, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    if p.returncode != 0:
        raise RuntimeError(f"自定义 TTS 失败: {p.stderr[-400:]}")


def _apply_pronunciation(text: str, cfg: dict) -> str:
    """读音纠错表：TTS 输入前把易错词替换为正确读音写法，字幕仍用原文。

    cfg["pronunciation"] = {"口播词": "读得对的同音写法"} 形式——直接替换成同音字。
    长键优先（如 国VI 先于 国V），否则短键会把长词拆开替换成"国五I"。
    """
    table = cfg.get("pronunciation") or {}
    for wrong, right in sorted(table.items(), key=lambda kv: -len(str(kv[0]))):
        if wrong and wrong in text:
            text = text.replace(wrong, right)
    return text


def synth(text: str, out_wav: str, cfg: dict, ref_wav: str | None = None,
          voice_id: str | None = None,
          speech_rate: float | None = None) -> str:
    """合成一句修正音频，统一输出 44100Hz 单声道 wav。"""
    text = _apply_pronunciation(text, cfg)
    engine = cfg.get("engine", "edge-tts")
    tmp = str(Path(out_wav).with_suffix(".tmp"))
    if engine == "dashscope":
        _dashscope_tts(text, tmp + ".mp3", cfg,
                       voice_id=voice_id, speech_rate=speech_rate)
        src = tmp + ".mp3"
    elif engine == "edge-tts":
        _edge_tts(text, tmp + ".mp3", cfg)
        src = tmp + ".mp3"
    elif engine == "command":
        _command_tts(text, tmp + ".wav", cfg, ref_wav)
        src = tmp + ".wav"
    else:
        raise RuntimeError(f"未知 TTS 引擎: {engine}")
    ffmpeg_convert(src, out_wav)
    for p in (tmp + ".mp3", tmp + ".wav"):
        Path(p).unlink(missing_ok=True)
    _trim_tail_silence(out_wav)
    return out_wav


def extract_reference(seg_wav: str, out_wav: str, start: float,
                      duration: float, rate: int = 44100) -> str | None:
    """从该段音频截取干净人声作为音色克隆参考。"""
    from .utils import find_binary, run
    p = run([find_binary("ffmpeg"), "-y", "-i", str(seg_wav),
             "-ss", f"{max(start, 0):.3f}", "-t", f"{duration:.3f}",
             "-ar", str(rate), "-ac", "1", str(out_wav)], timeout=120)
    if p.returncode != 0:
        return None
    return out_wav
