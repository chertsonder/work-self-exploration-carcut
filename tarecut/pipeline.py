"""车源级全流程编排：体检 → ASR → 对齐 → 审计 → 补丁 → 字幕 → 瑕疵图 → 渲染 → 拼接。"""
from __future__ import annotations

import re
import wave
from pathlib import Path

from . import asr, audit, flaws, render
from . import probe as probe_mod
from . import subtitles as subs_mod
from . import tts, patch
from .align import align_units, split_units
from .utils import media_duration, find_binary

_MARK_RE = re.compile(
    r"^\s*(?:#+\s*)?[【\[]?\s*第\s*([0-9一二三四五六])\s*段\s*[】\]]?\s*[：:、.]?\s*$")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6}
_VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".flv"}
_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _log(msg: str):
    print(msg, flush=True)


# ---------- 输入解析 ----------

def parse_script(txt: str) -> dict:
    """解析文案 → {段号: 文本}。支持标记行与 6 段空行分隔兜底。"""
    segs, cur, marked = {}, 0, False
    for line in txt.splitlines():
        m = _MARK_RE.match(line)
        if m:
            marked = True
            n = m.group(1)
            cur = int(n) if n.isdigit() else _CN_NUM.get(n, 0)
            segs.setdefault(cur, [])
            continue
        if cur:
            segs[cur].append(line)
    if marked:
        out = {k: "\n".join(v).strip() for k, v in segs.items()}
        missing = [i for i in range(1, 7) if i not in out]
        if missing:
            raise ValueError(f"文案标记不完整，缺少: {missing}（格式：# 第N段）")
        return out
    paras = [p.strip() for p in re.split(r"\n\s*\n", txt) if p.strip()]
    if len(paras) == 6:
        _log("提示: 文案未用「第N段」标记，按 6 个空行分段解析")
        return {i + 1: p for i, p in enumerate(paras)}
    raise ValueError("文案格式无法识别：请用「# 第N段」标记 6 段，或用空行分隔 6 段")


def find_videos(videos_dir: Path) -> list:
    files = [f for f in videos_dir.iterdir()
             if f.is_file() and f.suffix.lower() in _VIDEO_EXT]

    def num(f: Path) -> int:
        m = re.findall(r"\d+", f.stem)
        return int(m[0]) if m else 999
    files.sort(key=lambda f: (num(f), f.name))
    return files


def find_images(src_dir: Path) -> list:
    img_dir = None
    for cand in src_dir.iterdir():
        if cand.is_dir() and ("瑕疵" in cand.name or "图" in cand.name):
            img_dir = cand
            break
    if img_dir is None:
        return [], None
    imgs = sorted([f for f in img_dir.iterdir()
                   if f.is_file() and f.suffix.lower() in _IMG_EXT])
    return imgs, img_dir


def _fmt_t(sec: float) -> str:
    sec = max(sec, 0.0)
    return f"{int(sec // 60):02d}:{sec % 60:04.1f}"


def _flush_group(out: dict, seg: int, group: list, jobs: dict, seg_align: dict):
    units_seg = seg_align[seg]["units"]
    text = "".join(units_seg[u]["text"] for u in group)
    errs = [e for u in group for e in jobs[(seg, u)]]
    out[(seg, group[0])] = {"units": list(group), "text": text, "errs": errs}


def _merge_jobs(jobs: dict, seg_align: dict) -> dict:
    """同段 unit 连续且窗口间隔 < 0.5s 的补丁句合并为一组，一次 TTS 整组重读。

    语速极快的段落逐句替换塞不下（变速到上限仍要截断），整组重读 +
    允许溢出才能保证台词念完整；组内子句节奏也更连贯。
    """
    out = {}
    by_seg = {}
    for (seg, unit) in jobs:
        by_seg.setdefault(seg, []).append(unit)
    for seg, units in by_seg.items():
        units.sort()
        group = [units[0]]
        for u in units[1:]:
            prev = seg_align[seg]["units"][group[-1]]
            cur = seg_align[seg]["units"][u]
            if u == group[-1] + 1 and cur["s"] - prev["e"] < 0.5:
                group.append(u)
            else:
                _flush_group(out, seg, group, jobs, seg_align)
                group = [u]
        _flush_group(out, seg, group, jobs, seg_align)
    return out


# ---------- 主流程 ----------

