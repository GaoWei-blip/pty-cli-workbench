#!/bin/bash
# 停止 Claude 工作台后台服务（会结束它启动的所有 claude 进程）。留一个标记，启动器的看护就不会把它重新拉起来
# 第十三轮 E：claude 在终端守护进程 ptyd 里，停掉后台服务后再让 ptyd 退出（它会先按原规则结束所有 claude）
DIR="$(cd "$(dirname "$0")" && pwd)"; PORT="${CWB_PORT:-47821}"
mkdir -p "$DIR/.run"; touch "$DIR/.run/stopped-$PORT"
pkill -TERM -f "$DIR/server.py" && echo 已停止 || echo 未在运行
P="$(head -1 "$DIR/.run/ptyd-$PORT.lock" 2>/dev/null)"
if [ -n "$P" ] && ps -p "$P" -o command= 2>/dev/null | grep -q "ptyd.py"; then
  sleep 0.5; kill -TERM "$P" && echo 已结束终端守护进程
fi
