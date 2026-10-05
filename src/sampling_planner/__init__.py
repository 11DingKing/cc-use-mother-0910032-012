"""监管抽检计划服务端。

仅依赖 Python 3.11 标准库：
- sqlite3 持久化与事务锁定
- http.server 提供 JSON API
"""
from __future__ import annotations

__version__ = "0.2.0"
