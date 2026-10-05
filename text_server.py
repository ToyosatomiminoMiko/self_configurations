#!/usr/bin/env python3
"""内网文件投放服务器:只依赖标准库,收 curl 发来的文件或文本。

用法:
  curl -F "file=@a.txt" http://ip:8080/                       # 传文件(推荐)
  curl -T a.bin http://ip:8080/                               # PUT;URL 以 / 结尾时 curl 会自己补上文件名
  tar cz . | curl -T - "http://ip:8080/?name=src.tgz"         # 管道流式上传(大小未知,走 chunked)
  curl --data-binary @a.bin -H "X-Filename: a.bin" http://ip:8080/
  echo 一段文字 | curl --data-binary @- http://ip:8080/        # 纯文本只打到 stdout

存还是不存(规则就这三条):
  multipart(用了 -F)                     -> 存文件
  带文件名提示(X-Filename / ?name= / URL 路径) -> 存
  其余(纯文本 POST)                      -> 只打到 stdout,响应里也会这么说
  同名不覆盖,自动加 .1 .2 后缀。

启动:
  python3 text_server.py [--host 0.0.0.0] [--port 8080] [--dir ./uploads]
  请求 body 整块读进内存,适合中小文件;这是内网自用小工具,没做额外防御,
  出错就回一行 500 或者直接崩掉重起。

元信息:精简重写 2026-10-06,547 行 -> 229 行(实际代码约 130 行);需要 Python 3.10+。
"""

from __future__ import annotations

import argparse
import os
import sys
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import BinaryIO, ClassVar, Sequence
from urllib.parse import parse_qs, unquote, urlparse

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


def fix_name(name: str) -> str:
    """把头部里的文件名还原成正常文本。

    为什么:HTTP 头按 RFC 只能装 latin-1,curl 却把文件名按 UTF-8 字节直接塞进去,
    解析出来是乱码;先转回原始字节再按 UTF-8 解,中文名才不会变乱码。
    """
    try:
        raw = name.encode("latin-1", "surrogateescape")
    except UnicodeEncodeError:
        return name  # 已经是正确解码的(例如 filename*=utf-8''...)
    return raw.decode("utf-8", "replace")


def parse_multipart(body: bytes, content_type: str) -> list[tuple[str, str | None, bytes]]:
    """拆 multipart/form-data,返回 (字段名, 文件名或 None, 内容)。

    为什么交给标准库的 email 解析器:边界扫描、引号、RFC 2231 编码它都自带,
    比手写 split(b"--boundary") 又短又稳(内容里恰好出现边界字节时,裸切会把文件截断)。
    坑:必须显式写 policy=default,默认的 compat32 政策没有 iter_parts()。
    """
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\n\r\n" + body
    )
    parts: list[tuple[str, str | None, bytes]] = []
    for part in message.iter_parts():
        field = part.get_param("name", header="content-disposition")
        if field is None:
            continue  # 没有 name= 的段不符合表单语义,丢掉
        parts.append((field, part.get_filename(), part.get_payload(decode=True) or b""))
    return parts


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
    server_version = "text_server/3.0"
    upload_dir: ClassVar[str] = ""

    def log_message(self, format: str, *args: object) -> None:
        print("%s - %s" % (self.address_string(), format % args), file=sys.stderr)

    def _reply(self, status: int, message: str) -> None:
        """回一个纯文本响应;body 一律带上长度,keep-alive 下才不错帧。"""
        payload = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # --- 各方法 -------------------------------------------------------------

    def do_GET(self) -> None:
        # 是:浏览器或 curl 直接打开能看到 ok,方便确认连得上
        self._reply(200, "ok")

    def do_POST(self) -> None:
        self._receive()

    def do_PUT(self) -> None:
        self._receive()

    def _receive(self) -> None:
        try:
            self._reply(200, self._store(read_body(self)))
        except Exception as error:  # 是:单个请求出错只回一行,不拖垮整个进程
            # 为什么关连接:此时 body 多半没读完,留着 keep-alive 会把残留字节当成下一轮的请求行
            self.close_connection = True
            self._reply(500, f"error: {type(error).__name__}: {error}")

    def _store(self, body: bytes) -> str:
        content_type = self.headers.get("Content-Type", "")
        if content_type.lower().startswith("multipart/form-data"):
            saved: list[str] = []
            for field, filename, data in parse_multipart(body, content_type):
                if filename is None:
                    # 是:curl -F "file=./a.nix" 少了 @,curl 会把这串路径当普通文本字段发过来
                    print(f"{field} = {data.decode('utf-8', 'replace')} "
                          f'(普通字段,不是文件;传文件要写 @: -F "{field}=@文件路径")')
                else:
                    saved.append(save(self.upload_dir, fix_name(filename), data))
            if saved:
                return "ok " + "; ".join(saved)
            return 'ok (multipart 里没有文件:传文件要写 @,例如 -F "file=@a.txt")'

        hint = self._name_hint()
        if hint is None:
            # 是:没给文件名的普通 POST 当文本处理,只打到 stdout,方便直接粘贴一段文字
            print(body.decode("utf-8", "replace"))
            return f"ok text {len(body)} bytes (只打印到 stdout,未落盘)"
        return "ok " + save(self.upload_dir, hint, body)

    def _name_hint(self) -> str | None:
        """文件名提示:X-Filename 头 > ?name= / ?filename= > URL 路径最后一段;都没有则 None。"""
        query = parse_qs(urlparse(self.path).query)
        name = (self.headers.get("X-Filename")
                or (query.get("name") or query.get("filename") or [""])[0]
                or os.path.basename(unquote(urlparse(self.path).path)))
        return name or None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="内网文件投放服务器:收 curl 发来的文件/文本")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址,0.0.0.0 = 所有网卡")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--dir", dest="upload_dir",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
                        help="存盘目录,默认脚本同级的 uploads/")
    args = parser.parse_args(argv)

    Handler.upload_dir = os.path.abspath(args.upload_dir)
    os.makedirs(Handler.upload_dir, exist_ok=True)
    try:
        # 为什么多线程:HTTPServer 是单线程的,一个挂住的连接会把后面所有请求一起堵死
        with ThreadingHTTPServer((args.host, args.port), Handler) as server:
            print(f"receiving on http://{args.host}:{server.server_address[1]}/  ->  {Handler.upload_dir}")
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    except OSError as error:
        print(f"error: 监听 {args.host}:{args.port} 失败: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
