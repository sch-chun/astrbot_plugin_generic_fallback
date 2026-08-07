def _create_proxy_http_client(provider_config: dict, timeout: int) -> httpx.AsyncClient:
    """根据 Provider 配置创建带代理的 HTTP 客户端，完全照搬 AstrBot 的逻辑"""
    proxy = provider_config.get("proxy", "")
    if proxy:
        return httpx.AsyncClient(
            proxy=proxy,
            timeout=httpx.Timeout(timeout, connect=30.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
        )
    else:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=30.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20)
        )