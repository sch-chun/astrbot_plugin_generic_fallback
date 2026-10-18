import json
import time
from typing import Optional, AsyncGenerator, Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from astrbot.api import logger
from astrbot.core.provider.manager import ProviderManager
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.agent.tool import ToolSet, FunctionTool
from astrbot.core.provider.provider import Provider

from .model_manager import ModelManager
from .config import ProxyConfig


class FallbackStreamResponse(Response):
    """流式回退响应：首个内容字节发出后才发送响应头

    生成器未产出任何字节（所有 Provider 均不可用）时改发 503 JSON，状态码与非流式路径一致；
    已开始发送后的失败由生成器自身以 SSE 错误 chunk 收尾（此时无法再更改状态码）。
    """

    def __init__(self, generator: AsyncGenerator[bytes, None]):
        super().__init__(media_type="text/event-stream")
        self.generator = generator

    async def __call__(self, scope, receive, send):
        started = False
        try:
            async for chunk in self.generator:
                if not started:
                    await send({
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"text/event-stream"),
                            (b"cache-control", b"no-cache"),
                            (b"connection", b"keep-alive"),
                        ],
                    })
                    started = True
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
        except Exception:
            logger.exception("流式响应发送异常")
            try:
                if not started:
                    await JSONResponse(
                        status_code=500,
                        content={"error": {"message": "流式响应异常", "type": "internal_error"}},
                    )(scope, receive, send)
                else:
                    await send({
                        "type": "http.response.body",
                        "body": b'data: {"error": "stream aborted"}\n\n',
                        "more_body": False,
                    })
            except Exception:
                pass  # 客户端已断开，发送必然失败
            return

        if not started:
            # 所有 Provider 均未产出内容：改发 503，与非流式路径一致
            await JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "所有 Provider 均不可用（配额/限流/超时），请稍后重试",
                        "type": "service_unavailable",
                        "code": "all_providers_unavailable",
                    }
                },
            )(scope, receive, send)
            return

        await send({"type": "http.response.body", "body": b"", "more_body": False})


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

    def _is_no_cooldown_error(err_msg: str) -> bool:
        """判断异常是否属于「上游拒绝/无有效内容」——这类错误重试无意义，只回退不冷却

        覆盖三类：内容安全过滤、415 Unsupported Media Type、completion has no choices。
        （no choices 属于「上游无有效内容」，会先走空响应重试，耗尽后同样是「回退不冷却」，
        流式与非流式一致；内容安全过滤与 415 是明确拒绝，不重试、只回退。）
        """
        lowered = err_msg.lower()
        return (
            "内容安全过滤" in err_msg
            or "unsupported media type" in lowered
            or "completion has no choices" in lowered
        )

    def _prepare_call(body: dict) -> tuple:
        """从请求体解出调用参数（流式与非流式共用）

        Returns:
            (system_prompt, contexts, func_tool, tool_choice, extra_kwargs)
        """
        messages = body.get("messages", [])
        system_prompt, contexts = _extract_system_and_contexts(messages)
        tools = body.get("tools")
        extra_kwargs = {
            k: v for k, v in body.items()
            if k not in ("messages", "model", "stream", "tools", "tool_choice")
        }
        func_tool = _build_toolset(tools) if tools else None
        return system_prompt, contexts, func_tool, body.get("tool_choice", "auto"), extra_kwargs

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
        log_resp: bool,
        requested_model: str,
        empty_response_max_attempts: int
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

        system_prompt, contexts, func_tool, tool_choice, extra_kwargs = _prepare_call(body)

        for attempt in range(1, empty_response_max_attempts + 1):
            try:
                llm_resp = await provider.text_chat(
                    prompt=None,
                    contexts=contexts,
                    system_prompt=system_prompt,
                    func_tool=func_tool,
                    tool_choice=tool_choice,
                    request_max_retries=1,
                    **extra_kwargs,
                )
            except Exception as e:
                err_msg = str(e)

                # 判断是否为可重试的特定异常
                if "completion has no choices" in err_msg:
                    logger.warning(f"Provider {provider_id} 请求异常 (尝试 {attempt}/{empty_response_max_attempts}): {e}")
                    if attempt < empty_response_max_attempts:
                        continue
                    else:

                        # 达到重试上限，回退但不进入冷却
                        await model_manager.record_call(provider_id, False, err_msg)
                        return None
                elif _is_no_cooldown_error(err_msg):
                    logger.warning(f"Provider {provider_id} 请求被上游拒绝，跳过且不进入冷却: {e}")
                    await model_manager.record_call(provider_id, False, err_msg)
                    return None
                else:

                    # 其他异常直接冷却
                    logger.warning(f"Provider {provider_id} 请求异常: {e}")
                    await model_manager.mark_cooldown(provider_id, err_msg)
                    await model_manager.record_call(provider_id, False, err_msg)
                    return None

            # 检查空响应
            if not llm_resp.completion_text and not llm_resp.tools_call_args:
                logger.warning(f"Provider {provider_id} 返回空响应 (尝试 {attempt}/{empty_response_max_attempts})")
                if attempt < empty_response_max_attempts:
                    continue
                else:

                    # 达到重试上限，回退但不进入冷却
                    await model_manager.record_call(
                        provider_id, False, f"连续 {empty_response_max_attempts} 次返回空响应"
                    )
                    return None
            else:
                openai_resp = _llm_response_to_openai_chat_completion(llm_resp, requested_model)
                await model_manager.mark_success(provider_id)
                await model_manager.record_call(provider_id, True)
                if log_resp:
                    logger.info(f"[响应日志] Provider={provider_id} 非流式响应: {json.dumps(openai_resp, ensure_ascii=False)[:500]}...")
                return JSONResponse(content=openai_resp, status_code=200)

    async def _stream_with_fallback(
        provider_ids: list[str],
        body: dict,
        log_resp: bool,
        requested_model: str,
        empty_response_max_attempts: int,
    ) -> FallbackStreamResponse:
        """流式请求：在生成器内完成 Provider 回退与空响应重试

        响应头一旦发出就无法切换 Provider，因此所有回退决策都赶在首个内容字节流出前完成；
        全部 Provider 均未产出内容时不 yield 任何字节，由 FallbackStreamResponse 改发 503。

        Args:
            provider_ids: 虚拟模型的 Provider 回退链，按优先级从高到低。
            body: 原始请求体。
            log_resp: 是否打印上游响应日志。
            requested_model: 客户端请求的虚拟模型名。
            empty_response_max_attempts: 空响应原地重试次数。

        Returns:
            自带回退逻辑的流式响应。
        """
        system_prompt, contexts, func_tool, tool_choice, extra_kwargs = _prepare_call(body)

        async def stream_generator() -> AsyncGenerator[bytes, None]:
            id = f"chatcmpl-{int(time.time())}"
            for pid in provider_ids:
                if not await model_manager.is_available(pid):
                    logger.info(f"Provider {pid} 处于冷却中，跳过")
                    continue

                logger.info(f"尝试 Provider: {pid}")
                if not provider_manager:
                    logger.error("ProviderManager 未初始化")
                    continue
                provider = await provider_manager.get_provider_by_id(pid)
                if not provider:
                    logger.warning(f"Provider '{pid}' 不存在")
                    continue
                if not isinstance(provider, Provider):
                    logger.warning(f"Provider '{pid}' 不是 Chat Provider，跳过")
                    continue

                for attempt in range(1, empty_response_max_attempts + 1):
                    content_sent = False
                    final_resp = None
                    try:
                        async for llm_chunk in provider.text_chat_stream(
                            prompt=None,
                            contexts=contexts,
                            system_prompt=system_prompt,
                            func_tool=func_tool,
                            tool_choice=tool_choice,
                            request_max_retries=1,
                            **extra_kwargs,
                        ):
                            if llm_chunk.is_chunk:

                                # 增量 chunk —— 发送内容增量
                                chunk_dict = await _llm_response_to_openai_stream(
                                    llm_chunk, requested_model, id, is_final=False
                                )
                                yield f"data: {json.dumps(chunk_dict, ensure_ascii=False)}\n\n".encode("utf-8")
                                content_sent = True
                            else:

                                # 最终响应 —— 暂存，待确认不是空流后再发送终止 chunk
                                final_resp = llm_chunk

                        # 响应头一旦发出就无法重试或切换 Provider，因此空流判定与终止 chunk
                        # 的发送都必须赶在任何字节流出之前完成
                        if content_sent or (final_resp and final_resp.tools_call_args):

                            # 如果包含工具调用，需要以流式 chunk 形式发送
                            if final_resp and final_resp.tools_call_args:

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

                            # 如果存在 usage，则单独发送一个 usage chunk
                            elif final_resp and final_resp.usage:
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

                            # 客户端收完数据可能立刻断连，此时生成器会在 yield 处被关闭、后续代码不再执行，
                            # 因此成功计数必须在 [DONE] 之前清掉
                            await model_manager.mark_success(pid)
                            await model_manager.record_call(pid, True)

                            # 发送结束标记
                            yield b"data: [DONE]\n\n"

                            if log_resp and final_resp:
                                logger.info(f"[响应日志] Provider={pid} 流式响应完成，内容长度 {len(final_resp.completion_text or '')}")
                            return

                        # 空流：原地重试；耗尽后记录失败（不冷却）并回退到下一个 Provider
                        logger.warning(f"Provider {pid} 流式返回空响应 (尝试 {attempt}/{empty_response_max_attempts})")
                        if attempt < empty_response_max_attempts:
                            continue
                        await model_manager.record_call(
                            pid, False, f"连续 {empty_response_max_attempts} 次流式返回空响应"
                        )
                        break
                    except Exception as e:
                        err_msg = str(e)
                        if content_sent:

                            # 内容已发出，重试会向客户端重复发送内容，也无法切换 Provider
                            logger.warning(f"Provider {pid} 流式请求异常（内容已发出，无法重试）: {e}")
                            await model_manager.record_call(pid, False, err_msg)
                            if not _is_no_cooldown_error(err_msg):
                                await model_manager.mark_cooldown(pid, err_msg)
                            yield b'data: {"error": "provider error"}\n\n'
                            yield b"data: [DONE]\n\n"
                            return

                        # 尚未发出任何内容：completion has no choices 与非流式一致原地重试，
                        # 耗尽后只记录不冷却；其余异常冷却后回退到下一个 Provider
                        if "completion has no choices" in err_msg:
                            logger.warning(f"Provider {pid} 流式请求异常 (尝试 {attempt}/{empty_response_max_attempts}): {e}")
                            if attempt < empty_response_max_attempts:
                                continue
                            await model_manager.record_call(pid, False, err_msg)
                            break

                        logger.warning(f"Provider {pid} 流式请求异常: {e}")
                        await model_manager.record_call(pid, False, err_msg)
                        if not _is_no_cooldown_error(err_msg):
                            await model_manager.mark_cooldown(pid, err_msg)
                        break

        # 所有 Provider 均未产出内容：不 yield 任何字节，由 FallbackStreamResponse 改发 503
        return FallbackStreamResponse(stream_generator())

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
        max_retries = config.empty_response_max_attempts
        log_resp = config.log_response

        if is_stream:
            # 流式：Provider 回退在生成器内完成（响应头发出后无法切换 Provider），
            # 全部不可用时由 FallbackStreamResponse 改发 503
            return await _stream_with_fallback(
                provider_ids, body, log_resp, requested_model, max_retries
            )

        for pid in provider_ids:
            if not await model_manager.is_available(pid):
                logger.info(f"Provider {pid} 处于冷却中，跳过")
                continue

            logger.info(f"尝试 Provider: {pid}")
            resp = await _call_provider(pid, body, log_resp, requested_model, max_retries)
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
