#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
qmt-remote-auto-order 开源信号中转服务端 (Python 实现)

用于替代官方未开源的 Go 二进制服务端 (qmt-auto-order-server-*)。
职责非常单一：在「聚宽回测/模拟策略端」与「本机 QMT 客户端」之间做信号中转。

协议 (与客户端代码完全对齐, salt 鉴权模式 run_model_type=1):

1. QMT 客户端 (api/remote.py) 通过 WebSocket 连接 `GET /ws`
   - 请求头携带 `Authorization: Bearer <JWT>`
   - JWT 用客户端"加密盐"(salt) 以 HS256 签名, payload 形如 {"u": <unique_id>}
   - 服务端解出 u (机器唯一 id), 作为该连接的路由 key

2. 聚宽策略端 (转换后的代码, 见 api/tools/template.py) 通过 HTTP `POST /send_message`
   - 请求头携带 `Authorization: Bearer <JWT>` (同一个 salt, payload {"u": <unique_id>, "p": "local"})
   - body 为信号 JSON (state=begin/run/end/dividends ...)
   - 服务端解出 u, 转发给同一个 u 下已连接的 WS 客户端

3. 服务端 -> 客户端 的 WS 信封格式 (api/remote.py handle_messages 要求):
       {"id": "<消息id>", "content": "<内层JSON的字符串>"}
   注意 content 是"二次编码"的字符串, 客户端会再做一次 json.loads。

4. 客户端收到后回 ack: {"type": "ack", "id": "<消息id>", "timestamp": <毫秒>}
   服务端据此实现"重发机制": 未在超时内收到 ack 则重发, 直到达到最大次数。