def _trim_wav(src: str, dst: str, t0: float) -> None:
    """纯 python 掐掉 wav 开头 t0 秒（16k 单声道 16bit，无外部依赖）。"""
    with wave.open(src, "rb") as r:
        fr = r.getframerate()
        n0 = min(int(t0 * fr), max(r.getnframes() - 1, 0))
        r.setpos(n0)
        data = r.readframes(r.getnframes() - n0)
        with wave.open(dst, "wb") as w:
            w.setparams(r.getparams())
            w.writeframes(data)


def _mix_bgm(final: Path, bgm: str, vol: float, log=_log) -> None:
    """成片混入BGM：循环补齐时长、开头0.8s淡入、结尾2s淡出，音量压低垫在
    人声下（normalize=0 不衰减人声）。视频流直拷不重编，仅重编音频。"""
    import subprocess as sp
    dur = media_duration(str(final))
    out = final.with_name(final.stem + ".bgm.mp4")
    fc = (f"[1:a]volume={vol:.3f},afade=t=in:d=0.8,"
          f"afade=t=out:st={max(dur - 2.0, 0.0):.2f}:d=2.0[bg];"
          f"[0:a][bg]amix=inputs=2:duration=first:dropout_transition=0"
          f":normalize=0[a]")
    cmd = [find_binary("ffmpeg"), "-y", "-i", str(final),
           "-stream_loop", "-1", "-i", str(bgm),
           "-filter_complex", fc,
           "-map", "0:v", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(out)]
    r = sp.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"BGM混入失败: {r.stderr[-400:]}")
    out.replace(final)


