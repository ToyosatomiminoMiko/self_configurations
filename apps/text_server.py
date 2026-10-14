#!/usr/bin/env python3
"""内网文件投放服务器:只依赖标准库,收 curl 发来的文件或文本。

用法:
  curl -T a.bin http://ip:8080/                               # PUT;URL 以 / 结尾时 curl 会自己补上文件名
  tar cz . | curl -T - "http://ip:8080/?name=src.tgz"         # 管道流式上传(大小未知,走 chunked)
  curl --data-binary @a.bin -H "X-Filename: a.bin" http://ip:8080/
  echo 一段文字 | curl --data-binary @- http://ip:8080/        # 纯文本只打到 stdout

存还是不存(规则就这两条):
  带文件名提示(X-Filename / ?name= / URL 路径) -> 存
  其余(纯文本 POST)                      -> 只打到 stdout,响应里也会这么说
  同名不覆盖,自动加 .1 .2 后缀。

响应规范(所有回包都出自 Handler.respond,没有第二个写 wfile 的地方):
  HTTP 状态码 + 一行纯文本 body,以 \n 结尾;首词由状态码推出,2xx 是 ok,4xx/5xx 是 error,
  调用方只提供后半句"说明":
    ok                                    # GET / 探活
    ok saved uploads/a.txt (16 bytes)     # 落盘成功
    ok text 16 bytes (只打印到 stdout,未落盘)
    error 不再支持 multipart(-F);改用 ...  # 出错时一行写完,不换行、不带 HTML
  http.server 自带的错误回包(畸形请求行、没实现的方法等)已覆写进同一个出口,
  所以不管哪条路径,客户端拿到的都是这一种形状,看首词就能判成败。

为什么不再收 multipart:curl -F 那套(多字段表单、RFC 2231 文件名编码)要让标准库的
email 解析器来拆才稳,为了一个文件多背整个解析器不划算,而 PUT / --data-binary 本来就
够用;现在 -F 一律回 415 并提示改法,绝不落盘半截内容。

启动:
  python3 text_server.py
  参数没有命令行开关:默认值在下面的 DEFAULTS 里,想在不动源码的前提下覆盖,就在脚本同目录
  放一个 text_server.toml,只写要改的字段,例如 port = 9000。
  为什么用 TOML:解析用标准库 tomllib(Python 3.11+),不用装依赖;而以前那套"把 .py 当配置
  import 进来"要靠捕获 ModuleNotFoundError 才能判断文件在不在,路径还依赖 sys.path,
  变量名打错也照样静默用默认值(见元信息)。
  文件不存在就全用 DEFAULTS;字段名写错或类型不对会在启动阶段直接报错,不静默忽略。
  请求 body 整块读进内存,适合中小文件;这是内网自用小工具,没做额外防御,
  出错就回一行 500 或者直接崩掉重起。

元信息:精简重写 2026-10-06,547 行 -> 229 行(实际代码约 130 行);同日去掉 multipart
(-F)支持(不再依赖 email 解析器),去掉命令行参数改为内部变量 + 可选
text_server_config.py,并把所有回包收敛到 Handler.respond 一种格式。
2026-10-15 把可选的 text_server_config.py 换成同目录的 text_server.toml(tomllib),
配置字段名写错不再静默失效。需要 Python 3.11+。
"""

from __future__ import annotations

import os
import sys
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO, ClassVar
from urllib.parse import parse_qs, unquote, urlparse

# --- 配置:默认值写在这里,脚本同目录的 text_server.toml 按字段覆盖 -----------------

# 为什么锚定脚本目录:从任何 cwd 启动,配置和落盘目录都跟着脚本走,不会散在工作目录里
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "text_server.toml")

# 字段名 -> 默认值;TOML 里写了哪个就覆盖哪个,没写的用这里的值
DEFAULTS: dict[str, Any] = {
    "host": "0.0.0.0",  # 监听地址,0.0.0.0 = 所有网卡
    "port": 8080,  # 监听端口
    "upload_dir": os.path.join(HERE, "uploads"),  # 落盘目录
}

