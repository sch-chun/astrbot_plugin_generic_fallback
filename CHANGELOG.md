# Changelog

All notable changes to this project will be documented in this file.

## [0.0.7] - 2026-09-30

### Added
- 新增 `cooldown_policy` 配置：以嵌套列表 `[[连续失败次数, 冷却分钟数], ...]` 定义阶梯冷却，
  默认 `[[1, 1], [2, 5], [5, 30]]`（连续失败 1 次冷却 1 分钟、2-4 次 5 分钟、5 次及以上 30 分钟）
- 连续失败计数：请求成功会清零对应 Provider 的连续失败次数
- 监控面板支持随时禁用/解除禁用单个 Provider，并显示连续失败次数与当前冷却阶梯
- 新增 pytest 测试套件（冷却策略校验、阶梯冷却、禁用/启用、代理路由）

### Changed
- 冷却时长不再是固定的 60 秒，改为按配置的阶梯计算；冷却日志会打印连续失败次数与实际冷却分钟数
- 请求返回 415 `Unsupported Media Type` 时只回退、不进入冷却（与内容安全过滤一致：这类上游拒绝重试无意义，不该惩罚 Provider）
- 监控面板：禁用按钮移到卡片右下角、与状态点同一行（模型名独占一行，不再被挤压），去掉按钮与标题的 emoji，「解除禁用」改为「解禁」
- `/v1/status` 与插件 `status` 接口新增 `disabled_list`、`fail_counts`、`cooldown_policy` 字段
- 状态接口的冷却条目新增 `consecutive_failures` 字段

### Fixed
- 配置列表不再被静默接受：非法 `cooldown_policy` 会在日志报错并回退为默认值
- README 配置表移除已删除的 `show_model_tag`，补充 `empty_response_max_attempts` 与 `cooldown_policy`
- 流式请求遇到 `completion has no choices` 不再进入冷却，与非流式行为保持一致
- 流式成功计数移到 `data: [DONE]` 之前，客户端收完数据立刻断连也能清零连续失败次数
- 禁用接口校验 `provider_id` 必须在虚拟模型回退链中，脏 id 不会再攒进 `disabled_list`
- 监控面板用自建模态框替代 `alert()`（插件页跑在 iframe 沙箱里，`alert()` 会被拦截）

## [0.0.6] - 2026-08-14

### Changed
- 现在请求因内容安全被过滤时不会触发冷却

## [0.0.5] - 2026-08-14

### Fixed
- 修复了无法正常捕获 no choices 错误的问题

## [0.0.4] - 2026-08-11

### Changed
- 删除了全局重试次数，增加了非流式空响应时的原地重试配置
- 增强流式处理器内部异常处理

## [0.0.3] - 2026-08-08

### Added
- 初始版本发布