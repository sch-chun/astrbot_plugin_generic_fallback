from dataclasses import dataclass, field


@dataclass
class ProxyConfig:
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 3474
    proxy_api_key: str = ""
    show_model_tag: bool = False
    log_response: bool = False
    virtual_models: list[dict] = field(default_factory=list)