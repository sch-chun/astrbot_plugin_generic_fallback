import threading
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger

from .config import (
    DEFAULT_COOLDOWN_POLICY,
    normalize_cooldown_policy,
    resolve_cooldown_minutes,
)

# 调用日志环形缓冲容量：仅供人工排查近期调用，够用即可，避免无限增长
CALL_LOG_MAX_ENTRIES = 200


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
        """清零 Provider 的连续失败次数"""
        with self._lock:
            self._fail_counts.pop(provider_id, None)

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
        logger.info(f"Provider {provider_id} 已{'禁用' if disabled else '解除禁用（同时清空冷却与连续失败次数）'}")

    async def is_disabled(self, provider_id: str) -> bool:
        """检查 Provider 是否被手动禁用"""
        with self._lock:
            return provider_id in self._disabled

    async def clear_all(self) -> None:
        """清空所有冷却与连续失败计数（手动禁用状态保留）"""
        with self._lock:
            self._cooldown.clear()
            self._fail_counts.clear()
        logger.info("已清空所有 Provider 冷却状态")

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
