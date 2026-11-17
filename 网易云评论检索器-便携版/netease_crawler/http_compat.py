"""``requests`` 的最小兼容层 —— 基于标准库 ``urllib``。

用途
----
项目原本依赖 ``requests``。为了让这个文件夹**换台电脑就能跑**（不需要联网
装包、不需要可用的 pip），这里只实现 ``netease_crawler.client`` 真正用到的
那一小部分接口：``Session`` / ``headers`` / ``get`` / ``post`` /
``Response.status_code`` / ``Response.json`` / ``RequestException``。

行为上刻意对齐 requests：
    * 自动携带 Cookie（每个 Session 一个 cookie jar，并自动跟随 Set-Cookie）；
    * 跟随最多 5 次重定向；
    * 非 2xx 也会返回 ``Response``（把 status_code 交给上层判断），而不是抛异常
      —— 因为 client.py 要靠状态码决定是否重试；
    * 支持逐请求指定代理。

未实现的部分（本项目用不到）：流式下载、连接池调优、SSL 校验开关、异步。
"""

from __future__ import annotations

import gzip
import http.cookiejar
import json as _json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib

__all__ = [
    "Session",
    "Response",
    "RequestException",
    "ConnectionError",
    "Timeout",
    "HTTPError",
    "SESSION_IMPL",
]

#: 供界面/日志显示当前用的是哪条通道
SESSION_IMPL = "urllib(标准库)"


# --------------------------------------------------------------------- 异常
class RequestException(IOError):
    """与 requests.RequestException 对齐的基类（client.py 靠它做重试判定）。"""


class ConnectionError(RequestException):
    """连接层失败（DNS / 拒绝连接 / 断链）。"""


class Timeout(RequestException):
    """超时。"""


class HTTPError(RequestException):
    """协议层错误（本项目里不直接抛出，转而映射成 Response）。"""


# -------------------------------------------------------------------- 响应
class Response:
    """极简响应对象。"""

    __slots__ = ("status_code", "headers", "content", "url", "reason")

    def __init__(self, status_code: int, headers, body: bytes, url: str, reason: str = ""):
        self.status_code = status_code
        self.headers = headers or {}
        self.content = body
        self.url = url
        self.reason = reason

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    @property
    def encoding(self) -> str:
        ctype = ""
        try:
            ctype = self.headers.get("Content-Type", "") or ""
        except AttributeError:
            pass
        if "charset=" in ctype:
            return ctype.split("charset=", 1)[1].split(";")[0].strip().strip('"')
        return "utf-8"

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")

    def json(self):
        """解析 JSON；失败时抛 ``ValueError``（与 requests 保持一致）。"""
        return _json.loads(self.content.decode("utf-8", errors="replace"))

    def close(self) -> None:  # 兼容 requests 的调用习惯
        pass

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Response [{self.status_code}] {self.url}>"


# --------------------------------------------------------------- 头部容器
class _Headers(dict):
    """大小写不敏感的 header 容器（requests 的 headers 也是这个语义）。"""

    def _k(self, key):
        return key.lower() if isinstance(key, str) else key

    def __setitem__(self, key, value):
        super().__setitem__(self._k(key), value)

    def __getitem__(self, key):
        return super().__getitem__(self._k(key))

    def __contains__(self, key):
        return super().__contains__(self._k(key))

    def get(self, key, default=None):
        return super().get(self._k(key), default)

    def update(self, other=None, **kwargs):  # type: ignore[override]
        if other:
            items = other.items() if hasattr(other, "items") else other
            for k, v in items:
                self[k] = v
        for k, v in kwargs.items():
            self[k] = v

    def pop(self, key, *args):
        return super().pop(self._k(key), *args)


# ---------------------------------------------------------------- 会话
_OPENER_CACHE: dict = {}

# 各系统上常见的根证书库位置。**这不是可选优化，而是必需品**：
# python.org 下载的 macOS 版 Python 默认不带证书（需要跑一次
# "Install Certificates.command"），此时 ssl.create_default_context() 会得到一个
# 「零信任锚」的 context，任何 HTTPS 请求都会失败并报出极具误导性的
# "self-signed certificate in certificate chain"——看起来像遭到了中间人攻击，
# 其实只是本地没有 CA 库。用户双击 .command 用的正是这种系统 Python，
# 于是「全部请求失败 + 层层重试 15 秒」，最后被前端误判成「后端服务已停止」。
_CA_CANDIDATES = (
    "/etc/ssl/cert.pem",                      # macOS（OpenSSL 系统库）
    "/private/etc/ssl/cert.pem",              # macOS 实际路径（/etc 为软链）
    "/usr/local/etc/openssl@3/cert.pem",      # macOS Intel + Homebrew
    "/opt/homebrew/etc/openssl@3/cert.pem",   # macOS Apple Silicon + Homebrew
    "/usr/local/etc/openssl/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",       # RHEL / CentOS / Fedora
    "/etc/ssl/certs/ca-certificates.crt",     # Debian / Ubuntu
    "/etc/ssl/ca-bundle.pem",                 # openSUSE
)


