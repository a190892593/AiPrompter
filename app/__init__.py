"""AI 面试副驾 · 本地桥。"""

from .logging_setup import setup

# 任何入口（python -m app / 测试脚本 / uvicorn）导入 app 时先配好日志，
# 这样模块导入阶段的日志也不会丢。
setup()

__all__ = ["__version__"]
__version__ = "2.0.0"
