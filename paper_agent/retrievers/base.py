"""检索器基类（SPEC §4.1）。"""
from __future__ import annotations

import logging

from ..models import Paper

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
USER_AGENT = "PaperHunterAgent/0.1 (academic paper retrieval tool)"


class RetrieverError(Exception):
    """检索器失败时抛出的自定义异常，由上层捕获降级。"""


class BaseRetriever:
    name: str = "base"

    def search(self, query: str, limit: int) -> list[Paper]:
        """检索论文。失败时 raise RetrieverError，由上层捕获降级。"""
        raise NotImplementedError

    def safe_search(self, query: str, limit: int) -> list[Paper]:
        """带降级的包装：单来源失败不中断整体流程，log 警告后返回已得结果。"""
        try:
            return self.search(query, limit)
        except Exception as exc:  # noqa: BLE001 - 任何单来源失败都不应中断
            logger.warning("[%s] 检索失败: %s", self.name, exc)
            return []
