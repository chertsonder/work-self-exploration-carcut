"""阿里云百炼 DashScope 录音文件识别（Paraformer）适配器。

流程：本地 wav → 临时 OSS 上传 → 异步转写任务 → 轮询 → 拉取结果 → 字级时间戳。
输出与本地 whisper 统一：[{"w": 文本, "s": 秒, "e": 秒}]（繁体转简体）。
API Key：优先环境变量 DASHSCOPE_API_KEY，其次 config asr.dashscope.api_key。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests

BASE = "https://dashscope.aliyuncs.com"


def _key(cfg: dict) -> str:
    key = os.environ.get("DASHSCOPE_API_KEY") or (
        cfg.get("dashscope", {}) or {}).get("api_key") or ""
    if not key:
        raise RuntimeError(
            "缺少 API Key：请设置环境变量 DASHSCOPE_API_KEY，"
            "或在 config.yaml asr.dashscope.api_key 填入")
    return key


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def _upload_local(wav: str, key: str, model: str) -> str:
    """上传本地文件到 DashScope 临时存储，返回临时 URL（oss:// 前缀）。

    2026-09 新版接口：GET /api/v1/uploads?action=getPolicy 获取上传凭证，
    再表单上传到 OSS；旧版 POST /api/v1/uploads 已废弃（返回 405）。
    """
    r = requests.get(f"{BASE}/api/v1/uploads",
                     headers=_headers(key),
                     params={"action": "getPolicy", "model": model},
                     timeout=30)
    r.raise_for_status()
    policy = (r.json() or {}).get("data") or {}
    if not policy.get("upload_host") or not policy.get("upload_dir"):
        raise RuntimeError(f"临时上传凭证获取失败: {r.text[:300]}")
    import hashlib
    import uuid
    # OSS 键必须全局唯一：并发跑多个车源时同名 seg1.wav 会互相覆盖，
    # 而转写任务是异步读文件的，读到别的车的音频 → 转写串台
    uniq = f"{uuid.uuid4().hex[:8]}_{hashlib.md5(open(wav, 'rb').read()).hexdigest()[:8]}"
    key_path = f"{policy['upload_dir']}/{uniq}_{Path(wav).name}"
    with open(wav, "rb") as f:
        up = requests.post(policy["upload_host"], files={
            "OSSAccessKeyId": (None, policy["oss_access_key_id"]),
            "Signature": (None, policy["signature"]),
            "policy": (None, policy["policy"]),
            "x-oss-object-acl": (None, policy["x_oss_object_acl"]),
            "x-oss-forbid-overwrite": (None, policy["x_oss_forbid_overwrite"]),
            "key": (None, key_path),
            "success_action_status": (None, "200"),
            "file": (Path(wav).name, f),
        }, timeout=120)
    if up.status_code != 200:
        raise RuntimeError(f"文件上传失败: HTTP {up.status_code} {up.text[:300]}")
    return f"oss://{key_path}"


def _transcribe(file_url: str, key: str, model: str, api_base: str) -> list:
    """提交转写任务并轮询解析，返回 [{"w","s","e"}]。"""
    import zhconv
    r = requests.post(
        f"{api_base}/api/v1/services/audio/asr/transcription",
        headers={**_headers(key), "Content-Type": "application/json",
                 "X-DashScope-Async": "enable"},
        json={"model": model,
              "input": {"file_urls": [file_url]},
              "parameters": {"language_hints": ["zh"],
                             "disfluency_removal_enabled": True}},
        timeout=30)
    r.raise_for_status()
    body = r.json() or {}
    out = body.get("output") or {}
    task_id = out.get("task_id")
    if not task_id:
        raise RuntimeError(f"转写任务提交失败: {r.text[:300]}")

    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(2)
        t = requests.get(f"{api_base}/api/v1/tasks/{task_id}",
                         headers=_headers(key), timeout=30)
        t.raise_for_status()
        tb = (t.json() or {}).get("output") or {}
        status = tb.get("task_status")
        if status in ("PENDING", "RUNNING"):
            continue
        if status != "SUCCEEDED":
            raise RuntimeError(f"转写任务失败: {tb.get('message') or tb}")
        results = tb.get("results") or []
        if not results or not results[0].get("transcription_url"):
            raise RuntimeError(f"转写结果为空: {tb}")
        break
    else:
        raise RuntimeError("转写任务超时(180s)")

    res = requests.get(results[0]["transcription_url"], timeout=60)
    res.raise_for_status()
    payload = res.json() or {}
    words = []
    for tr in payload.get("transcripts", []):
        for sent in tr.get("sentences", []):
            for w in sent.get("words", []):
                txt = (w.get("text") or "").strip()
                if not txt:
                    continue
                words.append({"w": zhconv.convert(txt, "zh-cn"),
                              "s": w["begin_time"] / 1000.0,
                              "e": w["end_time"] / 1000.0})
    if not words:
        raise RuntimeError("转写成功但未解析到任何字级时间戳，"
                           f"响应片段: {str(payload)[:300]}")
    return words


def transcribe_words(audio_wav: str, cfg: dict) -> list:
    """DashScope Paraformer 转写，返回 [{"w","s","e"}]。

    优先公网直链提交（uguu 验证可用）。对结果做自校验：转写末词
    时间应覆盖音频时长的 60% 以上——服务端故障时会把"账号上次
    成功的转写结果"原样返回（曾导致所有文件转出同一段引导语），
    自校验发现可疑即换通道重试。
    """
    from .utils import media_duration

    key = _key(cfg)
    ds = cfg.get("dashscope", {}) or {}
    model = ds.get("model", "paraformer-v2")
    api_base = ds.get("base") or BASE

    def _plausible(words: list) -> bool:
        if not words:
            return False
        dur = media_duration(audio_wav)
        return words[-1]["e"] >= dur * 0.6 or dur <= 2.0

    from .tts import _public_url
    url = _public_url(audio_wav)
    if url:
        try:
            words = _transcribe(url, key, model, api_base)
            if _plausible(words):
                return words
            print(f"[asr] 直链结果可疑(末词{words[-1]['e']:.1f}s/"
                  f"音频{media_duration(audio_wav):.1f}s)，换通道重试…",
                  flush=True)
        except RuntimeError as exc:
            if "SERVER_ERROR" not in str(exc):
                raise
            print("[asr] 公网直链提交遇 SERVER_ERROR，改用临时OSS重试…",
                  flush=True)
    try:
        words = _transcribe(_upload_local(audio_wav, key, model),
                            key, model, api_base)
        if _plausible(words):
            return words
        raise RuntimeError(
            f"转写结果与音频时长不符(疑似服务端返回旧结果)，"
            f"末词 {words[-1]['e']:.1f}s / 音频 {media_duration(audio_wav):.1f}s")
    except RuntimeError as exc:
        if not url or "SERVER_ERROR" not in str(exc):
            raise
    raise RuntimeError("公网直链与临时OSS通道均转写失败")
