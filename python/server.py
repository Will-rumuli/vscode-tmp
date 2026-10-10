"""Quectel M2 SDK VS Code 插件后端入口。

用法:
    python3 server.py [--port 8765] [--key <token>]

启动后监听 127.0.0.1:<port>, 所有 /api/* 请求需带 X-API-Key 头。
VS Code extension 激活时自动拉起本进程, 通过 stdout 首行获取
"READY port=<port> key=<key>" 建立连接。
"""
from __future__ import annotations

import argparse
import secrets
import sys
import uvicorn

from quecpi import api
from quecpi.api import router


def main() -> None:
    parser = argparse.ArgumentParser(description="QPi SDK backend")
    parser.add_argument("--port", type=int, default=0, help="监听端口, 0=随机")
    parser.add_argument("--key", default="", help="API key, 空=自动生成")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    api_key = args.key or secrets.token_hex(16)
    api.set_state({}, api_key)

    app = uvicorn_singleton(router, api_key)
    port = args.port

    if port == 0:
        # 随机端口: 先绑定一个空闲端口
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((args.host, 0))
        port = sock.getsockname()[1]
        sock.close()

    # 用 Server + 线程: 等 uvicorn 真正开始监听后再打印 READY,
    # 否则 extension 拿到 READY 立即请求会 Connection refused (竞态)
    import threading
    import time
    from uvicorn import Config, Server

    server = Server(Config(app, host=args.host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # started 在不同 uvicorn 版本是 bool / Event, 统一轮询
    deadline = time.time() + 15
    while time.time() < deadline:
        started = getattr(server, "started", False)
        if callable(started):
            started = started.is_set()
        if started:
            break
        time.sleep(0.05)
    if not started:
        print("FAILED server did not start", flush=True)
        sys.exit(1)

    # 通知 extension 就绪 (首行协议)
    print(f"READY port={port} key={api_key}", flush=True)
    thread.join()


def uvicorn_singleton(router, api_key):
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI(title="qpi-m2-backend", version="1.0.0")

    # 后端文案语言: 每个请求按 X-QPi-Lang 头切换 (扩展宿主与 webview 同一语言)
    from quecpi import i18n

    @app.middleware("http")
    async def _lang_middleware(request, call_next):
        i18n.set_lang(request.headers.get("x-qpi-lang"))
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # webview origin 不固定, 用 API key 鉴权
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app


if __name__ == "__main__":
    main()
