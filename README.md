# Generic Fallback Proxy

一个 AstrBot 插件，提供基于 Provider 回退链的通用 OpenAI 兼容代理服务。

## 功能

- **虚拟模型**：将多个 Provider 组合成一个虚拟模型，对外暴露统一的 OpenAI 兼容端点
- **回退链**：按优先级依次尝试 Provider，失败时自动切换到下一个
- **冷却机制**：Provider 请求失败后进入 60 秒冷却，避免反复重试失效节点
- **流式支持**：完整支持 SSE 流式响应代理
- **API Key 验证**：可选启用代理层自身的 API Key 认证
- **模型标签**：可选在响应文本前插入 `[Provider名称]` 标识，方便追踪实际使用的模型
- **监控面板**：内置 Web 状态页面，查看冷却状态和虚拟模型配置

## 安装

在 AstrBot 管理面板的插件市场中搜索 `generic_fallback` 安装，或手动克隆到插件目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/sch-chun/astrbot_plugin_generic_fallback.git
```

## 配置

安装后在管理面板的插件配置中设置以下项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `proxy_host` | string | `127.0.0.1` | 代理服务监听地址，建议保持本机 |
| `proxy_port` | int | `3474` | 代理服务监听端口 |
| `proxy_api_key` | string | (空) | 代理层 API Key，留空则不验证 |
| `show_model_tag` | bool | `false` | 在回复前插入 `[Provider名称]` 标识 |
| `log_response` | bool | `false` | 打印上游响应到日志（调试用） |
| `virtual_models` | list | (见下) | 虚拟模型配置列表 |

### virtual_models 配置

每个虚拟模型包含：

- **name**：虚拟模型名称，客户端请求时使用（如 `my-fallback`）
- **provider_ids**：选择作为回退链的 Provider，按优先级从高到低排列
- **timeout**：请求超时时间（秒），默认 120

> ⚠️ 注意：provider_ids 中不要选择虚拟模型自身（插件会自动过滤循环引用并警告）

## 使用

### 在 AstrBot 中配置 Provider

插件启动后，在 AstrBot 的 OpenAI 提供商配置中填入：

- **API Base URL**：`http://127.0.0.1:3474/v1`（根据 proxy_host/proxy_port 调整）
- **API Key**：填写 `proxy_api_key` 设置的值（如未设置则留空，但 OpenAI 提供商要求非空，可填任意占位值如 `no-key`）
- **模型名称**：填写 virtual_models 中配置的虚拟模型 name（如 `my-fallback`）

### 外部调用

代理服务提供标准 OpenAI 兼容端点：

```bash
# 列出虚拟模型
curl http://127.0.0.1:3474/v1/models

# 聊天补全
curl http://127.0.0.1:3474/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-proxy-api-key" \
  -d '{"model": "my-fallback", "messages": [{"role": "user", "content": "你好"}]}'

# 查看状态
curl http://127.0.0.1:3474/v1/status
```

## 监控

插件内置监控面板，可在 AstrBot 管理面板的插件页面查看，显示各 Provider 的冷却状态和虚拟模型配置信息。

## 版本

v0.0.1 - 初版，基础回退代理功能

## 许可

GNU Affero General Public License v3.0