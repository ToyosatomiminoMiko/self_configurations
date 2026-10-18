"""局域网只读图片/文本浏览服务.

是什么: 一个 FastAPI 应用, 把 web_img_conf.toml 里 dir 指定的目录暴露成网页;
        网页左栏是文件列表, 右栏看图片(可上一张/下一张翻页)或纯文本.
        不接受任何命令行参数, dir/host/port 全部来自当前目录下的 web_img_conf.toml.

为什么:
  1. 只在局域网自用, 所以没有登录、没有鉴权、没有 HTTPS; 唯一保留的校验是路径越界检查,
     因为 URL 里的 path 参数直接来自浏览器, 不检查的话 "../../etc/passwd" 就能读到目录外.
  2. 不做缩略图/图片处理: 浏览器自己会把 <img> 缩小显示, 省掉 Pillow 依赖和磁盘缓存.
  3. 不引入 Jinja2: 页面是一份静态 HTML, 数据全部由 /api/list 等 JSON 接口提供,
     前端拿 JSON 自己拼 DOM, 少一层模板渲染就少一份耦合.
  4. 配置用 TOML 而不引第三方库: Python 3.11 起 tomllib 在标准库里, 且它只接受
     二进制流、只读不写, 正好符合"启动时读一次配置"的用法.

元信息: tempest, 2026-10-12.
"""

import mimetypes
import re
import socket
import tomllib
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

# 图片后缀集合, 决定哪些文件在网页里按图片翻页浏览.
IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".avif",
    ".ico",
    ".tif",
    ".tiff",
}

# 文本后缀集合, 决定哪些文件可以当纯文本打开.
TEXT_EXTS = {
    ".txt",
    ".md",
    ".markdown",
    ".log",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".htm",
    ".css",
    ".sh",
    ".bat",
    ".xml",
    ".rst",
    ".tex",
    ".sql",
}

# 单个文本文件最多读这么多字节.理由: 用户可能手滑点开几百 MB 的日志,
# 一次性读进内存再塞给浏览器会卡死两端, 超出就截断并在页面提示.
TEXT_MAX_BYTES = 1 * 1024 * 1024

app = FastAPI(title="web_img")

# 配置文件固定是当前目录下的 web_img_conf.toml: 没有命令行参数, 这是唯一的配置入口.
CONFIG_FILE = "web_img_conf.toml"

# 配置文件缺失时打印出来让用户照抄; 这些默认值同时也是字段缺失时的取值.
CONFIG_TEMPLATE = """\
# 要浏览的目录.相对路径按启动时的工作目录(也就是 web_img_conf.toml 所在目录)解析.
dir = "."
# 监听地址.0.0.0.0 表示监听所有网卡, 局域网其他机器才能访问.
host = "0.0.0.0"
# 监听端口.
port = 8000
"""

# 服务根目录, 由 load_config() 根据 web_img_conf.toml 赋值; 所有 URL 路径都相对它解析.
ROOT: Path = Path(".").resolve()


def natural_key(name: str):
    """文件名排序键: img2 排在 img10 前面.

    原理: 用捕获组把字符串切成 "非数字段/数字段" 交替的列表, 数字段转成 int 比较.
    re.split 带捕获组时输出位置固定(偶数位非数字, 奇数位数字), 所以列表元素类型逐位一致,
    不会出现 str 和 int 相比的 TypeError.
    """
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", name)
    ]


def resolve_in_root(rel: str) -> Path:
    """把 URL 里的相对路径解析成真实路径, 并保证它没跑出 ROOT.

    resolve() 会展开 ".." 和符号链接; 展开后必须等于 ROOT 或是 ROOT 的子路径,
    否则抛 403 —— 这是防目录穿越的关键一步.
    """
    candidate = (ROOT / rel).resolve()
    if candidate != ROOT and ROOT not in candidate.parents:
        raise HTTPException(status_code=403, detail="路径越界")
    return candidate


def rel_of(path: Path) -> str:
    """真实路径 -> 相对 ROOT 的 URL 路径(始终用 / 分隔), 根目录返回空串."""
    rel = path.relative_to(ROOT)
    return "" if str(rel) == "." else rel.as_posix()


def classify(path: Path) -> str:
    """按后缀把文件分成 image / text / other, 前端据此决定点击行为."""
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in TEXT_EXTS:
        return "text"
    return "other"


