"""在临时目录安装，不修改真实用户的 Skill 或 Python 环境。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SUITE = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "需要 ffmpeg / ffprobe")
class InstallationTests(unittest.TestCase):
    def test_suite_can_install_from_an_arbitrary_directory_and_rerun(self):
        self.assertTrue((SUITE / "setup.sh").is_file(), "套装内缺少 setup.sh")
        with tempfile.TemporaryDirectory(prefix="sherry-install-test-") as temp:
            relocated = Path(temp) / "任意目录 with spaces" / "suite"
            shutil.copytree(SUITE, relocated, ignore=shutil.ignore_patterns(".venv", "venv", "outputs", "__pycache__"))
            destination = Path(temp) / "installed skills"
            unrelated = destination / "other-skill"
            unrelated.mkdir(parents=True)
            (unrelated / "keep.txt").write_text("keep")
            env = os.environ.copy()
            env["SKILLS_DIR"] = str(destination)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            for _ in range(2):
                result = subprocess.run(["bash", str(relocated / "setup.sh"), "--skip-deps"],
                                        cwd=temp, env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("SETUP_DONE", result.stdout)
            self.assertEqual(len(list(destination.glob("sherry-*/SKILL.md"))), 12)
            self.assertEqual((unrelated / "keep.txt").read_text(), "keep")
            self.assertFalse((relocated / ".venv").exists())
            source = relocated / "skills/sherry-douyin-review/scripts/prepare_video.py"
            installed = destination / "sherry-douyin-review/scripts/prepare_video.py"
            self.assertEqual(source.read_bytes(), installed.read_bytes())
            sample_summary = next((relocated / "outputs").glob("*/过程文件_*/summary.md"))
            self.assertIn("共 18 张", sample_summary.read_text())


if __name__ == "__main__":
    unittest.main()
