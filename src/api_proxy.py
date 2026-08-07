import json
import time
from typing import Optional, AsyncGenerator, Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse

from astrbot.api import logger
from astrbot.core.provider.manager import ProviderManager
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.agent.tool import ToolSet, FunctionTool
from astrbot.core.provider.provider import Provider

from .model_manager import ModelManager
from .config import ProxyConfig


def create_proxy_router(
    config: ProxyConfig,
    model_manager: ModelManager,
    virtual_models: list[dict],
    provider_manager: Optional[ProviderManager] = None,
):
    router = APIRouter()

    # ---------- 辅助函数 ----------
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

    def _build_toolset(tools: list[dict]) -> Optional[ToolSet]:
        if not tools:
            return None
        tool_set = ToolSet()
        for tool_def in tools:
            func_def = tool_def.get("function", {})
            tool = FunctionTool(
                name=func_def.get("name", ""),
                description=func_def.get("description", ""),
                parameters=func_def.get("parameters", {}),
            )
            tool_set.add_tool(tool)
        return tool_set

    def _extract_system_and_contexts(messages: list[dict]):
        system_prompt = None
        contexts = []
        for msg in messages:
            if msg.get("role") == "system":
                system_prompt = msg.get("content")
            else:
                contexts.append(msg)
        return system_prompt, contexts

    def _llm_response_to_openai_chat_completion(
        llm_resp: LLMResponse,
        model: str,
        id: Optional[str] = None,
    ) -> dict:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": llm_resp.completion_text or "",
        }
        if llm_resp.reasoning_content:
            message["reasoning_content"] = llm_resp.reasoning_content

        tool_calls: list[dict] = []
        if llm_resp.tools_call_args:
            for i, args in enumerate(llm_resp.tools_call_args):
                tool_calls.append({
                    "id": llm_resp.tools_call_ids[i] if i < len(llm_resp.tools_call_ids) else f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": llm_resp.tools_call_name[i] if i < len(llm_resp.tools_call_name) else "",
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
        if tool_calls:
            message["tool_calls"] = tool_calls

        usage = {
            "prompt_tokens": llm_resp.usage.input_other + llm_resp.usage.input_cached if llm_resp.usage else 0,
            "completion_tokens": llm_resp.usage.output if llm_resp.usage else 0,
            "total_tokens": (llm_resp.usage.input_other + llm_resp.usage.input_cached + llm_resp.usage.output) if llm_resp.usage else 0,
        }

        return {
            "id": id or f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if tool_calls else "stop",
                }
            ],
            "usage": usage,
        }

    async def _llm_response_to_openai_stream(
        llm_resp: LLMResponse,
        model: str,
        id: Optional[str] = None,
        is_final: bool = False,
    ) -> dict:
        """将 LLMResponse 转换为 OpenAI 流式 chunk 字典（仅用于增量 chunk）"""
        delta: dict[str, Any] = {}
        tool_calls: list[dict] = []

        # 我们只使用 is_final=False 的模式，因此这里忽略 is_final 分支
        # 只处理增量内容
        if llm_resp.completion_text:
            delta["content"] = llm_resp.completion_text
        if llm_resp.reasoning_content:
            delta["reasoning_content"] = llm_resp.reasoning_content

        # 增量工具调用（某些 Provider 可能在增量中发送工具调用）
        if llm_resp.tools_call_args and not is_final:
            for i, args in enumerate(llm_resp.tools_call_args):
                tool_calls.append({
                    "id": llm_resp.tools_call_ids[i] if i < len(llm_resp.tools_call_ids) else f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": llm_resp.tools_call_name[i] if i < len(llm_resp.tools_call_name) else "",
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
            if tool_calls:
                delta["tool_calls"] = tool_calls

        chunk: dict[str, Any] = {
            "id": id or f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": None,  # 增量 chunk 不设置 finish_reason
                }
            ],
        }
        return chunk

    async def _call_provider(
        provider_id: str,
        body: dict,
        is_stream: bool,
        log_resp: bool,
        requested_model: str,
    ) -> Optional[Response]:
        if not provider_manager:
            logger.error("ProviderManager 未初始化")
            return None

        provider = await provider_manager.get_provider_by_id(provider_id)
        if not provider:
            logger.warning(f"Provider '{provider_id}' 不存在")
            return None

        if not isinstance(provider, Provider):
            logger.warning(f"Provider '{provider_id}' 不是 Chat Provider，跳过")
            return None

        messages = body.get("messages", [])
        system_prompt, contexts = _extract_system_and_contexts(messages)
        tools = body.get("tools")
        tool_choice = body.get("tool_choice", "auto")
        extra_kwargs = {
            k: v for k, v in body.items()
            if k not in ("messages", "model", "stream", "tools", "tool_choice")
        }

        func_tool = _build_toolset(tools) if tools else None

        try:
            if not is_stream:
                llm_resp = await provider.text_chat(
                    prompt=None,
                    contexts=contexts,
                    system_prompt=system_prompt,
                    func_tool=func_tool,
                    tool_choice=tool_choice,
                    **extra_kwargs,
                )
                openai_resp = _llm_response_to_openai_chat_completion(llm_resp, requested_model)
                if log_resp:
                    logger.info(f"[响应日志] Provider={provider_id} 非流式响应: {json.dumps(openai_resp, ensure_ascii=False)[:500]}...")
                return JSONResponse(content=openai_resp, status_code=200)

            else:
                async def stream_generator() -> AsyncGenerator[bytes, None]:
                    id = f"chatcmpl-{int(time.time())}"
                    final_resp = None
                    usage_sent = False

                    async for llm_chunk in provider.text_chat_stream(
                        prompt=None,
                        contexts=contexts,
                        system_prompt=system_prompt,
                        func_tool=func_tool,
                        tool_choice=tool_choice,
                        **extra_kwargs,
                    ):
                        if llm_chunk.is_chunk:
                            
                            # 增量 chunk —— 发送内容增量
                            chunk_dict = await _llm_response_to_openai_stream(
                                llm_chunk, requested_model, id, is_final=False
                            )
                            yield f"data: {json.dumps(chunk_dict, ensure_ascii=False)}\n\n".encode("utf-8")
                        else:

                            # 最终响应 —— 只提取 usage，不发送内容
                            final_resp = llm_chunk

                            # 如果包含工具调用，需要以流式 chunk 形式发送
                            if final_resp.tools_call_args:

                                # 构建 tool_calls 列表
                                tool_calls: list[dict] = []
                                for i, args in enumerate(final_resp.tools_call_args):
                                    tool_calls.append({
                                        "id": final_resp.tools_call_ids[i] if i < len(final_resp.tools_call_ids) else f"call_{i}",
                                        "type": "function",
                                        "function": {
                                            "name": final_resp.tools_call_name[i] if i < len(final_resp.tools_call_name) else "",
                                            "arguments": json.dumps(args, ensure_ascii=False),
                                        },
                                    })

                                # 发送 tool_calls chunk
                                tool_chunk = {
                                    "id": id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": requested_model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": tool_calls
                                            },
                                            "finish_reason": "tool_calls",
                                        }
                                    ],
                                    "usage": {
                                        "prompt_tokens": final_resp.usage.input_other + final_resp.usage.input_cached if final_resp.usage else 0,
                                        "completion_tokens": final_resp.usage.output if final_resp.usage else 0,
                                        "total_tokens": final_resp.usage.input_other + final_resp.usage.input_cached + final_resp.usage.output if final_resp.usage else 0,
                                    } if final_resp.usage else None,
                                }

                                # 移除 None 值的 usage
                                if tool_chunk.get("usage") is None:
                                    del tool_chunk["usage"]
                                yield f"data: {json.dumps(tool_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                            
                            # 如果存在 usage 且尚未发送，则单独发送一个 usage chunk
                            elif final_resp.usage and not usage_sent:
                                usage_chunk = {
                                    "id": id,
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": requested_model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {},   # 空 delta
                                            "finish_reason": "tool_calls" if final_resp.tools_call_args else "stop",
                                        }
                                    ],
                                    "usage": {
                                        "prompt_tokens": final_resp.usage.input_other + final_resp.usage.input_cached if final_resp.usage else 0,
                                        "completion_tokens": final_resp.usage.output if final_resp.usage else 0,
                                        "total_tokens": final_resp.usage.input_other + final_resp.usage.input_cached + final_resp.usage.output if final_resp.usage else 0,
                                    } if final_resp.usage else None,
                                }

                                # 移除 None 值的 usage
                                if usage_chunk.get("usage") is None:
                                    del usage_chunk["usage"]
                                yield f"data: {json.dumps(usage_chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                                usage_sent = True

                    # 发送结束标记
                    yield b"data: [DONE]\n\n"

                    if log_resp and final_resp:
                        logger.info(f"[响应日志] Provider={provider_id} 流式响应完成，内容长度 {len(final_resp.completion_text or '')}")

                return StreamingResponse(
                    stream_generator(),
                    status_code=200,
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    },
                )

        except Exception as e:
            logger.warning(f"Provider {provider_id} 请求失败: {e}")
            await model_manager.mark_cooldown(provider_id, str(e))
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

        vconf = next((v for v in virtual_models if v.get("name") == requested_model), None)
        if not vconf:
            return JSONResponse(status_code=404, content={"error": {"message": f"虚拟模型 '{requested_model}' 不存在"}})

        provider_ids = vconf.get("provider_ids", [])
        if not provider_ids:
            return JSONResponse(status_code=503, content={"error": {"message": "该虚拟模型未配置任何 Provider"}})

        is_stream = body.get("stream", False)
        log_resp = config.log_response

        for pid in provider_ids:
            if not await model_manager.is_available(pid):
                logger.info(f"Provider {pid} 处于冷却中，跳过")
                continue

            logger.info(f"尝试 Provider: {pid}")
            resp = await _call_provider(pid, body, is_stream, log_resp, requested_model)
            if resp is not None:
                return resp

        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": "所有 Provider 均不可用（配额/限流/超时），请稍后重试",
                    "type": "service_unavailable",
                    "code": "all_providers_unavailable",
                }
            },
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

    return router