@app.get("/")
def index():
    """返回唯一的静态页面.数据不在这里渲染, 由页面里的 fetch 去调下面的 JSON 接口."""
    page = Path(__file__).parent / "templates" / "index.html"
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/api/list")
def api_list(path: str = ""):
    """列目录: 子目录在前、文件在后, 各自按自然序排.

    返回里同时给出 crumbs(面包屑)和 entries; entries 里 image 类型的 path 顺序
    就是前端翻页的顺序, 前端不需要自己再判断后缀.
    """
    target = resolve_in_root(path)
    if not target.is_dir():
        raise HTTPException(status_code=404, detail="不是目录")

    dirs, files = [], []
    for child in target.iterdir():
        try:
            if child.is_dir():
                dirs.append({"type": "dir", "name": child.name, "path": rel_of(child)})
            elif child.is_file():
                kind = classify(child)
                files.append(
                    {
                        "type": kind,
                        "name": child.name,
                        "path": rel_of(child),
                        "size": child.stat().st_size,
                    }
                )
        except OSError:
            # 断链的符号链接、权限不足的项: 直接跳过, 不能让一个坏文件毁掉整个列表.
            continue

    dirs.sort(key=lambda item: natural_key(item["name"]))
    files.sort(key=lambda item: natural_key(item["name"]))

    crumbs, cursor = [], target
    while True:
        crumbs.append(
            {
                "name": ROOT.name if cursor == ROOT else cursor.name,
                "path": rel_of(cursor),
            }
        )
        if cursor == ROOT:
            break
        cursor = cursor.parent
    crumbs.reverse()

    return JSONResponse(
        {
            "path": rel_of(target),
            "crumbs": crumbs,
            "entries": dirs + files,
            "total_images": sum(1 for item in files if item["type"] == "image"),
        }
    )


@app.get("/api/text")
def api_text(path: str = ""):
    """读文本文件内容.用 errors="replace" 兜底, 因为编码未知的文件(GBK、二进制混入)
    不该让接口 500, 显示成乱码也比报错有用."""
    target = resolve_in_root(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="不是文件")

    size = target.stat().st_size
    with target.open("rb") as handle:
        raw = handle.read(TEXT_MAX_BYTES)

    return JSONResponse(
        {
            "path": rel_of(target),
            "name": target.name,
            "size": size,
            "truncated": size > TEXT_MAX_BYTES,
            "content": raw.decode("utf-8", errors="replace"),
        }
    )


@app.get("/media/{path:path}")
def media(path: str):
    """原样回传文件字节.图片的 src、other 类型的下载链接都指向这里.

    path:path 是 FastAPI 的"通配路径"写法, 让 "/" 也能作为参数的一部分传进来,
    否则多层子目录的文件取不到.
    """
    target = resolve_in_root(path)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")

    media_type, _ = mimetypes.guess_type(target.name)
    return FileResponse(target, media_type=media_type or "application/octet-stream")


def local_ip():
    """猜出本机在局域网里的 IP, 只是为了启动时把可分享的网址打印出来.

    原理: 对一个不可达的 UDP 地址调 connect() 不会真的发包, 只是让内核按路由表挑出
    出口网卡, 再从 getsockname() 读回该网卡的地址.失败(比如没接网线)就返回 None,
    打印环节跳过即可, 不影响服务启动.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return None


def load_config() -> dict:
    """读取并校验当前目录下的 web_img_conf.toml, 返回 {config_path, host, port} 并顺手设置 ROOT.

    为什么配错就直接退出: 服务起来之后就很难发现"根目录指错了", 不如启动阶段一次报清楚.
    缺字段走默认值, 只有真跑不起来的情况(dir 不存在、port 非法)才 SystemExit.
    """
    global ROOT
    config_path = Path(CONFIG_FILE)
    if not config_path.is_file():
        raise SystemExit(
            f"缺少配置文件 {config_path.resolve()}, 请创建它, 内容示例:\n\n{CONFIG_TEMPLATE}"
        )

    # tomllib 只接受二进制流, 且只会解析、不会写回, 所以配置文件不会被程序改动.
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        # 手改配置最常见的失误就是 TOML 语法写错, 而原始报错里不带文件名和行号上下文, 这里补上.
        raise SystemExit(f"web_img_conf.toml 语法错误: {exc}")

    # dir 相对当前工作目录解析(web_img_conf.toml 也在那儿), 所以在哪个目录启动就以哪里为基准.
    ROOT = Path(str(raw.get("dir", "."))).expanduser().resolve()
    if not ROOT.is_dir():
        raise SystemExit(f"web_img_conf.toml 的 dir 不是已存在的目录: {ROOT}")

    try:
        port = int(raw.get("port", 8000))
    except (TypeError, ValueError):
        raise SystemExit(f"web_img_conf.toml 的 port 不是整数: {raw.get('port')!r}")
    if not 1 <= port <= 65535:
        raise SystemExit(f"web_img_conf.toml 的 port 超出 1-65535: {port}")

    return {
        "config_path": config_path.resolve(),
        "host": str(raw.get("host", "0.0.0.0")),
        "port": port,
    }


def main():
    config = load_config()

    print(f"配置文件: {config['config_path']}")
    print(f"根目录: {ROOT}")
    print(f"本机访问: http://127.0.0.1:{config['port']}")
    ip = local_ip()
    if ip:
        print(f"局域网访问: http://{ip}:{config['port']}")
    # host 配成 0.0.0.0 才会监听所有网卡, 局域网其他机器才能通过本机 IP 访问.
    uvicorn.run(app, host=config["host"], port=config["port"])


if __name__ == "__main__":
    main()
