# self_configurations

自用配置文件、脚本和小工具。都是不够格单开 repo 的小东西,所以集中在这一个仓库里,
按「能不能直接跑」分成三类:

```
apps/      自成一体的小应用(有依赖、有自己的文档)
config/    直接拷到系统里的配置文件
scripts/   一次性或偶尔用的小脚本
```

## apps/

| 目录 | 是什么 |
| --- | --- |
| `apps/web_img/` | FastAPI 只读服务,把某个目录的图片/文本暴露成网页,局域网内用浏览器看。 |
| `apps/text_server.py` | 只依赖标准库的文件投放服务,收 curl 发来的文件或文本。 |

### web_img

```bash
cd apps/web_img
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python main.py
```

配置固定读**当前目录**的 `conf.toml`(该文件已 ignore,不入库),字段只有 `dir` / `host` / `port`;
不写会直接退出并打印模板,也可以照抄 `main.py` 里的 `CONFIG_TEMPLATE`。细节见
`apps/web_img/README.md`。

### text_server

```bash
python3 apps/text_server.py
```

没有命令行开关,直接改脚本顶部的 `HOST` / `PORT` / `UPLOAD_DIR`;
想在不动源码的前提下覆盖,就在**脚本同目录**放一个 `apps/text_server_config.py`,写同名变量即可
(该文件名已被 ignore,是本地配置)。上传落盘目录是 `apps/uploads/`,同样不入库。

## config/

| 文件 | 说明 |
| --- | --- |
| `config/bashrc.sh` | 追加/合并进 `~/.bashrc` 的片段:cargo/GHCup 环境变量、venv 与时间提示的 PS1、`info`/`error` 函数。 |
| `config/kitty.conf` | kitty 终端配置(Catppuccin Mocha、透明度、背景模糊),合并进 `~/.config/kitty/kitty.conf`。 |

这两个文件是**片段**,仓库里不生效,需要手动拷进系统对应位置。

## scripts/

| 文件 | 说明 |
| --- | --- |
| `scripts/rename_index.sh` | 把文件名末尾 `_p<数字>` 补零对齐(默认 2 位,`-3` 为 3 位),方便排序。 |
| `scripts/browser_webgpu.sh` | 用无沙盒 + WebGPU 参数启动 chromium 和 VS Code。 |

## 说明

- `uploads/`、`__pycache__/`、`.venv/`、`conf.toml`、`text_server_config.py` 均为本地内容,已在
  `.gitignore` 或各自目录的 `.gitignore` 里排除。
- 原先散在根目录的文件已用 `git mv` 归位,历史保留;移动记录见 `git log --follow <文件>`。
