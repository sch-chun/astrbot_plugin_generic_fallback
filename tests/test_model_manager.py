"""ModelManager 的冷却阶梯、成功清零、手动禁用与自定义规则测试。"""
import asyncio
import threading
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as clock_time

import pytest

from astrbot_plugin_generic_fallback.src.model_manager import (
    CALL_LOG_MAX_ENTRIES,
    ModelManager,
    _parse_midnight_tz,
    validate_rules,
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


async def test_call_log_is_per_provider_independent():
    manager = ModelManager()
    # 调用多的 Provider 持续写入，远超单家上限
    for index in range(CALL_LOG_MAX_ENTRIES + 50):
        await manager.record_call("busy/provider", True, f"c{index}")
    # 调用少的 Provider 仅写入少量记录（且较早）
    for index in range(5):
        await manager.record_call("quiet/provider", False, f"q{index}")

    quiet_logs = await manager.get_call_log("quiet/provider")
    # 关键：调用少的 Provider 没有被调用多的 Provider 挤出
    assert len(quiet_logs) == 5
    # get_call_log 最新在前
    assert [entry["reason"] for entry in quiet_logs] == [f"q{i}" for i in reversed(range(5))]

    busy_logs = await manager.get_call_log("busy/provider")
    assert len(busy_logs) == CALL_LOG_MAX_ENTRIES


async def test_call_log_respects_configurable_cap():
    manager = ModelManager(call_log_max_per_provider=3)
    for index in range(10):
        await manager.record_call(PROVIDER, True, f"c{index}")

    logs = await manager.get_call_log(PROVIDER)
    assert len(logs) == 3  # 仅保留最近 3 条
    assert [entry["reason"] for entry in logs] == ["c9", "c8", "c7"]  # 最新在前


# ---------- 自定义规则 ----------

RULE_429 = {"pattern": "429", "threshold": 2, "action": "cooldown", "cooldown_minutes": 7}


async def rules_map(manager: ModelManager, provider_id: str = PROVIDER) -> dict:
    rules = (await manager.get_all_rules()).get(provider_id, [])
    return {rule["pattern"]: rule for rule in rules}


async def test_validate_rules_normalizes_and_generates_ids():
    rules = validate_rules([
        {"pattern": "  429  ", "threshold": 2, "action": "cooldown", "cooldown_minutes": 7},
        {"pattern": "quota", "threshold": 1, "action": "disable", "unknown_key": "ignored"},
    ])
    assert [rule["pattern"] for rule in rules] == ["429", "quota"]
    assert all(rule["id"].startswith("r-") and len(rule["id"]) == 10 for rule in rules)
    assert rules[0]["cooldown_minutes"] == 7
    assert rules[1]["action"] == "disable"
    assert "unknown_key" not in rules[1]  # 未知键忽略，不透传不存储


async def test_validate_rules_defaults_cooldown_minutes():
    (rule,) = validate_rules([{"pattern": "429", "threshold": 1, "action": "cooldown"}])
    assert rule["cooldown_minutes"] == 10


async def test_validate_rules_rejects_invalid_rules():
    invalid = [
        "not-a-list",
        {"pattern": "", "threshold": 1, "action": "disable"},
        {"pattern": "   ", "threshold": 1, "action": "disable"},
        {"pattern": "x", "threshold": 0, "action": "disable"},
        {"pattern": "x", "threshold": True, "action": "disable"},  # bool 是 int 子类，须排除
        {"pattern": "x", "threshold": 1.5, "action": "disable"},
        {"pattern": "x", "threshold": 1, "action": "explode"},
        {"pattern": "x", "threshold": 1, "action": "cooldown", "cooldown_minutes": 0},
        {"pattern": "x", "threshold": 1, "action": "cooldown", "cooldown_minutes": -1},
        {"pattern": "x", "threshold": 1, "action": "cooldown", "cooldown_minutes": "5"},
        {"pattern": "x" * 101, "threshold": 1, "action": "disable"},
        ["not", "a", "dict"],
    ]
    for rules in invalid:
        with pytest.raises(ValueError):
            validate_rules(rules)


async def test_apply_rules_counts_consecutive_hits_and_fires_cooldown():
    manager = ModelManager()
    await manager.set_rules(PROVIDER, [RULE_429])

    assert await manager.apply_rules(PROVIDER, "Error code: 429 rate limited") is False
    assert await manager.apply_rules(PROVIDER, "Error code: 429 rate limited") is True

    status = await manager.get_status()
    assert status["cooldown_count"] == 1
    assert 415 <= status["cooldown_list"][0]["remaining_secs"] <= 420  # 规则指定 7 分钟
    assert status["fail_counts"] == {}  # 规则触发，不计入内置阶梯
    assert (await rules_map(manager))["429"]["hits"] == 0  # 触发后归零


async def test_apply_rules_matches_case_insensitively():
    manager = ModelManager()
    await manager.set_rules(PROVIDER, [{"pattern": "Quota", "threshold": 1, "action": "disable"}])

    assert await manager.apply_rules(PROVIDER, "EXCEEDED QUOTA") is True
    assert await manager.is_disabled(PROVIDER) is True


async def test_apply_rules_non_match_resets_only_that_rule():
    manager = ModelManager()
    await manager.set_rules(PROVIDER, [
        {"pattern": "429", "threshold": 3, "action": "disable"},
        {"pattern": "timeout", "threshold": 3, "action": "disable"},
    ])

    await manager.apply_rules(PROVIDER, "429")
    await manager.apply_rules(PROVIDER, "429")
    await manager.apply_rules(PROVIDER, "timeout")  # 未命中 429，其计数应被清零

    rules = await rules_map(manager)
    assert rules["429"]["hits"] == 0
    assert rules["timeout"]["hits"] == 1


async def test_apply_rules_until_midnight():
    manager = ModelManager()
    await manager.set_rules(PROVIDER, [{"pattern": "timeout", "threshold": 1, "action": "until_midnight"}])

    assert await manager.apply_rules(PROVIDER, "connection timeout") is True

    (item,) = (await manager.get_status())["cooldown_list"]
    until = datetime.fromisoformat(item["cooldown_until"])
    assert until.time() == clock_time(0, 0)  # 次日 00:00（本机时区）
    assert until.date() > date.today()


async def test_apply_rules_until_midnight_tz():
    # 每条规则可单独指定时区：UTC 基准下冷却应在下一个 UTC 00:00 结束
    manager = ModelManager()
    await manager.set_rules(
        PROVIDER,
        [{"pattern": "timeout", "threshold": 1, "action": "until_midnight", "tz": "UTC"}],
    )
    assert await manager.apply_rules(PROVIDER, "connection timeout") is True

    (item,) = (await manager.get_status())["cooldown_list"]
    until = datetime.fromisoformat(item["cooldown_until"])
    now_utc = datetime.now(timezone.utc)
    expected = datetime.combine(
        now_utc.date() + timedelta(days=1), clock_time.min, tzinfo=timezone.utc
    )
    assert until == expected.astimezone().replace(tzinfo=None)


async def test_apply_rules_until_midnight_tz_invalid():
    # 非法 tz 应在规则校验阶段报错，而非默默回退本机时区
    manager = ModelManager()
    with pytest.raises(ValueError):
        await manager.set_rules(
            PROVIDER,
            [{"pattern": "timeout", "threshold": 1, "action": "until_midnight", "tz": "not-a-tz"}],
        )


async def test_apply_rules_until_midnight_invalid_tz_does_not_crash():
    # 兜底：即便有人绕过 set_rules 直接写 _rules 塞入非法 tz，apply_rules 也不应抛异常，
    # 而是回退本机时区并照常冷却（对应 _fire_rule 里的 try/except）
    manager = ModelManager()
    manager._rules[PROVIDER] = [
        {"id": "r-bad", "pattern": "timeout", "threshold": 1, "action": "until_midnight", "tz": "not-a-tz"}
    ]

    # 不应抛 ValueError；应正常触发冷却动作
    assert await manager.apply_rules(PROVIDER, "connection timeout") is True

    (item,) = (await manager.get_status())["cooldown_list"]
    until = datetime.fromisoformat(item["cooldown_until"])
    # 回退本机时区 → 次日本地 00:00
    assert until.date() > date.today()
    assert until.time() == clock_time(0, 0)


async def test_apply_rules_until_midnight_tz_offset():
    # 每条规则可单独指定偏移时区：UTC+8 基准下冷却应在下一个 UTC+8 00:00 结束。
    # 复刻 _fire_rule 的算法：以「目标时区自己的当前日期」+1 天作为次日，再投影回本地朴素时间。
    manager = ModelManager()
    await manager.set_rules(
        PROVIDER,
        [{"pattern": "timeout", "threshold": 1, "action": "until_midnight", "tz": "UTC+8"}],
    )
    assert await manager.apply_rules(PROVIDER, "connection timeout") is True

    (item,) = (await manager.get_status())["cooldown_list"]
    until = datetime.fromisoformat(item["cooldown_until"])
    ref_now = datetime.now(timezone(timedelta(hours=8)))
    until_aware = datetime.combine(
        ref_now.date() + timedelta(days=1), clock_time.min, tzinfo=timezone(timedelta(hours=8))
    )
    expected = until_aware.astimezone().replace(tzinfo=None)
    assert until == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", None),
        ("local", None),
        ("UTC", timezone.utc),
        ("gmt", timezone.utc),
        ("UTC+8", timezone(timedelta(hours=8))),
        ("UTC +8", timezone(timedelta(hours=8))),
        ("GMT-5.5", timezone(timedelta(hours=-5.5))),
        ("UTC+05:30", timezone(timedelta(hours=5.5))),
        ("+8", timezone(timedelta(hours=8))),
        ("-5", timezone(timedelta(hours=-5))),
        ("5.5", timezone(timedelta(hours=5.5))),
    ],
)
def test_parse_midnight_tz_formats(raw, expected):
    assert _parse_midnight_tz(raw) == expected