# 为什么留一份模板:同目录没有 text_server.toml 时,报错信息里直接给出可照抄的完整内容
CONFIG_TEMPLATE = """\
# 是什么: text_server 的本地配置,放脚本同目录,只写要改的字段,缺的字段用脚本里的默认值.
host = "0.0.0.0"        # 监听地址, 0.0.0.0 = 所有网卡
port = 8080             # 监听端口
upload_dir = "uploads"  # 落盘目录, 相对路径按本文件所在目录解析
"""


def load_config(path: str = CONFIG_FILE) -> tuple[str, int, str]:
    """读 text_server.toml 并校验,返回 (host, port, upload_dir);文件不存在就全用 DEFAULTS。

    为什么多出来的字段要报错而不是忽略:配置里出现 DEFAULTS 之外的键一定是笔误(比如
    prot = 9000),静默忽略会让人以为配置生效了其实没有——旧的 .py 配置就是这个问题。
    为什么相对路径按本文件所在目录解析:配置和脚本在一起,这样从任何 cwd 启动都指向同一处。
    """
    values: dict[str, Any] = dict(DEFAULTS)

    if os.path.isfile(path):
        # tomllib 只接受二进制流,且只解析不写回,所以配置文件不会被程序改动
        try:
            with open(path, "rb") as handle:
                raw: dict[str, Any] = tomllib.load(handle)
        except tomllib.TOMLDecodeError as error:
            # 手改配置最常见的失误就是 TOML 语法写错,而原始报错不带文件名,这里补上
            raise SystemExit(f"{path} 语法错误: {error}")
        unknown = sorted(set(raw) - set(DEFAULTS))
        if unknown:
            raise SystemExit(f"{path} 里有不认识的字段 {unknown},可用字段 "
                             f"{sorted(DEFAULTS)}\n\n{CONFIG_TEMPLATE}")
        values.update(raw)

    host = values["host"]
    if not isinstance(host, str) or not host.strip():
        raise SystemExit(f"配置项 host 必须是非空字符串: {host!r}")

    port = values["port"]
    # 为什么单独挡 bool:TOML 的 true/false 在 Python 里是 int 的子类,int(True) == 1 会被静默当成端口 1
    if isinstance(port, bool) or not isinstance(port, int):
        raise SystemExit(f"配置项 port 必须是整数: {port!r}")
    if not 1 <= port <= 65535:
        raise SystemExit(f"配置项 port 超出 1-65535: {port}")

    upload_dir = values["upload_dir"]
    if not isinstance(upload_dir, str) or not upload_dir.strip():
        raise SystemExit(f"配置项 upload_dir 必须是非空字符串: {upload_dir!r}")
    # 为什么 expanduser:配置里写 ~/drop 也能落在预期位置(TOML 只能写字符串,不能算表达式)
    upload_dir = os.path.expanduser(upload_dir.strip())
    if not os.path.isabs(upload_dir):
        upload_dir = os.path.join(HERE, upload_dir)

    return host.strip(), port, upload_dir


# 为什么:输出重定向到文件/管道时 stdout 默认块缓冲,收到的内容会卡在缓冲区里看不见
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


def read_chunked(rfile: BinaryIO) -> bytes:
    """读 chunked 请求体。

    为什么必须支持:curl 用 -T - 从管道读数据(长度未知)时只能发 chunked,
    旧版一律拒掉 TE,于是 `tar cz . | curl -T - ...` 这种流式上传直接失败。
    只处理 curl 会发的形态:十六进制长度 + CRLF + 数据 + CRLF,以 0 长度收尾。
    """
    body = bytearray()
    while True:
        line = rfile.readline(1024)
        if not line:
            break
        size = int(line.split(b";")[0].strip() or b"0", 16)  # 忽略 chunk 扩展
        if size == 0:
            while rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                pass  # 吃掉结束 chunk 后面的 trailer,免得残留字节错帧
            break
        body += rfile.read(size)
        rfile.read(2)  # chunk 数据后面的 CRLF
    return bytes(body)


def read_body(handler: BaseHTTPRequestHandler) -> bytes:
    """按 Content-Length 或 chunked 读完整个请求体。"""
    if "chunked" in (handler.headers.get("Transfer-Encoding") or "").lower():
        return read_chunked(handler.rfile)
    return handler.rfile.read(int(handler.headers.get("Content-Length") or 0))


