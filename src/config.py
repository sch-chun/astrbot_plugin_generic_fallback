import json
import math
from dataclasses import dataclass, field
from typing import Any

# 默认冷却阶梯：连续失败 1 次冷却 1 分钟，2-4 次冷却 5 分钟，5 次及以上冷却 30 分钟
DEFAULT_COOLDOWN_POLICY = [[1, 1], [2, 5], [5, 30]]


@dataclass
class ProxyConfig:
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 3474
    proxy_api_key: str = ""
    log_response: bool = False
    virtual_models: list[dict] = field(default_factory=list)
    empty_response_max_attempts: int = 3
    cooldown_policy: list[tuple[int, float]] = field(
        default_factory=lambda: normalize_cooldown_policy(DEFAULT_COOLDOWN_POLICY)
    )


def normalize_cooldown_policy(raw: Any) -> list[tuple[int, float]]:
    """校验冷却阶梯配置并归一化。

    Args:
        raw: 配置值，形如 ``[[1, 1], [2, 5], [5, 30]]``，每项为
            ``[连续失败次数, 冷却分钟数]``；也接受等价的 JSON 字符串。

    Returns:
        按连续失败次数升序排列的 ``(连续失败次数, 冷却分钟数)`` 列表。

    Raises:
        ValueError: 配置不是合法的冷却阶梯列表。
    """
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError) as e:
            raise ValueError(f"冷却策略不是合法的 JSON: {e}") from e

    if not isinstance(raw, (list, tuple)):
        raise ValueError("冷却策略必须是列表，例如 [[1, 1], [2, 5], [5, 30]]")
    if not raw:
        raise ValueError("冷却策略不能为空")

    entries: list[tuple[int, float]] = []
    for index, item in enumerate(raw, start=1):
        if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError(f"第 {index} 项必须是 [连续失败次数, 冷却分钟数]，实际为 {item!r}")

        failures, minutes = item
        if isinstance(failures, bool) or not isinstance(failures, int) or failures < 1:
            raise ValueError(f"第 {index} 项的连续失败次数必须是 >= 1 的整数，实际为 {failures!r}")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
            raise ValueError(f"第 {index} 项的冷却分钟数必须是数字，实际为 {minutes!r}")
        try:
            minutes = float(minutes)
        except OverflowError as e:
            raise ValueError(f"第 {index} 项的冷却分钟数过大，实际为 {minutes!r}") from e
        if not math.isfinite(minutes) or minutes < 0:
            raise ValueError(f"第 {index} 项的冷却分钟数必须是非负有限数字，实际为 {minutes!r}")

        entries.append((failures, float(minutes)))

    entries.sort(key=lambda entry: entry[0])
    for prev_entry, entry in zip(entries, entries[1:]):
        if prev_entry[0] == entry[0]:
            raise ValueError(f"连续失败次数 {entry[0]} 在冷却策略中重复，每项的阈值必须唯一")

    return entries


def resolve_cooldown_minutes(policy: list[tuple[int, float]], consecutive_failures: int) -> float:
    """按连续失败次数在冷却阶梯中查找对应的冷却分钟数。

    Args:
        policy: 已归一化的冷却阶梯（升序）。
        consecutive_failures: 连续失败次数。

    Returns:
        冷却分钟数；次数低于首档阈值时取首档值。
    """
    if not policy:
        return 0.0

    minutes = policy[0][1]
    for threshold, value in policy:
        if consecutive_failures < threshold:
            break
        minutes = value
    return minutes