@pytest.mark.parametrize("bad", ["not-a-tz", "UTC+24", "GMT-99", "UTC+8x", "abc"])
def test_parse_midnight_tz_invalid(bad):
    with pytest.raises(ValueError):
        _parse_midnight_tz(bad)


async def test_apply_rules_without_rules_is_noop():
    manager = ModelManager()
    assert await manager.apply_rules(PROVIDER, "boom") is False
    assert (await manager.get_status())["cooldown_count"] == 0


async def test_success_resets_rule_hits():
    manager = ModelManager()
    await manager.set_rules(PROVIDER, [RULE_429])
    await manager.apply_rules(PROVIDER, "429")

    await manager.mark_success(PROVIDER)

    assert (await rules_map(manager))["429"]["hits"] == 0
    # 计数已清零：再命中一次不会触发（阈值 2）
    assert await manager.apply_rules(PROVIDER, "429") is False


async def test_enable_resets_rule_hits():
    manager = ModelManager()
    await manager.set_rules(PROVIDER, [RULE_429])
    await manager.apply_rules(PROVIDER, "429")  # hits = 1

    await manager.set_disabled(PROVIDER, True)
    await manager.set_disabled(PROVIDER, False)

    assert await manager.apply_rules(PROVIDER, "429") is False


async def test_clear_all_resets_rule_hits():
    manager = ModelManager()
    await manager.set_rules(PROVIDER, [RULE_429])
    await manager.apply_rules(PROVIDER, "429")

    await manager.clear_all()

    assert await manager.apply_rules(PROVIDER, "429") is False


