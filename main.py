"""Generic Fallback Proxy — AstrBot 插件版 v0.0.2
基于 Provider 回退的通用代理服务，支持用户从管理面板选择多个 Provider 作为回退链。
"""
import asyncio
import threading
import socket
import time
import uvicorn
from fastapi import FastAPI
import yaml
from pathlib import Path

from typing import Optional, Callable, Union
from fastapi.responses import JSONResponse
from quart.wrappers import Response

from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig

# 兼容旧版 json_response
try:
    from astrbot.api.web import json_response
except ImportError:
    from quart import jsonify as json_response

from .src.config import ProxyConfig
from .src.model_manager import ModelManager
from .src.api_proxy import create_proxy_router


@register(
    "generic_fallback",
    "sch-chun",
    "通用回退代理插件（基于 Provider 回退链）",
    "0.0.2",
    "https://github.com/sch-chun/astrbot_plugin_generic_fallback"
)
class GenericFallbackProxyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self._uvicorn_server: Optional[uvicorn.Server] = None
        self._server_thread: Optional[threading.Thread] = None
        self._model_manager: Optional[ModelManager] = None
        self._proxy_config: Optional[ProxyConfig] = None
        self._fastapi_app: Optional[FastAPI] = None
        self._virtual_models: list[dict] = []
        self.config: AstrBotConfig = config
        self._close_http_client: Optional[Callable] = None

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
        show_model_tag = bool(self.config.get("show_model_tag", False))
        log_response = bool(self.config.get("log_response", False))

        # 3. 创建 ProxyConfig
        self._proxy_config = ProxyConfig(
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            proxy_api_key=proxy_api_key,
            show_model_tag=show_model_tag,
            log_response=log_response,
            virtual_models=self._virtual_models,
        )

        # 4. 初始化模型管理器
        self._model_manager = ModelManager()

        # 5. 注册 Web API
        self.context.register_web_api(
            f"/{self._plugin_name}/status",
            self.status_handler,
            ["GET"],
            "获取代理状态"
        )

        # 6. 创建 FastAPI 应用
        self._fastapi_app = FastAPI(title="Generic Fallback", version="1.0.0")

        provider_manager = self.context.provider_manager if hasattr(self.context, "provider_manager") else None
        if not provider_manager:
            logger.warning("⚠️ ProviderManager 不可用，回退将无法工作（请检查 AstrBot 版本）")

        proxy_router, self._close_http_client = create_proxy_router(
            config=self._proxy_config,
            model_manager=self._model_manager,
            virtual_models=self._virtual_models,
            provider_manager=provider_manager,
        )
        self._fastapi_app.include_router(proxy_router)

        # 7. 启动 uvicorn
        if not self._start_uvicorn():
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
        return json_response(status)

    def _start_uvicorn(self) -> bool:
        assert self._fastapi_app is not None
        assert self._proxy_config is not None
        host = self._proxy_config.proxy_host
        port = self._proxy_config.proxy_port

        # 端口重试
        for attempt in range(5):
            if self._is_port_available(port, host):
                break
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
        )
        self._uvicorn_server = uvicorn.Server(config=config)
        self._server_thread = threading.Thread(
            target=self._uvicorn_server.run,
            daemon=True,
            name="generic-fallback",
        )
        self._server_thread.start()
        return True

    def _is_port_available(self, port: int, host: str = "127.0.0.1") -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, port))
                return True
        except OSError:
            return False

    async def terminate(self) -> None:
        logger.info("正在关闭通用回退代理服务...")
        if self._uvicorn_server:
            self._uvicorn_server.should_exit = True
            self._uvicorn_server = None
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=5)
        await asyncio.sleep(0.5)
        if self._close_http_client:
            try:
                await self._close_http_client()
            except Exception as e:
                logger.warning(f"关闭 HTTP 客户端时异常: {e}")
        logger.info("👋 通用回退代理服务已关闭")