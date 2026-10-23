import threading
import uuid
from collections import deque
from datetime import datetime, time, timedelta, timezone
from typing import Any

from astrbot.api import logger

from .config import (
    DEFAULT_COOLDOWN_POLICY,
    normalize_cooldown_policy,
    resolve_cooldown_minutes,
)

# 调用日志环形缓冲容量：仅供人工排查近期调用，够用即可，避免无限增长
CALL_LOG_MAX_ENTRIES = 200

# 自定义规则支持的动作：冷却指定分钟数 / 冷却至次日 00:00 / 禁用
RULE_ACTIONS = ("cooldown", "until_midnight", "disable")
# 匹配内容长度上限：错误信息片段，100 字符足以定位，避免无意义的长串
RULE_PATTERN_MAX_LENGTH = 100


def _normalize_rule(rule: Any, index: int, seen_ids: set[str]) -> dict:
    """校验并归一化单条自定义规则

    Args:
        rule: 待校验的规则（通常来自 Web API 请求体）。
        index: 规则在列表中的序号（从 1 开始），仅用于错误消息。
        seen_ids: 已占用的规则 ID 集合，用于避免 ID 冲突。

    Returns:
        归一化后的规则，含 id / pattern / threshold / action / cooldown_minutes 字段。

    Raises:
        ValueError: 规则不合法，消息为中文，可直接透传给前端。
    """
    if not isinstance(rule, dict):
        raise ValueError(f"第 {index} 条规则必须是对象")
    pattern = rule.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError(f"第 {index} 条规则的匹配内容不能为空")
    pattern = pattern.strip()
    if len(pattern) > RULE_PATTERN_MAX_LENGTH:
        raise ValueError(f"第 {index} 条规则的匹配内容不能超过 {RULE_PATTERN_MAX_LENGTH} 个字符")
    threshold = rule.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 1:
        raise ValueError(f"第 {index} 条规则的连续次数必须是大于 0 的整数")
    action = rule.get("action")
    if action not in RULE_ACTIONS:
        raise ValueError(f"第 {index} 条规则的动作必须是 {' / '.join(RULE_ACTIONS)} 之一")
    cooldown_minutes = None
    tz = ""
    if action == "cooldown":
        cooldown_minutes = rule.get("cooldown_minutes", 10)
        if (
            isinstance(cooldown_minutes, bool)
            or not isinstance(cooldown_minutes, (int, float))
            or cooldown_minutes <= 0
        ):
            raise ValueError(f"第 {index} 条规则的冷却分钟数必须是大于 0 的数字")
    elif action == "until_midnight":
        tz = rule.get("tz", "")
        if not isinstance(tz, str):
            raise ValueError(f"第 {index} 条规则的时区必须是字符串")
        tz = tz.strip()
        if tz != "":
            try:
                _parse_midnight_tz(tz)
            except ValueError as exc:
                raise ValueError(f"第 {index} 条规则的时区无效：{exc}")
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip() or len(rule_id.strip()) > 64:
        rule_id = f"r-{uuid.uuid4().hex[:8]}"
    else:
        rule_id = rule_id.strip()
    while rule_id in seen_ids:
        rule_id = f"r-{uuid.uuid4().hex[:8]}"
    seen_ids.add(rule_id)
    return {
        "id": rule_id,
        "pattern": pattern,
        "threshold": threshold,
        "action": action,
        "cooldown_minutes": cooldown_minutes,
        "tz": tz,
    }


def validate_rules(rules: Any) -> list[dict]:
    """校验并归一化自定义规则列表（整体替换语义）

    Args:
        rules: 待校验的规则列表（通常来自 Web API 请求体）。

    Returns:
        归一化后的规则列表。

    Raises:
        ValueError: 任一规则不合法，消息为中文，可直接透传给前端。
    """
    if not isinstance(rules, list):
        raise ValueError("规则必须是数组")
    seen_ids: set[str] = set()
    return [_normalize_rule(rule, index, seen_ids) for index, rule in enumerate(rules, start=1)]


