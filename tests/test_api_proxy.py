"""代理路由：回退、冷却与禁用行为的集成测试。"""
import json

from fastapi import FastAPI
from starlette.testclient import TestClient

from astrbot.core.provider.entities import LLMResponse
from astrbot.core.provider.provider import Provider

from astrbot_plugin_generic_fallback.src.api_proxy import create_proxy_router
from astrbot_plugin_generic_fallback.src.config import ProxyConfig
from astrbot_plugin_generic_fallback.src.model_manager import ModelManager

PROVIDER_ID = "provider/dummy"
VIRTUAL_MODEL = "vm"


class DummyProvider(Provider):
    """可注入失败/成功行为的 Chat Provider 替身"""

    def __init__(
        self,
        text: str = "ok",
        error: str | None = None,
        stream_empty: bool = False,
        stream_error_after_content: bool = False,
        stream_empty_first: int = 0,
    ):
        super().__init__(
            provider_config={"id": PROVIDER_ID, "type": "openai_chat_completion"},
            provider_settings={},
        )
        self.text = text
        self.error = error
        self.stream_empty = stream_empty
        self.stream_error_after_content = stream_error_after_content
        self.stream_empty_first = stream_empty_first
        self.calls = 0

    def get_current_key(self) -> str:
        return "dummy-key"

    def set_key(self, key: str) -> None:
        return None

    async def get_models(self) -> list[str]:
        return ["dummy-model"]

    async def text_chat(self, **kwargs):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return LLMResponse(role="assistant", completion_text=self.text)

    async def text_chat_stream(self, **kwargs):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        if self.stream_empty_first > 0:
            self.stream_empty_first -= 1
            return
        if self.stream_error_after_content:
            yield LLMResponse(role="assistant", completion_text="chunk", is_chunk=True)
            raise RuntimeError("mid-stream failure")
        if self.stream_empty:
            return
        yield LLMResponse(role="assistant", completion_text="chunk", is_chunk=True)
        yield LLMResponse(role="assistant", completion_text=self.text)


class FakeProviderManager:
    def __init__(self, providers: dict):
        self._providers = providers

    async def get_provider_by_id(self, provider_id: str):
        return self._providers.get(provider_id)


def build_app(provider: DummyProvider, policy=None, api_key: str = ""):
    manager = ModelManager(cooldown_policy=policy)
    config = ProxyConfig(proxy_api_key=api_key)
    router = create_proxy_router(
        config=config,
        model_manager=manager,
        virtual_models=[{"name": VIRTUAL_MODEL, "provider_ids": [PROVIDER_ID]}],
        provider_manager=FakeProviderManager({PROVIDER_ID: provider}),
    )
    app = FastAPI()
    app.include_router(router)
    return app, manager