def process_source(src_dir: Path, out_root: Path, cfg: dict,
                   do_patch: bool = True, log=_log) -> dict:
    src_dir, out_root = Path(src_dir), Path(out_root)
    name = src_dir.name
    # 输出目录/成片命名：src_dir/命名.txt（CSV 成片命名列），无则用车源目录名
    title = ""
    title_file = src_dir / "命名.txt"
    if title_file.exists():
        title = title_file.read_text(encoding="utf-8", errors="replace").strip()
    out_dir = out_root / (title or name)
    work = out_dir / "work"
    cache = work / "cache"
    for d in (work, cache):
        d.mkdir(parents=True, exist_ok=True)

    # 素材
    script_file = src_dir / "文案.txt"
    if not script_file.exists():
        cands = [f for f in src_dir.iterdir() if f.is_file()
                 and "文案" in f.stem and f.suffix.lower() == ".txt"]
        if not cands:
            raise FileNotFoundError(f"缺少 文案.txt: {src_dir}")
        script_file = cands[0]
    segs = parse_script(script_file.read_text(encoding="utf-8", errors="replace"))

    videos_dir = src_dir / "videos"
    if not videos_dir.is_dir():
        raise FileNotFoundError(f"缺少 videos 目录: {src_dir}")
    videos = find_videos(videos_dir)
    if len(videos) != 6:
        raise ValueError(f" videos/ 下应为 6 段视频，实际 {len(videos)} 个: "
                         f"{[v.name for v in videos]}")
    log(f"[{name}] 素材就绪：6 段视频 + 文案 {sum(len(v) for v in segs.values())} 字")

    # ① 体检
    fps_cfg = cfg.get("render", {}).get("fps", "first")
    fps_override = None if fps_cfg == "first" else float(fps_cfg)
    pr = probe_mod.probe_segments([str(v) for v in videos], fps_override)
    fps = pr.fps
    for w in pr.warnings:
        log(f"[{name}] 体检: {w}")
    log(f"[{name}] 基准: {pr.width}x{pr.height} @{fps:.3f}fps, "
        f"总时长 {pr.total_duration:.1f}s")

    # TTS 配置（补丁 / 全文配音两模式共用）
    tts_cfg = dict(cfg.get("tts", {}))
    ds_tts = tts_cfg.setdefault("dashscope", {}) or {}
    if not (ds_tts.get("api_key") or "").strip():
        ds_tts["api_key"] = (
            (cfg.get("asr", {}).get("dashscope", {}) or {}).get("api_key") or "")
    ref_cfg = tts_cfg.get("reference", {}) or {}
    clone_id = None
    full_mode = bool(do_patch and str(tts_cfg.get("mode", "patch")).lower() == "full"
                     and any(v.strip() for v in segs.values()))
    full_wavs = {}

    # 音色克隆：全局只注册一次，所有车源复用（全局缓存 25 天，到期自动重注册）
    gcache = out_root.parent / "cache"
    gcache.mkdir(parents=True, exist_ok=True)

    def _setup_clone() -> None:
        nonlocal clone_id
        if clone_id or not ref_cfg.get("enabled") \
                or tts_cfg.get("engine") != "dashscope":
            return
        fixed_ref = str(ref_cfg.get("path") or "").strip()
        if not fixed_ref:
            # 无固定参考音：全局复用首次注册的音色（各车 seg1 本就不同，
            # 不能按参考音 md5 判断——否则每辆车都会重新注册烧配额）
            clone_id = tts.cached_voice_id(str(gcache))
            if clone_id:
                log(f"[{name}] 复用全局克隆音色: {clone_id}")
                return
            ref_src = str(cache / "seg1.wav")
        else:
            # 配置了固定参考音：md5 校验，参考音变了自动重新注册
            ref_src = fixed_ref
        if not Path(ref_src).exists():
            log(f"[{name}] ⚠ 参考音不存在，跳过克隆: {ref_src}")
            return
        ref_wav = tts.extract_reference(
            ref_src, str(gcache / "reference.wav"),
            float(ref_cfg.get("start", 1.0)),
            float(ref_cfg.get("duration", 11.0)), rate=16000)
        if not ref_wav:
            return
        try:
            # enroll_voice 内部按参考音 md5 校验缓存：相同→复用，
            # 不同→重新注册（仅固定参考音路径下有意义）
            clone_id = tts.enroll_voice(ref_wav, str(gcache), tts_cfg)
            log(f"[{name}] 全局克隆音色: {clone_id}")
        except Exception as exc:
            log(f"[{name}] ⚠ 音色克隆失败，回退官方音色: {exc}")

    def _full_dub(i: int, align0: dict, sils: list) -> tuple:
        """句级对齐全文配音：每句单独合成压进原句时间窗，逐句替换原声。

        句窗口已在 align 阶段贴合真实语音边界（口型最好），句内语速均匀；
        末句允许溢出到段尾。返回成品 wav（时间轴与原声一致）。
        """
        seg_dur = pr.segments[i - 1].duration
        ul = align0["units"]
        ids = list(range(len(ul)))
        files = [str(work / f"fdub_{i}_a.wav"), str(work / f"fdub_{i}_b.wav")]
        cur = str(cache / f"seg{i}.wav")
        stats = {"spill": 0, "cut": 0}
        for n, k in enumerate(ids):
            u = ul[k]
            synth_wav = str(work / f"fdub_{i}_{k}.wav")
            tts.synth(u["text"], synth_wav, tts_cfg, voice_id=clone_id)
            out = files[n % 2] if n + 1 < len(ids) \
                else str(cache / f"full_seg{i}.wav")
            budget = None
            if n + 1 < len(ids):  # 中间句：可吃句间隙，不压下句真实起音
                next_s = ul[ids[n + 1]]["s"]
                next_true = patch.true_onset(sils, next_s)
                cap = (next_true - 0.06) if next_true is not None \
                    else (next_s - 0.30)
                budget = cap if cap > u["e"] else u["e"]
            elif seg_dur - 0.05 > u["e"]:  # 末句：允许溢出到段尾
                budget = seg_dur - 0.05
            info = patch.replace_window(cur, (u["s"], u["e"]), synth_wav,
                                        out, budget_end=budget)
            if info["spill"] > 0.05:
                stats["spill"] += 1
            if info["trimmed"]:
                stats["cut"] += 1
            cur = out
        return cur, stats

    # ② 提取各段音频（两模式共用；指纹不匹配自动重提取）
    for i, video in enumerate(videos, 1):
        asr.extract_audio(str(video), str(cache / f"seg{i}.wav"),
                          sig=asr.video_sig(str(video)))

    # ③ 对齐 + ④ 审计（全文配音模式：先整段配音，再对配音结果 ASR 对齐字幕）
    asr_cfg = dict(cfg.get("asr", {}))
    seg_align = {}
    seg_sils = {}
    words_by_seg = {}   # 各段 ASR 词表（优先本地真实时间戳），供掐头判定用
    all_errors = []
    alt_ok = True  # 本地词级时间戳可用性（模型缺失/加载失败则整轮退回云端）

    def _alt_words(i: int, wav: Path) -> list:
        """本地 whisper 真实词级时间戳，作对齐的时间主数据。

        云端 Paraformer 的字级时间戳是按 token 数均分句长的虚构值，无法
        用于定界；本地 DTW 词时间戳是真实声学边界。取不到则退回云端时间戳。
        缓存指纹=音频文件+本地模型配置：换模型/换音频自动重转。
        """
        nonlocal alt_ok
        if not alt_ok:
            return []
        try:
            wl_sig = (f"{asr.video_sig(str(wav))}|local|"
                      f"{asr_cfg.get('model_local', 'small')}"
                      f"|{asr_cfg.get('device', 'auto')}"
                      f"|{asr_cfg.get('compute_type', 'auto')}")
            return asr.local_words_cached(
                str(wav), str(cache / f"seg{i}.wl.json"), asr_cfg, sig=wl_sig)
        except Exception as exc:  # 模型缺失/内存不足等，整轮退回
            alt_ok = False
            log(f"[{name}] ⚠ 本地词级时间戳不可用（对齐退回云端时间戳）: {exc}")
            return []

    def _asr_sig(wav_path: str) -> str:
        """云端 ASR 缓存指纹=音频文件+backend+模型。"""
        return (f"{asr.video_sig(wav_path)}"
                f"|{asr_cfg.get('backend') or 'dashscope'}"
                f"|{asr_cfg.get('model', 'paraformer-v2')}")

    if full_mode:
        _setup_clone()
        if not clone_id:
            log(f"[{name}] ⚠ 克隆音色不可用，全文配音降级官方音色")
    for i, video in enumerate(videos, 1):
        # 必须重新赋值：否则沿用上一循环残留的 wav（=seg6.wav），
        # 导致所有分段都在转写第 6 段的结尾话术音频
        wav = cache / f"seg{i}.wav"
        units = split_units(segs[i], int(cfg.get("subtitle", {})
                                         .get("max_line_chars", 16)))
        dur_i = pr.segments[i - 1].duration
        has_text = bool(segs[i].strip())
        # 真实静音区间：ASR 词时间戳有 ±0.4s 级误差且方向不一，单元边界
        # 需贴合到静音边界（补丁窗口/字幕时间/全文配音窗口共用）
        sils_i = patch.detect_silences(str(wav)) if has_text else []
        seg_sils[i] = sils_i
        alt_words = _alt_words(i, wav) if has_text else []
        if has_text and full_mode:
            # 句级对齐：先对原声 ASR 拿句窗口 → 逐句配音替换 → 复检配音成品
            words0 = asr.transcribe_cached(str(wav),
                                           str(cache / f"seg{i}.asr.json"),
                                           asr_cfg, expect_text=segs[i],
                                           sig=_asr_sig(str(wav)))
            align0 = align_units(units, words0, dur_i, silences=sils_i,
                                 alt_words=alt_words)
            fwav, dstats = _full_dub(i, align0, sils_i)
            full_wavs[i] = fwav
            align = align0  # 字幕用原声句窗口（口型对齐）
            # 复检读音/漏读；配音每轮新生成(mtime变)→指纹必不匹配→必重转
            words = asr.transcribe_cached(fwav,
                                          str(cache / f"seg{i}.full.asr.json"),
                                          asr_cfg, sig=_asr_sig(fwav))
            warn = (f", ⚠截断{dstats['cut']}句" if dstats["cut"] else "")
            log(f"[{name}] 第{i}段句级配音: {len(align0['units'])}句"
                f"(溢出{dstats['spill']}句{warn}), 时间轴=原声")
        elif has_text:
            words = asr.transcribe_cached(str(wav), str(cache / f"seg{i}.asr.json"),
                                          asr_cfg, expect_text=segs[i],
                                          sig=_asr_sig(str(wav)))
        else:
            words = []  # 纯画面段：无文案则不做 ASR 与审计
            log(f"[{name}] 第{i}段无文案（纯画面段），跳过审计")
        if not (has_text and full_mode):
            align = align_units(units, words, dur_i, silences=sils_i,
                                alt_words=alt_words)
        seg_align[i] = align
        words_by_seg[i] = alt_words or (words if has_text else [])
        if has_text and full_mode:
            # 审计用配音复检对齐（读音/漏读检测），字幕仍用原声窗口
            errs = audit.audit_segment(
                i, align_units(units, words, dur_i, silences=sils_i), cfg)
        else:
            errs = audit.audit_segment(i, align, cfg)
        all_errors.extend(errs)
        bad = sum(1 for e in errs if e["type"] in ("错读", "漏读"))
        tag = "配音音频" if (has_text and full_mode) else "原声"
        if not has_text:
            tsrc = "时间戳=无文案"
        elif alt_words:
            tsrc = f"时间戳=本地{len(alt_words)}词(真实)"
        else:
            tsrc = "时间戳=云端(虚构)"
        log(f"[{name}] 第{i}段: {len(units)}条字幕, ASR {len(words)}词({tag}), "
            f"可疑差异 {bad} 处, {tsrc}")

    # ⑤ TTS 补丁 + ⑥ 定点替换（全文配音模式整段重读，不做定点补丁）
    patched_wav, patch_rows = {}, []
    # 强制补丁：原声把某些词读错（声调/多音字），ASR 逐字比对只认字形查不
    # 出来（如"保险杠"读 gāng、"剐蹭"读 guā），配置命中的句子直接重念，
    # 配合 tts.pronunciation 得到正确读音
    forced_errs = []
    if not full_mode and do_patch:
        forced_words = [str(w).strip()
                        for w in (tts_cfg.get("force_patch_words") or [])
                        if str(w).strip()]
        # 车源级强制补丁词：input/{车源}/强制补丁.txt，一行一词（仅该车生效，
        # 用于个别播音员把"匹配/2.0"这类 ASR 听不出的读音错误）
        extra_f = src_dir / "强制补丁.txt"
        if extra_f.exists():
            extra = [ln.strip() for ln in
                     extra_f.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
            if extra:
                forced_words += extra
                log(f"[{name}] 车源级强制补丁词: {'、'.join(extra)}")
        if forced_words:
            # 只跳过「本来就会重念」的单元；同音异字等不会补丁的单元仍要
            # 强制重念（它可能正含读错的词）
            will_patch = {(e["seg"], e["unit"]) for e in all_errors
                          if audit.is_patchable(e)}
            for seg, al in sorted(seg_align.items()):
                for i, u in enumerate(al["units"]):
                    if (seg, i) in will_patch \
                            or not any(w in u["text"] for w in forced_words):
                        continue
                    forced_errs.append({
                        "seg": seg, "type": "读音", "script": u["text"],
                        "asr": "", "s": u["s"], "e": u["e"],
                        "ci0": u["ci0"], "ci1": u["ci1"], "unit": i})
            if forced_errs:
                all_errors.extend(forced_errs)
                all_errors.sort(key=lambda e: (e["seg"], e["s"]))
                log(f"[{name}] 强制补丁词命中 {len(forced_errs)} 句"
                    f"（原声读音不可靠，改为重念）")
    patchable = [] if full_mode else (
        [e for e in all_errors if audit.is_patchable(e)] + forced_errs)
    jobs = {}
    for e in patchable:
        jobs.setdefault((e["seg"], e["unit"]), []).append(e)
    merged_jobs = _merge_jobs(jobs, seg_align)

    if do_patch and merged_jobs:
        _setup_clone()
        n_units = sum(len(j["units"]) for j in merged_jobs.values())
        log(f"[{name}] 需补丁 {len(merged_jobs)} 组（{n_units} 句），开始 TTS 定点替换…")
        for (seg, unit), job in sorted(merged_jobs.items()):
            units_seg = seg_align[seg]["units"]
            u_first = units_seg[job["units"][0]]
            u_last = units_seg[job["units"][-1]]
            cur_wav = patched_wav.get(seg, str(cache / f"seg{seg}.wav"))
            out_wav = work / f"patch_seg{seg}_u{unit}.wav"
            # 单元窗口已在 align 阶段贴合真实语音边界（ASR 词时间戳有
            # ±0.4s 级误差且方向不一，静音边界才可靠），直接取用
            nxt = job["units"][-1] + 1
            next_s = units_seg[nxt]["s"] if nxt < len(units_seg) else None
            s0, e0 = u_first["s"], u_last["e"]
            # 补丁预算右界：优先用下一句真实起音（静音边界，比 ASR 起点准，
            # 只留极小防粘连带）；无静音信息时退回 ASR 起点-0.30s 保护带；
            # 间隙不足则完全不允许溢出，否则会吃掉下一句开头（听感=截断）
            budget = None
            if nxt < len(units_seg):
                next_true = patch.true_onset(seg_sils.get(seg, []), next_s)
                cap = (next_true - 0.06) if next_true is not None \
                    else (next_s - 0.30)
                budget = cap if cap > e0 else e0
            try:
                synth_wav = str(work / f"tts_seg{seg}_u{unit}.wav")
                tts.synth(job["text"], synth_wav, tts_cfg, voice_id=clone_id)
                native = None
                wd = max(e0 - s0, 0.3)
                need = media_duration(synth_wav) / wd
                max_native = float(tts_cfg.get("max_native_speed", 1.6))
                if need > max_native:
                    # 句子远超窗口（原声语速极快），强行 2x+ 压缩又赶又假还
                    # 可能溢出吃下一句（742 CLTC续航句教训）——放弃补丁保留
                    # 原声，报告中提示人工确认
                    patch_rows.append({"seg": seg, "unit": unit,
                                       "units": list(job["units"]),
                                       "text": job["text"], "ok": False,
                                       "native": None,
                                       "voice": "clone" if clone_id else "official",
                                       "errs": [e["type"] for e in job["errs"]],
                                       "error": f"需压缩x{need:.2f}>上限{max_native:.1f}，保留原声"})
                    log(f"[{name}] ⚠ 放弃补丁 第{seg}段「{job['text'][:14]}…」"
                        f"需变速x{need:.2f}超上限，保留原声待人工确认")
                    continue
                if need > 1.30:  # DSP 变速吃力 → TTS 原生加速分担（音质更好）
                    native = min(need / 1.25, 1.8)  # DSP 只分担 ~1.25x，保完整不截断
                    tts.synth(job["text"], synth_wav, tts_cfg,
                              voice_id=clone_id, speech_rate=native)
                info = patch.replace_window(
                    cur_wav, (s0, e0),
                    synth_wav, str(out_wav), budget_end=budget)
                patched_wav[seg] = str(out_wav)
                patch_rows.append({"seg": seg, "unit": unit,
                                   "units": list(job["units"]),
                                   "text": job["text"], "window": info,
                                   "ok": True, "native": native,
                                   "voice": "clone" if clone_id else "official",
                                   "errs": [e["type"] for e in job["errs"]]})
                mark = ""
                if info["trimmed"]:
                    mark = " ⚠仍截断"
                elif info["spill"] > 0.01:
                    mark = f" 溢出+{info['spill']:.2f}s"
                log(f"[{name}] 已补丁 第{seg}段 {job['text'][:18]}… "
                    f"(变速 x{info['tempo']:.2f}"
                    f"{' ⚠超界' if info['clamped'] else ''}{mark}"
                    f"{' [克隆音色]' if clone_id else ''})")
            except Exception as exc:
                patch_rows.append({"seg": seg, "unit": unit,
                                   "units": list(job["units"]),
                                   "text": job["text"], "ok": False,
                                   "native": None,
                                   "voice": "clone" if clone_id else "official",
                                   "errs": [e["type"] for e in job["errs"]],
                                   "error": str(exc)})
                log(f"[{name}] ⚠ 补丁失败(保留原音频) 第{seg}段: {exc}")
    elif do_patch:
        log(f"[{name}] 无需音频补丁")
    else:
        log(f"[{name}] 已跳过补丁(--no-patch)")

    if full_mode and full_wavs:  # 全文配音：整段音频交给渲染
        patched_wav.update(full_wavs)

    # 开头掐头：首句真实起音之前若还有文案外的口播（ASR 实听有词、文案
    # 无，如 740 开头"兄弟们今天给你们看个狠货"），音画同掐，字幕/瑕疵
    # 窗随之前移。无多余语音（首句起点 <0.6s 或首句前无词）则不动。
    rd = cfg.get("render", {})
    trims = {}
    if not full_mode and rd.get("leading_trim", True):
        for i in range(1, 7):
            al = seg_align.get(i)
            if not al or not al["units"]:
                continue
            t0 = al["units"][0]["s"]
            if t0 < 0.6:
                continue
            ws = words_by_seg.get(i) or []
            if any(float(w.get("s", 9e9)) < t0 - 0.35 for w in ws):
                trims[i] = round(float(t0), 3)
    for i, t0 in trims.items():
        src_wav = patched_wav.get(i, str(cache / f"seg{i}.wav"))
        dst = str(work / f"trim_seg{i}.wav")
        _trim_wav(src_wav, dst, t0)
        patched_wav[i] = dst
        for u in seg_align[i]["units"]:
            u["s"] = round(max(0.0, u["s"] - t0), 3)
            u["e"] = round(max(0.0, u["e"] - t0), 3)
        log(f"[{name}] 第{i}段掐头 {t0:.2f}s（首句前有文案外口播，音画同剪）")

    # ⑦ 字幕
    sub_cfg = cfg.get("subtitle", {})
    offsets = {}
    acc = 0.0
    for i in range(1, 7):
        offsets[i] = acc
        acc += (media_duration(patched_wav[i]) if i in patched_wav
                else pr.segments[i - 1].duration)
    global_entries = []
    for i in range(1, 7):
        entries = [(u["s"], u["e"], u["text"]) for u in seg_align[i]["units"]
                   if u["text"].strip()]
        seg = pr.segments[i - 1]  # 各分镜保留原始分辨率，字幕按该段尺寸排版
        subs_mod.write_ass(entries, str(work / f"subs_{i}.ass"),
                           seg.width, seg.height, sub_cfg)
        global_entries += [(s + offsets[i], e + offsets[i], t)
                           for s, e, t in entries]
    subs_mod.write_srt(global_entries, str(out_dir / "字幕.srt"))
    subs_mod.write_ass(global_entries, str(out_dir / "字幕.ass"),
                       pr.width, pr.height, sub_cfg)

    # ⑧ 瑕疵图
    flaw_cfg = cfg.get("flaws", {})
    syn_cfg = flaw_cfg.get("synonyms", {}) or {}
    plan, flaw_warnings = [], []
    if flaw_cfg.get("enabled", True):
        imgs, img_dir = find_images(src_dir)
        if img_dir is None:
            flaw_warnings.append("未找到瑕疵图目录（瑕疵图/），跳过叠加")
        elif not imgs:
            flaw_warnings.append("瑕疵图目录为空")
        else:
            units5 = seg_align[5]["units"]
            seg5_dur = (media_duration(patched_wav[5]) if 5 in patched_wav
                        else pr.segments[4].duration)
            plan, flaw_warnings = flaws.plan_overlays(
                [flaws.parse_image(p, syn_cfg) for p in imgs],
                units5, seg5_dur, syn_cfg, flaw_cfg)
    flaw_by_seg = {}
    for p in plan:
        flaw_by_seg.setdefault(5, []).append(p)

    # ⑨ 渲染 + 拼接
    rd = cfg.get("render", {})
    part_names = []
    for i, video in enumerate(videos, 1):
        out_name = f"seg{i}.render.mp4"
        imgs = [dict(x, path=str(Path(x["path"]).resolve()))
                for x in flaw_by_seg.get(i, [])]
        aw = patched_wav.get(i)
        aw_path = str(Path(aw).resolve()) if aw else None
        out_dur = None
        if aw_path:
            out_dur = media_duration(aw_path)
        elif (cache / f"seg{i}.wav").exists():
            out_dur = media_duration(str(cache / f"seg{i}.wav"))
        render.render_segment(
            video=str(Path(video).resolve()), ass_name=f"subs_{i}.ass",
            images=imgs,
            audio_wav=aw_path,
            out_dur=out_dur,
            video_dur=pr.segments[i - 1].duration,
            trim_start=trims.get(i, 0.0),
            out_name=out_name,
            width=pr.segments[i - 1].width,   # 保留该分镜原始分辨率
            height=pr.segments[i - 1].height,
            fps=fps,
            flaw_cfg=flaw_cfg, crf=int(rd.get("crf", 18)),
            preset=str(rd.get("preset", "medium")), workdir=str(work))
        part_names.append(out_name)
        log(f"[{name}] 第{i}段渲染完成")
    # 成片：目录已按命名.txt 命名；同名成片已存在则加序号 -2/-3… 区分，不覆盖旧片
    base = title or '成片'
    final = out_dir / f"{base}.mp4"
    k = 2
    while final.exists():
        final = out_dir / f"{base}-{k}.mp4"
        k += 1
    render.concat_segments(part_names, str(final), str(work))
    log(f"[{name}] 成片: {final}")
    # ⑩ BGM：混入配置的背景音乐（音量压低垫人声下，结尾淡出）
    bgm_path = str(rd.get("bgm") or "").strip()
    if bgm_path:
        if Path(bgm_path).exists():
            _mix_bgm(final, bgm_path, float(rd.get("bgm_volume", 0.12)), log)
            log(f"[{name}] 已混入BGM (音量{rd.get('bgm_volume', 0.12)})")
        else:
            log(f"[{name}] ⚠ BGM文件不存在，跳过: {bgm_path}")

    # 报告
    report = _build_report(name, pr, fps, all_errors, patch_rows,
                           plan, flaw_warnings, do_patch, offsets, full_mode)
    (out_dir / "report.md").write_text(report, encoding="utf-8")

    return {"name": name, "final": str(final), "errors": all_errors,
            "patched": [r for r in patch_rows if r["ok"]],
            "patch_failed": [r for r in patch_rows if not r["ok"]],
            "flaw_plan": plan, "warnings": pr.warnings + flaw_warnings}


# ---------- 报告 ----------

def _build_report(name, pr, fps, errors, patch_rows, plan, flaw_warnings,
                  do_patch, offsets, full_mode=False) -> str:
    L = [f"# 自动剪辑报告 · {name}", "",
         f"- 视频基准: {pr.width}x{pr.height} @ {fps:.3f}fps，总时长 "
         f"{pr.total_duration:.1f}s",
         f"- 配音模式: {'全文配音（克隆音色整段重读）' if full_mode else 'TTS 定点补丁'}",
         f"- 素材体检警告: {len(pr.warnings)} 条", ""]

    L += ["## 一、音频体检（ASR 与文案逐字比对）", ""]
    patch_keys = set()
    for r in patch_rows:
        if r["ok"]:
            for u in (r.get("units") or [r["unit"]]):
                patch_keys.add((r["seg"], u))
    if errors:
        L += ["| 段 | 时间(段内) | 类型 | 文案 | 实际读出 | 处理 |",
              "|---|---|---|---|---|---|"]
        for e in errors:
            key = (e["seg"], e["unit"])
            done = "✅ 已TTS补丁" if key in patch_keys else (
                "ℹ 全文重配已覆盖" if full_mode else
                "低优先级(同音)" if e["type"] == "同音异字" else
                "ℹ ASR幻听(未补丁)" if e["type"] == "字母数字幻听" else
                "⚠ 建议人工/LibTV重生成")
            L.append(f"| {e['seg']} | {_fmt_t(e['s'])}-{_fmt_t(e['e'])} "
                     f"| {e['type']} | {e['script'] or '—'} "
                     f"| {e['asr'] or '—'} | {done} |")
    else:
        L.append("未发现文案与语音的差异。")
    L.append("")

    L += ["## 二、TTS 补丁记录", ""]
    if patch_rows:
        L += ["| 段 | 句子 | 替换窗口(s) | TTS原生语速 | DSP变速 | 结果 |",
              "|---|---|---|---|---|---|"]
        for r in patch_rows:
            if r["ok"]:
                w = r["window"]
                win = f"{w['s']:.2f}-{w['e2']:.2f}"
                if w.get("spill", 0) > 0.01:
                    win += f"(溢出{w['spill']:.2f}s)"
                native = f"x{r['native']:.2f}" if r.get("native") else "—"
                tempo = (f"x{w['tempo']:.2f}{'⚠超界' if w['clamped'] else ''}"
                         f"{'⚠截断' if w.get('trimmed') else ''}")
                voice = "克隆音色" if r.get("voice") == "clone" else "官方音色"
                L.append(f"| {r['seg']} | {r['text'][:24]} | {win} | {native} "
                         f"| {tempo} | ✅ {voice} |")
            else:
                L.append(f"| {r['seg']} | {r['text'][:24]} | — | — | — "
                         f"| ❌ {r.get('error','')[:60]} |")
    else:
        L.append("无补丁。" if do_patch else "本次运行未启用补丁(--no-patch)。")
    L.append("")

    L += ["## 三、瑕疵图叠加", ""]
    if plan:
        L += ["| 图片 | 段 | 时间(段内) | 对应台词 |", "|---|---|---|---|"]
        for p in plan:
            L.append(f"| {p['name']} | 5 | {_fmt_t(p['s'])}-{_fmt_t(p['e'])} | — |")
    else:
        L.append("无叠加计划。")
    if flaw_warnings:
        L += ["", "**警告:**"]
        L += [f"- {w}" for w in flaw_warnings]
    L += ["", "## 四、其他警告", ""] if pr.warnings else ["", "## 四、其他警告", ""]
    L += [f"- {w}" for w in pr.warnings] or ["- 无"]
    return "\n".join(L)
