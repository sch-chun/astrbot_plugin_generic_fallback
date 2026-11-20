"""i18n 资源完整性测试：四语 JSON 可解析、叶子键对齐、关键文案齐备。

设计目标：防止新增/删除文案时漏掉某一种语言，或改动键名后四语字典结构漂移。
只做静态资源校验，不依赖 AstrBot 运行时的 i18n 加载器。
"""
import json
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
I18N_DIR = PLUGIN_DIR / ".astrbot-plugin" / "i18n"
LOCALES = ("zh-CN", "en-US", "ja-JP", "ru-RU")


def _load(locale: str) -> dict:
    with (I18N_DIR / f"{locale}.json").open(encoding="utf-8") as f:
        return json.load(f)


def _leaf_paths(node: dict, prefix: str = "") -> set[str]:
    """递归展开字典，返回所有叶子节点的点分路径集合。"""
    paths = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            paths |= _leaf_paths(value, path)
        else:
            paths.add(path)
    return paths


@pytest.fixture(scope="module")
def bundles() -> dict[str, dict]:
    return {locale: _load(locale) for locale in LOCALES}


@pytest.mark.parametrize("locale", LOCALES)
def test_i18n_file_is_valid_json(bundles, locale):
    """每语文件必须是合法 JSON 且顶层为对象。"""
    assert isinstance(bundles[locale], dict)


def test_all_locales_have_identical_leaf_keys(bundles):
    """四语叶子键集合必须完全一致，避免切换语言后缺文案。"""
    baseline = _leaf_paths(bundles["zh-CN"])
    assert baseline, "基线语言不应为空"
    for locale in LOCALES[1:]:
        paths = _leaf_paths(bundles[locale])
        assert paths == baseline, (
            f"{locale} 与 zh-CN 的键不一致："
            f"缺少 {sorted(baseline - paths)}，多出 {sorted(paths - baseline)}"
        )


# 关键路径：i18n 迁移与面板文案依赖这些键，缺失会导致界面回退中文或显示键名
REQUIRED_KEYS = (
    "metadata.display_name",
    "metadata.short_desc",
    "metadata.desc",
    "config.virtual_models.templates.default.name",
    "config.virtual_models.templates.default.hint",
    "config.virtual_models.templates.default.display_name.description",
    "config.virtual_models.templates.default.display_name.hint",
    "config.virtual_models.templates.default.provider_ids.description",
    "config.cooldown_policy.description",
    "config.call_log_max_per_provider.description",
    "views.monitor.title",
    "views.monitor.headerTitle",
    "views.monitor.ruleMinutesPlaceholder",
    "views.monitor.ruleTzPlaceholder",
    "views.monitor.ruleClear",
    "views.monitor.actionUntilMidnight",
    "views.monitor.emptyResponse",
    "views.monitor.streamEmptyResponse",
)


@pytest.mark.parametrize("locale", LOCALES)
def test_required_keys_present_and_nonempty(bundles, locale):
    """关键键在每语必须存在且非空字符串。"""
    paths = _leaf_paths(bundles[locale])
    for key in REQUIRED_KEYS:
        assert key in paths, f"{locale} 缺少关键键 {key}"


def _lookup(node: dict, path: str):
    for part in path.split("."):
        node = node[part]
    return node


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_required_values_are_nonempty_strings(bundles, locale, key):
    """关键键的值必须是非空字符串，防止空翻译导致界面空白。"""
    value = _lookup(bundles[locale], key)
    assert isinstance(value, str) and value.strip(), f"{locale}.{key} 值为空"


@pytest.mark.parametrize("locale", LOCALES)
def test_reason_i18n_keeps_placeholders(bundles, locale):
    """日志原因文案的 {n} 占位符必须保留，否则 fmt 无法填入次数。"""
    for key in ("emptyResponse", "streamEmptyResponse"):
        value = bundles[locale]["views"]["monitor"][key]
        assert "{n}" in value, f"{locale}.views.monitor.{key} 缺少 {{n}} 占位符"


def test_sidebar_titles_are_short(bundles):
    """侧边栏标题过长会在 AstrBot 默认宽度下被截断。"""
    for locale in LOCALES:
        title = bundles[locale]["views"]["monitor"]["title"]
        assert len(title) <= 20, f"{locale} 侧边栏标题过长: {title!r} ({len(title)} 字符)"
