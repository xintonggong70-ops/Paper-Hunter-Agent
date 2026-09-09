"""领域→来源路由（SPEC §4.1）。"""
from __future__ import annotations

import re

from .retrievers import (
    ArxivRetriever,
    BaseRetriever,
    CrossrefRetriever,
    PubMedRetriever,
    SemanticScholarRetriever,
)

BIOMEDICAL_KEYWORDS = [
    "基因", "蛋白", "细胞", "临床", "疾病", "药物",
    "drug", "clinical", "protein", "gene", "disease",
    "cell", "cancer", "genome", "genomic", "patient",
    "biomedical", "medicine", "medical", "therapy",
    "antibody", "virus", "crispr", "rna", "dna",
]

_DOMAIN_MAP = {
    "biomedical": [PubMedRetriever, SemanticScholarRetriever, CrossrefRetriever],
    "cs": [ArxivRetriever, SemanticScholarRetriever, CrossrefRetriever],
    "physics": [ArxivRetriever, SemanticScholarRetriever, CrossrefRetriever],
    "math": [ArxivRetriever, SemanticScholarRetriever, CrossrefRetriever],
    "eess": [ArxivRetriever, SemanticScholarRetriever, CrossrefRetriever],
    "general": [SemanticScholarRetriever, CrossrefRetriever, ArxivRetriever],
}


def infer_domain(query: str) -> str:
    """从 query 关键词推断领域：含生物医学词 -> 'biomedical'，否则 'general'。"""
    text = query.lower()
    for kw in BIOMEDICAL_KEYWORDS:
        if re.search(re.escape(kw.lower()), text):
            return "biomedical"
    return "general"


def select_retrievers(query: str, domain: str | None = None) -> list[BaseRetriever]:
    """
    domain 可为 None/'auto'（自动从 query 关键词推断）或显式：
    'biomedical' -> [PubMed, SemanticScholar, Crossref]
    'cs'/'physics'/'math'/'eess' -> [ArXiv, SemanticScholar, Crossref]
    'general' -> [SemanticScholar, Crossref, ArXiv]
    判断 biomedical：query 含基因/蛋白/细胞/drug/clinical/protein/gene/disease 等词。
    """
    if domain is None or str(domain).lower() in ("auto", "none", ""):
        domain = infer_domain(query)
    retriever_classes = _DOMAIN_MAP.get(str(domain).lower(), _DOMAIN_MAP["general"])
    return [cls() for cls in retriever_classes]
