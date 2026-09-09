"""Crossref REST API 检索器（SPEC §4）。

带 mailto UA（polite pool）。
"""
from __future__ import annotations

import html
import logging

import requests

from ..models import Paper
from .base import DEFAULT_TIMEOUT, BaseRetriever, RetrieverError

logger = logging.getLogger(__name__)

API_URL = "https://api.crossref.org/works"
MAILTO = "paperhunter@example.com"
USER_AGENT = f"PaperHunterAgent/0.1 (mailto:{MAILTO})"


class CrossrefRetriever(BaseRetriever):
    name = "crossref"

    def search(self, query: str, limit: int) -> list[Paper]:
        params = {
            "query": query,
            "rows": max(1, min(int(limit), 1000)),
            "filter": "type:journal-article",
        }
        headers = {"User-Agent": USER_AGENT}
        try:
            resp = requests.get(API_URL, params=params, headers=headers,
                                timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise RetrieverError(f"Crossref API 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise RetrieverError(
                f"Crossref API HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return self._parse(resp.json())
        except (ValueError, KeyError) as exc:
            raise RetrieverError(f"Crossref API 响应解析失败: {exc}") from exc

    @staticmethod
    def _year_from(item: dict) -> int | None:
        for key in ("published-print", "published-online", "issued", "created"):
            parts = (item.get(key) or {}).get("date-parts") or []
            if parts and parts[0] and isinstance(parts[0][0], int):
                return parts[0][0]
        return None

    @staticmethod
    def _abstract_from(item: dict) -> str:
        import re
        raw = item.get("abstract") or ""
        # Crossref 摘要常为 JATS XML，剥掉标签
        return re.sub(r"<[^>]+>", " ", raw).strip()

    def _parse(self, payload: dict) -> list[Paper]:
        papers: list[Paper] = []
        items = ((payload.get("message") or {}).get("items")) or []
        for item in items:
            doi = (item.get("DOI") or "").strip() or None
            titles = item.get("title") or []
            title = (titles[0] if titles else "").strip()
            authors = []
            for a in item.get("author") or []:
                name = " ".join(x for x in [a.get("given", ""), a.get("family", "")] if x).strip()
                if name:
                    authors.append(name)
            containers = item.get("container-title") or []
            venue = html.unescape((containers[0] if containers else "").strip())
            pdf_url = None
            for link in item.get("link") or []:
                if link.get("content-type") == "application/pdf" and link.get("URL"):
                    pdf_url = link["URL"]
                    break
            if not doi:
                continue  # Crossref 条目以 DOI 为主键，无 DOI 跳过
            papers.append(Paper(
                id=f"doi:{doi.lower()}",
                source="crossref",
                title=title,
                authors=authors,
                abstract=self._abstract_from(item),
                year=self._year_from(item),
                venue=venue,
                doi=doi,
                url=item.get("URL") or f"https://doi.org/{doi}",
                pdf_url=pdf_url,
                citation_count=item.get("is-referenced-by-count"),
                keywords=[s for s in (item.get("subject") or []) if s],
            ))
        logger.info("[%s] 返回 %d 篇", self.name, len(papers))
        return papers
