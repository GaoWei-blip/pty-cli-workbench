#!/bin/sh
# Claude 工作台按对话注入的状态行（第十五轮 A1）。用法：statusline.sh <状态目录> <用户原来的状态行命令>
# 1. 把 CLI 从标准输入给的 JSON 原子写到 <状态目录>/<CWB_GEN>.json（先写临时文件再改名），工作台据此显示上下文用量；
# 2. 把同一份内容交给用户原来的状态行命令，原样输出它的结果（额度文件照常更新）；没有原来的命令就输出空行。
# 第 1 步任何失败都不影响第 2 步。不起 Python，不写别的文件。
in=$(cat; printf x)
in=${in%x}
d=$1
case ${CWB_GEN:-} in
  '' | .* | */*) ;;
  *)
    if [ -n "$d" ] && [ -d "$d" ]; then
      t="$d/.$CWB_GEN.$$.tmp"
      if printf '%s' "$in" > "$t" 2>/dev/null; then
        mv -f "$t" "$d/$CWB_GEN.json" 2>/dev/null || rm -f "$t" 2>/dev/null
      fi
    fi
    ;;
esac
if [ -n "${2:-}" ]; then
  printf '%s' "$in" | /bin/sh -c "$2"
else
  echo
fi
