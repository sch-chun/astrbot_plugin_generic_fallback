import asyncio
from datetime import datetime, timedelta

from astrbot.api import logger


# 冷却时间（秒）
COOLDOWN_SECONDS = 60  # 1 分钟


class ModelManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._cooldown: dict[str, datetime] = {}  # provider_id -> 冷却结束时间

    async def is_available(self, provider_id: str) -> bool:
        """检查 Provider 是否可用（不在冷却中）"""
        async with self._lock:
            if provider_id not in self._cooldown:
                return True
            if datetime.now() >= self._cooldown[provider_id]:
                del self._cooldown[provider_id]
                return True
            return False

    async def mark_cooldown(self, provider_id: str, reason: str = "") -> None:
        """标记 Provider 进入冷却"""
        async with self._lock:
            self._cooldown[provider_id] = datetime.now() + timedelta(seconds=COOLDOWN_SECONDS)
        logger.warning(f"Provider {provider_id} 进入冷却 {COOLDOWN_SECONDS//60} 分钟 (原因: {reason})")

    async def clear_all(self) -> None:
        """清空所有冷却（用于重启或调试）"""
        async with self._lock:
            self._cooldown.clear()
        logger.info("已清空所有 Provider 冷却状态")

    async def get_status(self) -> dict:
        """获取当前状态（用于 Web API）"""
        async with self._lock:
            now = datetime.now()
            cooldown_list = [
                {
                    "id": pid,
                    "cooldown_until": until.isoformat(),
                    "remaining_secs": max(0, int((until - now).total_seconds()))
                }
                for pid, until in self._cooldown.items() if until > now
            ]
            return {
                "cooldown_count": len(cooldown_list),
                "cooldown_list": cooldown_list,
            }
        