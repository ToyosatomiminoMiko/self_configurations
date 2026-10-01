#!/bin/bash
# 重命名文件:将 _p 后的一位数字补零为两位,便于排序

for file in *; do
    # 跳过目录
    [ -f "$file" ] || continue

    # 分离基名和扩展名
    if [[ "$file" == *.* ]]; then
        base="${file%.*}"
        ext=".${file##*.}"
    else
        base="$file"
        ext=""
    fi

    # 检查基名是否以 "_p" + 一位数字 结尾(且之后无其他数字)
    if [[ "$base" =~ _p[0-9]$ ]]; then
        # 提取该一位数字
        digit="${base##*_p}"
        # 构造新基名:去掉末尾的 "_pX",再拼上 "_p0X"
        new_base="${base%_p?}_p0${digit}"
        new_name="${new_base}${ext}"

        # 仅当名称确实变化时才重命名
        if [ "$file" != "$new_name" ]; then
            mv -v "$file" "$new_name"
        fi
    fi
done
