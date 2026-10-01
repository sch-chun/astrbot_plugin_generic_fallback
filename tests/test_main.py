"""插件入口：初始化、状态接口与禁用接口测试。"""
import json
from types import SimpleNamespace

from astrbot.api.web import PluginRequest, bind_request_context
from astrbot_plugin_generic_fallback.main import GenericFallbackProxyPlugin

DEFAULT_POLICY = [[1, 1], [2, 5], [5, 30]]
STATUS_ROUTE = "/generic_fallback/status"
DISABLE_ROUTE = "/generic_fallback/provider/set_disabled"


class FakeContext:
    """仅实现插件初始化所需的 Context 能力"""

    def __init__(self):
        self.registered_web_apis = []
        self.provider_manager = None

    def register_web_api(self, route, view_handler, methods, desc):
        self.registered_web_apis.append((route, view_handler, methods, desc))


class FakeRawRequest:
    """构造 astrbot.api.web.PluginRequest 所需的原始请求对象"""

    def __init__(self, payload):
        self.method = "POST"
        self.url = SimpleNamespace(path="/api/test")
        self.headers = {}
        self.cookies = {}
        self.query_params = SimpleNamespace(multi_items=lambda: [])
        self.client = None
        self._payload = payload

    async def json(self):
        return self._payload


def make_plugin(config: dict) -> GenericFallbackProxyPlugin:
    return GenericFallbackProxyPlugin(context=FakeContext(), config=config)


def body_of(response) -> dict:
    return json.loads(response.body)


async def call_with_body(plugin: GenericFallbackProxyPlugin, payload):
    with bind_request_context(PluginRequest(FakeRawRequest(payload))):
        return body_of(await plugin.set_provider_disabled_handler())


async def test_initialize_registers_web_apis(monkeypatch):
    plugin = make_plugin({
        "virtual_models": [{"name": "vm", "provider_ids": ["provider/a"]}],
    })
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)

    await plugin.initialize()

    routes = [route for route, _, _, _ in plugin.context.registered_web_apis]
    assert STATUS_ROUTE in routes
    assert DISABLE_ROUTE in routes
    assert plugin._model_manager is not None
    assert plugin._model_manager.cooldown_policy == DEFAULT_POLICY


async def test_initialize_uses_configured_policy(monkeypatch):
    plugin = make_plugin({
        "virtual_models": [{"name": "vm", "provider_ids": ["provider/a"]}],
        "cooldown_policy": [[1, 2], [3, 20]],
    })
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)

    await plugin.initialize()

    assert plugin._model_manager.cooldown_policy == [[1, 2.0], [3, 20.0]]
    assert plugin._proxy_config.cooldown_policy == [(1, 2.0), (3, 20.0)]


async def test_initialize_falls_back_on_invalid_policy(monkeypatch):
    plugin = make_plugin({
        "virtual_models": [{"name": "vm", "provider_ids": ["provider/a"]}],
        "cooldown_policy": [[1, 1], [1, 5]],  # 阈值重复
    })
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)

    await plugin.initialize()

    assert plugin._model_manager.cooldown_policy == DEFAULT_POLICY


async def test_initialize_without_virtual_models(monkeypatch):
    plugin = make_plugin({})
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)

    await plugin.initialize()

    assert plugin._model_manager is None
    assert plugin.context.registered_web_apis == []


async def test_status_handler_reports_error_when_uninitialized():
    plugin = make_plugin({})

    assert body_of(await plugin.status_handler()) == {"error": "服务未初始化"}


async def test_status_handler_includes_disabled_list(monkeypatch):
    plugin = make_plugin({
        "virtual_models": [{"name": "vm", "provider_ids": ["provider/a", "provider/b"]}],
    })
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)
    await plugin.initialize()
    await plugin._model_manager.set_disabled("provider/a", True)

    status = body_of(await plugin.status_handler())

    assert status["disabled_list"] == ["provider/a"]
    assert status["cooldown_policy"] == DEFAULT_POLICY
    assert status["virtual_models"] == [{"name": "vm", "provider_ids": ["provider/a", "provider/b"]}]


async def test_set_disabled_handler_disables_provider(monkeypatch):
    plugin = make_plugin({"virtual_models": [{"name": "vm", "provider_ids": ["provider/a"]}]})
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)
    await plugin.initialize()

    result = await call_with_body(plugin, {"provider_id": "provider/a", "disabled": True})

    assert result == {"ok": True, "provider_id": "provider/a", "disabled": True}
    assert await plugin._model_manager.is_disabled("provider/a") is True
    assert await plugin._model_manager.is_available("provider/a") is False


async def test_set_disabled_handler_enables_provider(monkeypatch):
    plugin = make_plugin({"virtual_models": [{"name": "vm", "provider_ids": ["provider/a"]}]})
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)
    await plugin.initialize()
    await plugin._model_manager.set_disabled("provider/a", True)

    result = await call_with_body(plugin, {"provider_id": "provider/a", "disabled": False})

    assert result["ok"] is True
    assert await plugin._model_manager.is_disabled("provider/a") is False
    assert await plugin._model_manager.is_available("provider/a") is True


async def test_set_disabled_handler_rejects_invalid_payload(monkeypatch):
    plugin = make_plugin({"virtual_models": [{"name": "vm", "provider_ids": ["provider/a"]}]})
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)
    await plugin.initialize()

    for payload in (
        {},
        {"provider_id": "", "disabled": True},
        {"provider_id": "   ", "disabled": True},
        {"provider_id": 123, "disabled": True},
        {"provider_id": "provider/a"},
        {"provider_id": "provider/a", "disabled": "yes"},
        {"provider_id": "provider/a", "disabled": 1},
        ["provider/a", True],
        None,
    ):
        result = await call_with_body(plugin, payload)
        assert result["ok"] is False, f"应拒绝载荷 {payload!r}"
        assert "error" in result

    assert await plugin._model_manager.is_disabled("provider/a") is False


async def test_set_disabled_handler_rejects_unknown_provider(monkeypatch):
    # 脏 provider_id 会永久攒进 disabled_list，必须挡在入口
    plugin = make_plugin({"virtual_models": [{"name": "vm", "provider_ids": ["provider/a"]}]})
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)
    await plugin.initialize()

    result = await call_with_body(plugin, {"provider_id": "provider/ghost", "disabled": True})

    assert result["ok"] is False
    assert "回退链" in result["error"]
    assert await plugin._model_manager.is_disabled("provider/ghost") is False
    assert body_of(await plugin.status_handler())["disabled_list"] == []


async def test_set_disabled_handler_strips_provider_id(monkeypatch):
    plugin = make_plugin({"virtual_models": [{"name": "vm", "provider_ids": ["provider/a"]}]})
    monkeypatch.setattr(plugin, "_start_uvicorn", lambda: True)
    await plugin.initialize()

    await call_with_body(plugin, {"provider_id": "  provider/a  ", "disabled": True})

    assert await plugin._model_manager.is_disabled("provider/a") is True
    assert await plugin._model_manager.is_disabled("  provider/a  ") is False


async def test_set_disabled_handler_when_uninitialized():
    plugin = make_plugin({})

    assert await call_with_body(plugin, {"provider_id": "provider/a", "disabled": True}) == {
        "ok": False,
        "error": "服务未初始化",
    }
