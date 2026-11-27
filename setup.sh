#!/usr/bin/env bash
# 安装当前套装并用自带字幕自检；不改写测试媒体、不下载语音模型。
set -euo pipefail

SUITE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${SKILLS_DIR:-${HOME}/.claude/skills}"
VENV_DIR="${VENV_DIR:-${SUITE}/.venv}"
SKIP_DEPS=0
WITH_TRANSCRIPTION=0

for arg in "$@"; do
  case "$arg" in
    --skip-deps) SKIP_DEPS=1 ;;
    --with-transcription) WITH_TRANSCRIPTION=1 ;;
    --help|-h)
      echo "用法：bash setup.sh [--skip-deps | --with-transcription]"
      echo "SKILLS_DIR：Skill 安装目录（默认 ~/.claude/skills）"
      echo "VENV_DIR：Python 环境目录（默认 套装/.venv）"
      exit 0 ;;
    *) echo "未知参数：${arg}" >&2; exit 2 ;;
  esac
done
if [ "$SKIP_DEPS" -eq 1 ] && [ "$WITH_TRANSCRIPTION" -eq 1 ]; then
  echo "--skip-deps 和 --with-transcription 不能同时使用" >&2
  exit 2
fi

for tool in python3 ffmpeg ffprobe; do
  command -v "$tool" >/dev/null || {
    echo "缺少 ${tool}，请先安装。macOS 的 ffmpeg 可用 brew install ffmpeg；Linux 可用系统包管理器。" >&2
    exit 1
  }
done

PYTHON="$(command -v python3)"
if [ "$SKIP_DEPS" -eq 0 ]; then
  [ -x "${VENV_DIR}/bin/python3" ] || python3 -m venv "$VENV_DIR"
  PYTHON="${VENV_DIR}/bin/python3"
  REQUIREMENTS="${SUITE}/requirements.txt"
  if [ "$WITH_TRANSCRIPTION" -eq 1 ]; then
    REQUIREMENTS="${SUITE}/requirements-asr.txt"
  fi
  "$PYTHON" -m pip install -r "$REQUIREMENTS"
fi

mkdir -p "$SKILLS_DIR"
count=0
for skill in "${SUITE}"/skills/sherry-*; do
  [ -f "${skill}/SKILL.md" ] || continue
  name="$(basename "$skill")"
  mkdir -p "${SKILLS_DIR}/${name}"
  cp -R "${skill}/." "${SKILLS_DIR}/${name}/"
  count=$((count + 1))
done
[ "$count" -eq 12 ] || { echo "套装不完整：应有 12 个 Skill，实际 ${count} 个" >&2; exit 1; }

"$PYTHON" -B "${SUITE}/skills/sherry-douyin-review/scripts/prepare_video.py" \
  "${SUITE}/测试素材/样例_泳后护发.mp4" --scenes --out-root "${SUITE}/outputs"

printf '\nSETUP_DONE：已安装 %s 个 Skill 到 %s。下一步见 %s/验收清单.md\n' "$count" "$SKILLS_DIR" "$SUITE"
if [ "$WITH_TRANSCRIPTION" -eq 1 ]; then
  echo "已安装可选转写依赖；首次转写可能下载模型。启动 AI 工具前在同一终端设置："
  printf 'export SHERRY_WHISPER_PYTHON=%q\n' "$PYTHON"
fi
