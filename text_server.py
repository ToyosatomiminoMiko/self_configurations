#!/usr/bin/env python3
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# 为什么:输出重定向到文件/管道时 stdout 默认块缓冲,收到的内容会卡在缓冲区里看不见
sys.stdout.reconfigure(line_buffering=True)

# 用法(四种都支持):
#   curl -F "file=@a.txt" http://server_ip:8080/                              # 文件(推荐)
#   curl --data-binary @a.bin -H "X-Filename: a.bin" http://server_ip:8080/   # 原始 body + 指定文件名
#   curl -T a.bin "http://server_ip:8080/?filename=a.bin"                     # PUT
#   curl -X POST -H "Content-Type: text/plain" -d "hello_text" http://server_ip:8080/   # 文本

# 是:收到的文件放在脚本同级的 uploads/,同名不覆盖而是加 .1 .2 后缀;
# 响应体会回显存到哪了(以前不分情况一律回 "ok",看不出文件到底存没存)
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")


def safe_name(name):
    # 为什么:basename 掉路径,防止客户端传 "../../x" 把文件写到 uploads/ 外面
    name = os.path.basename(name.replace("\\", "/")).strip()
    return name or "unnamed"


def unique_path(name):
    path = os.path.join(UPLOAD_DIR, name)
    i = 1
    while os.path.exists(path):
        path = os.path.join(UPLOAD_DIR, "%s.%d" % (name, i))
        i += 1
    return path


def save(name, data):
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = unique_path(safe_name(name))
    with open(path, "wb") as f:
        f.write(data)
    info = "saved uploads/%s (%d bytes)" % (os.path.basename(path), len(data))
    print(info)
    return info


def parse_multipart(body, boundary):
    # 为什么手写解析:Python 3.13 起 cgi 模块已被删除,这里只用标准库做最简实现。
    # 坑:按分隔符裸切,若文件内容本身含 boundary 会切错(概率极低);body 已在内存里,
    # 大文件会占内存,适合小文件接收。
    out = []
    for part in body.split(b"--" + boundary)[1:]:
        if part.startswith(b"--"):
            break
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]  # 分隔符前的那个 CRLF 属于协议,不属于内容
        m = re.search(rb'name="([^"]*)"', head)
        if not m:
            continue
        # 文件名有两种写法:filename="a.txt" 和 filename=a.txt
        fm = re.search(rb'filename="([^"]*)"', head) or re.search(rb'filename\*?=([^;\r\n]+)', head)
        field = m.group(1).decode("utf-8", "replace")
        if fm:
            out.append((field, fm.group(1).strip().decode("utf-8", "replace"), data))
        else:
            out.append((field, None, data))
    return out


class H(BaseHTTPRequestHandler):
    def _ok(self, msg="ok"):
        body = msg.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        # 坑:BaseHTTPRequestHandler 不解码 chunked,没有 Content-Length 只会读到空 body,
        # 客户端却仍收到 200;这里直接回错误提示,避免"看起来成功但什么都没存"
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return None
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _name_hint(self, query):
        # 是:文件名优先级 X-Filename 头 > ?filename= > URL 路径最后一段 > 按时间生成
        name = self.headers.get("X-Filename") or (query.get("filename") or [""])[0]
        if not name:
            name = os.path.basename(unquote(urlparse(self.path).path))
        return name or time.strftime("recv_%Y%m%d_%H%M%S.bin")

    def do_OPTIONS(self):
        self._ok()

    def do_GET(self):
        # 是:浏览器直接打开也能看到 "ok",方便确认能不能连上
        self._ok("ok")

    def do_POST(self):
        query = parse_qs(urlparse(self.path).query)
        raw = self._read_body()
        if raw is None:
            self._ok("error: chunked 传输不支持,请用 curl -F 或 -T")
            return
        ct = self.headers.get("Content-Type", "")
        results = []
        hint = ""

        if ct.startswith("multipart/form-data"):
            m = re.search(r'boundary="?([^";,]+)"?', ct)
            boundary = m.group(1).strip().encode("utf-8") if m else b""
            for field, filename, data in parse_multipart(raw, boundary):
                if filename is None:
                    # 坑:curl -F "file=./a.nix" 少了 @,curl 会把这串路径当普通文本字段发过来,
                    # 服务端只收到文字、没有文件,这里明确提示一下
                    print(field, "=", data.decode("utf-8", "replace"),
                          '(普通字段,不是文件;传文件要写 @: -F "%s=@文件路径")' % field)
                else:
                    results.append(save(filename, data))
            if not results:
                hint = ' (multipart 里没有文件:curl -F 传文件必须写 @,例如 -F "file=@a.txt")'
        elif ct.startswith("application/octet-stream") or self.headers.get("X-Filename") or "filename" in query:
            # 是:没有 multipart 包装的原始 body,靠文件名提示存盘
            results.append(save(self._name_hint(query), raw))
        elif ct.startswith("application/x-www-form-urlencoded"):
            print(parse_qs(raw.decode("utf-8")))
        else:
            print(raw.decode("utf-8", "replace"))

        self._ok((("ok " + "; ".join(results)) if results else "ok text %d bytes" % len(raw)) + hint)

    def do_PUT(self):
        query = parse_qs(urlparse(self.path).query)
        raw = self._read_body()
        if raw is None:
            self._ok("error: chunked 传输不支持,请用 curl -F 或 -T")
            return
        self._ok("ok " + save(self._name_hint(query), raw))


HTTPServer(("0.0.0.0", 8080), H).serve_forever()
