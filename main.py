"""Generic Fallback Proxy — AstrBot 插件版
基于 Provider 回退的通用代理服务，支持用户从管理面板选择多个 Provider 作为回退链。
"""
import asyncio
import contextlib
import socket
import time
import uvicorn
from fastapi import FastAPI
import yaml
from pathlib import Path

from typing import Optional, Union
from fastapi.responses import JSONResponse
from quart.wrappers import Response

from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig

# 兼容旧版 json_response
try:
    from astrbot.api.web import json_response
    from astrbot.api.web import request as web_request
except ImportError:
    from quart import jsonify as json_response
    from quart import request as web_request

from .src.config import DEFAULT_COOLDOWN_POLICY, ProxyConfig, normalize_cooldown_policy
from .src.model_manager import ModelManager
from .src.api_proxy import create_proxy_router

# 自定义规则在插件 KV 中的存储键
RULES_KV_KEY = "custom_rules"

# 优雅关闭上限（秒）：插件重载/卸载时给在途 LLM 调用留完成时间，
# 超时后 uvicorn 会取消在途任务，保证端口一定被释放
GRACEFUL_SHUTDOWN_TIMEOUT = 10

# 启动等待上限（秒）与轮询间隔（秒）：uvicorn 从 create_task 到真正 bind+listen
# 实测 150~300ms，慢机器更久。轮询 server.started 而非魔法数 sleep——正常路径
# 一等就退，只有异常慢启动才会耗到上限，且不会在服务没起来时误打「已启动」日志
STARTUP_WAIT_TIMEOUT = 2.0
STARTUP_POLL_INTERVAL = 0.01


class _MainLoopProxyServer(uvicorn.Server):
    """运行在 AstrBot 主事件循环上的代理 Server。

    uvicorn 默认会在 serve() 期间捕获 SIGINT/SIGTERM 并自行退出；信号所有权归 AstrBot，
    这里禁掉捕获——代理服务的退出完全由插件 terminate() 置 should_exit 驱动。
    """

    def capture_signals(self):
        return contextlib.nullcontext()


class GenericFallbackProxyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._serve_task: Optional[asyncio.Task] = None
        self._model_manager: Optional[ModelManager] = None
        self._proxy_config: Optional[ProxyConfig] = None
        self._fastapi_app: Optional[FastAPI] = None
        self._virtual_models: list[dict] = []
        self.config: AstrBotConfig = config

        meta_path = Path(__file__).parent / "metadata.yaml"
        plugin_name = "generic_fallback"
        if meta_path.exists():
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = yaml.safe_load(f)
                    self._plugin_name = meta.get("name", plugin_name)
            except Exception as e:
                logger.error(f"读取插件元数据失败: {e}")
        self._plugin_name = plugin_name

    async def initialize(self) -> None:
        """初始化：读取配置，启动代理服务"""

        # 1. 检查 virtual_models 配置
        virtual_models_raw = self.config.get("virtual_models", [])
        if not virtual_models_raw:
            logger.error("❌ virtual_models 未配置！请在管理面板中至少配置一个虚拟模型。")
            return

        # 解析并过滤有效配置（至少有一个 provider_ids）
        self._virtual_models = []
        for v in virtual_models_raw:
            name = v.get("name", "")
            provider_ids = v.get("provider_ids", [])
            if not provider_ids:
                logger.warning(f"虚拟模型 '{name}' 的 provider_ids 为空，跳过")
                continue
            self._virtual_models.append({
                "name": name,
                "provider_ids": provider_ids,
            })

        # 过滤循环引用
        virtual_names = [v["name"] for v in self._virtual_models]
        filtered_models = []
        for v in self._virtual_models:
            name = v["name"]
            original_ids = v["provider_ids"]

            # 移除所有虚拟模型名称（包括自身）
            filtered_ids = [pid for pid in original_ids if pid.split("/")[-1] not in virtual_names]
            removed = [pid for pid in original_ids if pid not in filtered_ids]
            if removed:
                logger.warning(f"虚拟模型 '{name}' 的 provider_ids 中包含循环引用，已移除: {removed}")
            if not filtered_ids:
                logger.warning(f"虚拟模型 '{name}' 过滤后没有有效 Provider，将跳过该虚拟模型")
                continue

            v["provider_ids"] = filtered_ids
            filtered_models.append(v)

        self._virtual_models = filtered_models

        if not self._virtual_models:
            logger.error("❌ 解析后无有效的虚拟模型配置，代理服务不会启动")
            return

        # 2. 读取其他配置
        proxy_port = int(self.config.get("proxy_port", 3474))
        proxy_host = self.config.get("proxy_host", "127.0.0.1")
        proxy_api_key = self.config.get("proxy_api_key", "")
        empty_response_max_attempts = int(self.config.get("empty_response_max_attempts", 3))
        until_midnight_tz = self.config.get("until_midnight_tz", "")

        # 冷却阶梯：[[连续失败次数, 冷却分钟数], ...]，非法配置回退到默认值
        try:
            cooldown_policy = normalize_cooldown_policy(
                self.config.get("cooldown_policy", DEFAULT_COOLDOWN_POLICY)
            )
        except ValueError as e:
            logger.error(f"❌ cooldown_policy 配置无效（{e}），将使用默认策略 {DEFAULT_COOLDOWN_POLICY}")
            cooldown_policy = normalize_cooldown_policy(DEFAULT_COOLDOWN_POLICY)

        # 3. 创建 ProxyConfig
        self._proxy_config = ProxyConfig(
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_api_key=proxy_api_key,
            virtual_models=self._virtual_models,
            empty_response_max_attempts=empty_response_max_attempts,
            cooldown_policy=cooldown_policy
        )

        # 4. 初始化模型管理器
        self._model_manager = ModelManager(
            cooldown_policy=cooldown_policy, until_midnight_tz=until_midnight_tz
        )

        # 4.5 从 KV 加载持久化的自定义规则（读取失败不影响代理启动，仅丢失规则）
        try:
            stored_rules = await self.get_kv_data(RULES_KV_KEY, {})
        except Exception as e:
            logger.warning(f"读取自定义规则失败，本次将以空规则启动: {e}")
            stored_rules = {}
        await self._model_manager.set_rules_map(stored_rules)

        # 5. 注册 Web API
        self.context.register_web_api(
            f"/{self._plugin_name}/status",
            self.status_handler,
            ["GET"],
            "获取代理状态"
        )
        self.context.register_web_api(
            f"/{self._plugin_name}/provider/set_disabled",
            self.set_provider_disabled_handler,
            ["POST"],
            "禁用或解除禁用 Provider"
        )
        self.context.register_web_api(
            f"/{self._plugin_name}/rules/set",
            self.set_rules_handler,
            ["POST"],
            "设置指定 Provider 的自定义规则"
        )
        self.context.register_web_api(
            f"/{self._plugin_name}/logs",
            self.logs_handler,
            ["GET"],
            "获取指定 Provider 的近期调用记录"
        )

        # 6. 创建 FastAPI 应用
        self._fastapi_app = FastAPI(title="Generic Fallback", version="1.0.0")

        provider_manager = self.context.provider_manager if hasattr(self.context, "provider_manager") else None
        if not provider_manager:
            logger.warning("⚠️ ProviderManager 不可用，回退将无法工作（请检查 AstrBot 版本）")

        proxy_router = create_proxy_router(
            config=self._proxy_config,
            model_manager=self._model_manager,
            virtual_models=self._virtual_models,
            provider_manager=provider_manager,
        )
        self._fastapi_app.include_router(proxy_router)

        # 7. 启动 uvicorn
        if not self._start_uvicorn():
            return

        # serve() 是主循环上的任务，启动期错误（如端口绑定失败）异步抛出——
        # 轮询 server.started（uvicorn 在 create_server 成功后置位）而非魔法数
        # sleep：既等足 listen 延迟，也不会在服务没起来时误打「已启动」日志
        # （具体报错由 _on_serve_task_done 统一记录，这里只做状态清理）
        for _ in range(int(STARTUP_WAIT_TIMEOUT / STARTUP_POLL_INTERVAL)):
            if (
                self._serve_task is None
                or self._serve_task.done()
                or self._uvicorn_server is None
                or self._uvicorn_server.started
            ):
                break
            await asyncio.sleep(STARTUP_POLL_INTERVAL)
        if self._serve_task is not None and self._serve_task.done() and not self._serve_task.cancelled():
            if self._serve_task.exception() is not None:
                self._serve_task = None
                self._uvicorn_server = None
                return

        logger.info(f"✅ 通用回退代理服务已启动 (地址: {proxy_host}:{proxy_port})")
        if proxy_api_key:
            logger.info("   🔑 API Key 验证已启用")
        else:
            logger.info("   ⚠️ 未启用 API Key 验证，建议设置 proxy_api_key")
        logger.info(f"   虚拟模型数量: {len(self._virtual_models)}")
        for v in self._virtual_models:
            logger.info(f"   - {v.get('name')}: {len(v.get('provider_ids', []))} 个 Provider")

    async def status_handler(self) -> Union[Response, JSONResponse]:
        """返回当前状态"""
        if not self._model_manager:
            return json_response({"error": "服务未初始化"})
        status = await self._model_manager.get_status()
        status["virtual_models"] = [
            {
                "name": v.get("name"),
                "provider_ids": v.get("provider_ids", []),
            }
            for v in self._virtual_models
        ]
        status["custom_rules"] = await self._model_manager.get_all_rules()
        return json_response(status)

    async def logs_handler(self) -> Union[Response, JSONResponse]:
        """返回指定 Provider 的近期调用记录（时间、成败、失败原因）"""
        if not self._model_manager:
            return json_response({"error": "服务未初始化"})

        provider_id = web_request.query.get("provider_id", "")
        if not isinstance(provider_id, str) or not provider_id.strip():
            return json_response({"error": "provider_id 不能为空"})
        provider_id = provider_id.strip()

        logs = await self._model_manager.get_call_log(provider_id)
        return json_response({"provider_id": provider_id, "logs": logs})

    async def set_provider_disabled_handler(self) -> Union[Response, JSONResponse]:
        """处理监控面板的禁用/解除禁用请求"""
        if not self._model_manager:
            return json_response({"ok": False, "error": "服务未初始化"})

        payload = await self._read_json_body()
        provider_id = payload.get("provider_id")
        disabled = payload.get("disabled")

        if not isinstance(provider_id, str) or not provider_id.strip():
            return json_response({"ok": False, "error": "provider_id 不能为空"})
        if not isinstance(disabled, bool):
            return json_response({"ok": False, "error": "disabled 必须是布尔值"})

        provider_id = provider_id.strip()

        # 只允许操作回退链里真实存在的 Provider，避免脏 id 攒进 disabled_list
        known_ids = {pid for v in self._virtual_models for pid in v.get("provider_ids", [])}
        if provider_id not in known_ids:
            return json_response({"ok": False, "error": f"provider_id '{provider_id}' 不在任何虚拟模型的回退链中"})

        await self._model_manager.set_disabled(provider_id, disabled)
        return json_response({"ok": True, "provider_id": provider_id, "disabled": disabled})

    async def set_rules_handler(self) -> Response | JSONResponse:
        """处理监控面板的自定义规则保存请求（整体替换指定 Provider 的规则）"""
        if not self._model_manager:
            return json_response({"ok": False, "error": "服务未初始化"})

        payload = await self._read_json_body()
        provider_id = payload.get("provider_id")
        rules = payload.get("rules")

        if not isinstance(provider_id, str) or not provider_id.strip():
            return json_response({"ok": False, "error": "provider_id 不能为空"})
        provider_id = provider_id.strip()

        # 只允许操作回退链里真实存在的 Provider，避免脏 id 攒进规则表
        known_ids = {pid for v in self._virtual_models for pid in v.get("provider_ids", [])}
        if provider_id not in known_ids:
            return json_response({"ok": False, "error": f"provider_id '{provider_id}' 不在任何虚拟模型的回退链中"})

        try:
            saved = await self._model_manager.set_rules(provider_id, rules)
        except ValueError as e:
            return json_response({"ok": False, "error": str(e)})

        await self._persist_rules()
        return json_response({"ok": True, "provider_id": provider_id, "rules": saved})

    async def _persist_rules(self) -> None:
        """把当前规则写入 KV（持久化失败只告警，不影响已生效的内存状态）"""
        try:
            await self.put_kv_data(RULES_KV_KEY, await self._model_manager.export_rules())
        except Exception as e:
            logger.warning(f"自定义规则持久化失败，重启后将丢失本次修改: {e}")

    async def _read_json_body(self) -> dict:
        """读取插件 Web API 的请求体并解析为 dict"""
        try:
            data = await web_request.json(default={})
        except (AttributeError, TypeError):
            data = await web_request.get_json(silent=True) or {}
        return data if isinstance(data, dict) else {}

    def _start_uvicorn(self) -> bool:
        assert self._fastapi_app is not None
        assert self._proxy_config is not None
        host = self._proxy_config.proxy_host
        port = self._proxy_config.proxy_port

        # 端口重试：terminate() 会等优雅关闭完成、端口必然已释放，正常情况下不会命中；
        # 这里是兜底——端口被其他进程占用时自动重试数次，持续失败则明确报错
        for attempt in range(5):
            if self._is_port_available(port, host):
                break
            if attempt == 0:
                logger.warning(
                    f"端口 {host}:{port} 被占用（可能有其他进程占用，或上次关闭尚未完全结束），"
                    "下面会自动重试数次，若持续失败可查看占用进程："
                    f"netstat -ano | findstr :{port}（Windows）或 ss -ltnp | grep {port}（Linux）"
                )
            if attempt < 4:
                logger.warning(f"端口 {host}:{port} 被占用，等待 0.5 秒后重试 ({attempt+1}/5)")
                time.sleep(0.5)
            else:
                logger.error(f"❌ 端口 {host}:{port} 已被占用，请修改配置")
                return False

        config = uvicorn.Config(
            app=self._fastapi_app,
            host=host,
            port=port,
            log_level="info",
            loop="asyncio",
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT,
        )
        self._uvicorn_server = _MainLoopProxyServer(config=config)
        # 关键：serve() 作为任务跑在 AstrBot 主事件循环上，而不是独立线程的循环。
        # Provider 的 HTTP 连接池（httpx/httpcore keep-alive）随主循环存活，插件重载时
        # 不会留下「绑定到已关闭循环」的僵尸连接——否则重载后首批请求会集中报
        # RuntimeError: Event loop is closed，被 _call_provider 的 except 吞掉，
        # 还把 Provider 误标记进冷却。
        self._serve_task = asyncio.get_running_loop().create_task(
            self._uvicorn_server.serve(),
            name="generic-fallback-proxy",
        )
        self._serve_task.add_done_callback(self._on_serve_task_done)
        return True

    def _on_serve_task_done(self, task: "asyncio.Task") -> None:
        """serve() 任务非正常退出时留痕（正常关闭与主动取消不记）"""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        server = self._uvicorn_server
        if server is not None and not server.started:
            logger.error(f"❌ 代理服务启动失败: {exc}")
        else:
            logger.error(f"代理服务异常退出: {exc}")

    def _is_port_available(self, port: int, host: str = "127.0.0.1") -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
                return True
        except OSError:
            return False

    async def terminate(self) -> None:
        logger.info("正在关闭通用回退代理服务...")
        server = self._uvicorn_server
        task = self._serve_task
        if server is not None and task is not None and not task.done():
            server.should_exit = True
            # 等优雅关闭真正完成（在途请求有机会发完响应、端口一定释放）后再返回；
            # 等待是 await 而非阻塞式 join，主循环上的其他任务不受影响
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=GRACEFUL_SHUTDOWN_TIMEOUT + 5
                )
            except asyncio.TimeoutError:
                logger.warning("代理服务优雅关闭超时，强制取消在途请求")
                server.force_exit = True
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except asyncio.TimeoutError:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        self._uvicorn_server = None
        self._serve_task = None
        logger.info("👋 通用回退代理服务已关闭")
