"""水木验码 MCP 接口：把本地审查能力交给 MCP 客户端。"""

from .server import build_manager, dispatch, TOOLS

__all__ = ["build_manager", "dispatch", "TOOLS"]