依赖: aiohttp, PyJWT, PyYAML
运行: python server/app.py -c server/config.yaml
"""

import argparse
import asyncio
import json
import logging
import os
import uuid
from typing import Dict, Optional

import jwt
import yaml
from aiohttp import WSMsgType, web

ALGORITHM = "HS256"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("qmt-server")


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def parse_bearer(authorization: Optional[str]) -> Optional[str]:
    """从 `Authorization: Bearer xxx` 头中取出 token。"""
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    # 兼容直接传 token 的写法
    return authorization.strip() or None


def decode_token(token: str, salt: str) -> Optional[dict]:
    """用 salt 校验 JWT, 成功返回 payload, 失败返回 None。"""
    try:
        return jwt.decode(token, salt, algorithms=[ALGORITHM])
    except jwt.InvalidTokenError as e:
        log.warning("token 校验失败: %s", e)
        return None


def extract_uid(payload: dict) -> Optional[str]:
    """从 payload 取路由 key。客户端固定用字段 'u' 存放 unique_id。"""
    if not payload:
        return None
    return payload.get("u")


# --------------------------------------------------------------------------- #
# 客户端连接 (单个 QMT 客户端的 WS 会话)
# --------------------------------------------------------------------------- #
class ClientConn:
    def __init__(self, ws: web.WebSocketResponse, uid: str):
        self.ws = ws
        self.uid = uid
        # 等待 ack 的消息: msg_id -> Future
        self.pending: Dict[str, asyncio.Future] = {}

    def resolve_ack(self, msg_id: str) -> None:
        fut = self.pending.get(msg_id)
        if fut and not fut.done():
            fut.set_result(True)

    async def _send_envelope(self, content: dict, msg_id: Optional[str] = None) -> str:
        """按客户端要求的信封发送 (content 二次编码为字符串)。"""
        msg_id = msg_id or uuid.uuid4().hex
        envelope = {
            "id": msg_id,
            # 关键: content 必须是字符串, 客户端会再 json.loads 一次
            "content": json.dumps(content, ensure_ascii=False),
        }
        await self.ws.send_str(json.dumps(envelope, ensure_ascii=False))
        return msg_id

    async def send_login(self) -> None:
        """连接建立后通知客户端"登录成功", 前端会显示已连接。"""
        await self._send_envelope({"type": "login", "message": "登录成功"})

    async def send_logout(self) -> None:
        """同一 uid 在别处登录时, 踢掉旧连接。"""
        await self._send_envelope(
            {"type": "logout", "message": "其他地方已登录断开连接请重新登录"}
        )

    async def deliver(self, payload: dict, ack_timeout: float, max_retries: int) -> bool:
        """
        向客户端投递一条信号, 带 ack 确认的重发机制。
        成功收到 ack 返回 True, 重试耗尽仍未确认返回 False。
        """
        msg_id = uuid.uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[msg_id] = fut
        try:
            for attempt in range(max_retries + 1):
                await self._send_envelope(payload, msg_id=msg_id)
                try:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=ack_timeout)
                    return True
                except asyncio.TimeoutError:
                    if attempt < max_retries:
                        log.warning(
                            "uid=%s 消息 %s 未收到 ack, 第 %d 次重发",
                            self.uid, msg_id, attempt + 1,
                        )
            return False
        finally:
            self.pending.pop(msg_id, None)


# --------------------------------------------------------------------------- #
# 连接注册中心
# --------------------------------------------------------------------------- #
class Hub:
    """uid -> 当前活跃的 QMT 客户端连接 (单会话, 新连接踢旧连接)。"""

    def __init__(self):
        self._clients: Dict[str, ClientConn] = {}
        self._lock = asyncio.Lock()

    async def register(self, conn: ClientConn) -> None:
        async with self._lock:
            old = self._clients.get(conn.uid)
            self._clients[conn.uid] = conn
        if old is not None:
            try:
                await old.send_logout()
                await old.ws.close()
            except Exception:
                pass
            log.info("uid=%s 旧连接被新连接替换", conn.uid)

    async def unregister(self, conn: ClientConn) -> None:
        async with self._lock:
            if self._clients.get(conn.uid) is conn:
                del self._clients[conn.uid]

    def get(self, uid: str) -> Optional[ClientConn]:
        return self._clients.get(uid)

    def count(self) -> int:
        return len(self._clients)


# --------------------------------------------------------------------------- #
# HTTP / WS 处理
# --------------------------------------------------------------------------- #
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    salt = request.app["salt"]
    hub: Hub = request.app["hub"]

    token = parse_bearer(request.headers.get("Authorization"))
    if not token:
        return web.json_response({"error": "missing token"}, status=401)

    payload = decode_token(token, salt)
    uid = extract_uid(payload)
    if not uid:
        return web.json_response({"error": "invalid token"}, status=401)

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    conn = ClientConn(ws, uid)
    await hub.register(conn)
    log.info("QMT 客户端已连接 uid=%s (当前在线 %d)", uid, hub.count())

    try:
        await conn.send_login()
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "ack":
                    conn.resolve_ack(data.get("id", ""))
            elif msg.type == WSMsgType.ERROR:
                log.warning("ws 连接异常 uid=%s: %s", uid, ws.exception())
    finally:
        await hub.unregister(conn)
        log.info("QMT 客户端已断开 uid=%s (当前在线 %d)", uid, hub.count())

    return ws


async def send_message_handler(request: web.Request) -> web.Response:
    salt = request.app["salt"]
    hub: Hub = request.app["hub"]
    ack_timeout = request.app["ack_timeout"]
    max_retries = request.app["max_retries"]

    token = parse_bearer(request.headers.get("Authorization"))
    if not token:
        return web.json_response({"code": 401, "data": "missing token"}, status=401)

    payload = decode_token(token, salt)
    uid = extract_uid(payload)
    if not uid:
        return web.json_response({"code": 401, "data": "invalid token"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"code": 400, "data": "invalid json body"}, status=400)

    conn = hub.get(uid)
    if conn is None:
        log.warning("uid=%s 暂无在线客户端, 信号丢弃: %s", uid, body.get("state"))
        return web.json_response(
            {"code": 404, "data": "client offline"}, status=200
        )

    ok = await conn.deliver(body, ack_timeout=ack_timeout, max_retries=max_retries)
    log.info(
        "转发信号 uid=%s state=%s strategy=%s -> %s",
        uid, body.get("state"), body.get("strategy_code"),
        "ok" if ok else "no-ack",
    )
    if not ok:
        return web.json_response(
            {"code": 504, "data": "client no ack"}, status=200
        )
    return web.json_response({"code": 200, "data": "ok"})


async def health_handler(request: web.Request) -> web.Response:
    hub: Hub = request.app["hub"]
    return web.json_response({"code": 200, "online_clients": hub.count()})


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def load_config(path: str) -> dict:
    cfg: dict = {}
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    server = cfg.get("server", {})
    security = cfg.get("security", {})
    forward = cfg.get("forward", {})

    return {
        "host": os.getenv("HOST", server.get("host", "0.0.0.0")),
        "port": int(os.getenv("PORT", server.get("port", 8080))),
        "salt": os.getenv("TOKEN_SALT", security.get("token_salt", "")),
        "ack_timeout": float(forward.get("ack_timeout", 5)),
        "max_retries": int(forward.get("max_retries", 3)),
    }


def build_app(cfg: dict) -> web.Application:
    app = web.Application()
    app["hub"] = Hub()
    app["salt"] = cfg["salt"]
    app["ack_timeout"] = cfg["ack_timeout"]
    app["max_retries"] = cfg["max_retries"]

    app.router.add_get("/ws", ws_handler)
    app.router.add_post("/send_message", send_message_handler)
    app.router.add_get("/health", health_handler)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="qmt 开源信号中转服务端")
    parser.add_argument(
        "-c", "--config", default="config.yaml", help="配置文件路径 (yaml)"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not cfg["salt"]:
        raise SystemExit(
            "未配置 token_salt! 请在 config.yaml 的 security.token_salt 填写, "
            "且必须与客户端的'加密盐'一致。"
        )

    log.info(
        "启动服务端 %s:%d (ack_timeout=%ss, max_retries=%d)",
        cfg["host"], cfg["port"], cfg["ack_timeout"], cfg["max_retries"],
    )
    web.run_app(build_app(cfg), host=cfg["host"], port=cfg["port"])


if __name__ == "__main__":
    main()
