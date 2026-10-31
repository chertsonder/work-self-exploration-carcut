"""转写封装：云端 DashScope / 本地 faster-whisper + 词级时间戳 + 缓存指纹失效。"""
from __future__ import annotations

import os
from pathlib import Path

from .utils import find_binary, load_json, run, save_json

_MODEL_CACHE: dict = {}


def _resolve_device(cfg: dict):
    device = (cfg.get("device") or "auto")
    compute = (cfg.get("compute_type") or "auto")
    if compute == "auto":
        compute = "default"
    return device, compute


def get_model(cfg: dict):
    hf_endpoint = cfg.get("hf_endpoint")
    if hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", hf_endpoint)
    device, compute = _resolve_device(cfg)
    key = (cfg.get("model_local", "small"), device, compute)
    if key not in _MODEL_CACHE:
        from faster_whisper import WhisperModel
        _MODEL_CACHE[key] = WhisperModel(
            key[0], device=device, compute_type=compute)
    return _MODEL_CACHE[key]


def video_sig(path: str) -> str:
    """文件指纹（大小+mtime），用于缓存失效判断：源变了缓存即失效。"""
    st = Path(path).stat()
    return f"{st.st_size:x}-{st.st_mtime_ns:x}"


def _sig_path(cache_path: str) -> Path:
    return Path(str(cache_path) + ".sig")


def _sig_ok(cache_path: str, sig: str | None) -> bool:
    """缓存命中时校验指纹；sig=None 视为不校验（兼容诊断脚本）。"""
    if sig is None:
        return True
    p = _sig_path(cache_path)
    if not p.exists():
        return False
    try:
        return p.read_text(encoding="utf-8").strip() == sig
    except Exception:
        return False


def _sig_save(cache_path: str, sig: str | None) -> None:
    if sig is not None:
        try:
            _sig_path(cache_path).write_text(sig, encoding="utf-8")
        except Exception:
            pass


def extract_audio(video: str, out_wav: str, rate: int = 16000,
                  sig: str | None = None) -> str:
    """提取 16k 单声道 wav 供 ASR。sig=源视频指纹，不匹配则重提取。

    重提取会更新 wav 的 mtime，从而级联失效下游 asr/wl 缓存。
    """
    if Path(out_wav).exists() and _sig_ok(out_wav, sig):
        return out_wav
    Path(out_wav).parent.mkdir(parents=True, exist_ok=True)
    p = run([find_binary("ffmpeg"), "-y", "-i", str(video), "-vn",
             "-ac", "1", "-ar", str(rate), str(out_wav)], timeout=300)
    if p.returncode != 0 or not Path(out_wav).exists():
        raise RuntimeError(f"提取音频失败: {video}\n{p.stderr[-400:]}")
    _sig_save(out_wav, sig)
    return out_wav


def transcribe_words(audio_wav: str, cfg: dict) -> list:
    """返回 [{"w": 文本, "s": 起, "e": 止}]，繁体统一转简体。

    backend: "dashscope"（云端 Paraformer，缺省）或 "local"（faster-whisper）。
    """
    import zhconv
    if (cfg.get("backend") or "dashscope") == "dashscope":
        from .asr_dashscope import transcribe_words as ds_words
        return ds_words(audio_wav, cfg)
    model = get_model(cfg)
    kwargs = {"word_timestamps": True, "beam_size": 5}
    if cfg.get("language"):
        kwargs["language"] = cfg["language"]
    segments, _info = model.transcribe(audio_wav, **kwargs)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            txt = (w.word or "").strip()
            if not txt:
                continue
            words.append({"w": zhconv.convert(txt, "zh-cn"),
                          "s": float(w.start), "e": float(w.end)})
    return words


def transcribe_cached(audio_wav: str, cache_path: str, cfg: dict,
                      expect_text: str | None = None,
                      log=print, sig: str | None = None) -> list:
    """转写并缓存 json。expect_text 传该段台词（归一化前）时做内容
    校验：服务端故障会把"账号上次成功的转写结果"原样返回（所有
    文件转出同一段话），重叠率 <55% 视为假结果，自动重试。

    sig=缓存指纹（音频文件指纹+ASR 配置），不匹配则视为未命中重转。
    """
    if Path(cache_path).exists() and _sig_ok(cache_path, sig):
        return load_json(cache_path)

    from .normalize import normalize
    want = normalize(expect_text) if expect_text else ""

    def _bad(words: list) -> bool:
        if not want:
            return False
        got = normalize("".join(w["w"] for w in words))
        if not got:
            return True
        common = sum(min(want.count(c), got.count(c)) for c in set(want))
        return common / max(len(want), 1) < 0.55

    def _preview(words: list) -> str:
        got = normalize("".join(w["w"] for w in words)) if words else ""
        return got[:40] or "空"

    import time as _time
    waits = (15, 30, 60, 120, 180)  # 服务端坏窗口约几分钟，指数退避跨过去
    for attempt, wait in enumerate(waits, 1):
        words = transcribe_words(audio_wav, cfg)
        if not _bad(words):
            save_json(cache_path, words)
            _sig_save(cache_path, sig)
            return words
        log(f"[asr] 转写内容与台词不符(第{attempt}次，实得: {_preview(words)})，"
            f"{wait}s 后重试…")
        _time.sleep(wait)

    # 云端连续失败 → 降级本地 whisper 兜底（离线，无服务端故障）
    log("[asr] 云端转写持续异常，降级本地 whisper（离线，速度慢但稳定）…")
    local_cfg = {**cfg, "backend": "local"}
    for attempt in (1, 2):
        words = transcribe_words(audio_wav, local_cfg)
        if not _bad(words):
            save_json(cache_path, words)
            _sig_save(cache_path, sig)
            return words
        log(f"[asr] 本地转写内容仍不符(第{attempt}次，实得: {_preview(words)})，重试…")
    raise RuntimeError(
        "云端(5次)与本地(2次)转写均与台词不符——请检查音频与文案是否匹配")


def local_words_cached(audio_wav: str, cache_path: str, cfg: dict,
                       sig: str | None = None) -> list:
    """本地 faster-whisper 词级转写并缓存，用于对齐的真实时间戳。

    与 backend 配置无关（云端 Paraformer 的字级时间戳是虚构的均匀网格）。
    文本仅供内部映射用，审计仍以云端结果为准，故不做内容校验重试。
    sig=缓存指纹（音频文件指纹+本地模型配置）。
    """
    if Path(cache_path).exists() and _sig_ok(cache_path, sig):
        return load_json(cache_path)
    words = transcribe_words(audio_wav, {**cfg, "backend": "local"})
    save_json(cache_path, words)
    _sig_save(cache_path, sig)
    return words