class UnsupportedMedia(Exception):
    """客户端用了本服务器不支持的请求编码(目前只有 multipart/form-data),由 Handler 回 415。"""


def fix_name(name: str) -> str:
    """把头部里的文件名还原成正常文本。

    为什么:HTTP 头按 RFC 只能装 latin-1,curl 却把文件名按 UTF-8 字节直接塞进去,
    解析出来是乱码;先转回原始字节再按 UTF-8 解,中文名才不会变乱码。
    """
    try:
        raw = name.encode("latin-1", "surrogateescape")
    except UnicodeEncodeError:
        return name  # 已经是正确解码的(值里有 latin-1 装不下的字符)
    return raw.decode("utf-8", "replace")


def safe_name(name: str) -> str:
    """把客户端给的文件名压成 uploads/ 下的一个纯文件名。"""
    # 为什么 basename:防止 "../../x" 这种名字把文件写到 uploads/ 外面
    name = os.path.basename(name.replace("\\", "/")).strip()
    # 为什么按字节截断:文件名上限是 255 字节,200 个汉字就有 600 字节,直接 open() 会报错
    name = name.encode("utf-8")[:200].decode("utf-8", "ignore").strip()
    return name or "unnamed"


def save(upload_dir: str, name: str, data: bytes) -> str:
    """存盘并返回回显给客户端的那行;同名不覆盖,自动加 .1 .2。"""
    os.makedirs(upload_dir, exist_ok=True)
    base = safe_name(name)
    path = os.path.join(upload_dir, base)
    index = 1
    while True:
        try:
            # 为什么用 "x"(O_CREAT|O_EXCL):创建这个动作由内核保证原子,
            # 换成 exists() 再 open() 的话,两个并发同名请求会挑中同一个路径互相覆盖
            with open(path, "xb") as handle:
                handle.write(data)
            break
        except FileExistsError:
            path = os.path.join(upload_dir, f"{base}.{index}")
            index += 1
    info = (f"saved {os.path.basename(os.path.normpath(upload_dir))}/"
            f"{os.path.basename(path)} ({len(data)} bytes)")
    print(info)
    return info


