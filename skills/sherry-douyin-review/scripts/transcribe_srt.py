#!/usr/bin/env python3
"""
用 faster-whisper 把音视频转成 SRT。被 prepare_video.py 调用，也可单独用：

    python3 transcribe_srt.py 视频.mp4 输出.srt [--model small] [--language zh]

模型缓存目录：环境变量 SHERRY_WHISPER_MODELS，缺省用 faster-whisper 的默认缓存（~/.cache/huggingface）。
"""
import argparse
import os
import sys


def fmt(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("srt_out")
    ap.add_argument("--model", default="small")
    ap.add_argument("--language", default="zh")
    args = ap.parse_args()

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        print("faster-whisper 未安装：pip3 install faster-whisper", file=sys.stderr)
        sys.exit(3)

    root = os.environ.get("SHERRY_WHISPER_MODELS") or None
    try:
        model = WhisperModel(args.model, compute_type="int8", download_root=root, local_files_only=True)
    except Exception:
        print(f"本地没有 {args.model} 模型，开始下载（首次约数百 MB）……", file=sys.stderr)
        model = WhisperModel(args.model, compute_type="int8", download_root=root)

    segments, info = model.transcribe(args.media, language=args.language, vad_filter=True, beam_size=5)
    cues = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            cues.append((seg.start, seg.end, text))
    with open(args.srt_out, "w", encoding="utf-8") as f:
        for i, (s, e, text) in enumerate(cues, 1):
            f.write(f"{i}\n{fmt(s)} --> {fmt(e)}\n{text}\n\n")
    print(f"转写完成：{len(cues)} 条，语言 {info.language}（置信度 {info.language_probability:.2f}）", file=sys.stderr)


if __name__ == "__main__":
    main()
