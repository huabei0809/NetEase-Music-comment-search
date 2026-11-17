"""HTTP 通道层：会话管理、自动重试、全局限速。

设计要点
--------
1. **线程安全**：``requests.Session`` 不是线程安全的，这里为每个线程惰性创建
   独立 Session（``threading.local``），避免并发下 cookie/socket 互相踩踏。
2. **自动重试**：网易云在压力下会返回 ``code=-460``（触发风控）、HTTP 429/5xx，
   或直接返回非 JSON 页面。这些都要识别出来并做指数退避。
3. **全局限速**：所有线程共享一个令牌间隔阀，保证整体请求速率可控，
   而不是"每个线程各自限速"导致总量放大。

HTTP 通道是可插拔的
-------------------
优先用已安装的 ``requests``；没装则自动回退到本包内置的 ``http_compat``
（基于标准库 urllib，接口对齐 requests）。因此**目标机器不需要 pip 装任何东西**。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Mapping

try:  # 可选：装了 requests 就用它
    import requests

    HTTP_BACKEND = "requests"
except ImportError:  # pragma: no cover - 取决于运行环境
    from . import http_compat as requests  # type: ignore[no-redef]

    HTTP_BACKEND = "urllib(标准库)"

from .crypto import encrypted_request

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_URL = "https://music.163.com"

#: 需要重试的业务返回码
RETRYABLE_CODES = {-460, -462, 400, 405, 429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    """接口返回了非 200 的业务码，或响应结构不符合预期。"""

    def __init__(self, code: int, message: str, path: str = ""):
        super().__init__(f"[{path}] code={code} message={message!r}")
        self.code = code
        self.message = message
        self.path = path


class RateLimiter:
    """跨线程共享的最小请求间隔阀（简单令牌桶的退化形式）。"""

    def __init__(self, min_interval: float = 0.35):
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self.min_interval


class WeapiClient:
    """网易云 weapi 通道客户端。

    :param cookie:        可选的登录 Cookie 串（例如 ``MUSIC_U=xxx; __csrf=yyy``）。
                          匿名也能跑，但带上登录态能显著降低被风控的概率。
    :param timeout:       单次请求超时（秒）
    :param retries:       失败后的最大重试次数
    :param backoff:       退避基数，第 n 次重试等待 ``backoff ** n`` 秒
    :param min_interval:  全局最小请求间隔（秒），用于限速
    :param proxy:         形如 ``http://127.0.0.1:7890`` 的代理
    """

    def __init__(
        self,
        cookie: str | None = None,
        timeout: float = 20.0,
        retries: int = 4,
        backoff: float = 1.6,
        min_interval: float = 0.35,
        proxy: str | None = None,
        user_agent: str = DEFAULT_UA,
    ):
        self.cookie = cookie
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.user_agent = user_agent
        self.limiter = RateLimiter(min_interval)
        self._local = threading.local()
        self._proxy = {"http": proxy, "https": proxy} if proxy else None
        self._bootstrapped = threading.local()
        self.request_count = 0
        self._count_lock = threading.Lock()

    # ------------------------------------------------------------------ 会话
    def _session(self) -> requests.Session:
        """取当前线程的 Session，首次调用时自动完成初始化。"""
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            sess.headers.update(
                {
                    "User-Agent": self.user_agent,
                    "Referer": f"{BASE_URL}/",
                    "Origin": BASE_URL,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                }
            )
            if self.cookie:
                sess.headers["Cookie"] = self.cookie
            self._local.session = sess

        # 先访问一次首页拿基础 cookie（NMTID 等），有些接口缺了会返回 -460
        if not getattr(self._bootstrapped, "done", False):
            try:
                sess.get(BASE_URL + "/", timeout=self.timeout)
            except requests.RequestException as exc:  # pragma: no cover
                logger.warning("首页预热失败（不影响后续请求）：%s", exc)
            self._bootstrapped.done = True
        return sess

    def _bump(self) -> None:
        with self._count_lock:
            self.request_count += 1

    # -------------------------------------------------------------- 核心请求
    def weapi(self, path: str, payload: Mapping[str, Any]) -> dict:
        """向 ``/weapi/...`` 发一次加密 POST，返回解析后的 JSON dict。

        :param path:    形如 ``"/weapi/v3/song/detail"``
        :param payload: 业务参数字典（会自动加密）
        """
        url = f"{BASE_URL}{path}"
        if "?" in url:
            url = url.split("?")[0] + "?csrf_token="
        else:
            url = url + "?csrf_token="

        body = dict(payload)
        body.setdefault("csrf_token", "")
        data = encrypted_request(body)

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.limiter.acquire()
            try:
                resp = self._session().post(url, data=data, timeout=self.timeout,
                                            proxies=self._proxy)
            except requests.RequestException as exc:
                last_error = exc
                self._sleep_backoff(attempt, f"网络异常 {exc.__class__.__name__}")
                continue

            self._bump()

            if resp.status_code in (429, 503):
                self._sleep_backoff(attempt, f"HTTP {resp.status_code} 被限流")
                last_error = ApiError(resp.status_code, "rate limited", path)
                continue
            if resp.status_code != 200:
                last_error = ApiError(resp.status_code, f"HTTP {resp.status_code}", path)
                self._sleep_backoff(attempt, f"HTTP {resp.status_code}")
                continue

            try:
                result = resp.json()
            except ValueError:
                # 风控时可能返回 HTML 验证页
                last_error = ApiError(-1, "响应不是合法 JSON（可能触发风控）", path)
                self._sleep_backoff(attempt, "响应非 JSON")
                continue

            code = int(result.get("code", -1))
            if code in RETRYABLE_CODES:
                last_error = ApiError(code, str(result.get("message", "")), path)
                self._sleep_backoff(attempt, f"业务码 {code}")
                continue
            return result

        raise ApiError(-1, f"重试 {self.retries} 次后仍失败：{last_error}", path)

    def _sleep_backoff(self, attempt: int, reason: str) -> None:
        if attempt >= self.retries:
            return
        delay = (self.backoff ** (attempt + 1)) + random.uniform(0, 0.4)
        logger.warning("第 %d 次重试（%s），等待 %.1fs", attempt + 1, reason, delay)
        time.sleep(delay)

    def plain_get(self, path: str, params: Mapping[str, Any] | None = None) -> dict:
        """走明文 ``/api/...`` 通道（参数**不加密**，用 GET 查询串传递）。

        实测：``/weapi/cloudsearch/get/web`` 在匿名状态下常返回风控码
        ``50000005``，而明文 ``/api/search/get`` 可以正常搜索歌曲。
        搜索类接口优先走这里。
        """
        url = f"{BASE_URL}{path}"

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self.limiter.acquire()
            try:
                resp = self._session().get(url, params=dict(params or {}),
                                           timeout=self.timeout, proxies=self._proxy)
            except requests.RequestException as exc:
                last_error = exc
                self._sleep_backoff(attempt, f"网络异常 {exc.__class__.__name__}")
                continue

            self._bump()

            if resp.status_code != 200:
                last_error = ApiError(resp.status_code, f"HTTP {resp.status_code}", path)
                self._sleep_backoff(attempt, f"HTTP {resp.status_code}")
                continue

            try:
                result = resp.json()
            except ValueError:
                last_error = ApiError(-1, "响应不是合法 JSON", path)
                self._sleep_backoff(attempt, "响应非 JSON")
                continue

            code = int(result.get("code", -1))
            if code in RETRYABLE_CODES:
                last_error = ApiError(code, str(result.get("message", "")), path)
                self._sleep_backoff(attempt, f"业务码 {code}")
                continue
            return result

        raise ApiError(-1, f"重试 {self.retries} 次后仍失败：{last_error}", path)

    # ------------------------------------------------------------- 便捷方法
    def playlist_detail(self, playlist_id: int, n: int = 1000) -> dict:
        """歌单详情。

        注意：实测 ``trackIds`` 会返回**完整**曲目 ID 列表（9816/9816 全给），
        而 ``tracks`` 明细会被截断成约 10 条，所以曲目信息要用
        :meth:`NeteaseAPI.songs_detail` 另行批量获取。
        """
        return self.weapi(
            "/weapi/v6/playlist/detail",
            {"id": str(playlist_id), "n": n, "s": 8},
        )

    def close(self) -> None:
        sess = getattr(self._local, "session", None)
        if sess is not None:
            sess.close()
            self._local.session = None
