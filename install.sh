#!/bin/bash
# 一键安装并启动：创建 Python 虚拟环境、安装唯一依赖 pyte、编译 macOS 原生 App（可选）
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Claude 工作台.app"

echo "== PTY CLI Workbench 安装 =="

PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null)" /usr/bin/python3; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    PY="$c"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "错误：未找到 Python 3.9 及以上版本。" >&2
  exit 1
fi
echo "Python: $PY ($("$PY" --version 2>&1))"

if [ ! -d "$DIR/.venv" ]; then
  echo "创建虚拟环境 .venv ..."
  "$PY" -m venv "$DIR/.venv"
fi
echo "安装唯一 Python 依赖 (pyte) ..."
"$DIR/.venv/bin/python" -m pip install -q --disable-pip-version-check -r "$DIR/requirements.txt"

if [ "$(uname -s)" = "Darwin" ] && command -v swiftc >/dev/null 2>&1; then
  echo "编译原生 macOS 应用 -> $APP ..."
  mkdir -p "$HOME/Applications"
  bash "$DIR/native/build.sh" "$APP"
  echo "已编译原生应用：$APP"
else
  echo "未检测到 macOS swiftc，跳过原生 App 编译；可直接运行 ./start.sh 以浏览器应用窗口打开。"
fi

if [ -d "$DIR/skills/claude-workbench-ui" ]; then
  mkdir -p "$HOME/.claude/skills"
  cp -R "$DIR/skills/claude-workbench-ui" "$HOME/.claude/skills/"
  echo "已安装可选 UI 联动技能到 ~/.claude/skills/claude-workbench-ui"
fi

echo ""
echo "安装完成！运行 ./start.sh 或打开 $APP 即可使用。"
