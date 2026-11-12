"""作品草稿里的图片、视频、配音：给界面预览用的本机小服务。

为什么需要它：界面是一个本地网页，视频、音频直接塞进网页（base64）太大太慢，
所以在本机开一个只有自己能访问的小服务，界面里的 <img> / <video> / <audio> 直接从它读文件。

安全：
- 只监听 127.0.0.1（别的电脑连不上），端口由系统随机分配；
- 地址里带一串这次启动随机生成的口令，不知道口令的程序读不到；
- 只给“草稿里登记过的文件”：按（草稿编号、文件名）去问 resolve，问不到就 404，不能拿它读电脑上别的文件。

支持断点读取（Range），视频可以拖动进度条。
"""
from __future__ import annotations

import logging
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

log = logging.getLogger(__name__)

CHUNK = 256 * 1024
TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".m4v": "video/mp4",
         ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}

_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None
_token = ""
_resolve = None          # resolve(did: int, name: str) -> Path | None


def content_type(path: Path) -> str:
    return TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Range 请求头 → (开始, 结束)（都包含）。不认识 / 超出范围返回 None。"""
    if not header or not header.startswith("bytes=") or "," in header:
        return None
    a, _, b = header[6:].strip().partition("-")
    try:
        if a == "":                      # bytes=-500：最后 500 个字节
            n = int(b)
            if n <= 0:
                return None
            return max(0, size - n), size - 1
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:      # 不往控制台刷访问记录
        return

    def _target(self) -> Path | None:
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 4 or parts[0] != _token or parts[1] != "draft" or not _token:
            return None
        try:
            did = int(parts[2])
        except ValueError:
            return None
        name = unquote(parts[3])
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return None
        try:
            f = _resolve(did, name) if _resolve else None
        except Exception:  # noqa: BLE001
            return None
        return f if f is not None and Path(f).is_file() else None

    def _send(self, body: bool) -> None:
        f = self._target()
        if f is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        f = Path(f)
        size = f.stat().st_size
        rng = self.headers.get("Range")
        span = parse_range(rng, size) if rng else None
        if rng and span is None:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = span if span else (0, size - 1)
        length = max(0, end - start + 1)
        self.send_response(206 if span else 200)
        self.send_header("Content-Type", content_type(f))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if span:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not body or length <= 0:
            return
        try:
            with open(f, "rb") as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    data = fh.read(min(CHUNK, left))
                    if not data:
                        break
                    self.wfile.write(data)
                    left -= len(data)
        except (BrokenPipeError, ConnectionError, OSError):
            pass                         # 界面换了一个视频 / 关了预览：读到一半断开是正常的

    def do_GET(self) -> None:  # noqa: N802
        self._send(True)

    def do_HEAD(self) -> None:  # noqa: N802
        self._send(False)


def serve(resolve) -> str:
    """启动（只启动一次）并登记“怎么找文件”。返回服务的地址前缀；启动不了返回空字符串。"""
    global _server, _token, _resolve
    with _lock:
        _resolve = resolve
        if _server is not None:
            return base()
        try:
            _server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        except OSError as e:
            log.warning("预览用的本机小服务没启动起来：%s（作品里的视频和配音暂时不能在软件里播放）", e)
            _server = None
            return ""
        _server.daemon_threads = True
        _token = secrets.token_urlsafe(18)
        threading.Thread(target=_server.serve_forever, name="media-server", daemon=True).start()
        return base()


def base() -> str:
    return f"http://127.0.0.1:{_server.server_address[1]}/{_token}" if _server is not None else ""


def url(did: int, name: str) -> str:
    """草稿里某个文件的预览地址；服务没启动返回空字符串。"""
    b = base()
    return f"{b}/draft/{int(did)}/{quote(name, safe='')}" if b and name else ""


def stop() -> None:
    global _server
    with _lock:
        if _server is not None:
            try:
                _server.shutdown()
                _server.server_close()
            except Exception:  # noqa: BLE001
                pass
            _server = None