class Handler(BaseHTTPRequestHandler):
    """请求处理器;upload_dir 由 main() 在启动时写成类属性。"""

    # 为什么用 HTTP/1.1:curl 发 body 前会等 Expect: 100-continue,1.0 不回应它,curl 要白等 1 秒
    protocol_version = "HTTP/1.1"
    server_version = "text_server/3.1"
    upload_dir: ClassVar[str] = ""

    def log_message(self, format: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), format % args), file=sys.stderr)

    # --- 响应:所有回包只走这一个出口 -----------------------------------------

    def respond(self, status: int, detail: str = "") -> None:
        """回给客户端的所有内容都从这里出去;格式见模块开头的"响应规范"。

        为什么只留一个出口:状态码和文案分两处写迟早对不上(比如回了 415、文案还是 "ok ..."),
        现在 ok/error 前缀由状态码自己推出来,调用方只写"说明"那一半。
        为什么每次都要精确的 Content-Length:keep-alive 下客户端靠长度判断 body 结束,
        少算一个字节就会把响应粘到下一条请求的响应上。
        """
        word = "ok" if status < 400 else "error"
        # 为什么没有说明时不补空格:"ok " 带个尾空格很难看也不好比对
        text = f"{word} {detail}" if detail else word
        payload = (text + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        # 为什么 HEAD 不写 body:HEAD 的定义就是只要头部;本服务没实现 do_HEAD,
        # HEAD 会走到 send_error 再回到这里,标准库原来也是这么跳过的
        if self.command != "HEAD":
            self.wfile.write(payload)

    def send_error(self, code: int, message: str | None = None,
                   explain: str | None = None) -> None:
        """把 http.server 自带的错误回包(畸形请求行、没实现的方法等)也收回 respond。

        为什么必须覆写:这几条路径是标准库自己触发的,不拦下来就会回一坨 HTML,
        客户端有时拿到一行纯文本、有时拿到一个网页,格式就谈不上统一了。
        为什么不做别的:日志照旧打一份(否则错误在 stderr 里看不见),是否关连接由
        parse_request 在出错路径上置好的 close_connection 决定,这里不抢着改。
        """
        self.log_error("code %d, message %s", code, message)
        if message is None:
            # 为什么自己查表:标准库在这里也是拿 responses 里的短语补 message 的,照做才不会
            # 让 414 这种"只给状态码"的路径回一个光秃秃的 "error"(连原因都没有)
            message = self.responses.get(code, ("", ""))[0]
        self.respond(code, message or explain or "")

    # --- 各方法 -------------------------------------------------------------

    def do_GET(self) -> None:
        # 是:浏览器或 curl 直接打开能看到 ok,方便确认连得上
        self.respond(200)

    def do_POST(self) -> None:
        self._receive()

    def do_PUT(self) -> None:
        self._receive()

    def _receive(self) -> None:
        try:
            self.respond(200, self._store(read_body(self)))
        except UnsupportedMedia as error:
            # 为什么不用关连接:read_body 已经把 body 读干净,keep-alive 下不会再错帧
            self.respond(415, str(error))
        except Exception as error:  # 是:单个请求出错只回一行,不拖垮整个进程
            # 为什么关连接:此时 body 多半没读完,留着 keep-alive 会把残留字节当成下一轮的请求行
            self.close_connection = True
            self.respond(500, f"{type(error).__name__}: {error}")

    def _store(self, body: bytes) -> str:
        """存盘/打印,返回给 respond 的"说明"部分(不带 ok/error 前缀,那是 respond 按状态码加的)。"""
        if self.headers.get("Content-Type", "").lower().startswith("multipart/form-data"):
            # 为什么直接拒而不是退回自己拆:multipart 的边界扫描和 RFC 2231 编码手写很容易出错,
            # 会存下被截断或乱码的文件;拒掉并报出替代写法,客户端一眼能改对
            raise UnsupportedMedia(
                '不再支持 multipart(-F);改用 "curl -T a.bin http://ip:8080/" 或 '
                '"curl --data-binary @a.bin -H \'X-Filename: a.bin\' http://ip:8080/"'
            )
        hint = self._name_hint()
        if hint is None:
            # 是:没给文件名的普通 POST 当文本处理,只打到 stdout,方便直接粘贴一段文字
            print(body.decode("utf-8", "replace"))
            return f"text {len(body)} bytes (只打印到 stdout,未落盘)"
        return save(self.upload_dir, hint, body)

    def _name_hint(self) -> str | None:
        """文件名提示:X-Filename 头 > ?name= / ?filename= > URL 路径最后一段;都没有则 None。"""
        query = parse_qs(urlparse(self.path).query)
        # 为什么只有头要 fix_name:?name= 与 URL 路径在 parse_qs/unquote 里已按 UTF-8 解码,
        # 而自定义头被 http.server 按 latin-1 解,中文名到了这里还是乱码
        header = self.headers.get("X-Filename")
        name = (fix_name(header) if header
                else (query.get("name") or query.get("filename") or [""])[0]
                or os.path.basename(unquote(urlparse(self.path).path)))
        return name or None


def main() -> int:
    host, port, upload_dir = load_config()
    Handler.upload_dir = os.path.abspath(upload_dir)
    os.makedirs(Handler.upload_dir, exist_ok=True)
    # 是:启动时打一行,让人一眼看出配置到底是从文件来的还是全用默认值
    config_note = CONFIG_FILE if os.path.isfile(CONFIG_FILE) else "(无配置文件,全用默认值)"
    try:
        # 为什么多线程:HTTPServer 是单线程的,一个挂住的连接会把后面所有请求一起堵死
        with ThreadingHTTPServer((host, port), Handler) as server:
            # 为什么用 server_address[1] 而不是 port:port 写 0 时由系统挑端口,这里报的是真实端口
            print(f"receiving on http://{host}:{server.server_address[1]}/  ->  {Handler.upload_dir}")
            print(f"config: {config_note}")
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    except OSError as error:
        print(f"error: 监听 {host}:{port} 失败: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