def chat(client: TestClient, **overrides):
    body = {"model": VIRTUAL_MODEL, "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return client.post("/v1/chat/completions", json=body)


async def test_missing_model_field():
    app, _ = build_app(DummyProvider())
    response = TestClient(app).post("/v1/chat/completions", json={"messages": []})
    assert response.status_code == 400


async def test_unknown_virtual_model():
    app, _ = build_app(DummyProvider())
    response = chat(TestClient(app), model="nope")
    assert response.status_code == 404


async def test_success_returns_openai_payload():
    app, _ = build_app(DummyProvider(text="hello"))

    response = chat(TestClient(app))

    assert response.status_code == 200
    payload = response.json()
    assert payload["choices"][0]["message"]["content"] == "hello"
    assert payload["model"] == VIRTUAL_MODEL


async def test_failure_marks_cooldown():
    app, _ = build_app(DummyProvider(error="boom"), policy=[[1, 1]])
    client = TestClient(app)

    assert chat(client).status_code == 503

    status = client.get("/v1/status").json()
    assert status["cooldown_count"] == 1
    assert status["fail_counts"] == {PROVIDER_ID: 1}
    assert 55 <= status["cooldown_list"][0]["remaining_secs"] <= 60


async def test_unsupported_media_type_does_not_cooldown():
    # 415 属于上游「不支持」，重试无意义，只回退不冷却
    provider = DummyProvider(error="Error code: 415 - {'message': 'Unsupported Media Type', 'code': 415}")
    app, _ = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    assert chat(client).status_code == 503

    status = client.get("/v1/status").json()
    assert status["cooldown_count"] == 0
    assert status["fail_counts"] == {}


async def test_content_filter_does_not_cooldown():
    provider = DummyProvider(error="内容安全过滤: blocked")
    app, _ = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    assert chat(client).status_code == 503
    assert client.get("/v1/status").json()["cooldown_count"] == 0


async def test_no_choices_does_not_cooldown():
    # 非流式下的 no choices 会先原地重试（默认 3 次），耗尽后回退但不冷却
    provider = DummyProvider(error="completion has no choices")
    app, _ = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    assert chat(client).status_code == 503

    assert provider.calls == 3  # 等于 empty_response_max_attempts 默认值
    status = client.get("/v1/status").json()
    assert status["cooldown_count"] == 0
    assert status["fail_counts"] == {}


async def test_stream_no_choices_does_not_cooldown():
    # no choices 属于「上游无有效内容」，与非流式一致：原地重试、只回退不冷却
    provider = DummyProvider(error="completion has no choices")
    app, _ = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    assert chat(client, stream=True).status_code == 200

    assert provider.calls == 3  # 等于 empty_response_max_attempts 默认值
    status = client.get("/v1/status").json()
    assert status["cooldown_count"] == 0
    assert status["fail_counts"] == {}


async def test_stream_empty_response_retries_in_place():
    # 上游返回空流（零 chunk）：应与非流式一样原地重试，耗尽后记录失败但不冷却
    provider = DummyProvider(stream_empty=True)
    app, manager = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    response = chat(client, stream=True)
    assert response.status_code == 200

    assert provider.calls == 3  # 原地重试满 empty_response_max_attempts
    assert 'data: {"error": "empty stream response"}' in response.text
    assert "data: [DONE]" in response.text
    status = client.get("/v1/status").json()
    assert status["cooldown_count"] == 0
    assert status["fail_counts"] == {}  # 空响应不计入连续失败
    logs = await manager.get_call_log(PROVIDER_ID)
    assert len(logs) == 1  # 原地重试只记一条结局
    assert logs[0]["success"] is False
    assert "空响应" in logs[0]["reason"]


async def test_stream_empty_final_response_retries_in_place():
    # 只回来一个空的最终响应（无内容、无工具调用）：同样视为空流
    provider = DummyProvider(stream_empty_first=3)
    app, _ = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    response = chat(client, stream=True)

    assert provider.calls == 3
    assert 'data: {"error": "empty stream response"}' in response.text
    assert client.get("/v1/status").json()["cooldown_count"] == 0


async def test_stream_empty_then_success():
    # 空流重试后恢复：第二次调用有内容，应标记成功且不出现错误 chunk
    provider = DummyProvider(text="hello", stream_empty_first=1)
    app, manager = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    response = chat(client, stream=True)

    assert provider.calls == 2
    assert "data: [DONE]" in response.text
    assert "error" not in response.text
    logs = await manager.get_call_log(PROVIDER_ID)
    assert len(logs) == 1
    assert logs[0]["success"] is True


async def test_stream_error_after_content_does_not_retry():
    # 内容已发出后异常：重试会向客户端重复发送内容，只能以错误 chunk 收尾
    provider = DummyProvider(stream_error_after_content=True)
    app, _ = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    response = chat(client, stream=True)

    assert provider.calls == 1  # 不重试
    assert 'data: {"error": "provider error"}' in response.text
    assert "data: [DONE]" in response.text
    status = client.get("/v1/status").json()
    assert status["cooldown_count"] == 1  # 普通异常照常冷却
    assert status["fail_counts"] == {PROVIDER_ID: 1}


async def test_stream_unsupported_media_type_does_not_cooldown():
    provider = DummyProvider(error="Unsupported Media Type")
    app, _ = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    response = chat(client, stream=True)
    assert response.status_code == 200
    assert client.get("/v1/status").json()["cooldown_count"] == 0


async def test_other_errors_still_cooldown():
    provider = DummyProvider(error="Connection timeout")
    app, _ = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    assert chat(client).status_code == 503
    assert client.get("/v1/status").json()["cooldown_count"] == 1


async def test_cooling_provider_is_skipped():
    provider = DummyProvider(text="hello")
    app, _ = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    provider.error = "boom"
    assert chat(client).status_code == 503
    assert provider.calls == 1

    # 仍在冷却中，不应再次调用上游
    provider.error = None
    assert chat(client).status_code == 503
    assert provider.calls == 1


async def test_success_resets_fail_count():
    provider = DummyProvider(text="hello")
    app, _ = build_app(provider, policy=[[1, 0]])  # 0 分钟：冷却立即过期
    client = TestClient(app)

    provider.error = "boom"
    assert chat(client).status_code == 503
    assert client.get("/v1/status").json()["fail_counts"] == {PROVIDER_ID: 1}

    provider.error = None
    assert chat(client).status_code == 200
    assert client.get("/v1/status").json()["fail_counts"] == {}


async def test_disabled_provider_is_skipped():
    provider = DummyProvider(text="hello")
    app, manager = build_app(provider)
    await manager.set_disabled(PROVIDER_ID, True)

    response = chat(TestClient(app))

    assert response.status_code == 503
    assert provider.calls == 0


async def test_stream_success_resets_fail_count():
    provider = DummyProvider(text="hello")
    app, _ = build_app(provider, policy=[[1, 0]])
    client = TestClient(app)

    provider.error = "boom"
    assert chat(client, stream=True).status_code == 200  # 流式请求总是先返回响应头
    assert client.get("/v1/status").json()["fail_counts"] == {PROVIDER_ID: 1}

    provider.error = None
    response = chat(client, stream=True)
    assert response.status_code == 200
    assert "data:" in response.text
    assert client.get("/v1/status").json()["fail_counts"] == {}


async def test_models_endpoint_lists_virtual_models():
    app, _ = build_app(DummyProvider())
    payload = TestClient(app).get("/v1/models").json()
    assert [item["id"] for item in payload["data"]] == [VIRTUAL_MODEL]


async def test_status_endpoint_exposes_policy_and_disabled_list():
    app, manager = build_app(DummyProvider())
    await manager.set_disabled(PROVIDER_ID, True)

    status = TestClient(app).get("/v1/status").json()

    assert status["disabled_list"] == [PROVIDER_ID]
    assert status["cooldown_policy"] == [[1, 1], [2, 5], [5, 30]]
    assert status["virtual_models"] == [{"name": VIRTUAL_MODEL, "provider_ids": [PROVIDER_ID]}]


async def test_api_key_is_enforced():
    app, _ = build_app(DummyProvider(text="hello"), api_key="secret")
    client = TestClient(app)

    assert chat(client).status_code == 401
    assert chat(client, headers={"Authorization": "Bearer wrong"}).status_code == 401

    response = client.post(
        "/v1/chat/completions",
        json={"model": VIRTUAL_MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert json.loads(response.text)["choices"][0]["message"]["content"] == "hello"


async def test_call_log_records_success():
    app, manager = build_app(DummyProvider(text="hello"))

    assert chat(TestClient(app)).status_code == 200

    logs = await manager.get_call_log(PROVIDER_ID)
    assert len(logs) == 1
    assert logs[0]["success"] is True
    assert logs[0]["reason"] == ""


async def test_call_log_records_failure_reason():
    app, manager = build_app(DummyProvider(error="Connection timeout"), policy=[[1, 1]])

    assert chat(TestClient(app)).status_code == 503

    logs = await manager.get_call_log(PROVIDER_ID)
    assert len(logs) == 1
    assert logs[0]["success"] is False
    assert "Connection timeout" in logs[0]["reason"]


async def test_call_log_records_no_cooldown_failure():
    # 上游拒绝类失败不冷却，但调用日志仍要留痕
    provider = DummyProvider(error="内容安全过滤: blocked")
    app, manager = build_app(provider, policy=[[1, 1]])

    assert chat(TestClient(app)).status_code == 503

    logs = await manager.get_call_log(PROVIDER_ID)
    assert len(logs) == 1
    assert logs[0]["success"] is False
    assert "内容安全过滤" in logs[0]["reason"]
    assert (await manager.get_status())["cooldown_count"] == 0


async def test_call_log_records_empty_response_exhaustion():
    app, manager = build_app(DummyProvider(text=""), policy=[[1, 1]])

    assert chat(TestClient(app)).status_code == 503

    logs = await manager.get_call_log(PROVIDER_ID)
    assert len(logs) == 1  # 原地重试只记一条结局
    assert logs[0]["success"] is False
    assert "空响应" in logs[0]["reason"]


async def test_call_log_records_stream_success():
    app, manager = build_app(DummyProvider(text="hello"))
    client = TestClient(app)

    response = chat(client, stream=True)
    assert response.status_code == 200
    assert "data: [DONE]" in response.text  # 读完整流，确保生成器执行完

    logs = await manager.get_call_log(PROVIDER_ID)
    assert len(logs) == 1
    assert logs[0]["success"] is True


async def test_call_log_records_stream_failure():
    provider = DummyProvider(error="Connection reset")
    app, manager = build_app(provider, policy=[[1, 1]])
    client = TestClient(app)

    response = chat(client, stream=True)
    assert response.status_code == 200
    assert "data: [DONE]" in response.text

    logs = await manager.get_call_log(PROVIDER_ID)
    assert len(logs) == 1
    assert logs[0]["success"] is False
    assert "Connection reset" in logs[0]["reason"]
