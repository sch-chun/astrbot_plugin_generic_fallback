# Generic Fallback Proxy

一个 AstrBot 插件，提供基于 Provider 回退链的通用 OpenAI 兼容代理服务。

## 功能

- **虚拟模型**：将多个 Provider 组合成一个虚拟模型，对外暴露统一的 OpenAI 兼容端点
- **回退链**：按优先级依次尝试 Provider，失败时自动切换到下一个
- **冷却机制**：Provider 请求失败后进入冷却，冷却时长按「连续失败次数」阶梯递增，可自定义阶梯
- **手动禁用**：可在监控面板随时禁用/解除禁用某个 Provider，被禁用的节点不再参与回退
- **流式支持**：完整支持 SSE 流式响应代理
- **API Key 验证**：可选启用代理层自身的 API Key 认证
- **模型标签**：可选在响应文本前插入 `[Provider名称]` 标识，方便追踪实际使用的模型
- **监控面板**：内置 Web 状态页面，查看冷却状态和虚拟模型配置

## 安装

在 AstrBot 管理面板通过链接（https://github.com/sch-chun/astrbot_plugin_generic_fallback）安装，或手动克隆到插件目录：

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
| `empty_response_max_attempts` | int | `3` | 空响应重试次数，超过后回退到下一个 Provider 但不计入连续失败 |
| `cooldown_policy` | list | `[[1, 1], [2, 5], [5, 30]]` | 连续失败冷却阶梯（分钟），详见下 |
| `log_response` | bool | `false` | 打印上游响应到日志（调试用） |
| `virtual_models` | list | (见下) | 虚拟模型配置列表 |

### virtual_models 配置

每个虚拟模型包含：

- **name**：虚拟模型名称，客户端请求时使用（如 `my-fallback`）
- **provider_ids**：选择作为回退链的 Provider，按优先级从高到低排列
- **timeout**：请求超时时间（秒），默认 120

> ⚠️ 注意：provider_ids 中不要选择虚拟模型自身（插件会自动过滤循环引用并警告）

### cooldown_policy 配置

`cooldown_policy` 是一个嵌套列表，每项为 `[连续失败次数, 冷却分钟数]`，按失败次数升序：

```json
[[1, 1], [2, 5], [5, 30]]
```

表示：连续失败 **1 次**冷却 **1 分钟**、连续失败 **2-4 次**冷却 **5 分钟**、连续失败 **5 次及以上**冷却 **30 分钟**。
某档的冷却时长一直生效到下一档的阈值之前；连续失败次数低于首档阈值时按首档处理（例如 `[[3, 10]]` 表示无论失败几次都冷却 10 分钟）。

- 请求成功会**清零**该 Provider 的连续失败次数。
- 冷却时长支持小数（如 `0.5` 表示 30 秒），填 `0` 表示不冷却。
- 也接受等价的 JSON 字符串（如 `"[[1, 1], [2, 5]]"`）。
- 空响应重试耗尽、`completion has no choices`、内容安全过滤、415 `Unsupported Media Type` 这几种情况**不计数、不冷却**，只会回退到下一个 Provider（后两者属于上游明确拒绝，重试无意义，不该惩罚 Provider）。
- 配置非法（不是列表、某项不是两项、阈值非正整数或重复、分钟数为负或非数字）时，插件会在日志里报错并**回退为默认值** `[[1, 1], [2, 5], [5, 30]]`。

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

插件内置监控面板，可在 AstrBot 管理面板的插件页面查看，显示各 Provider 的冷却状态、连续失败次数和虚拟模型配置信息。

面板上每个 Provider 卡片都有一个**禁用 / 解除禁用**按钮：

- 禁用后该 Provider 立即不参与回退（状态显示为「已禁用」），直到手动解除；
- 解除禁用会同时清空它的冷却与连续失败次数；
- 该状态仅保存在内存中，插件重载或 AstrBot 重启后会恢复为全部可用。

面板顶部会显示当前生效的冷却阶梯（如 `1 次→1 分钟，2 次→5 分钟，5 次→30 分钟`）。

## 测试

在插件目录下执行（需先安装 `pytest` 与 `pytest-asyncio`，并把 AstrBot 所在目录加入 `PYTHONPATH`）：

```bash
pytest -q
```

## 许可

GNU Affero General Public License v3.0
