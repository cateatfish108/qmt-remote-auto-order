#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试: 模拟 QMT 客户端(WS) + 聚宽策略端(HTTP POST), 验证中转服务端。

模拟客户端复刻 api/remote.py 的关键行为:
  - 用 salt 签发 JWT {"u": uid} 作为 WS 鉴权
  - 收到信封后 content = json.loads(data['content']) (验证二次编码)
  - 回 ack {"type":"ack","id":...}

运行: python server/test_e2e.py
"""

import asyncio
import json
import sys

import aiohttp
import jwt

from app import build_app
from aiohttp import web

SALT = "test-salt-123"
UID = "machine-uid-abc"
PORT = 18080
BASE = f"http://127.0.0.1:{PORT}"


def make_token(payload):
    return jwt.encode(payload, SALT, algorithm="HS256")


async def run_qmt_client(received: list, ready: asyncio.Event, stop: asyncio.Event):
    """模拟 QMT 客户端: 连接 WS, 复刻 remote.py 的收信+ack 逻辑。"""
    token = make_token({"u": UID})
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(
        f"ws://127.0.0.1:{PORT}/ws",
        headers={"Authorization": f"Bearer {token}"},
    )
    ready.set()
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                # 复刻 remote.py: content 必须能再被 json.loads 一次
                content = json.loads(data["content"])
                received.append(content)
                # 回 ack
                await ws.send_str(json.dumps({"type": "ack", "id": data.get("id", "")}))
                if content.get("state") == "run":
                    stop.set()
    finally:
        await ws.close()
        await session.close()


async def main():
    cfg = {"salt": SALT, "ack_timeout": 2.0, "max_retries": 2,
           "host": "127.0.0.1", "port": PORT}
    app = build_app(cfg)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", PORT)
    await site.start()

    received: list = []
    ready = asyncio.Event()
    stop = asyncio.Event()
    client_task = asyncio.create_task(run_qmt_client(received, ready, stop))
    await asyncio.wait_for(ready.wait(), timeout=5)
    await asyncio.sleep(0.3)  # 等 login 消息到达

    failures = []
    async with aiohttp.ClientSession() as s:
        strat_token = make_token({"u": UID, "p": "local"})
        headers = {"Authorization": f"Bearer {strat_token}",
                   "Content-Type": "application/json"}

        # 1) begin 信号
        begin = {"run_params": "sim_trade", "strategy_code": "S001",
                 "state": "begin", "params": {"total_value": 100000}}
        async with s.post(f"{BASE}/send_message", headers=headers,
                          data=json.dumps(begin)) as r:
            body = await r.json()
            if not (r.status == 200 and body["code"] == 200):
                failures.append(f"begin 转发失败: {r.status} {body}")

        # 2) run 信号 (一笔买入)
        run = {"method": "order_target", "run_params": "sim_trade",
               "state": "run", "strategy_code": "S001", "positions": [],
               "params": {"security": "000001.XSHE", "price": 12.3,
                          "amount": 100, "is_buy": True,
                          "add_time": "2026-06-27 09:30:00",
                          "total_value": 100000}}
        async with s.post(f"{BASE}/send_message", headers=headers,
                          data=json.dumps(run)) as r:
            body = await r.json()
            if not (r.status == 200 and body["code"] == 200):
                failures.append(f"run 转发失败: {r.status} {body}")

        # 3) 错误 salt 应被拒绝
        bad = jwt.encode({"u": UID}, "wrong-salt", algorithm="HS256")
        async with s.post(f"{BASE}/send_message",
                          headers={"Authorization": f"Bearer {bad}",
                                   "Content-Type": "application/json"},
                          data=json.dumps(run)) as r:
            if r.status != 401:
                failures.append(f"错误 salt 未被拒绝: status={r.status}")

        # 4) 离线 uid 应返回 client offline
        off_token = make_token({"u": "no-such-uid"})
        async with s.post(f"{BASE}/send_message",
                          headers={"Authorization": f"Bearer {off_token}",
                                   "Content-Type": "application/json"},
                          data=json.dumps(run)) as r:
            body = await r.json()
            if body.get("data") != "client offline":
                failures.append(f"离线路由处理异常: {body}")

    # 等客户端收到 run
    try:
        await asyncio.wait_for(stop.wait(), timeout=5)
    except asyncio.TimeoutError:
        failures.append("客户端未收到 run 信号")

    client_task.cancel()
    await runner.cleanup()

    # 校验收到的内容 (login + begin + run)
    states = [c.get("state") for c in received if "state" in c]
    types = [c.get("type") for c in received if "type" in c]
    if "login" not in types:
        failures.append(f"未收到 login 消息: {received}")
    if "begin" not in states or "run" not in states:
        failures.append(f"未正确收到 begin/run 信号: {states}")
    run_sig = next((c for c in received if c.get("state") == "run"), None)
    if not run_sig or run_sig["params"]["security"] != "000001.XSHE":
        failures.append(f"run 信号内容错误: {run_sig}")

    if failures:
        print("❌ 测试失败:")
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("✅ 全部通过")
    print(f"   客户端收到 {len(received)} 条消息, types={types}, states={states}")


if __name__ == "__main__":
    asyncio.run(main())
