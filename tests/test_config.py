"""冷却阶梯配置的校验与解析测试。"""
import math

import pytest

from astrbot_plugin_generic_fallback.src.config import (
    DEFAULT_COOLDOWN_POLICY,
    ProxyConfig,
    normalize_cooldown_policy,
    resolve_cooldown_minutes,
)


class TestNormalizeCooldownPolicy:
    def test_default_policy(self):
        assert normalize_cooldown_policy(DEFAULT_COOLDOWN_POLICY) == [(1, 1.0), (2, 5.0), (5, 30.0)]

    def test_unsorted_entries_are_sorted(self):
        assert normalize_cooldown_policy([[5, 30], [1, 1], [2, 5]]) == [(1, 1.0), (2, 5.0), (5, 30.0)]

    def test_accepts_tuples(self):
        assert normalize_cooldown_policy([(1, 1), (3, 10)]) == [(1, 1.0), (3, 10.0)]

    def test_accepts_json_string(self):
        assert normalize_cooldown_policy("[[1, 1], [2, 5]]") == [(1, 1.0), (2, 5.0)]

    def test_single_entry(self):
        assert normalize_cooldown_policy([[3, 10]]) == [(3, 10.0)]

    def test_accepts_float_minutes(self):
        assert normalize_cooldown_policy([[1, 0.5]]) == [(1, 0.5)]

    def test_zero_minutes_allowed(self):
        assert normalize_cooldown_policy([[1, 0]]) == [(1, 0.0)]

    def test_is_idempotent(self):
        once = normalize_cooldown_policy(DEFAULT_COOLDOWN_POLICY)
        assert normalize_cooldown_policy(once) == once

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            60,
            "60",
            {"1": 1},
            [],
            [[1, 1], 5],
            [1, 1],
            [[1]],
            [[1, 2, 3]],
            [[0, 1]],
            [[-1, 1]],
            [[1.5, 1]],
            [["2", 1]],
            [[True, 1]],
            [[1, "5"]],
            [[1, None]],
            [[1, True]],
            [[1, -1]],
            [[1, float("nan")]],
            [[1, float("inf")]],
            [[1, 10**400]],
            [[1, 1], [1, 5]],
            "not json",
        ],
        ids=repr,
    )
    def test_invalid_values_rejected(self, raw):
        with pytest.raises(ValueError):
            normalize_cooldown_policy(raw)

    def test_invalid_message_mentions_index(self):
        with pytest.raises(ValueError, match="第 2 项"):
            normalize_cooldown_policy([[1, 1], [2]])


class TestResolveCooldownMinutes:
    policy = [(1, 1.0), (2, 5.0), (5, 30.0)]

    @pytest.mark.parametrize(
        ("failures", "expected"),
        [(1, 1.0), (2, 5.0), (3, 5.0), (4, 5.0), (5, 30.0), (6, 30.0), (100, 30.0)],
    )
    def test_default_ladder(self, failures, expected):
        assert resolve_cooldown_minutes(self.policy, failures) == expected

    def test_empty_policy_means_no_cooldown(self):
        assert resolve_cooldown_minutes([], 3) == 0

    def test_failures_below_first_threshold_use_first_entry(self):
        assert resolve_cooldown_minutes([(3, 10.0)], 1) == 10.0
        assert resolve_cooldown_minutes([(3, 10.0)], 2) == 10.0
        assert resolve_cooldown_minutes([(3, 10.0)], 3) == 10.0

    def test_result_is_finite(self):
        assert math.isfinite(resolve_cooldown_minutes(self.policy, 10))


class TestProxyConfig:
    def test_default_cooldown_policy_is_normalized(self):
        config = ProxyConfig()
        assert config.cooldown_policy == [(1, 1.0), (2, 5.0), (5, 30.0)]

    def test_default_policy_not_shared_between_instances(self):
        first = ProxyConfig()
        first.cooldown_policy.append([9, 99])
        assert ProxyConfig().cooldown_policy == [(1, 1.0), (2, 5.0), (5, 30.0)]
