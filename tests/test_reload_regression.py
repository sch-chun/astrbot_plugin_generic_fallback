"""重载回归：插件重载后 Provider 的 HTTP 连接必须保持可用。

用户报告的现象：「Event loop is closed 会在重载后集中爆发一遍然后正常」。
根因：代理服务曾在独立线程的事件循环上运行，Provider（AstrBot 核心单例，跨插件
重载存活）的 HTTP 连接池会把 keep-alive 连接绑定到该线程的循环；插件重载时循环
关闭、池里留下僵尸连接，重载后首批请求复用它们时报 RuntimeError: Event loop is
closed——被 _call_provider 的 except Exception 吞掉，还把 Provider 误标记进冷却。

本测试用真实 uvicorn + 真实 HTTP 客户端复现该生命周期：共享客户端跨两个插件实例
存活（等价于 AstrBot 的 ProviderManager 单例），断言第二次初始化后的请求依然成功。
旧实现下此测试失败（第二次请求 503）；新实现（serve() 跑在主循环上）通过。
"""

import asyncio
import socket
import threading
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from astrbot_plugin_generic_fallback.main import GenericFallbackProxyPlugin

from astrbot.core.provider.provider import Provider

UPSTREAM_PORT = 18993

# 模拟 AstrBot ProviderManager 单例持有的共享 HTTP 客户端：跨插件实例存活，
# 且懒创建（首次被哪个循环调用，连接就绑定到哪个循环——与真实 Provider 一致）
_shared_client: httpx.AsyncClient | None = None


async def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(trust_env=False)
    return _shared_client


class FakeProvider(Provider):
    """对话 Provider：经共享客户端调上游（连接池行为与真实 Provider 一致）"""

    def get_current_key(self):
        return "fake-key"

    def get_models(self):
        return ["fake-model"]

    def set_key(self, key):
        pass

    def __init__(self):
        pass  # 跳过 Provider.__init__ 的配置依赖，仅 text_chat 被调用

    async def text_chat(self, **kwargs) -> SimpleNamespace:
        client = await _get_shared_client()
        resp = await client.post(f"http://127.0.0.1:{UPSTREAM_PORT}/echo", timeout=10)
        resp.raise_for_status()
        return SimpleNamespace(
            completion_text="pong",
            reasoning_content=None,
            tools_call_args=None,
            tools_call_ids=[],
            tools_call_name=[],
            usage=None,
        )


class FakeProviderManager:
    async def get_provider_by_id(self, provider_id: str):
        return FakeProvider()


class FakeContext:
    def __init__(self):
        self.registered_web_apis = []
        self.provider_manager = FakeProviderManager()

    def register_web_api(self, route, view_handler, methods, desc):
        self.registered_web_apis.append((route, view_handler, methods, desc))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _make_plugin(port: int) -> GenericFallbackProxyPlugin:
    return GenericFallbackProxyPlugin(
        context=FakeContext(),
        config={
            "virtual_models": [{"name": "vm", "provider_ids": ["fake/provider"]}],
            "proxy_port": port,
        },
    )


async def _chat_via_proxy(port: int) -> dict:
    """经代理发一次对话请求，返回响应体（失败时带状态码断言信息）"""
    async with httpx.AsyncClient(trust_env=False) as client:
        resp = await client.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json={"model": "vm", "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
    assert resp.status_code == 200, f"代理返回 {resp.status_code}: {resp.text}"
    return resp.json()


@pytest.fixture()
def upstream():
    """常驻上游「LLM」：让 Provider 的共享客户端能建起 keep-alive 连接"""

    def serve():
        async def main():
            async def echo(request):
                return web.json_response({"ok": True})

            app = web.Application()
            app.router.add_post("/echo", echo)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", UPSTREAM_PORT).start()
            await asyncio.Event().wait()

        asyncio.run(main())

    thread = threading.Thread(target=serve, daemon=True, name="fake-upstream")
    thread.start()
    yield
    # 守护线程随进程退出，无需显式停止


async def test_provider_connections_survive_plugin_reload(upstream):
    """重载后（新插件实例）经代理的请求依然成功——连接池未留下僵尸连接。"""
    global _shared_client
    port = _free_port()
    try:
        # 实例 1：首次调用，共享客户端的连接在代理服务所在循环上建立
        plugin1 = _make_plugin(port)
        await plugin1.initialize()
        first = await _chat_via_proxy(port)
        assert first["choices"][0]["message"]["content"] == "pong"

        # 重载：旧实例优雅关闭（其事件循环随之关闭）
        await plugin1.terminate()

        # 实例 2：复用同一共享客户端。旧实现下这里会命中「绑定到已关闭循环」的
        # 僵尸连接，请求失败并被误标记冷却，返回 503
        plugin2 = _make_plugin(port)
        await plugin2.initialize()
        second = await _chat_via_proxy(port)
        assert second["choices"][0]["message"]["content"] == "pong"
        await plugin2.terminate()
    finally:
        if _shared_client is not None:
            await _shared_client.aclose()
            _shared_client = None
