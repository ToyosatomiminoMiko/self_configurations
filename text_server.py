#!/usr/bin/env python3
"""极简文件接收服务器:只依赖标准库,收 curl 发过来的文件或文本。

用法(四种都支持):
  curl -F "file=@a.txt" http://server_ip:8080/                              # 文件(推荐)
  curl --data-binary @a.bin -H "X-Filename: a.bin" http://server_ip:8080/   # 原始 body + 指定文件名
  curl -T a.bin "http://server_ip:8080/?filename=a.bin"                     # PUT
  curl -X POST -H "Content-Type: text/plain" -d "hello_text" http://server_ip:8080/   # 文本

启动:
  python3 text_server.py [--host 0.0.0.0] [--port 8080] [--dir ./uploads]
                         [--max-bytes 67108864] [--timeout 60]
  # --max-bytes 是单个请求 body 的上限(默认 64 MiB,0 = 不限制),因为 body 是整块读进内存的
  # --timeout 是读请求的超时秒数(默认 60,0 = 不超时),慢速传大文件时调大或设 0

响应体会回显存到哪了(以前不分情况一律回 "ok",看不出文件到底存没存)。

元信息:重写 2026-10-06;需要 Python 3.10+(用了 `X | None` 与 `list[...]` 写法)。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from contextlib import suppress
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, BinaryIO, Callable, ClassVar, Final, Literal, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

# 为什么:输出重定向到文件/管道时 stdout 默认块缓冲,收到的内容会卡在缓冲区里看不见
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# 是:multipart 的一个段 —— (表单字段名, 文件名或 None, 段内容)
Part = tuple[str, str | None, bytes]
# 是:分隔符尾部形态 —— 后面还有段,还是整个 multipart 的结束
DelimiterTail = Literal["part", "close"]

# 是:文件名保留的最大字符数/字节数,留出 .N 后缀和路径本身的余量
_MAX_NAME_CHARS: Final[int] = 200
_MAX_NAME_BYTES: Final[int] = 200
# 是:同名时最多尝试 .1 ~ .999 后缀
_MAX_NAME_ATTEMPTS: Final[int] = 1000
# 是:RFC 7578 规定 boundary 不超过 70 字符,这里放宽到 200 只做防御,避免拿超长串去搜 body
_MAX_BOUNDARY_CHARS: Final[int] = 200
# 是:GET/OPTIONS 这类请求不该带 body,真带了最多愿意读掉这么多字节再决定能否复用连接
_MAX_DISCARD_BYTES: Final[int] = 64 * 1024
# 是:即使 --max-bytes 0(不限制)也要挡住的硬上限 —— 再大 rfile.read() 的参数会溢出成 OverflowError
_HARD_MAX_BYTES: Final[int] = 1 << 40

# 是:HTTP 头里的 `;key=value` 参数,值可以是带引号或不带引号两种写法
_HEADER_PARAM: Final[re.Pattern[str]] = re.compile(
    r';\s*([A-Za-z0-9_*\-]+)\s*=\s*(?:"([^"]*)"|([^;\r\n]*))'
)


class RequestError(Exception):
    """请求本身不合法;handler 统一把它翻成对应的 HTTP 状态码和文本。"""

    def __init__(self, status: HTTPStatus, message: str, *, close: bool = False) -> None:
        super().__init__(message)
        self.status: HTTPStatus = status
        self.message: str = message
        # 是:body 还没读完就要报错时必须关连接,否则残留字节会被当成下一轮的请求行
        self.close: bool = close


class ReadTimeout(RequestError):
    """读 body 超时。

    为什么连响应都不发:socket 一旦记录过超时,自己也不允许再写("cannot write to timed out
    object"),硬写只会再抛一次异常、把 traceback 刷进日志;客户端本来也已经收不到了。
    """


def emit(message: str, *, err: bool = False) -> None:
    """把日志写到 stdout(err=True 时写 stderr)。

    为什么吞 OSError:stdout 常接管道,读端(比如 `| head`)提前关掉后 print 会抛
    BrokenPipeError;文件其实已经存好了,不能因为日志打不出去就把请求判成 500。
    """
    with suppress(OSError, ValueError):
        print(message, file=sys.stderr if err else sys.stdout)


def safe_name(name: str) -> str:
    """把客户端给的文件名压成一个安全的纯文件名。

    为什么:basename 掉路径,防止客户端传 "../../x" 把文件写到 uploads/ 外面;
    再剥掉控制字符(open() 见到 NUL 会直接抛 ValueError)和纯点名("." / "..")。
    """
    name = os.path.basename(name.replace("\\", "/"))
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if not name or set(name) <= {"."}:
        return "unnamed"
    # 为什么按字节再截一刀:文件名上限是 255 字节而不是 255 个字符,200 个汉字就是 600 字节,
    # 直接交给 open() 会 ENAMETOOLONG(OSError → 500)
    trimmed = name[:_MAX_NAME_CHARS]
    while trimmed and len(trimmed.encode("utf-8")) > _MAX_NAME_BYTES:
        trimmed = trimmed[:-1]
    return trimmed or "unnamed"


def open_exclusive(upload_dir: str, name: str) -> tuple[str, BinaryIO]:
    """在 upload_dir 下独占创建一个文件,返回 (路径, 已打开的二进制句柄)。

    为什么用 O_EXCL:先 exists() 再 open() 是竞态,两个并发请求会挑中同一个路径互相覆盖;
    O_CREAT|O_EXCL 由内核保证"创建"这个动作本身是原子的失败/成功。
    """
    for index in range(_MAX_NAME_ATTEMPTS):
        candidate = name if index == 0 else f"{name}.{index}"
        path = os.path.join(upload_dir, candidate)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        return path, os.fdopen(fd, "wb")
    raise RuntimeError(f"同名文件太多,给 {name!r} 找不到可用后缀")


def save(upload_dir: str, name: str, data: bytes) -> str:
    """把 data 存到 upload_dir 下,返回给客户端回显的那行说明。"""
    os.makedirs(upload_dir, exist_ok=True)
    path, handle = open_exclusive(upload_dir, safe_name(name))
    with handle:
        handle.write(data)
    # 是:回显写成 "目录名/文件名" 而不是写死 uploads/,这样 --dir 换到别处时也看得出存哪了
    shown = os.path.join(os.path.basename(os.path.normpath(upload_dir)), os.path.basename(path))
    info = f"saved {shown} ({len(data)} bytes)"
    emit(info)
    return info


def header_param(header: str, key: str) -> str | None:
    """从 "Content-Type: ..." / "Content-Disposition: ..." 这类头里取一个参数;没有则 None。"""
    for match in _HEADER_PARAM.finditer(header):
        if match.group(1).lower() != key.lower():
            continue
        value = match.group(2) if match.group(2) is not None else (match.group(3) or "")
        return value.strip()
    return None


def decode_param(value: str) -> str:
    """把普通头部参数还原成文本。

    为什么:HTTP 头按 RFC 只能装 latin-1,而 curl 直接把文件名按 UTF-8 字节塞进来;
    header 解析出的是 latin-1 码点,先 encode 回原始字节再按 UTF-8 解,中文名才不会变乱码。
    """
    return value.encode("latin-1", "replace").decode("utf-8", "replace")


def decode_extended_param(value: str) -> str:
    """把 RFC 5987 的 ext-value(charset'lang'percent-encoded)还原成文本。

    坑:这个写法只能用在 `filename*=` 上;普通 `filename=` 里出现两个单引号是完全合法的
    (例如 filename="a''b.txt"),所以两者必须分开处理,不能靠"值里有没有 ''"来判断。
    """
    parts = value.split("'", 2)
    encoded = parts[2] if len(parts) == 3 else value
    return unquote(encoded, encoding="utf-8", errors="replace")


def _delimiter_tail(body: bytes, position: int) -> DelimiterTail | None:
    """position 紧接在 "--boundary" 之后;返回 "part" / "close",不合法则 None。

    为什么不能只看后面两字节是不是 "--":RFC 2046 规定分隔符后面只允许
    「行尾空白 + CRLF」(还有段)或「-- + 可选空白 + CRLF/结束」(最后一段)。
    内容里出现 `--boundary-------x` 时,前两字节也是 "--",只看两字节会把它误当成结束分隔符,
    于是文件被拦腰截断 —— 必须一直看到行尾。
    """
    index = position
    closing = body[index:index + 2] == b"--"
    if closing:
        index += 2
    # 是:分隔符后面允许有行尾空白(BWS)
    while index < len(body) and body[index:index + 1] in (b" ", b"\t"):
        index += 1
    if body[index:index + 2] == b"\r\n" or body[index:index + 1] == b"\n":
        return "close" if closing else "part"
    if index >= len(body):
        # 是:body 正好停在分隔符上 —— 只有结束形式(--boundary--)才算合法
        return "close" if closing else None
    return None


def _find_delimiter(body: bytes, delimiter: bytes, start: int) -> int:
    """找 start 之后独占一行的分隔符,返回它前面那个 CRLF 的下标;找不到返回 -1。"""
    needle = b"\r\n" + delimiter
    position = body.find(needle, start)
    while position != -1:
        if _delimiter_tail(body, position + len(needle)) is not None:
            return position
        # 坑:内容里恰好出现 "--boundary" 这样的字节时它不是分隔符,必须按上面的规则验证尾部,
        # 不然会把文件内容拦腰切断
        position = body.find(needle, position + 1)
    return -1


def parse_multipart(body: bytes, boundary: bytes) -> list[Part]:
    """按 RFC 7578 拆开 multipart/form-data 的各个段。

    为什么手写解析:Python 3.13 起 cgi 模块已被删除,这里只用标准库做最简实现。
    坑:body 已在内存里,大文件会占内存,适合小文件接收 —— 需要传大文件请调 --max-bytes 并接受内存占用。
    """
    delimiter = b"--" + boundary
    parts: list[Part] = []
    if body.startswith(delimiter) and _delimiter_tail(body, len(delimiter)) is not None:
        position = 0
    else:
        # 是:分隔符之前允许有一段 preamble(邮件正文里常见),跳过它
        preamble = _find_delimiter(body, delimiter, 0)
        position = preamble + 2 if preamble != -1 else -1

    while position != -1:
        cursor = position + len(delimiter)
        if _delimiter_tail(body, cursor) == "close":
            break  # 结束分隔符 --boundary--
        header_end = body.find(b"\r\n\r\n", cursor)
        if header_end == -1:
            break  # 头都没收全,后面不可能有完好的段
        header = body[cursor:header_end].lstrip(b"\r\n").decode("latin-1")
        content_start = header_end + 4
        next_position = _find_delimiter(body, delimiter, content_start)
        if next_position == -1:
            content = body[content_start:]
            position = -1
        else:
            content = body[content_start:next_position]
            position = next_position + 2

        field = header_param(header, "name")
        if field is None:
            continue  # 没有 name= 的段不符合表单语义,丢掉
        filename = header_param(header, "filename")
        if filename is None:
            extended = header_param(header, "filename*")
            filename = decode_extended_param(extended) if extended is not None else None
        else:
            filename = decode_param(filename)
        parts.append((decode_param(field), filename, content))
    return parts


class UploadServer(ThreadingHTTPServer):
    """多线程 + 端口可复用。

    为什么:HTTPServer 是单线程的,一个慢客户端(例如挂在 socket 上的端口扫描或
    只发头不发 body 的连接)会把后面所有请求一起堵死。
    """

    daemon_threads = True
    allow_reuse_address = True


class UploadHandler(BaseHTTPRequestHandler):
    """请求处理器;upload_dir / max_bytes / timeout 由 main() 在启动时写入类属性。"""

    # 为什么用 HTTP/1.1:curl 传大 body 会先发 Expect: 100-continue,1.0 不回应它,
    # curl 要白等 1 秒才肯发 body
    protocol_version = "HTTP/1.1"
    server_version = "text_server/2.0"
    # 为什么设超时:客户端声明了 body 却不发完(掉线、扫描器挂着连接)会把线程永远挂在
    # rfile.read 上,超时后 socketserver 关连接,线程才能回收
    # 为什么是 ClassVar:基类 StreamRequestHandler 就是这么声明的,main() 也按类赋值
    timeout: ClassVar[float | None] = 60
    upload_dir: ClassVar[str] = ""
    max_bytes: ClassVar[int] = 0  # 0 = 不限制

    # --- 响应 ---------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        """访问日志也走 emit。

        为什么:基类实现直接 sys.stderr.write,而 stderr 断管道(例如 `| head`)时抛出的
        BrokenPipeError 会顺着 send_response 冒上来,把已经存好的请求变成 500。
        """
        emit("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format % args),
             err=True)

    def _respond(
        self,
        status: HTTPStatus,
        message: str,
        *,
        close: bool = False,
        extra_headers: Sequence[tuple[str, str]] = (),
    ) -> None:
        """发一个纯文本响应;body 一律算出长度,配合 HTTP/1.1 keep-alive 才不错帧。"""
        payload = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        for key, value in extra_headers:
            self.send_header(key, value)
        if close:
            self.close_connection = True
            self.send_header("Connection", "close")
        self.end_headers()
        # 为什么吞 OSError:客户端可能在收到响应前就断开或已超时(socket 会抛
        # "cannot write to timed out object"),报错只会刷屏,没有可做的补救
        with suppress(OSError):
            self.wfile.write(payload)

    def _dispatch(self, action: Callable[[], str]) -> None:
        """统一收口:RequestError → 对应状态码,其他异常 → 500,不让 traceback 冲进 socket。"""
        try:
            message = action()
        except ReadTimeout as error:
            # 是:超时的 socket 写不出响应,只记一行日志并关连接,免得 socketserver 打 traceback
            self.log_error("读 body 超时,关闭连接(%s)", error)
            self.close_connection = True
        except RequestError as error:
            self._respond(error.status, error.message, close=error.close)
        except OSError as error:
            # 为什么 close=True:此时 body 十有八九没读完,复用连接就会错帧
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR, f"error: IO 异常: {error}", close=True)
        except Exception as error:  # 兜底:单个请求的意外不能让整个服务进程倒掉
            self._respond(HTTPStatus.INTERNAL_SERVER_ERROR,
                          f"error: {type(error).__name__}: {error}", close=True)
        else:
            self._respond(HTTPStatus.OK, message)

    # --- 请求体 -------------------------------------------------------------

    def _read_body(self) -> bytes:
        """读完整个 body;任何不合法的情形都抛 RequestError,由 _dispatch 翻成响应。"""
        transfer_encoding = self.headers.get("Transfer-Encoding")
        content_lengths = self.headers.get_all("Content-Length") or []
        if transfer_encoding is not None:
            # 坑:BaseHTTPRequestHandler 不解码 chunked,而 TE 与 CL 同时出现更是 RFC 9112 明令
            # 拒绝的走私面(CL.TE / TE.CL),所以只要出现 TE 就拒掉,不看它是什么编码
            status = HTTPStatus.BAD_REQUEST if content_lengths else HTTPStatus.LENGTH_REQUIRED
            raise RequestError(status, f"error: 不支持 Transfer-Encoding: {transfer_encoding},"
                                       "请用 curl -F 或 -T", close=True)
        if len(set(content_lengths)) > 1:
            # 为什么:多个取值不同的 Content-Length 让 body 边界不确定,必须拒
            raise RequestError(HTTPStatus.BAD_REQUEST,
                               f"error: 多个互相冲突的 Content-Length: {content_lengths}", close=True)
        raw_length = content_lengths[0] if content_lengths else None
        if raw_length is None:
            # 坑:没有 Content-Length 时 rfile.read(0) 只会读到空字节,客户端却仍收到 200;
            # 这里直接报错,避免"看起来成功但什么都没存"
            raise RequestError(HTTPStatus.LENGTH_REQUIRED,
                               "error: 缺少 Content-Length,请用 curl -F / -T 或 --data-binary", close=True)
        try:
            length = int(raw_length)
        except ValueError:
            raise RequestError(HTTPStatus.BAD_REQUEST,
                               f"error: Content-Length 不是整数: {raw_length!r}", close=True) from None
        if length < 0:
            raise RequestError(HTTPStatus.BAD_REQUEST, "error: Content-Length 为负数", close=True)
        if self.max_bytes and length > self.max_bytes:
            # 是:413 的写成 HTTPStatus(413),因为 CONTENT_TOO_LARGE 这个名字要 Python 3.13 才有
            raise RequestError(HTTPStatus(413),
                               f"error: body {length} 字节超过上限 {self.max_bytes} 字节", close=True)
        if length > _HARD_MAX_BYTES:
            # 为什么 --max-bytes 0 也要挡:天文数字传给 rfile.read() 会抛 OverflowError(500),
            # 而 1 TiB 之上对这个工具没有任何实际意义
            raise RequestError(HTTPStatus(413),
                               f"error: body {length} 字节超过硬上限 {_HARD_MAX_BYTES} 字节", close=True)
        try:
            data = self.rfile.read(length)
        except OSError as error:
            # 是:超时(socket 会抛 "cannot read from timed out object",它不是 TimeoutError)、
            # 连接重置都落这里 —— 读不下去就没法接着解析,只能关连接
            raise ReadTimeout(HTTPStatus.REQUEST_TIMEOUT,
                              f"error: 读 body 失败: {error}", close=True) from error
        if len(data) != length:
            raise RequestError(HTTPStatus.BAD_REQUEST,
                               f"error: body 没读全(声明 {length} 字节,实收 {len(data)} 字节)", close=True)
        return data

    def _discard_body(self) -> bool:
        """读掉并丢弃请求体,返回 True 表示连接还能安全复用。

        为什么:GET/OPTIONS 不该带 body,但带了而我们不读的话,残留字节会被当成下一轮的请求行
        (keep-alive 下直接错帧,也是请求走私的常见入口)。
        """
        if self.headers.get("Transfer-Encoding"):
            return False  # chunked 不解码,跳不过去
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return True
        try:
            length = int(raw_length)
        except ValueError:
            return False
        if length <= 0:
            return True
        if length > _MAX_DISCARD_BYTES:
            return False  # 太大不值得读,直接关连接
        try:
            return len(self.rfile.read(length)) == length
        except OSError:
            return False

    def _name_hint(self, query: Mapping[str, list[str]]) -> str:
        """挑一个文件名:优先级 X-Filename 头 > ?filename= > URL 路径最后一段 > 按时间生成。"""
        name = self.headers.get("X-Filename") or (query.get("filename") or [""])[0]
        if not name:
            name = os.path.basename(unquote(urlparse(self.path).path))
        return name or time.strftime("recv_%Y%m%d_%H%M%S.bin")

    def handle_expect_100(self) -> bool:
        """借 100-continue 的窗口提前拒掉超限的 body,省得客户端白传一遍。

        是:这里只是优化,真正的判定(含 --max-bytes 0 时的硬上限)在 _read_body 里。
        """
        if self.max_bytes:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            if length > self.max_bytes:
                self._respond(HTTPStatus(413),
                              f"error: body {length} 字节超过上限 {self.max_bytes} 字节", close=True)
                return False
        return super().handle_expect_100()

    # --- 各 HTTP 方法 -------------------------------------------------------

    def do_OPTIONS(self) -> None:
        # 是:CORS 预检要显式列出允许的方法和头,否则浏览器侧带 X-Filename 的请求会被拦
        self._respond(HTTPStatus.OK, "ok", close=not self._discard_body(), extra_headers=(
            ("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type, X-Filename"),
        ))

    def do_GET(self) -> None:
        # 是:浏览器直接打开也能看到 "ok",方便确认能不能连上;
        # 顺带丢弃可能存在的 body,否则残留字节会被当成下一轮的请求行
        self._respond(HTTPStatus.OK, "ok", close=not self._discard_body())

    def do_POST(self) -> None:
        self._dispatch(self._handle_post)

    def do_PUT(self) -> None:
        self._dispatch(self._handle_put)

    def _handle_post(self) -> str:
        query = parse_qs(urlparse(self.path).query)
        raw = self._read_body()
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.lower()  # 为什么统一小写:媒体类型大小写不敏感,别被 Octet-Stream 骗过去
        results: list[str] = []
        hint = ""

        if media_type.startswith("multipart/form-data"):
            boundary = header_param(content_type, "boundary") or ""
            if not boundary or len(boundary) > _MAX_BOUNDARY_CHARS:
                raise RequestError(HTTPStatus.BAD_REQUEST,
                                   "error: multipart 缺少合法的 boundary 参数", close=True)
            for field, filename, data in parse_multipart(raw, boundary.encode("latin-1", "replace")):
                if filename is None:
                    # 坑:curl -F "file=./a.nix" 少了 @,curl 会把这串路径当普通文本字段发过来,
                    # 服务端只收到文字、没有文件,这里明确提示一下
                    emit(f"{field} = {data.decode('utf-8', 'replace')} "
                         f'(普通字段,不是文件;传文件要写 @: -F "{field}=@文件路径")')
                else:
                    results.append(save(self.upload_dir, filename, data))
            if not results:
                hint = ' (multipart 里没有文件:curl -F 传文件必须写 @,例如 -F "file=@a.txt")'
        elif (media_type.startswith("application/octet-stream")
              or self.headers.get("X-Filename")
              or "filename" in query):
            # 是:没有 multipart 包装的原始 body,靠文件名提示存盘
            results.append(save(self.upload_dir, self._name_hint(query), raw))
        elif media_type.startswith("application/x-www-form-urlencoded"):
            emit(str(parse_qs(raw.decode("utf-8", "replace"))))
        else:
            # 是:纯文本只打印到 stdout 不落盘,响应里说明一下省得以为存了
            emit(raw.decode("utf-8", "replace"))

        if results:
            return "ok " + "; ".join(results) + hint
        return f"ok text {len(raw)} bytes ({hint.strip() or '只打印到 stdout,未落盘'})"

    def _handle_put(self) -> str:
        query = parse_qs(urlparse(self.path).query)
        raw = self._read_body()
        return "ok " + save(self.upload_dir, self._name_hint(query), raw)


def build_parser() -> argparse.ArgumentParser:
    """命令行参数;监听地址/端口/目录的默认值和原脚本一致(0.0.0.0:8080 + 同级 uploads/),
    另外加了 --max-bytes 和 --timeout 两个保护参数(旧版没有任何上限和保护)。"""
    parser = argparse.ArgumentParser(
        description="极简文件接收服务器:把 curl 发来的文件/文本收到 uploads/ 下",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址,0.0.0.0 = 所有网卡")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument("--dir", dest="upload_dir", default=None, help="存盘目录,默认脚本同级的 uploads/")
    parser.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024,
                        help="单个 body 上限(字节),0 = 不限制")
    parser.add_argument("--timeout", type=int, default=60,
                        help="读请求的超时秒数,0 = 不超时(慢速传大文件时可以调大)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 为什么手动查范围:argparse 的 type=int 不管值域,--port 99999 会在 bind() 里抛 OverflowError
    if not 0 <= args.port <= 65535:
        emit(f"error: --port 要在 0-65535 之间,收到 {args.port}", err=True)
        return 2
    if args.max_bytes < 0:
        emit(f"error: --max-bytes 不能为负(0 = 不限制),收到 {args.max_bytes}", err=True)
        return 2
    if args.timeout < 0:
        emit(f"error: --timeout 不能为负(0 = 不超时),收到 {args.timeout}", err=True)
        return 2

    here = os.path.dirname(os.path.abspath(__file__))
    upload_dir = os.path.abspath(args.upload_dir or os.path.join(here, "uploads"))
    os.makedirs(upload_dir, exist_ok=True)

    # 为什么写类属性:BaseHTTPRequestHandler 由服务器在每个连接上实例化,构造参数不好透传
    UploadHandler.upload_dir = upload_dir
    UploadHandler.max_bytes = args.max_bytes
    UploadHandler.timeout = float(args.timeout) if args.timeout else None

    limit = "不限制" if not args.max_bytes else f"{args.max_bytes} 字节"
    wait = "不超时" if args.timeout == 0 else f"{args.timeout:g} 秒"
    try:
        with UploadServer((args.host, args.port), UploadHandler) as server:
            # 是:--port 0 时内核会挑一个随机端口,所以启动行要打印真正绑上的那个
            bound_port = server.server_address[1]
            emit(f"receiving on http://{args.host}:{bound_port}/  ->  {upload_dir}  "
                 f"(单次上限 {limit},读超时 {wait})")
            server.serve_forever()
    except KeyboardInterrupt:
        emit("\nbye")
    except OSError as error:
        emit(f"error: 监听 {args.host}:{args.port} 失败: {error}", err=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