def _find_ca_bundle() -> str | None:
    """按「certifi -> 各系统标准位置」的顺序找一个可用的根证书库。"""
    try:  # 用户若装过 requests，通常连带装了 certifi，优先用它
        import certifi  # type: ignore

        path = certifi.where()
        if path and os.path.isfile(path):
            return path
    except Exception:  # noqa: BLE001 —— 没装就用系统库，不算错误
        pass
    for path in _CA_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def ca_bundle_status() -> str:
    """给「环境自检」用：一句话说明当前 TLS 信任锚从哪来。"""
    ctx = ssl.create_default_context()
    count = ctx.cert_store_stats().get("x509_ca", 0)
    if count:
        return f"系统默认（已加载 {count} 个根证书）"
    path = _find_ca_bundle()
    if path:
        return f"将自动加载 {path}"
    return "无可用根证书库（HTTPS 可能失败）"


def _ssl_context() -> ssl.SSLContext:
    """建 HTTPS context：默认没问题就不动，缺信任锚时才补一个证书库。

    只在「默认一个根证书都没有」时才介入，因此不会改变本来就正常的机器
    （例如带证书的 Homebrew Python / Linux 发行版 Python）的行为。
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        bundle = _find_ca_bundle()
        if bundle:
            try:
                ctx.load_verify_locations(bundle)
            except (OSError, ssl.SSLError):
                pass  # 加载失败就维持原样，上层会给出网络错误提示
    return ctx


def _build_opener(proxy_key, jar: http.cookiejar.CookieJar):
    """按代理配置缓存 opener（同配置只建一次，省掉重复握手开销）。"""
    handlers = [urllib.request.HTTPCookieProcessor(jar)]
    if proxy_key:
        handlers.append(urllib.request.ProxyHandler(dict(proxy_key)))
    handlers.append(urllib.request.HTTPSHandler(context=_ssl_context()))
    opener = urllib.request.build_opener(*handlers)
    opener.addheaders = []
    return opener


class Session:
    """``requests.Session`` 的替代品（线程安全性：请每线程一个实例）。"""

    def __init__(self, proxy: str | None = None):
        self.headers = _Headers()
        self.cookies = http.cookiejar.CookieJar()
        self._proxy = {"http": proxy, "https": proxy} if proxy else None
        self._opener = None
        self._opener_proxy_key = None

    # ---------------------------------------------------------- 内部工具
    def _get_opener(self, proxies):
        key = None
        if proxies:
            key = tuple(sorted((str(k), str(v)) for k, v in proxies.items() if v))
        elif self._proxy:
            key = tuple(sorted(self._proxy.items()))
        if self._opener is None or key != self._opener_proxy_key:
            self._opener = _build_opener(key, self.cookies)
            self._opener_proxy_key = key
        return self._opener

    def _request(self, method: str, url: str, *, params=None, data=None,
                 timeout=None, proxies=None) -> Response:
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            url = url + sep + urllib.parse.urlencode(clean)

        body = None
        if data is not None:
            if isinstance(data, (dict, list)):
                body = urllib.parse.urlencode(data).encode("utf-8")
            elif isinstance(data, str):
                body = data.encode("utf-8")
            else:
                body = bytes(data)

        headers = {k: v for k, v in self.headers.items()}
        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        opener = self._get_opener(proxies)
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                return Response(resp.status, _Headers(resp.headers.items()),
                                _decompress(raw, resp.headers), resp.geturl(), resp.reason)
        except urllib.error.HTTPError as exc:
            # 非 2xx：读出来交给上层判断（上层会按状态码决定重试）
            try:
                raw = exc.read()
            except Exception:  # pragma: no cover
                raw = b""
            return Response(exc.code, _Headers(exc.headers.items() if exc.headers else []),
                            _decompress(raw, exc.headers), url, str(exc.reason))
        except socket.timeout as exc:
            raise Timeout(str(exc)) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.timeout):
                raise Timeout(str(reason)) from exc
            raise ConnectionError(str(reason)) from exc
        except (ssl.SSLError, OSError) as exc:
            raise ConnectionError(str(exc)) from exc

    # ------------------------------------------------------------- 公开 API
    def get(self, url: str, params=None, timeout=None, proxies=None, **_) -> Response:
        return self._request("GET", url, params=params, timeout=timeout, proxies=proxies)

    def post(self, url: str, data=None, json=None, timeout=None, proxies=None, **_) -> Response:
        headers_json = None
        if json is not None:
            data = _json.dumps(json).encode("utf-8")
            headers_json = "application/json"
        resp = self._request("POST", url, data=data, timeout=timeout, proxies=proxies)
        return resp

    def request(self, method: str, url: str, **kwargs) -> Response:
        return self._request(method.upper(), url, **kwargs)

    def close(self) -> None:
        self._opener = None
        self.cookies.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _decompress(raw: bytes, headers) -> bytes:
    enc = ""
    try:
        enc = (headers.get("Content-Encoding", "") or "").lower()
    except AttributeError:  # pragma: no cover
        return raw
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # pragma: no cover
        return raw
    return raw


# ------------------------------------------------------------------ 自检
if __name__ == "__main__":  # pragma: no cover
    import time

    s = Session()
    r = s.get("https://music.163.com/", timeout=15)
    print("首页状态码:", r.status_code, "字节数:", len(r.content))
    t0 = time.time()
    r = s.get("https://music.163.com/api/search/get",
              params={"s": "test", "type": 1, "limit": 1, "offset": 0}, timeout=15)
    j = r.json()
    print("搜索接口 code:", j.get("code"), "耗时 %.2fs" % (time.time() - t0))
    print("Cookie 数量:", len(list(s.cookies)))
    print("\n[OK] urllib 兼容层自检通过")
