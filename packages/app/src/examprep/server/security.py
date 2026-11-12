"""本机服务的安全边界（R2）。

服务只给本机浏览器用，但浏览器里任何网页都能向 ``http://127.0.0.1:端口`` 发请求。两道检查：

1. Host：只接受回环地址（或启动时用 ``--host`` 指定的地址），防 DNS 重绑定——攻击者的域名解析到 127.0.0.1 后，
   请求的 Host 是攻击者的域名。
2. Origin：非 GET 请求带 Origin 时，Origin 必须也是回环地址（端口不限，兼容 Vite 开发服务器）。否则任意网站
   都能借浏览器让本机服务导入任意目录、花掉用户的模型额度。

鉴权占位：所有接口通过 ``current_user`` 拿当前用户，本机固定为 local / me；服务器模式（P6）换成真实鉴权。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from ..store import models as m

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True, slots=True)
class Principal:
    tenant: str = m.LOCAL_TENANT
    user: str = m.LOCAL_USER


LOCAL_USER = Principal()


def current_user() -> Principal:
    """FastAPI 依赖。本机模式只有一个用户。"""
    return LOCAL_USER


def hostname(netloc: str) -> str:
    """``127.0.0.1:8765`` → ``127.0.0.1``；``[::1]:8765`` → ``::1``。"""
    try:
        return (urlsplit(f"//{netloc}").hostname or "").lower()
    except ValueError:  # 端口不是数字等
        return ""


class LocalGuard:
    """纯 ASGI 中间件（不缓冲响应，SSE 流不受影响）。"""

    def __init__(self, app: ASGIApp, extra_hosts: Iterable[str] = ()) -> None:
        self.app = app
        self.hosts = LOOPBACK | {h.lower() for h in extra_hosts if h}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if hostname(headers.get("host", "")) not in self.hosts:
            await _forbid(send, "只接受本机访问（Host 不是本机地址）")
            return
        origin: str = headers.get("origin", "")
        method: str = scope.get("method", "GET")
        if origin and method not in SAFE_METHODS and hostname(urlsplit(origin).netloc) not in self.hosts:
            await _forbid(send, "拒绝来自其他网站的请求")
            return
        await self.app(scope, receive, send)


async def _forbid(send: Send, detail: str) -> None:
    body = json.dumps({"detail": detail}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
