#!/bin/zsh

set -u

script_directory="${0:A:h}"
python_command="$(command -v python3)"

pause_before_exit() {
  if [[ -t 0 && -t 1 ]]; then
    print ""
    read "? 按回车关闭窗口…"
  fi
}

if [[ -z "$python_command" ]]; then
  print -u2 "状态：拒绝应用"
  print -u2 "发现：未找到 Python 3。"
  print -u2 "写入：未写入运行文件、GET 来源或九个受控目标。"
  print -u2 "下一步：请将本结果交给 Codex 处理。"
  pause_before_exit
  exit 2
fi

"$python_command" "$script_directory/get_index_operator.py" "$@"
operator_exit_code=$?

pause_before_exit

exit "$operator_exit_code"
