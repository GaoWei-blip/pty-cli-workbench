#!/bin/bash
# 启动或切回 Claude 工作台：
#   start.sh            后台服务没运行就启动，然后打开原生 App（已开着就回到那个窗口）
#   start.sh --server   只确保后台服务在运行（App 启动时调用）
#   start.sh --ensure   看护：服务意外掉线就拉起（App 每 20 秒调一次；stop.sh 留了标记就不拉起）
#   没有原生 App 时，退回用 Chrome 应用窗口打开（下面的旧逻辑）
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${CWB_PORT:-47821}"
BROWSER_DIR="$DIR/.run/browser"
ping() { curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/ping" | grep -q '^ok'; }
clients() { curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/ping" | awk '{print $2+0}'; }
browser_pid() { ps -axo pid=,command= | grep -F -- "--user-data-dir=$BROWSER_DIR" | grep -v -- "--type=" | grep -v grep | awk '{print $1}' | head -1; }
start_server() {
  mkdir -p "$DIR/.run"; rm -f "$DIR/.run/stopped-$PORT"
  nohup "$DIR/.venv/bin/python" -I "$DIR/server.py" >> "$DIR/.run/server.log" 2>&1 < /dev/null &
  for _ in $(seq 1 80); do sleep 0.25; ping && return 0; done
  return 1
}

# 原生 App 用：--server 确保后台服务在运行；--ensure 看护（主动停掉的不拉起）
if [ "$1" = "--server" ]; then ping || start_server; exit 0; fi
if [ "$1" = "--ensure" ]; then
  if [ ! -e "$DIR/.run/stopped-$PORT" ] && ! ping; then echo "$(date '+%F %T') 服务不在了，重新启动"; start_server; fi
  exit 0
fi
APP_PATH="$HOME/Applications/Claude 工作台.app"
if [ -z "$1" ] && [ -x "$APP_PATH/Contents/MacOS/ClaudeWorkbench" ]; then
  ping || start_server
  open "$APP_PATH"; exit 0   # 已经开着就回到那个窗口
fi

if [ "$1" = "--watch" ]; then
  # 主动停掉的（stop.sh、长时间没窗口自动退出）不拉起；只管意外掉线
  if [ -n "$(browser_pid)" ] && [ ! -e "$DIR/.run/stopped-$PORT" ] && ! ping; then
    echo "$(date '+%F %T') 服务不在了，重新启动"; start_server
  fi
  exit 0
fi

ping || start_server

P="$(browser_pid)"
if [ -n "$P" ]; then
  # 窗口连着服务才算有窗口（Chrome 关掉窗口后进程可能还在）；刚重启的服务要等窗口重连几秒
  for _ in 1 2 3 4 5 6; do [ "$(clients)" -gt 0 ] && break; sleep 0.5; done
  if [ "$(clients)" -gt 0 ]; then
    r="$(osascript -l JavaScript "$DIR/focus.js" "$P" 2>/dev/null)"
    case "$r" in shown*|restored*) exit 0 ;; esac
  fi
fi

# Chrome 上次如果被强行结束，重开时会把旧页面“恢复”成一个普通标签页窗口；开之前先把这个配置标记成正常退出
PREF="$BROWSER_DIR/Default/Preferences"
if [ -z "$P" ] && [ -f "$PREF" ]; then
  "$DIR/.venv/bin/python" -I -c 'import json,sys
p=sys.argv[1]; d=json.load(open(p, encoding="utf-8")); pr=d.setdefault("profile", {})
pr["exit_type"]="Normal"; pr["exited_cleanly"]=True
json.dump(d, open(p, "w", encoding="utf-8"))' "$PREF" 2>/dev/null
fi
TOKEN="$(cat "$DIR/.run/token-$PORT" 2>/dev/null)"
URL="http://127.0.0.1:$PORT/?t=$TOKEN"
APP="Google Chrome"; [ -d "/Applications/Google Chrome.app" ] || APP="Microsoft Edge"
open -na "$APP" --args --app="$URL" --user-data-dir="$BROWSER_DIR" --window-size=1440,920 \
  --no-first-run --no-default-browser-check --disable-features=Translate --hide-crash-restore-bubble
