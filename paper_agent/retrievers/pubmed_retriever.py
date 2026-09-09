"""PubMed NCBI E-utilities 检索器（SPEC §4）。

esearch + efetch，XML 解析，无需 key（可选 email/tool 参数）。
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import requests

from ..models import Paper
from .base import DEFAULT_TIMEOUT, USER_AGENT, BaseRetriever, RetrieverError

logger = logging.getLogger(__name__)

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class PubMedRetriever(BaseRetriever):
    name = "pubmed"

    def __init__(self, email: str | None = None, tool: str = "paperhunter"):
        self.email = email
        self.tool = tool

    def _common_params(self) -> dict:
        params = {"tool": self.tool}
        if self.email:
            params["email"] = self.email
        return params

    def search(self, query: str, limit: int) -> list[Paper]:
        headers = {"User-Agent": USER_AGENT}
        limit = max(1, int(limit))
        # 1) esearch 拿 pmid 列表
        try:
            resp = requests.get(
                f"{EUTILS}/esearch.fcgi",
                params={**self._common_params(), "db": "pubmed", "term": query,
                        "retmax": limit, "retmode": "json", "sort": "relevance"},
                headers=headers, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise RetrieverError(f"PubMed esearch 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise RetrieverError(f"PubMed esearch HTTP {resp.status_code}")
        try:
            pmids = (resp.json().get("esearchresult") or {}).get("idlist") or []
        except ValueError as exc:
            raise RetrieverError(f"PubMed esearch 响应解析失败: {exc}") from exc
        if not pmids:
            logger.info("[%s] 无结果", self.name)
            return []
        # 2) efetch 拿 XML 详情
        try:
            resp = requests.get(
                f"{EUTILS}/efetch.fcgi",
                params={**self._common_params(), "db": "pubmed",
                        "id": ",".join(pmids), "retmode": "xml"},
                headers=headers, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise RetrieverError(f"PubMed efetch 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise RetrieverError(f"PubMed efetch HTTP {resp.status_code}")
        try:
            return self._parse(resp.content)
        except ET.ParseError as exc:
            raise RetrieverError(f"PubMed efetch 响应解析失败: {exc}") from exc

    def _parse(self, data: bytes) -> list[Paper]:
        root = ET.fromstring(data)
        papers: list[Paper] = []
        for art in root.iter("PubmedArticle"):
            med = art.find("MedlineCitation")
            if med is None:
                continue
            pmid_el = med.find("PMID")
            pmid = (pmid_el.text or "").strip() if pmid_el is not None and pmid_el.text else ""
            article = med.find("Article")
            if article is None:
                continue
            title_el = article.find("ArticleTitle")
            title = "".join(title_el.itertext()).strip() if title_el is not None else ""
            abstract_parts = []
            abs_el = article.find("Abstract")
            if abs_el is not None:
                for at in abs_el.iter("AbstractText"):
                    label = at.get("Label")
                    text = "".join(at.itertext()).strip()
                    abstract_parts.append(f"{label}: {text}" if label else text)
            abstract = " ".join(p for p in abstract_parts if p).strip()
            authors = []
            for au in article.iter("Author"):
                last = au.findtext("LastName") or ""
                fore = au.findtext("ForeName") or au.findtext("Initials") or ""
                name = " ".join(x for x in [fore, last] if x).strip()
                if name:
                    authors.append(name)
            year = None
            for ypath in ("Journal/JournalIssue/PubDate/Year",
                          "Journal/JournalIssue/PubDate/MedlineDate"):
                ytxt = article.findtext(ypath)
                if ytxt:
                    digits = "".join(ch for ch in ytxt[:4] if ch.isdigit())
                    if len(digits) == 4:
                        year = int(digits)
                        break
            venue = (article.findtext("Journal/Title")
                     or article.findtext("Journal/ISOAbbreviation") or "").strip()
            doi = None
            for aid in art.iter("ArticleId"):
                if aid.get("IdType") == "doi" and aid.text:
                    doi = aid.text.strip()
                    break
            # PMC 开放获取 PDF 链接
            pdf_url = None
            for aid in art.iter("ArticleId"):
                if aid.get("IdType") == "pmc" and aid.text:
                    pmc = aid.text.strip()
                    pdf_url = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc}/pdf/"
                    break
            keywords = []
            for kw in art.iter("Keyword"):
                if kw.text:
                    keywords.append(kw.text.strip())
            papers.append(Paper(
                id=f"pmid:{pmid}",
                source="pubmed",
                title=title,
                authors=authors,
                abstract=abstract,
                year=year,
                venue=venue,
                doi=doi,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                pdf_url=pdf_url,
                citation_count=None,
                keywords=keywords,
            ))
        logger.info("[%s] 返回 %d 篇", self.name, len(papers))
        return papers
