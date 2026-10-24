# Changelog

All notable changes to this project will be documented in this file.

## [0.1.2] - 2026-10-24

### Added
- 自定义规则「冷却至次日 00:00」动作新增**每条规则独立的时区字段 `tz`**：留空或 `local` 按本机时区；`UTC` / `GMT` 按世界标准时；支持带前缀的偏移写法（`UTC+8`、`UTC +8`、`GMT-5.5`、`UTC+05:30`，冒号表分钟、`.5` 表半小时）以及裸偏移小时数（如 `-8`、`+9`、`+5.5`），均按对应时区次日 00:00 重置额度。回退链中不同供应商额度常不在同一时区清零，可给每条规则单独设置（非法时区在保存规则时即报错；前后端解析完全对齐）
- 插件图标改用反色版（`#E0E1CC` 底 / `#1F1E33` 图形），与 TTS Enhancer、Watchdoc 同属一个双色家族；深色版保留为 `logo-dark.png` 备选
- 监控面板自定义规则支持编辑已有规则：规则列表每条提供「编辑」入口，可回填表单后原地更新；「连续次数」输入框默认填 1；选中「冷却到次日 00:00」动作时以灰字给出时区格式说明（留空/local 按本机时区，UTC/GMT 世界标准时，或偏移小时数如 -8、+9、+5.5、UTC+08:30）

### Removed
- 移除「打印上游响应到日志」配置 `log_response`：AstrBot 本体 Debug 模式已打印完整响应，插件重复打印属冗余

## [0.1.1] - 2026-10-23

### Fixed
- 修复 Gemini Function Call 因缺少 `thought_signature` 被上游拒绝（`400 INVALID_ARGUMENT`）的问题。插件把 `LLMResponse` 转 OpenAI 格式时漏传 `tools_call_extra_content`（思考签名），导致下一轮回传函数调用缺签名；现在三处 `tool_calls` 构造（非流式响应、流式增量、流式 final chunk）均透传 `extra_content`，与 AstrBot 核心 `LLMResponse.to_openai_tool_calls` 保持一致

## [0.1.0] - 2026-10-19

### Fixed
- 修复插件重载后首批 Provider 调用集中报 `Event loop is closed` 并被误标记冷却的问题。根因：代理服务（uvicorn）此前跑在独立线程的事件循环里，Provider（AstrBot 核心单例，跨插件重载存活）的 HTTP 连接池会把 keep-alive 连接绑定到该线程的循环；重载时循环关闭、池里留下僵尸连接，重载后首批请求复用它们即抛 `RuntimeError: Event loop is closed`——被回退逻辑的异常分支吞掉，记为一次失败并把 Provider 拖进冷却，表现为「集中爆发一遍然后正常」。现在 `serve()` 作为任务运行在 AstrBot 主事件循环上（与 AstrBot 仪表盘同循环），连接池随主循环存活，重载不再产生僵尸连接；`terminate()` 同步改为优雅等待在途请求完成（超时 10 秒后强制取消），不再以阻塞式 `join(timeout=5)` 放弃收尾，端口保证释放

### Changed
- 启动流程改为轮询代理服务实际 listen 状态（uvicorn `server.started`）后再打「已启动」日志，替代此前的固定 50ms 等待；慢机器上日志顺序不再早于端口真正可连接
- 启动时端口被占用的提示去除「旧代理线程阻塞」相关表述（重载流程已保证旧实例完全关闭后才会启动新实例，重试逻辑仅作兜底）

## [0.0.8] - 2026-10-18

### Added
- 监控面板支持为每个 Provider 配置可选的自定义规则：失败原因包含指定匹配内容（不区分大小写）并连续命中设定次数时，触发「冷却指定分钟 / 冷却到第二天 00:00 / 禁用」三种动作之一；规则命中时跳过内置冷却阶梯。规则与实时命中数（x/N）在卡片徽章与规则模态框中展示，达到阈值的规则高亮提示；规则保存在插件 KV 中，重启后自动恢复，单条规则非法只丢弃该条。新增 `POST /generic_fallback/rules/set` 接口，`status` 接口新增 `custom_rules` 字段
- 监控面板支持查看单个 Provider 的近期调用日志（时间、成败、失败原因）：Provider 卡片新增「日志」按钮，
  数据经 `GET /generic_fallback/logs?provider_id=...` 读取，内存中最多保留最近 200 条

### Changed
- 监控面板 Provider 卡片：剩余冷却时间与连续失败次数合并为同一行展示；配置了自定义规则的 Provider 显示「N 条自定义规则」徽章
- 启动时端口被占用的提示补充可能原因与排查命令（刚重载过插件时，疑似上次尚未返回的模型调用仍阻塞旧代理线程持有端口）
- 监控面板视觉优化：首次加载占位与错峰淡入、模态框弹出/关闭过渡动画，`prefers-reduced-motion` 下全部降级为瞬时切换；Provider 卡片改为左信息列 + 右按钮列双列布局（长 Provider ID 自动换行不撑破卡片），操作按钮按语义分色（禁用红 / 解禁绿 / 规则蓝 / 日志灰）

### Fixed
- 流式请求支持 Provider 回退：此前流式分支总是直接返回响应，路由的回退循环对流式完全不生效，第一个 Provider 失败后不会尝试下一个。现在 Provider 循环搬进流式生成器，在首个内容字节流出前完成全部回退决策；所有 Provider 均未产出内容时返回 503 JSON（状态码与非流式一致，此前为 200 + SSE 错误 chunk），响应头延迟到首个内容字节发出
- 流式空响应不再静默按成功处理：此前流式路径只调用一次上游，空流（无任何内容 chunk）会被标记为成功并清零连续失败计数；现在与非流式一致原地重试（默认 3 次），耗尽后记录失败、不进入冷却，以错误 chunk 收尾。`completion has no choices` 异常同样改为原地重试；内容已发出后的异常仍只记录并以错误 chunk 收尾（重试会向客户端重复发送内容）
- `ModelManager` 的锁由 `asyncio.Lock` 改为 `threading.Lock`：代理服务跑在独立线程的事件循环里，
  监控面板（AstrBot 主循环）与代理请求（线程循环）会并发操作 `ModelManager`，`asyncio.Lock`
  会绑定到首个进入慢路径的事件循环，另一循环再竞争即抛跨循环 `RuntimeError`，或因跨线程唤醒
  丢失把请求拖住数秒。临界区均为无 `await` 的内存字典操作，`threading.Lock` 与事件循环无关，
  且提供真正的跨线程互斥

## [0.0.7] - 2026-09-30

### Added
- 新增 `cooldown_policy` 配置：以嵌套列表 `[[连续失败次数, 冷却分钟数], ...]` 定义阶梯冷却，
  默认 `[[1, 1], [2, 5], [5, 30]]`（连续失败 1 次冷却 1 分钟、2-4 次 5 分钟、5 次及以上 30 分钟）
- 连续失败计数：请求成功会清零对应 Provider 的连续失败次数
- 监控面板支持随时禁用/解除禁用单个 Provider，并显示连续失败次数与当前冷却阶梯
- 新增 pytest 测试套件（冷却策略校验、阶梯冷却、禁用/启用、代理路由）
- 新增 GitHub Actions CI：Python 3.12 / 3.13 跑 pytest，ruff 静态检查，面板 JS 语法检查

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
