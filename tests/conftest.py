"""测试公共脚手架：把 AstrBot 与插件目录加入模块搜索路径。"""
import os
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = _TESTS_DIR.parents[0]
PLUGINS_DIR = _TESTS_DIR.parents[1]
ASTRBOT_ROOT = _TESTS_DIR.parents[3]

# 避免 astrbot 把「当前工作目录」当根目录，在插件下生成影子 data/
os.environ.setdefault("ASTRBOT_ROOT", str(ASTRBOT_ROOT))

for _path in (str(ASTRBOT_ROOT), str(PLUGINS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
