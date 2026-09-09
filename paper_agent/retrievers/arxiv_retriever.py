"""arXiv 官方 Atom API 检索器（SPEC §4）。

标准库 urllib + xml.etree 解析，不用第三方 arxiv 包。
"""
from __future__ import annotations

import logging
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from ..models import Paper
from .base import DEFAULT_TIMEOUT, USER_AGENT, BaseRetriever, RetrieverError

logger = logging.getLogger(__name__)

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
API_URL = "http://export.arxiv.org/api/query"


class ArxivRetriever(BaseRetriever):
    name = "arxiv"

    def search(self, query: str, limit: int) -> list[Paper]:
        params = urllib.parse.urlencode({
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max(1, int(limit)),
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        url = f"{API_URL}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                data = resp.read()
        except Exception as exc:  # noqa: BLE001
            raise RetrieverError(f"arXiv API 请求失败: {exc}") from exc
        try:
            return self._parse(data)
        except ET.ParseError as exc:
            raise RetrieverError(f"arXiv API 响应解析失败: {exc}") from exc

    def _parse(self, data: bytes) -> list[Paper]:
        root = ET.fromstring(data)
        papers: list[Paper] = []
        for entry in root.findall(f"{ATOM}entry"):
            raw_id = (entry.findtext(f"{ATOM}id") or "").strip()
            # http://arxiv.org/abs/2301.00001v2 -> 2301.00001v2
            arxiv_id = raw_id.rsplit("/abs/", 1)[-1]
            title = " ".join((entry.findtext(f"{ATOM}title") or "").split())
            abstract = " ".join((entry.findtext(f"{ATOM}summary") or "").split())
            authors = [
                (a.findtext(f"{ATOM}name") or "").strip()
                for a in entry.findall(f"{ATOM}author")
                if (a.findtext(f"{ATOM}name") or "").strip()
            ]
            published = (entry.findtext(f"{ATOM}published") or "").strip()
            year = int(published[:4]) if published[:4].isdigit() else None
            doi_el = entry.findtext(f"{ARXIV}doi")
            doi = doi_el.strip() if doi_el else None
            # SPEC: pdf_url 统一构造为 https://arxiv.org/pdf/{id}.pdf
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf" if arxiv_id else None
            keywords = [c.get("term", "").strip() for c in entry.findall(f"{ATOM}category")
                        if c.get("term")]
            papers.append(Paper(
                id=f"arxiv:{arxiv_id}",
                source="arxiv",
                title=title,
                authors=authors,
                abstract=abstract,
                year=year,
                venue="arXiv",
                doi=doi,
                url=raw_id or f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=pdf_url,
                citation_count=None,
                keywords=keywords,
            ))
        logger.info("[%s] 返回 %d 篇", self.name, len(papers))
        return papers
