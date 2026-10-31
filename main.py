"""tarecut CLI：单条 / 批量 车源处理入口。"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

# HF 模型缓存放到项目目录（E盘），不占用 C 盘用户目录
_ROOT = Path(__file__).resolve().parent

# 加载项目 .env（系统环境变量优先于文件内容）
_envf = _ROOT / ".env"
if _envf.exists():
    for _line in _envf.read_text(encoding="utf-8-sig").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip("'\""))

os.environ.setdefault("HF_HUB_CACHE", str(_ROOT / "hf-cache"))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from tarecut import pipeline  # noqa: E402  (需在环境变量之后导入)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="批量自动化剪辑：拼接/零错字硬字幕/音频体检/TTS补丁/瑕疵图叠加")
    ap.add_argument("source", nargs="*", help="车源名(input/ 下的目录名)或素材目录路径，可传多个并行处理")
    ap.add_argument("--all", action="store_true", help="处理 input/ 下所有合规车源（并发，跳过已有成片）")
    ap.add_argument("--workers", type=int, default=2, help="并发数（建议 2-3，过高会撞云端限流）")
    ap.add_argument("--redo", action="store_true", help="重跑已有成片的车源")
    ap.add_argument("--config", default="config.yaml", help="配置文件路径")
    ap.add_argument("--no-patch", action="store_true", help="只出报告，不做 TTS 补丁")
    ap.add_argument("--mode", choices=["patch", "full"], default=None,
                    help="配音方式：patch=定点补丁 / full=全文配音（默认读 tts.mode）")
    ap.add_argument("--asr-model", default=None, help="临时指定 ASR 模型，如 base/small")
    ap.add_argument("--out", default=None, help="输出根目录，默认 output/")
    args = ap.parse_args()

    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"配置不存在: {cfg_path}")
        return 2
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if args.asr_model:
        cfg.setdefault("asr", {})["model_local"] = args.asr_model
    if args.mode:
        cfg.setdefault("tts", {})["mode"] = args.mode

    input_root = Path(cfg.get("paths", {}).get("input", "input"))
    out_root = Path(args.out or cfg.get("paths", {}).get("output", "output"))

    if args.all:
        if not input_root.is_dir():
            print(f"输入目录不存在: {input_root.resolve()}")
            return 2
        sources = []
        for d in sorted(input_root.iterdir()):
            if not d.is_dir() or not d.name.isdigit():
                continue
            if not (d / "文案.txt").exists() or not (d / "videos").is_dir():
                print(f"[{d.name}] 跳过：缺 文案.txt/videos/（先运行 batch_prep.py）")
                continue
            done = out_root / d.name / "report.md"
            if not done.exists():  # 兼容新命名目录（729-皇冠-...）
                done = next(iter(out_root.glob(f"{d.name}-*/report.md")), None)
            if done and not args.redo:
                print(f"[{d.name}] 跳过：已有成片")
                continue
            sources.append(d)
        if not sources:
            print("没有待处理车源")
            return 0
        print(f"批量待处理 {len(sources)} 个: {[s.name for s in sources]}，"
              f"并发 {args.workers}", flush=True)
    elif args.source:
        sources = [Path(s) if Path(s).is_dir() else input_root / s
                   for s in args.source]
        print(f"待处理 {len(sources)} 个: {[s.name for s in sources]}，"
              f"并发 {args.workers}", flush=True)
    else:
        ap.print_help()
        return 2

    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _run(src: Path):
        t0 = time.time()
        r = pipeline.process_source(src, out_root, cfg,
                                    do_patch=not args.no_patch)
        return r["name"], r["final"], time.time() - t0

    ok = fail = 0
    t00 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(_run, s): s for s in sources}
        for fut in as_completed(futs):
            s = futs[fut]
            print("=" * 60, flush=True)
            try:
                name, final, dt = fut.result()
                print(f"[{name}] ✅ 完成({dt:.0f}s): {final}", flush=True)
                ok += 1
            except Exception as exc:
                print(f"[{s.name}] ❌ 失败: {exc}", flush=True)
                fail += 1
    print("=" * 60)
    print(f"批量完成: 成功 {ok} / 失败 {fail}，总耗时 {time.time() - t00:.0f}s")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
