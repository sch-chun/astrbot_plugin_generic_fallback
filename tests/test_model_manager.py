"""ModelManager 的冷却阶梯、成功清零与手动禁用测试。"""
import asyncio
import threading
import time

import pytest

from astrbot_plugin_generic_fallback.src.model_manager import (
    CALL_LOG_MAX_ENTRIES,
    ModelManager,
)

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


async def test_lock_is_safe_across_two_event_loops(monkeypatch):
    """回归：两个事件循环（线程循环 + 主循环）并发操作不得报错或挂死。

    插件把代理服务跑在独立线程的事件循环里，监控面板（AstrBot 主循环）与代理请求
    （线程循环）会并发调用 ModelManager。asyncio.Lock 会绑定到首个进入慢路径的事件
    循环，另一循环再竞争即抛跨循环 RuntimeError，或因跨线程唤醒丢失而挂死。
    """
    manager = ModelManager()

    def slow_resolve(policy, consecutive_failures):
        # 扩大临界区窗口，保证两个循环稳定撞进同一把锁
        time.sleep(0.2)
        return 1.0

    monkeypatch.setattr(
        "astrbot_plugin_generic_fallback.src.model_manager.resolve_cooldown_minutes",
        slow_resolve,
    )

    errors: list[Exception] = []
    entered = threading.Event()

    def thread_body():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        entered.set()
        try:
            loop.run_until_complete(manager.mark_cooldown(PROVIDER, "thread"))
        except Exception as e:
            errors.append(e)
        finally:
            loop.close()

    worker = threading.Thread(target=thread_body)
    worker.start()
    assert entered.wait(timeout=2)
    await asyncio.sleep(0.02)  # 等线程循环先拿到锁

    started = time.monotonic()
    try:
        await asyncio.wait_for(manager.mark_cooldown(PROVIDER, "main"), timeout=2)
    except asyncio.TimeoutError:
        pytest.fail("主循环在 ModelManager 锁上挂死：锁被绑定到了另一事件循环")
    elapsed = time.monotonic() - started

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not errors

    # 关键断言：旧实现（asyncio.Lock）会把主循环挂到跨线程 future 上，持锁线程
    # set_result 时无法唤醒主循环的 selector，只能等下一个定时器（此处的 wait_for
    # 2s）才被"救回"——表现为耗时被拖到约 2s。新实现在锁上同步等待，持锁方一放行
    # 当即继续，耗时只略大于临界区时长（0.2s）。
    assert elapsed < 0.5, f"主循环等待锁耗时 {elapsed:.2f}s，疑似被跨线程唤醒丢失拖住"

    status = await manager.get_status()
    assert status["fail_counts"] == {PROVIDER: 2}


async def test_call_log_records_outcomes_newest_first():
    manager = ModelManager()
    other = "provider/anthropic"

    await manager.record_call(PROVIDER, True)
    await manager.record_call(PROVIDER, False, "boom")
    await manager.record_call(other, False, "other boom")

    logs = await manager.get_call_log(PROVIDER)

    assert len(logs) == 2  # 只返回指定 Provider 的记录
    assert logs[0]["success"] is False
    assert logs[0]["reason"] == "boom"
    assert logs[1]["success"] is True
    assert logs[1]["reason"] == ""
    assert all(log["provider_id"] == PROVIDER for log in logs)


async def test_call_log_entries_carry_timestamp():
    manager = ModelManager()

    await manager.record_call(PROVIDER, False, "timeout")

    (entry,) = await manager.get_call_log(PROVIDER)
    assert entry["time"]  # ISO 时间串，供面板直接展示


async def test_call_log_is_capped():
    manager = ModelManager()

    for index in range(CALL_LOG_MAX_ENTRIES + 20):
        await manager.record_call(PROVIDER, True, f"call {index}")

    logs = await manager.get_call_log(PROVIDER)

    assert len(logs) == CALL_LOG_MAX_ENTRIES
    # 最旧的被挤出，最新一条在头部
    assert logs[0]["reason"] == f"call {CALL_LOG_MAX_ENTRIES + 19}"
    assert logs[-1]["reason"] == "call 20"


async def test_call_log_empty_for_unknown_provider():
    manager = ModelManager()
    await manager.record_call(PROVIDER, True)

    assert await manager.get_call_log("provider/ghost") == []
