"""监控页背景图：纯函数 _build_page_background_response 与 ETag 条件匹配回归测试。

覆盖评审 P2 点名的三条（未配置返回空、路径穿越被拒、ETag 随 opacity 变化）
以及 P3 的 If-None-Match: * 处理——这些是安全边界与缓存逻辑，最该有回归保护。
"""
import base64

from astrbot_plugin_generic_fallback.main import (
    _build_page_background_response,
    _if_none_match_matches,
)

# 1x1 红点 PNG，用于构造可读取的真实图片
_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _write_png(data_dir, name="bg.png"):
    p = data_dir / name
    p.write_bytes(base64.b64decode(_PNG_B64))
    return p


def test_unconfigured_returns_empty():
    # 未配置 page_background：返回空 data_url 且无 ETag（前端据此不渲染背景）
    payload, etag = _build_page_background_response({}, "<any-data-dir>")
    assert etag is None
    assert payload["code"] == 0
    assert payload["data"]["data_url"] == ""


def test_path_traversal_rejected(tmp_path):
    # 用 ../ 试图逃出插件数据目录：即便越界文件真实存在也必须被拒绝
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (tmp_path / "secret.png").write_bytes(b"x")  # 数据目录之外、但真实存在的文件

    payload, etag = _build_page_background_response(
        {"page_background": ["../secret.png"]}, str(data_dir)
    )
    assert etag is None
    assert payload["data"]["data_url"] == ""


def test_etag_changes_with_opacity(tmp_path):
    # 同一张图仅调透明度：ETag 必须变，否则 304 会把旧 opacity 继续发给前端
    _write_png(tmp_path)
    payload_a, etag_a = _build_page_background_response(
        {"page_background": ["bg.png"], "page_background_opacity": 0.2}, str(tmp_path)
    )
    payload_b, etag_b = _build_page_background_response(
        {"page_background": ["bg.png"], "page_background_opacity": 0.8}, str(tmp_path)
    )
    assert etag_a is not None and etag_b is not None
    assert etag_a != etag_b
    # 图片本身没变，data_url 应一致
    assert payload_a["data"]["data_url"] == payload_b["data"]["data_url"]
    assert payload_a["data"]["opacity"] == 0.2
    assert payload_b["data"]["opacity"] == 0.8


def test_if_none_match_star_matches_when_etag_present():
    # RFC 7232: * 命中任何现有表示；资源已有表示（etag 非空）时应返回 304
    assert _if_none_match_matches("*", '"abc123"') is True
    assert _if_none_match_matches("W/*", '"abc123"') is True


def test_if_none_match_star_does_not_match_without_etag():
    # 资源无当前表示（etag 为空）时，* 不应命中（handler 此时本就不会调用本函数，兜底）
    assert _if_none_match_matches("*", None) is False


def test_if_none_match_existing_etag_behavior():
    # 原有弱/强 ETag 逐项匹配行为保持不变
    assert _if_none_match_matches('W/"abc", "def"', '"def"') is True
    assert _if_none_match_matches('"abc"', '"abc"') is True
    assert _if_none_match_matches('"abc"', '"xyz"') is False
    assert _if_none_match_matches(None, '"abc"') is False