async def test_set_disabled_clears_call_log():
    manager = ModelManager()
    await manager.record_call(PROVIDER, False, "timeout")
    await manager.set_disabled(PROVIDER, True)
    await manager.set_disabled(PROVIDER, False)
    # 解除禁用时一并清空该 Provider 的旧调用日志（与冷却/连续失败/命中数一致）
    assert await manager.get_call_log(PROVIDER) == []


async def test_clear_call_log_removes_provider():
    manager = ModelManager()
    await manager.record_call(PROVIDER, True)
    await manager.clear_call_log(PROVIDER)
    assert await manager.get_call_log(PROVIDER) == []


async def test_set_rules_replaces_and_prunes_stale_hits():
    manager = ModelManager()
    await manager.set_rules(PROVIDER, [RULE_429])
    await manager.apply_rules(PROVIDER, "429")

    # 换成全新规则：旧规则的残留计数不应泄漏
    replaced = await manager.set_rules(PROVIDER, [{"pattern": "boom", "threshold": 2, "action": "disable"}])
    assert len(replaced) == 1
    assert replaced[0]["pattern"] == "boom"
    assert replaced[0]["hits"] == 0

    # 保留 id 的规则沿用已有计数（replaced[0] 带 hits 字段，服务端会忽略该键）
    await manager.apply_rules(PROVIDER, "boom")
    kept = await manager.set_rules(PROVIDER, [replaced[0]])
    assert kept[0]["hits"] == 1

    # 空列表 = 清空
    assert await manager.set_rules(PROVIDER, []) == []
    assert await manager.get_all_rules() == {}


async def test_set_rules_map_skips_only_invalid_entries():
    manager = ModelManager()
    await manager.set_rules_map({
        PROVIDER: [RULE_429, {"pattern": ""}],  # 第二条非法，只丢它
        "provider/ghost": "not-a-list",
        42: [],  # key 非字符串
    })

    rules = await manager.get_all_rules()
    assert list(rules) == [PROVIDER]
    assert len(rules[PROVIDER]) == 1


async def test_set_rules_map_rejects_non_dict():
    manager = ModelManager()
    await manager.set_rules_map("nope")
    assert await manager.get_all_rules() == {}


async def test_export_rules_excludes_hits():
    manager = ModelManager()
    await manager.set_rules(PROVIDER, [RULE_429])
    await manager.apply_rules(PROVIDER, "429")

    exported = await manager.export_rules()
    assert "hits" not in exported[PROVIDER][0]
    assert exported[PROVIDER][0]["pattern"] == "429"
