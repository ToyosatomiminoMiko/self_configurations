#!/bin/bash
# 重命名文件便于排序: 把基名末尾 `_p` 后的数字补零到固定位数
#   默认宽度 2 (-p1 -> -p01), 加 `-3` 则宽度 3 (-p1 -> -p001)
# 用法: rename_index.sh [-3] [目录]   目录默认为当前目录

width=2
dir=""
for arg in "$@"; do
    case "$arg" in
        -3) width=3 ;;
        *)  dir="$arg" ;;
    esac
done
dir="${dir:-.}"

if [ ! -d "$dir" ]; then
    echo "不是目录: $dir" >&2
    exit 1
fi

for file in "$dir"/*; do
    # 跳过目录和匹配失败的字面量
    [ -f "$file" ] || continue

    # 分离基名和扩展名(以最后一个点为准)
    name="${file##*/}"
    if [[ "$name" == *.* ]]; then
        base="${name%.*}"
        ext=".${name##*.}"
    else
        base="$name"
        ext=""
    fi

    # 只处理基名末尾为 `_p` + 纯数字, 且位数不足 width 的
    [[ "$base" =~ _p([0-9]+)$ ]] || continue
    num="${BASH_REMATCH[1]}"
    [ "${#num}" -lt "$width" ] || continue

    # 10# 前缀: 避免 08/09 被当作八进制导致算术报错
    printf -v new_base '%s_p%0*d' "${base%_p*}" "$width" "$((10#$num))"
    new_name="$new_base$ext"

    if [ -e "$dir/$new_name" ]; then
        echo "跳过(目标已存在): $file" >&2
        continue
    fi

    mv -v "$file" "$dir/$new_name"
done
