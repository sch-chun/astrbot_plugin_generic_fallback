import json
import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio
from typing import Optional, Any, AsyncGenerator

from .model_manager import ModelManager
from .config import ProxyConfig

from astrbot.api import logger
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.provider.manager import ProviderManager

# 全局 HTTP 客户端
_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_CLIENT_LOCK = asyncio.Lock()

async def get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        async with _CLIENT_LOCK:
            if _HTTP_CLIENT is None:
                _HTTP_CLIENT = httpx.AsyncClient(
                    timeout=httpx.Timeout(120.0, connect=30.0),
                    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
                )
    return _HTTP_CLIENT

async def close_http_client() -> None:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None:
        try:
            loop = asyncio.get_running_loop()
            if loop.is_closed():
                logger.warning("事件循环已关闭，跳过 HTTP 客户端关闭")
                _HTTP_CLIENT = None
                return
            await _HTTP_CLIENT.aclose()
            _HTTP_CLIENT = None
        except Exception as e:
            logger.warning(f"关闭 HTTP 客户端时发生异常: {e}")
            _HTTP_CLIENT = None

def create_proxy_router(
    config: ProxyConfig,
    model_manager: ModelManager,
    virtual_models: list[dict],
    provider_manager: Optional[ProviderManager] = None
):
    router = APIRouter()

    # ---------- 辅助函数 ----------
    def _short_provider_name(provider_id: str) -> str:
        return provider_id.split("/")[-1] if "/" in provider_id else provider_id

    def _inject_tag(content: str, provider_id: str) -> str:
        return f"[{_short_provider_name(provider_id)}] " + content

    def _unauthorized_response() -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "message": "Unauthorized: invalid or missing API Key",
                    "type": "authentication_error",
                    "code": "invalid_api_key",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def _verify_api_key(request: Request) -> Optional[JSONResponse]:
        proxy_key = config.proxy_api_key
        if not proxy_key:
            return None
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return _unauthorized_response()
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return _unauthorized_response()
        if parts[1] != proxy_key:
            logger.warning("API Key 验证失败")
            return _unauthorized_response()
        return None

    async def _call_provider(
        provider_id: str,
        body: dict,
        is_stream: bool,
        show_tag: bool,
        log_resp: bool,
        timeout: int,
    ) -> Optional[Response]:
        """调用单个 Provider，成功返回 Response，失败返回 None（并可能标记冷却）"""
        if not provider_manager:
            logger.error("ProviderManager 未初始化")
            return None

        provider = await provider_manager.get_provider_by_id(provider_id)
        if not provider:
            logger.warning(f"Provider '{provider_id}' 不存在")
            return None

        # 只支持 OpenAI 兼容 Provider
        if not isinstance(provider, ProviderOpenAIOfficial):
            logger.warning(f"Provider '{provider_id}' 不是 OpenAI 兼容类型，跳过")
            return None

        # 获取配置
        base_url = provider.provider_config.get("api_base", "")
        if not base_url:
            logger.warning(f"Provider '{provider_id}' 的 api_base 为空")
            return None
        api_key = provider.get_current_key()
        if not api_key:
            logger.warning(f"Provider '{provider_id}' 的 API Key 为空")
            return None
        model = provider.get_model()
        if not model:
            logger.warning(f"Provider '{provider_id}' 的 model 名称为空")
            return None

        # 构造请求
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = f"{base_url.rstrip('/')}/chat/completions"
        body_for_request = body.copy()
        body_for_request["model"] = model

        client = await get_http_client()
        try:
            if not is_stream:
                resp = await client.post(url, headers=headers, json=body_for_request, timeout=timeout)
                if resp.status_code != 200:

                    # 读取完整响应内容
                    try:
                        error_text = resp.text
                    except Exception:
                        error_text = resp.content.decode("utf-8", errors="ignore")
                    logger.warning(f"Provider {provider_id} 请求失败，HTTP {resp.status_code}，标记冷却并回退：{error_text}")
                    await model_manager.mark_cooldown(provider_id, f"HTTP {resp.status_code}")
                    return None
                
                # 成功
                if show_tag:
                    try:
                        resp_data = resp.json()
                        choices = resp_data.get("choices", [])
                        if choices:
                            msg = choices[0].get("message", {})
                            content = msg.get("content")
                            if isinstance(content, str) and content:
                                msg["content"] = _inject_tag(content, provider_id)
                                choices[0]["message"] = msg
                                resp_data["choices"] = choices
                        resp_content = json.dumps(resp_data, ensure_ascii=False).encode("utf-8")
                    except Exception:
                        resp_content = resp.content
                else:
                    resp_content = resp.content

                if log_resp:
                    logger.info(f"[响应日志] Provider={provider_id} 成功，长度 {len(resp_content)}")
                return Response(content=resp_content, status_code=resp.status_code)
            else:

                # 流式
                req = client.stream("POST", url, headers=headers, json=body_for_request, timeout=timeout)
                resp = await req.__aenter__()

                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_text = error_body.decode("utf-8", errors="ignore")
                    await req.__aexit__(None, None, None)
                    logger.warning(f"Provider {provider_id} 流式请求失败，HTTP {resp.status_code}，标记冷却并回退：{error_text}")
                    await model_manager.mark_cooldown(provider_id, f"HTTP {resp.status_code}")
                    return None

                # 成功流式，需要封装生成器
                collected = []
                injected = False

                async def stream_generator() -> AsyncGenerator:
                    nonlocal injected
                    try:
                        async for chunk in resp.aiter_bytes():
                            if log_resp:
                                try:
                                    text = chunk.decode("utf-8")
                                    for line in text.split("\n"):
                                        if line.startswith("data:"):
                                            data_str = line[5:].strip()
                                            if data_str == "[DONE]":
                                                continue
                                            data = json.loads(data_str)
                                            if data.get("choices"):
                                                delta = data["choices"][0].get("delta", {})
                                                content = delta.get("content")
                                                if isinstance(content, str) and content:
                                                    collected.append(content)
                                except Exception:
                                    pass

                            if show_tag and not injected:

                                # 简单尝试在第一个 chunk 插入标签（仅第一个 content）
                                try:
                                    text = chunk.decode("utf-8")

                                    # 寻找 data 行
                                    new_lines = []
                                    for line in text.split("\n"):
                                        if line.startswith("data:"):
                                            data_str = line[5:].strip()
                                            if data_str != "[DONE]":
                                                data = json.loads(data_str)
                                                choices = data.get("choices")
                                                if choices:
                                                    delta = choices[0].get("delta", {})
                                                    content = delta.get("content")
                                                    if isinstance(content, str) and content:
                                                        delta["content"] = _inject_tag(content, provider_id)
                                                        choices[0]["delta"] = delta
                                                        data["choices"] = choices
                                                        line = f"data: {json.dumps(data, ensure_ascii=False)}"
                                        new_lines.append(line)
                                    new_text = "\n".join(new_lines)
                                    if new_text != text:
                                        chunk = new_text.encode("utf-8")
                                        injected = True
                                except Exception:
                                    pass
                            yield chunk
                    finally:
                        if log_resp and collected:
                            logger.info(f"[响应日志] Provider={provider_id} (stream) 内容: {''.join(collected)[:500]}...")
                        await req.__aexit__(None, None, None)

                return StreamingResponse(
                    stream_generator(),
                    status_code=200,
                    headers={
                        "Content-Type": resp.headers.get("content-type", "text/event-stream"),
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    }
                )
        except (httpx.ReadTimeout, httpx.TimeoutException) as e:
            logger.warning(f"Provider {provider_id} 请求超时，标记冷却")
            await model_manager.mark_cooldown(provider_id, f"超时: {e}")
            return None
        except Exception as e:
            logger.error(f"Provider {provider_id} 未知异常: {e}", exc_info=True)

            # 其他异常也标记冷却，避免频繁重试
            await model_manager.mark_cooldown(provider_id, f"异常: {e}")
            return None

    # ---------- 路由 ----------
    @router.post("/v1/chat/completions")
    async def proxy_chat_completions(request: Request) -> Response:
        auth_error = await _verify_api_key(request)
        if auth_error:
            return auth_error

        try:
            body = await request.json()
        except Exception as e:
            return JSONResponse(status_code=400, content={"error": {"message": f"无效请求体: {e}", "type": "invalid_request"}})

        requested_model = body.get("model")
        if not requested_model:
            return JSONResponse(status_code=400, content={"error": {"message": "缺少 model 字段"}})

        # 查找虚拟模型配置
        vconf = next((v for v in virtual_models if v.get("name") == requested_model), None)
        if not vconf:
            return JSONResponse(status_code=404, content={"error": {"message": f"虚拟模型 '{requested_model}' 不存在"}})

        provider_ids = vconf.get("provider_ids", [])
        if not provider_ids:
            return JSONResponse(status_code=503, content={"error": {"message": "该虚拟模型未配置任何 Provider"}})

        timeout = vconf.get("timeout", 120)
        is_stream = body.get("stream", False)
        show_tag = config.show_model_tag
        log_resp = config.log_response

        # 依次尝试 Provider
        for pid in provider_ids:

            # 检查是否冷却中
            if not await model_manager.is_available(pid):
                logger.info(f"Provider {pid} 处于冷却中，跳过")
                continue

            logger.info(f"尝试 Provider: {pid}")
            resp = await _call_provider(pid, body, is_stream, show_tag, log_resp, timeout)
            if resp is not None:

                # 成功或返回了非配额错误，直接返回
                return resp
            
            # 如果返回 None，表示配额错误或超时，继续下一个

        # 全部失败
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "所有 Provider 均不可用（配额/限流/超时），请稍后重试",
                    "type": "service_unavailable",
                    "code": "all_providers_unavailable"
                }
            }
        )

    @router.get("/v1/models")
    async def proxy_models(request: Request) -> JSONResponse:
        auth_error = await _verify_api_key(request)
        if auth_error:
            return auth_error
        data = []
        for v in virtual_models:
            data.append({
                "id": v.get("name"),
                "object": "model",
                "owned_by": "generic-fallback-proxy",
                "created": 0,
            })
        return JSONResponse(content={"object": "list", "data": data})

    @router.get("/v1/status")
    async def proxy_status(request: Request) -> JSONResponse:
        auth_error = await _verify_api_key(request)
        if auth_error:
            return auth_error
        status = await model_manager.get_status()
        status["virtual_models"] = [
            {
                "name": v.get("name"),
                "provider_ids": v.get("provider_ids", []),
            }
            for v in virtual_models
        ]
        return JSONResponse(content=status)

    return router, close_http_client
