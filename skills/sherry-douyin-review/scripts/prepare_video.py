#!/usr/bin/env python3
"""
视频预处理：一条命令生成诊断/审核所需的全部素材。

    python3 prepare_video.py 视频.mp4 [--srt 字幕.srt] [--out-root outputs]
                             [--title 标题] [--scale 540] [--transcribe]
                             [--scenes] [--subdir-prefix 过程文件]

产物目录：{out-root}/{标题}/{subdir-prefix}_{sha256前8位}/
    media.json    时长、分辨率、帧率、是否有音轨、sha256
    asr.srt       字幕副本（有的话）
    L1_*.jpg      全片基础帧（每秒 1 帧）
    L2_asr_*.jpg  字幕起止对齐帧（长句每秒补抽）
    L3_head_*.jpg 开头 0-3 秒每 0.5 秒一帧
    mapping.json  时间 → 帧文件 → 对应字幕 的映射表
    scenes.json   镜头切换点（--scenes 时生成）
    summary.md    给主 agent 看的素材摘要

不删除任何已有文件。同一视频（同 sha256）重复运行会覆盖同名产物。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEDUP_WINDOW = 0.3
LAYER_PRIORITY = {"L2": 0, "L3": 1, "L1": 2}


def die(msg: str) -> None:
    print(f"错误：{msg}", file=sys.stderr)
    sys.exit(1)


def slugify(title: str) -> str:
    title = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", title).strip(" ._")
    return title[:60] or "untitled"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(video: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(video)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        die(f"ffprobe 失败：{out.stderr.strip()}")
    info = json.loads(out.stdout)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        die("文件里没有视频流")
    num, _, den = (v.get("avg_frame_rate") or "0/1").partition("/")
    fps = round(float(num) / float(den), 3) if den and float(den) else 0.0
    return {
        "duration": round(float(info["format"].get("duration") or v.get("duration") or 0), 3),
        "width": v.get("width"),
        "height": v.get("height"),
        "fps": fps,
        "has_audio": any(s.get("codec_type") == "audio" for s in info["streams"]),
        "codec": v.get("codec_name"),
    }


TS_RE = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})")


def parse_ts(s: str) -> float:
    m = TS_RE.search(s)
    if not m:
        raise ValueError(s)
    h, mi, se, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000


def parse_srt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        if "-->" not in lines[0] and len(lines) > 1 and "-->" in lines[1]:
            lines = lines[1:]
        if "-->" not in lines[0]:
            continue
        start_s, _, end_s = lines[0].partition("-->")
        try:
            start, end = parse_ts(start_s), parse_ts(end_s)
        except ValueError:
            continue
        cues.append({"index": len(cues) + 1, "start": round(start, 3), "end": round(end, 3),
                     "text": " ".join(lines[1:])})
    return cues


def find_whisper_python() -> str | None:
    """找转写解释器：环境变量 > 当前解释器 > 相邻的 .venv / venv。"""
    here = Path(__file__).resolve()
    cands = [os.environ.get("SHERRY_WHISPER_PYTHON"), sys.executable]
    for up in (3, 4, 5):
        if len(here.parents) > up:
            for name in (".venv", "venv"):
                cands.append(str(here.parents[up] / name / "bin" / "python3"))
    seen = set()
    for c in cands:
        if not c or c in seen or not Path(c).is_file():
            continue
        seen.add(c)
        r = subprocess.run([c, "-c", "import faster_whisper"], capture_output=True)
        if r.returncode == 0:
            return c
    return None


def transcribe(video: Path, srt_out: Path, model_size: str) -> list[dict] | None:
    py = find_whisper_python()
    if py is None:
        print("提示：没找到装有 faster-whisper 的 Python，跳过自动转写。"
              "安装：pip3 install faster-whisper，或设置 SHERRY_WHISPER_PYTHON 指向已安装的解释器。", file=sys.stderr)
        return None
    print(f"转写中（faster-whisper {model_size}，解释器 {py}）……", file=sys.stderr)
    worker = Path(__file__).resolve().with_name("transcribe_srt.py")
    r = subprocess.run([py, str(worker), str(video), str(srt_out), "--model", model_size], text=True,
                       capture_output=True)
    if r.stderr.strip():
        print(r.stderr.strip(), file=sys.stderr)
    if r.returncode != 0 or not srt_out.is_file():
        print("转写失败，继续但不含字幕。", file=sys.stderr)
        return None
    return parse_srt(srt_out)


def plan_frames(duration: float, cues: list[dict], l1_step: float) -> list[dict]:
    if not math.isfinite(l1_step) or l1_step < DEDUP_WINDOW:
        raise ValueError(f"--l1-step 必须是至少 {DEDUP_WINDOW} 秒的有限数（帧去重间隔）")
    cands: list[dict] = []
    last = max(duration - 0.1, 0.0)  # 最后一帧取不到，整体往前让 0.1 秒
    t = 0.0
    while t <= last + 1e-6:
        # 整秒保留旧文件名；分数秒不能四舍五入到整数，否则不同帧会覆盖同一文件。
        timestamp = round(t, 3)
        label = f"{int(timestamp):03d}" if timestamp.is_integer() else f"{timestamp:07.3f}"
        cands.append({"time": timestamp, "layer": "L1", "file": f"L1_{label}s.jpg"})
        t += l1_step
    for c in cues:
        points = [c["start"], c["end"]]
        if c["end"] - c["start"] > 1.5:
            m = c["start"] + 1.0
            while m < c["end"] - 0.5:
                points.append(round(m, 3))
                m += 1.0
        for p in points:
            p = min(max(p, 0.0), last)
            cands.append({"time": round(p, 3), "layer": "L2", "srt_index": c["index"],
                          "file": f"L2_asr_{c['index']:02d}_{p:.2f}s.jpg"})
    for i in range(7):
        t = i * 0.5
        if t <= last:
            cands.append({"time": t, "layer": "L3", "file": f"L3_head_{t:.1f}s.jpg"})

    cands.sort(key=lambda c: (c["time"], LAYER_PRIORITY[c["layer"]]))
    kept: list[dict] = []
    for c in cands:
        if kept and c["time"] - kept[-1]["time"] < DEDUP_WINDOW:
            if LAYER_PRIORITY[c["layer"]] < LAYER_PRIORITY[kept[-1]["layer"]]:
                kept[-1] = c
            continue
        kept.append(c)
    return kept


def extract_frame(video: Path, frame: dict, out_dir: Path, scale: int) -> tuple[dict, str | None]:
    vf = f"scale={scale}:-2" if scale > 0 else "null"
    # FFmpeg 可能返回 0 却没有输出；用本轮独有文件验证，不能拿旧帧顶替。
    with tempfile.NamedTemporaryFile(prefix=".frame-", suffix=".jpg", dir=out_dir, delete=False) as f:
        temporary = Path(f.name)
    try:
        cmd = ["ffmpeg", "-loglevel", "error", "-ss", f"{frame['time']:.3f}", "-i", str(video),
               "-frames:v", "1", "-q:v", "2", "-vf", vf, "-y", str(temporary)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return frame, r.stderr.strip() or f"ffmpeg exit {r.returncode}"
        if not temporary.is_file() or temporary.stat().st_size == 0:
            return frame, "ffmpeg 未生成本轮有效帧"
        temporary.replace(out_dir / frame["file"])
        return frame, None
    finally:
        temporary.unlink(missing_ok=True)


def detect_scenes(video: Path, duration: float, threshold: float) -> dict:
    cmd = ["ffmpeg", "-loglevel", "info", "-i", str(video), "-an",
           "-vf", f"select='gt(scene,{threshold})',metadata=print", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die(f"镜头检测失败：{r.stderr.strip() or f'ffmpeg exit {r.returncode}'}")
    pairs = re.findall(r"pts_time:([\d.]+).*?\n.*?scene_score=([\d.]+)", r.stderr)
    cut_scores = {round(float(t), 3): round(float(sc), 3) for t, sc in pairs}
    cuts = sorted(cut_scores)
    bounds = [0.0] + [c for c in cuts if 0 < c < duration] + [duration]
    shots = [{"start": a, "end": b, "duration": round(b - a, 3)} for a, b in zip(bounds, bounds[1:]) if b > a]
    durations = [s["duration"] for s in shots] or [duration]
    return {
        "threshold": threshold,
        "cut_times": cuts,
        "cut_scores": cut_scores,
        "shots": shots,
        "shot_count": len(shots),
        "avg_shot": round(sum(durations) / len(durations), 2),
        "longest_shot": max(shots, key=lambda s: s["duration"]) if shots else None,
        "note": "基于相邻帧差异的自动检测（阈值见 threshold，硬切通常 0.15-0.6，分值越高越可信）。缓慢推拉、同机位换动作会漏检，字幕弹出可能误检，判断时结合帧图。",
    }


def cue_at(cues: list[dict], t: float) -> dict | None:
    for c in cues:
        if c["start"] - 0.05 <= t <= c["end"] + 0.05:
            return c
    return None


def write_summary(path: Path, media: dict, cues: list[dict], frames: list[dict], scenes: dict | None,
                  work_dir: Path, srt_source: str) -> None:
    by_layer = {k: sum(1 for f in frames if f["layer"] == k) for k in ("L1", "L2", "L3")}
    lines = [
        "# 素材摘要",
        "",
        f"- 过程目录：`{work_dir}`",
        f"- 时长 {media['duration']} 秒，{media['width']}×{media['height']}，{media['fps']} fps，"
        f"{'有' if media['has_audio'] else '无'}音轨",
        f"- SHA256：`{media['sha256']}`",
        f"- 字幕：{srt_source}，共 {len(cues)} 条",
        f"- 帧：共 {len(frames)} 张（L1 基础 {by_layer['L1']}，L2 字幕对齐 {by_layer['L2']}，L3 开头 {by_layer['L3']}）",
    ]
    if scenes:
        ls = scenes["longest_shot"]
        lines.append(f"- 镜头：约 {scenes['shot_count']} 个，平均 {scenes['avg_shot']} 秒，"
                     f"最长 {ls['duration']} 秒（{ls['start']}-{ls['end']} 秒）" if ls else "- 镜头：未检测到切换")
    if not cues:
        lines.append("")
        lines.append("> 没有字幕：L2 层未生成，所有依赖口播内容的检查项应标注「无法判断」。")
    lines += ["", "## 逐字稿", ""]
    lines += [f"- [{c['start']:.2f}-{c['end']:.2f}] {c['text']}" for c in cues] or ["（无）"]
    lines += ["", "## 建议主 agent 先看的帧", ""]
    picks = []
    for target in (0.0, 2.0, media["duration"] / 2, media["duration"] * 0.75, media["duration"]):
        picks.append(min(frames, key=lambda f: abs(f["time"] - target)))
    seen = set()
    for f in picks:
        if f["file"] not in seen:
            seen.add(f["file"])
            lines.append(f"- {f['time']:.2f}s → `{work_dir / f['file']}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video")
    ap.add_argument("--srt", help="字幕文件；缺省时找同名 .srt")
    ap.add_argument("--out-root", default="outputs", help="输出根目录（默认 ./outputs）")
    ap.add_argument("--title", help="视频标题，用作目录名；缺省用文件名")
    ap.add_argument("--scale", type=int, default=540, help="帧宽度像素，0 表示不缩放（默认 540）")
    ap.add_argument("--l1-step", type=float, default=1.0, help="基础帧间隔秒数（默认 1，至少 0.3）")
    ap.add_argument("--transcribe", action="store_true", help="无字幕时用 faster-whisper 转写")
    ap.add_argument("--whisper-model", default="small")
    ap.add_argument("--scenes", action="store_true", help="检测镜头切换点，写入 scenes.json")
    ap.add_argument("--scene-threshold", type=float, default=0.15, help="镜头切换判定阈值（默认 0.15）")
    ap.add_argument("--subdir-prefix", default="过程文件", help="过程目录前缀（审核用「审核过程」）")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    if not math.isfinite(args.l1_step) or args.l1_step < DEDUP_WINDOW:
        ap.error(f"--l1-step 必须是至少 {DEDUP_WINDOW} 秒的有限数（帧去重间隔）")
    if args.workers <= 0:
        ap.error("--workers 必须大于 0")
    if args.scale < 0:
        ap.error("--scale 必须大于等于 0（0 表示不缩放）")
    if not math.isfinite(args.scene_threshold) or not 0 <= args.scene_threshold <= 1:
        ap.error("--scene-threshold 必须是 0 到 1 之间的有限数")

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            die(f"找不到 {tool}，请先安装（brew install ffmpeg）")

    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        die(f"视频不存在：{video}")
    srt_path = Path(args.srt).expanduser().resolve() if args.srt else video.with_suffix(".srt")
    if args.srt and not srt_path.is_file():
        die(f"字幕不存在：{srt_path}")

    media = probe(video)
    media["sha256"] = sha256_of(video)
    media["source"] = str(video)

    title = slugify(args.title or video.stem)
    work_dir = (Path(args.out_root).expanduser().resolve() / title / f"{args.subdir_prefix}_{media['sha256'][:8]}")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "media.json").write_text(json.dumps(media, ensure_ascii=False, indent=2), encoding="utf-8")

    cues: list[dict] = []
    if srt_path.is_file():
        cues = parse_srt(srt_path)
        shutil.copyfile(srt_path, work_dir / "asr.srt")
        srt_source = f"来自 {srt_path.name}"
    elif args.transcribe and media["has_audio"]:
        cues = transcribe(video, work_dir / "asr.srt", args.whisper_model) or []
        srt_source = "faster-whisper 自动转写" if cues else "无（转写不可用）"
    else:
        srt_source = "无" + ("" if media["has_audio"] else "（视频没有音轨）")
        print("提示：没有字幕文件。加 --transcribe 可自动转写；否则 L2 层跳过。", file=sys.stderr)

    frames = plan_frames(media["duration"], cues, args.l1_step)
    errors = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for frame, err in ex.map(lambda f: extract_frame(video, f, work_dir, args.scale), frames):
            target = work_dir / frame["file"]
            if err or not target.is_file() or target.stat().st_size == 0:
                errors.append(f"{frame['file']}: {err or '未生成有效帧文件'}")
    if errors:
        die("抽帧失败，本轮素材不完整，请排查后重跑；不要使用旧产物：\n  " + "\n  ".join(errors))
    if not frames:
        die("抽帧失败：没有可用画面，无法继续诊断")

    mapping = []
    for f in frames:
        c = cue_at(cues, f["time"])
        mapping.append({"time": f["time"], "file": f["file"], "layer": f["layer"],
                        "srt_index": c["index"] if c else None, "text": c["text"] if c else ""})
    (work_dir / "mapping.json").write_text(
        json.dumps({"video": str(video), "sha256": media["sha256"], "duration": media["duration"],
                    "scale": args.scale, "frames": mapping, "cues": cues}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    scenes = None
    if args.scenes:
        scenes = detect_scenes(video, media["duration"], args.scene_threshold)
        (work_dir / "scenes.json").write_text(json.dumps(scenes, ensure_ascii=False, indent=2), encoding="utf-8")

    write_summary(work_dir / "summary.md", media, cues, frames, scenes, work_dir, srt_source)

    print(json.dumps({"work_dir": str(work_dir), "frames": len(frames), "cues": len(cues),
                      "duration": media["duration"], "sha256_8": media["sha256"][:8],
                      "summary": str(work_dir / "summary.md"), "mapping": str(work_dir / "mapping.json"),
                      "srt": str(work_dir / "asr.srt") if cues else None,
                      "scenes": str(work_dir / "scenes.json") if scenes else None},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
