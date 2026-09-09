"""Semantic Scholar Graph API 检索器（SPEC §4）。

无需 key；429 时指数退避重试（最多 3 次）。
"""
from __future__ import annotations

import logging
import time

import requests

from ..models import Paper
from .base import DEFAULT_TIMEOUT, USER_AGENT, BaseRetriever, RetrieverError

logger = logging.getLogger(__name__)

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = ("title,abstract,authors,year,venue,citationCount,"
          "externalIds,openAccessPdf,fieldsOfStudy")
MAX_RETRIES = 3


class SemanticScholarRetriever(BaseRetriever):
    name = "semantic_scholar"

    def search(self, query: str, limit: int) -> list[Paper]:
        params = {"query": query, "limit": max(1, min(int(limit), 100)), "fields": FIELDS}
        headers = {"User-Agent": USER_AGENT}
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = requests.get(API_URL, params=params, headers=headers,
                                    timeout=DEFAULT_TIMEOUT)
                if resp.status_code == 429:
                    if attempt < MAX_RETRIES:
                        wait = 2 ** attempt
                        logger.warning("[%s] 429 限流，%ds 后重试 (%d/%d)",
                                       self.name, wait, attempt + 1, MAX_RETRIES)
                        time.sleep(wait)
                        continue
                    raise RetrieverError("Semantic Scholar API 持续 429 限流")
                if resp.status_code >= 400:
                    raise RetrieverError(
                        f"Semantic Scholar API HTTP {resp.status_code}: {resp.text[:200]}")
                return self._parse(resp.json())
            except RetrieverError:
                raise
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    time.sleep(2 ** attempt)
                    continue
        raise RetrieverError(f"Semantic Scholar API 请求失败: {last_exc}")

    def _parse(self, payload: dict) -> list[Paper]:
        papers: list[Paper] = []
        for item in payload.get("data") or []:
            ext = item.get("externalIds") or {}
            doi = ext.get("DOI")
            oa = item.get("openAccessPdf") or {}
            paper_id = item.get("paperId") or ""
            papers.append(Paper(
                id=f"s2:{paper_id}",
                source="semantic_scholar",
                title=(item.get("title") or "").strip(),
                authors=[a.get("name", "").strip() for a in (item.get("authors") or [])
                         if a.get("name")],
                abstract=(item.get("abstract") or "").strip(),
                year=item.get("year"),
                venue=(item.get("venue") or "").strip(),
                doi=doi,
                url=f"https://www.semanticscholar.org/paper/{paper_id}" if paper_id else "",
                pdf_url=oa.get("url"),
                citation_count=item.get("citationCount"),
                keywords=[f for f in (item.get("fieldsOfStudy") or []) if f],
            ))
        logger.info("[%s] 返回 %d 篇", self.name, len(papers))
        return papers
