import asyncio
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

from .config import (
    DEFAULT_COOLDOWN_POLICY,
    normalize_cooldown_policy,
    resolve_cooldown_minutes,
)


class ModelManager:
    def __init__(self, cooldown_policy: Any | None = None):
        self._lock = asyncio.Lock()
        self._cooldown: dict[str, datetime] = {}  # provider_id -> 冷却结束时间
        self._fail_counts: dict[str, int] = {}  # provider_id -> 连续失败次数
        self._disabled: set[str] = set()  # 被手动禁用的 provider_id
        self._cooldown_policy = normalize_cooldown_policy(
            DEFAULT_COOLDOWN_POLICY if cooldown_policy is None else cooldown_policy
        )

    @property
    def cooldown_policy(self) -> list[list[float]]:
        """当前生效的冷却阶梯（用于状态展示）"""
        return [[failures, minutes] for failures, minutes in self._cooldown_policy]

    async def is_available(self, provider_id: str) -> bool:
        """检查 Provider 是否可用（未被禁用且不在冷却中）"""
        async with self._lock:
            if provider_id in self._disabled:
                return False
            if provider_id not in self._cooldown:
                return True
            if datetime.now() >= self._cooldown[provider_id]:
                del self._cooldown[provider_id]
                return True
            return False

    async def mark_success(self, provider_id: str) -> None:
        """清零 Provider 的连续失败次数"""
        async with self._lock:
            self._fail_counts.pop(provider_id, None)

    async def mark_cooldown(self, provider_id: str, reason: str = "") -> None:
        """累加连续失败次数，并按冷却阶梯标记 Provider 进入冷却"""
        async with self._lock:
            failures = self._fail_counts.get(provider_id, 0) + 1
            self._fail_counts[provider_id] = failures
            minutes = resolve_cooldown_minutes(self._cooldown_policy, failures)
            self._cooldown[provider_id] = datetime.now() + timedelta(minutes=minutes)
        logger.warning(f"Provider {provider_id} 连续失败 {failures} 次，进入冷却 {minutes:g} 分钟 (原因: {reason})")

    async def set_disabled(self, provider_id: str, disabled: bool) -> None:
        """手动禁用或解除禁用 Provider"""
        async with self._lock:
            if disabled:
                self._disabled.add(provider_id)
            else:
                self._disabled.discard(provider_id)
                self._cooldown.pop(provider_id, None)
                self._fail_counts.pop(provider_id, None)
        logger.info(f"Provider {provider_id} 已{'禁用' if disabled else '解除禁用（同时清空冷却与连续失败次数）'}")

    async def is_disabled(self, provider_id: str) -> bool:
        """检查 Provider 是否被手动禁用"""
        async with self._lock:
            return provider_id in self._disabled

    async def clear_all(self) -> None:
        """清空所有冷却与连续失败计数（手动禁用状态保留）"""
        async with self._lock:
            self._cooldown.clear()
            self._fail_counts.clear()
        logger.info("已清空所有 Provider 冷却状态")

    async def get_status(self) -> dict:
        """获取当前状态（用于 Web API）"""
        async with self._lock:
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
