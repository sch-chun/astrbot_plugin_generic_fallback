"""代理服务生命周期：主循环运行、优雅关闭、信号所有权。

回归背景：代理服务曾在独立线程的事件循环上运行（uvicorn.Server.run + threading.Thread）。
Provider 的 HTTP 连接池（httpx/httpcore keep-alive）会把连接绑定到该线程的循环；
插件重载时循环关闭、池里留下僵尸连接，重载后首批请求集中报
RuntimeError: Event loop is closed——被 _call_provider 的 except Exception 吞掉，
还把 Provider 误标记进冷却（「集中爆发一遍然后正常」即此现象）。

修复：serve() 改为 AstrBot 主事件循环上的任务，连接池随主循环存活，重载不再产生
绑定到已关闭循环的连接；terminate() 同步改为优雅等待，端口保证释放。
"""

import asyncio
import signal
import socket
import time

import httpx
import uvicorn
from astrbot_plugin_generic_fallback.main import (
    GenericFallbackProxyPlugin,
    _MainLoopProxyServer,
)
from fastapi import FastAPI


class FakeContext:
    """仅实现插件初始化所需的 Context 能力"""

    def __init__(self):
        self.registered_web_apis = []
        self.provider_manager = None

    def register_web_api(self, route, view_handler, methods, desc):
        self.registered_web_apis.append((route, view_handler, methods, desc))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _plugin_on_free_port() -> tuple[GenericFallbackProxyPlugin, int]:
    port = _free_port()
    plugin = GenericFallbackProxyPlugin(
        context=FakeContext(),
        config={
            "virtual_models": [{"name": "vm", "provider_ids": ["provider/a"]}],
            "proxy_port": port,
        },
    )
    return plugin, port


async def _get_until_ok(client: httpx.AsyncClient, url: str, timeout: float = 3.0):
    """轮询直到服务可响应（serve() 任务异步绑定端口）"""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return await client.get(url)
        except httpx.HTTPError as e:  # 连接被拒等：服务尚未就绪
            last_exc = e
            await asyncio.sleep(0.05)
    raise AssertionError(f"服务在 {timeout}s 内未就绪: {last_exc}")


async def test_serve_task_runs_on_caller_event_loop():
    """核心断言：serve() 任务跑在调用方（AstrBot 主）事件循环上，而非独立线程的循环。"""
    plugin, port = _plugin_on_free_port()
    await plugin.initialize()

    task = plugin._serve_task
    assert task is not None, "启动失败时 _serve_task 不应为 None"
    assert isinstance(plugin._uvicorn_server, _MainLoopProxyServer)
    assert task.get_loop() is asyncio.get_running_loop()

    # 服务真的在监听：未配置 API Key 时 /v1/models 直接放行
    async with httpx.AsyncClient(trust_env=False) as client:
        resp = await _get_until_ok(client, f"http://127.0.0.1:{port}/v1/models")
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == ["vm"]

    # 优雅关闭：任务正常结束（无异常）、端口释放（可重新绑定）
    await plugin.terminate()
    assert task.done() and not task.cancelled()
    assert task.exception() is None
    assert plugin._uvicorn_server is None
    assert plugin._serve_task is None
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", port))


async def test_terminate_without_started_server_is_noop():
    """未启动过服务时 terminate() 直接返回，不留半启动状态。"""
    plugin, _ = _plugin_on_free_port()
    await plugin.terminate()
    assert plugin._uvicorn_server is None
    assert plugin._serve_task is None


async def test_capture_signals_leaves_astrbot_handlers_untouched():
    """信号所有权归 AstrBot：serve() 期间不得捕获 SIGINT/SIGTERM。"""
    server = _MainLoopProxyServer(config=uvicorn.Config(app=FastAPI()))

    before = signal.getsignal(signal.SIGINT)
    with server.capture_signals():
        assert signal.getsignal(signal.SIGINT) is before
    assert signal.getsignal(signal.SIGINT) is before


async def test_initialize_gives_up_cleanly_when_port_taken(monkeypatch):
    """端口预检失败：initialize 提前返回，不残留任务或半启动状态。"""
    plugin, port = _plugin_on_free_port()
    monkeypatch.setattr(plugin, "_is_port_available", lambda p, h="127.0.0.1": False)
    monkeypatch.setattr(time, "sleep", lambda _: None)  # 跳过重试等待

    await plugin.initialize()

    assert plugin._serve_task is None
    assert plugin._uvicorn_server is None
    # Web API 已注册（启动顺序在端口检查之前），但代理服务确实没起
    assert plugin.context.registered_web_apis
