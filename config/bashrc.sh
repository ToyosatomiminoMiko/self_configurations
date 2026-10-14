
# 用户定义 User-defined
# cargo
. "$HOME/.cargo/env"
# rustup
export RUSTUP_UPDATE_ROOT=https://mirrors.tuna.tsinghua.edu.cn/rustup/rustup
export RUSTUP_DIST_SERVER=https://mirrors.tuna.tsinghua.edu.cn/rustup
# GHCup
export PATH="$HOME/.ghcup/bin:$PATH"

# 抑制原有的 `(venv)`
export VIRTUAL_ENV_DISABLE_PROMPT=1

# 颜色定义(解耦颜色值)
# 使用 256 色或标准颜色,便于修改
COLOR_VENV="\033[38;2;144;255;144m"   # 浅绿
COLOR_USER="\033[38;2;243;168;183m"   # 粉红
COLOR_TIME="\033[38;2;91;206;250m"    # 粉蓝
COLOR_PATH="\033[38;2;255;255;255m"   # 白色
COLOR_RESET="\033[0m"

# venv 提示函数
__venv_prompt() {
    [[ -n "$VIRTUAL_ENV" ]] && echo "[$(basename "$VIRTUAL_ENV")] "
}

# 时间提示函数
__time_prompt() {
    date "+%Y.%m.%d.%H:%M:%S"
}

info() {
    printf '[%s][info] %s\n' "$(__time_prompt)" "$*"
}

error() {
    printf '[%s][error] %s\n' "$(__time_prompt)" "$*" >&2
}

# 构建 PS1
# 注意:${COLOR_...} 是 ANSI 转义序列,需放在 '[' 和 '\]' 之间以正确计算长度(可选)
PS1="\[${COLOR_VENV}\]\$(__venv_prompt)\[${COLOR_RESET}\]"
PS1+="\[${COLOR_USER}\][\u🍥\h] "
PS1+="\[${COLOR_TIME}\][\$(__time_prompt)]\[${COLOR_RESET}\]"
PS1+="\[${COLOR_PATH}\] [\w]\[${COLOR_RESET}\]"
PS1+="\r\n\$ "

export PS1

# 仅交互式 shell 打印:否则会污染 scp/sftp/rsync 等非交互会话的协议流
if [[ $- == *i* ]]; then
    printf "Was vernünftig ist, das ist wirklich;\r\n"
    printf "und was wirklich ist, das ist vernünftig.\r\n"
    printf "                                         ---- G.W.F.Hegel\r\n"
fi

#fastfetch #--sixel ~/fetch.png --logo-width 40