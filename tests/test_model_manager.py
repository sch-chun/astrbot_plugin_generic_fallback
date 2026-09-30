"""ModelManager 的冷却阶梯、成功清零与手动禁用测试。"""
import asyncio

import pytest

from astrbot_plugin_generic_fallback.src.model_manager import ModelManager

PROVIDER = "provider/openai"
DEFAULT_POLICY = [[1, 1], [2, 5], [5, 30]]


async def remaining_secs(manager: ModelManager, provider_id: str = PROVIDER) -> int:
    """读取指定 Provider 的剩余冷却秒数（未冷却返回 0）"""
    status = await manager.get_status()
    for item in status["cooldown_list"]:
        if item["id"] == provider_id:
            return item["remaining_secs"]
    return 0


async def test_new_provider_is_available():
    manager = ModelManager()
    assert await manager.is_available(PROVIDER) is True


async def test_default_policy_exposed_as_nested_list():
    manager = ModelManager()
    assert manager.cooldown_policy == DEFAULT_POLICY


async def test_cooldown_uses_first_ladder_entry():
    manager = ModelManager()
    await manager.mark_cooldown(PROVIDER, "boom")
    assert 55 <= await remaining_secs(manager) <= 60
    assert await manager.is_available(PROVIDER) is False


async def test_cooldown_escalates_with_consecutive_failures():
    manager = ModelManager()
    expected = [60, 300, 300, 300, 1800, 1800]
    for index, upper in enumerate(expected, start=1):
        await manager.mark_cooldown(PROVIDER, f"fail {index}")
        remaining = await remaining_secs(manager)
        assert upper - 5 <= remaining <= upper, f"第 {index} 次失败后冷却时长异常: {remaining}"

        status = await manager.get_status()
        assert status["cooldown_list"][0]["consecutive_failures"] == index


async def test_custom_policy_is_honoured():
    # [[1, 1], [3, 10]]：第 1-2 次失败冷却 1 分钟，第 3 次起冷却 10 分钟
    manager = ModelManager(cooldown_policy=[[1, 1], [3, 10]])
    await manager.mark_cooldown(PROVIDER)
    assert 55 <= await remaining_secs(manager) <= 60

    await manager.mark_cooldown(PROVIDER)
    assert 55 <= await remaining_secs(manager) <= 60

    await manager.mark_cooldown(PROVIDER)
    assert 595 <= await remaining_secs(manager) <= 600


def test_invalid_policy_raises():
    with pytest.raises(ValueError):
        ModelManager(cooldown_policy="oops")


async def test_success_resets_consecutive_failures():
    manager = ModelManager()
    await manager.mark_cooldown(PROVIDER)
    assert (await manager.get_status())["fail_counts"] == {PROVIDER: 1}

    await manager.mark_success(PROVIDER)
    assert (await manager.get_status())["fail_counts"] == {}

    # 计数被清零，下一次失败重新从首档开始
    await manager.mark_cooldown(PROVIDER)
    assert (await manager.get_status())["fail_counts"] == {PROVIDER: 1}
    assert 55 <= await remaining_secs(manager) <= 60


async def test_expired_cooldown_is_cleared():
    manager = ModelManager(cooldown_policy=[[1, 1 / 60]])  # 1 秒
    await manager.mark_cooldown(PROVIDER)
    assert await manager.is_available(PROVIDER) is False

    await asyncio.sleep(1.1)
    assert await manager.is_available(PROVIDER) is True
    assert (await manager.get_status())["cooldown_count"] == 0


async def test_disable_blocks_provider():
    manager = ModelManager()
    await manager.set_disabled(PROVIDER, True)
    assert await manager.is_disabled(PROVIDER) is True
    assert await manager.is_available(PROVIDER) is False
    assert (await manager.get_status())["disabled_list"] == [PROVIDER]


async def test_enable_restores_provider_and_clears_cooldown():
    manager = ModelManager()
    await manager.mark_cooldown(PROVIDER)
    await manager.set_disabled(PROVIDER, True)

    await manager.set_disabled(PROVIDER, False)
    assert await manager.is_disabled(PROVIDER) is False
    assert await manager.is_available(PROVIDER) is True

    status = await manager.get_status()
    assert status["disabled_list"] == []
    assert status["cooldown_count"] == 0
    assert status["fail_counts"] == {}


async def test_disable_is_idempotent():
    manager = ModelManager()
    await manager.set_disabled(PROVIDER, True)
    await manager.set_disabled(PROVIDER, True)
    assert (await manager.get_status())["disabled_list"] == [PROVIDER]

    await manager.set_disabled(PROVIDER, False)
    await manager.set_disabled(PROVIDER, False)
    assert (await manager.get_status())["disabled_list"] == []


async def test_clear_all_keeps_manual_disabled_state():
    manager = ModelManager()
    await manager.mark_cooldown(PROVIDER)
    await manager.set_disabled(PROVIDER, True)

    await manager.clear_all()
    status = await manager.get_status()
    assert status["cooldown_count"] == 0
    assert status["fail_counts"] == {}
    assert status["disabled_list"] == [PROVIDER]


async def test_status_exposes_policy_and_empty_state():
    status = await ModelManager().get_status()
    assert status["cooldown_count"] == 0
    assert status["cooldown_list"] == []
    assert status["disabled_list"] == []
    assert status["fail_counts"] == {}
    assert status["cooldown_policy"] == DEFAULT_POLICY


async def test_providers_are_tracked_independently():
    manager = ModelManager()
    other = "provider/anthropic"
    await manager.mark_cooldown(other)
    await manager.set_disabled(PROVIDER, True)

    assert await manager.is_available(PROVIDER) is False
    assert await manager.is_available(other) is False

    await manager.set_disabled(PROVIDER, False)
    await manager.clear_all()
    assert await manager.is_available(PROVIDER) is True
    assert await manager.is_available(other) is True