def _parse_midnight_tz(raw: str) -> timezone | None:
    """解析「冷却至次日 00:00」的时区基准（每条规则的 tz 字段）

    "" / "local" -> None（使用本机时区）
    "UTC" / "utc" -> timezone.utc
    "+8" / "-5" / "5.5" 等数值 -> 相对 UTC 的偏移时区
    非法值抛 ValueError，由调用方转为可透传前端的错误信息
    """
    raw = (raw or "").strip()
    if raw == "" or raw.lower() == "local":
        return None
    if raw.lower() == "utc":
        return timezone.utc
    offset = float(raw)
    if not (-24 < offset < 24):
        raise ValueError("时区偏移超出范围（应在 -24 ~ 24 之间）")
    return timezone(timedelta(hours=offset))


class ModelManager:
    def __init__(self, cooldown_policy: Any | None = None):
        # 代理服务跑在独立线程的事件循环里，监控面板（AstrBot 主循环）与代理请求
        # （线程循环）会并发操作本类。asyncio.Lock 会绑定到首个进入慢路径的事件循环，
        # 另一循环再竞争即抛跨循环 RuntimeError，且它本就无法提供跨线程互斥。
        # 临界区全是无 await 的内存字典操作，改用 threading.Lock：与事件循环无关，
        # 且提供真正的跨线程互斥。
        # 约束：临界区内不得引入 await，否则会阻塞所在线程的事件循环。
        self._lock = threading.Lock()
        self._cooldown: dict[str, datetime] = {}  # provider_id -> 冷却结束时间
        self._fail_counts: dict[str, int] = {}  # provider_id -> 连续失败次数
        self._disabled: set[str] = set()  # 被手动禁用的 provider_id
        self._call_log: deque[dict] = deque(maxlen=CALL_LOG_MAX_ENTRIES)  # 近期调用记录
        self._rules: dict[str, list[dict]] = {}  # provider_id -> 自定义规则列表
        self._rule_hits: dict[str, dict[str, int]] = {}  # provider_id -> {规则 ID -> 连续命中次数}
        self._cooldown_policy = normalize_cooldown_policy(
            DEFAULT_COOLDOWN_POLICY if cooldown_policy is None else cooldown_policy
        )

    @property
    def cooldown_policy(self) -> list[list[float]]:
        """当前生效的冷却阶梯（用于状态展示）"""
        return [[failures, minutes] for failures, minutes in self._cooldown_policy]

    async def is_available(self, provider_id: str) -> bool:
        """检查 Provider 是否可用（未被禁用且不在冷却中）"""
        with self._lock:
            if provider_id in self._disabled:
                return False
            if provider_id not in self._cooldown:
                return True
            if datetime.now() >= self._cooldown[provider_id]:
                del self._cooldown[provider_id]
                return True
            return False

    async def mark_success(self, provider_id: str) -> None:
        """清零 Provider 的连续失败次数与自定义规则命中数"""
        with self._lock:
            self._fail_counts.pop(provider_id, None)
            self._rule_hits.pop(provider_id, None)

    async def mark_cooldown(self, provider_id: str, reason: str = "") -> None:
        """累加连续失败次数，并按冷却阶梯标记 Provider 进入冷却"""
        with self._lock:
            failures = self._fail_counts.get(provider_id, 0) + 1
            self._fail_counts[provider_id] = failures
            minutes = resolve_cooldown_minutes(self._cooldown_policy, failures)
            self._cooldown[provider_id] = datetime.now() + timedelta(minutes=minutes)
        logger.warning(f"Provider {provider_id} 连续失败 {failures} 次，进入冷却 {minutes:g} 分钟 (原因: {reason})")

    async def set_disabled(self, provider_id: str, disabled: bool) -> None:
        """手动禁用或解除禁用 Provider"""
        with self._lock:
            if disabled:
                self._disabled.add(provider_id)
            else:
                self._disabled.discard(provider_id)
                self._cooldown.pop(provider_id, None)
                self._fail_counts.pop(provider_id, None)
                self._rule_hits.pop(provider_id, None)
        logger.info(f"Provider {provider_id} 已{'禁用' if disabled else '解除禁用（同时清空冷却、连续失败次数与规则命中数）'}")

    async def is_disabled(self, provider_id: str) -> bool:
        """检查 Provider 是否被手动禁用"""
        with self._lock:
            return provider_id in self._disabled

    async def clear_all(self) -> None:
        """清空所有冷却、连续失败计数与规则命中数（手动禁用状态保留）"""
        with self._lock:
            self._cooldown.clear()
            self._fail_counts.clear()
            self._rule_hits.clear()
        logger.info("已清空所有 Provider 冷却状态与规则命中数")

    async def apply_rules(self, provider_id: str, reason: str) -> bool:
        """按自定义规则评估一次失败，连续命中达到阈值时执行对应动作

        匹配语义：失败原因（不区分大小写）包含规则匹配内容即记一次命中，
        未命中则清零该规则的连续计数；达到阈值后触发动作并清零计数。
        同一轮评估中只有首个达到阈值的规则会触发。

        Args:
            provider_id: Provider ID。
            reason: 失败原因（错误信息或错误码的文本描述）。

        Returns:
            是否有规则被触发。调用方据此决定是否跳过内置冷却阶梯。
        """
        with self._lock:
            rules = self._rules.get(provider_id)
            if not rules:
                return False
            reason_lower = reason.lower()
            hits = self._rule_hits.setdefault(provider_id, {})
            fired_rule: tuple[dict, int] | None = None
            detail = ""
            for rule in rules:
                if rule["pattern"].lower() not in reason_lower:
                    hits.pop(rule["id"], None)
                    continue
                count = hits.get(rule["id"], 0) + 1
                if count < rule["threshold"]:
                    hits[rule["id"]] = count
                    continue
                hits[rule["id"]] = 0
                detail = self._fire_rule(provider_id, rule)
                fired_rule = (rule, count)
                break
            if not hits:
                self._rule_hits.pop(provider_id, None)
        if fired_rule is not None:
            rule, count = fired_rule
            logger.warning(
                f"Provider {provider_id} 触发自定义规则「{rule['pattern']}」"
                f"（连续命中 {count} 次），{detail} (原因: {reason})"
            )
            return True
        return False

    def _fire_rule(self, provider_id: str, rule: dict) -> str:
        """执行规则触发的动作，返回日志用的人类可读描述

        调用方须已持有 self._lock。
        """
        action = rule["action"]
        if action == "disable":
            self._disabled.add(provider_id)
            return "已禁用"
        if action == "until_midnight":
            base_tz = _parse_midnight_tz(rule.get("tz", ""))
            ref_now = datetime.now(base_tz) if base_tz is not None else datetime.now()
            until_aware = datetime.combine(
                ref_now.date() + timedelta(days=1), time.min, tzinfo=base_tz
            )
            # 以本地朴素时间存储，与 _cooldown 其余条目一致，统一比较与展示
            self._cooldown[provider_id] = until_aware.astimezone().replace(tzinfo=None)
            tz_label = "本机时区" if base_tz is None else "指定时区"
            return f"冷却至次日 00:00（{tz_label}，重置时刻 {until_aware:%Y-%m-%d %H:%M}）"
        minutes = rule["cooldown_minutes"]
        self._cooldown[provider_id] = datetime.now() + timedelta(minutes=minutes)
        return f"冷却 {minutes:g} 分钟"

    async def set_rules(self, provider_id: str, rules: Any) -> list[dict]:
        """整体替换指定 Provider 的自定义规则

        Args:
            provider_id: Provider ID。
            rules: 规则列表；空列表表示清空该 Provider 的规则。

        Returns:
            归一化后的规则列表，每条附带实时命中数字段 hits。

        Raises:
            ValueError: 任一规则不合法，消息为中文，可直接透传给前端。
        """
        normalized = validate_rules(rules)
        with self._lock:
            if normalized:
                self._rules[provider_id] = normalized
            else:
                self._rules.pop(provider_id, None)
            hits = self._rule_hits.get(provider_id)
            if hits:
                valid_ids = {rule["id"] for rule in normalized}
                for rule_id in [rid for rid in hits if rid not in valid_ids]:
                    del hits[rule_id]
                if not hits:
                    self._rule_hits.pop(provider_id, None)
            return [self._rule_with_hits(rule, hits or {}) for rule in normalized]

    async def set_rules_map(self, rules_map: Any) -> None:
        """启动时批量加载各 Provider 的持久化规则

        单条规则不合法时只丢弃该条，不影响同一 Provider 的其余规则。

        Args:
            rules_map: provider_id -> 规则列表 的映射（通常来自 KV 存储）。
        """
        if not isinstance(rules_map, dict):
            if rules_map:
                logger.warning("自定义规则存储数据格式不正确（期望对象），已忽略")
            return
        loaded = 0
        with self._lock:
            for provider_id, rules in rules_map.items():
                if not isinstance(provider_id, str) or not isinstance(rules, list):
                    continue
                seen_ids: set[str] = set()
                normalized = []
                for index, rule in enumerate(rules, start=1):
                    try:
                        normalized.append(_normalize_rule(rule, index, seen_ids))
                    except ValueError as exc:
                        logger.warning(f"Provider {provider_id} 第 {index} 条自定义规则不合法，已丢弃: {exc}")
                if normalized:
                    self._rules[provider_id] = normalized
                    loaded += len(normalized)
        if loaded:
            logger.info(f"已从持久化存储加载 {loaded} 条自定义规则")

    async def get_all_rules(self) -> dict[str, list[dict]]:
        """获取所有 Provider 的规则（含实时命中数，供监控页展示）"""
        with self._lock:
            return {
                provider_id: [
                    self._rule_with_hits(rule, self._rule_hits.get(provider_id, {}))
                    for rule in rules
                ]
                for provider_id, rules in self._rules.items()
            }

    async def export_rules(self) -> dict[str, list[dict]]:
        """导出规则（不含命中数，供持久化存储）"""
        with self._lock:
            return {
                provider_id: [dict(rule) for rule in rules]
                for provider_id, rules in self._rules.items()
            }

    @staticmethod
    def _rule_with_hits(rule: dict, hits: dict[str, int]) -> dict:
        """为规则副本附加实时命中数，避免把内部状态暴露给调用方"""
        result = dict(rule)
        result["hits"] = hits.get(rule["id"], 0)
        return result

    async def get_status(self) -> dict:
        """获取当前状态（用于 Web API）"""
        with self._lock:
            now = datetime.now()
            cooldown_list = [
                {
                    "id": pid,
                    "cooldown_until": until.isoformat(),
                    "remaining_secs": max(0, int((until - now).total_seconds())),
                    "consecutive_failures": self._fail_counts.get(pid, 0),
                }
                for pid, until in self._cooldown.items() if until > now
            ]
            return {
                "cooldown_count": len(cooldown_list),
                "cooldown_list": cooldown_list,
                "disabled_list": sorted(self._disabled),
                "fail_counts": {pid: count for pid, count in self._fail_counts.items() if count > 0},
                "cooldown_policy": self.cooldown_policy,
            }

    async def record_call(self, provider_id: str, success: bool, reason: str = "") -> None:
        """记录一次 Provider 调用的结果，供监控面板查看

        Args:
            provider_id: Provider ID。
            success: 调用是否成功。
            reason: 失败原因；成功时留空。
        """
        with self._lock:
            self._call_log.append({
                "time": datetime.now().isoformat(timespec="seconds"),
                "provider_id": provider_id,
                "success": success,
                "reason": reason,
            })

    async def get_call_log(self, provider_id: str) -> list[dict]:
        """读取指定 Provider 的近期调用记录（最新在前）

        Args:
            provider_id: Provider ID。

        Returns:
            调用记录列表，元素含 time / success / reason 字段。
        """
        with self._lock:
            return [
                entry for entry in reversed(self._call_log)
                if entry["provider_id"] == provider_id
            ]
