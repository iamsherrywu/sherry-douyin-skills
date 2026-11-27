"""预处理回归：python3 -B -m unittest discover -s tests -v。"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SUITE = Path(__file__).resolve().parents[1]
SCRIPT = SUITE / "skills/sherry-douyin-review/scripts/prepare_video.py"
VIDEO = SUITE / "测试素材/样例_泳后护发.mp4"
SPEC = importlib.util.spec_from_file_location("prepare_video", SCRIPT)
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class FramePlanTests(unittest.TestCase):
    def test_fractional_interval_has_one_file_per_timestamp(self):
        frames = prepare.plan_frames(12, [], 0.5)
        self.assertEqual(len(frames), len({f["file"] for f in frames}))
        self.assertTrue(any(f["time"] == 4.5 for f in frames))

    def test_default_integer_names_remain_compatible(self):
        frames = prepare.plan_frames(12, [], 1.0)
        frame = next(f for f in frames if f["time"] == 5.0)
        self.assertEqual(frame["file"], "L1_005s.jpg")


@unittest.skipUnless(HAS_FFMPEG, "需要 ffmpeg / ffprobe")
class PreprocessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sherry-preprocess-test-")
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)

    def run_cli(self, *args, env=None, timeout=20):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), str(VIDEO),
             "--out-root", str(self.output), *args],
            capture_output=True, text=True, env=env, timeout=timeout,
        )

    def test_invalid_intervals_exit_promptly(self):
        for step in ("0", "-1", "nan", "inf", "1e-12"):
            with self.subTest(step=step):
                try:
                    result = self.run_cli("--l1-step", step, timeout=0.7)
                except subprocess.TimeoutExpired:
                    self.fail(f"--l1-step {step} 没有立即拒绝，进入了抽帧循环")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("--l1-step", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_workers_scale_and_threshold_are_cli_errors(self):
        for option, value in (("--workers", "0"), ("--scale", "-1"),
                              ("--scene-threshold", "nan"), ("--scene-threshold", "1.1")):
            with self.subTest(option=option, value=value):
                result = self.run_cli(option, value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(option, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_scene_detection_failure_is_not_a_single_shot(self):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            with self.assertRaises(SystemExit) as error:
                prepare.detect_scenes(self.output / "missing.mp4", 12, 0.15)
        self.assertEqual(error.exception.code, 1)
        self.assertIn("镜头检测失败", stderr.getvalue())

    def test_failed_extraction_does_not_reuse_previous_frames(self):
        # 保留旧帧再模拟 ffmpeg 失败，避免把“缓存还在”当成本轮成功。
        work = self.output / VIDEO.stem / "过程文件_4ad71c43"
        work.mkdir(parents=True)
        (work / "L3_head_0.0s.jpg").write_bytes(b"old frame")
        fake = self.output / "ffmpeg"
        fake.write_text("#!/bin/sh\necho 'simulated extraction failure' >&2\nexit 1\n")
        fake.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(self.output) + os.pathsep + env["PATH"]
        result = self.run_cli(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("抽帧失败", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((work / "mapping.json").exists())

    def test_explicit_missing_srt_is_an_error(self):
        result = self.run_cli("--srt", str(self.output / "missing.srt"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("字幕不存在", result.stderr)

    def test_success_without_new_frame_does_not_reuse_previous_frames(self):
        initial = self.run_cli()
        self.assertEqual(initial.returncode, 0, initial.stderr)
        work = Path(json.loads(initial.stdout)["work_dir"])
        previous = {p.name: p.read_bytes() for p in work.glob("*.jpg")}
        fake = self.output / "ffmpeg"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = str(self.output) + os.pathsep + env["PATH"]
        result = self.run_cli("--scale", "320", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("抽帧失败", result.stderr)
        self.assertEqual({p.name: p.read_bytes() for p in work.glob("*.jpg")}, previous)
        self.assertFalse(list(work.glob(".frame-*")))

    def test_sample_outputs_are_complete_and_consistent(self):
        result = self.run_cli("--scenes")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual((output["frames"], output["cues"], output["duration"]), (18, 4, 12.0))
        mapping = json.loads(Path(output["mapping"]).read_text())
        scenes = json.loads(Path(output["scenes"]).read_text())
        self.assertEqual(scenes["cut_times"], [4.0, 8.0])
        self.assertEqual(scenes["shot_count"], 3)
        for frame in mapping["frames"]:
            self.assertGreater((Path(output["work_dir"]) / frame["file"]).stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
