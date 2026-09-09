"""相关度/权威度打分（SPEC §6）。"""
from __future__ import annotations

import logging
import math
from datetime import date

from .models import Paper

logger = logging.getLogger(__name__)

# 顶级 venue 内置名单（大小写不敏感子串匹配）
TOP_VENUES = [
    "nature", "science", "cell", "new england journal of medicine", "nejm",
    "lancet", "physical review letters", "prl", "cvpr", "neurips", "icml",
    "iclr", "pnas", "jama", "bmj", "ieee transactions on pattern analysis",
    "journal of the american chemical society", "angewandte chemie",
    "astrophysical journal", "nature medicine", "nature biotechnology",
]


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _tfidf_cosine(query: str, docs: list[str]) -> list[float]:
    """query 与各 doc 的 TF-IDF 余弦相似度列表。"""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    corpus = [query] + docs
    vectorizer = TfidfVectorizer(token_pattern=r"(?u)\b\w+\b")
    try:
        mat = vectorizer.fit_transform(corpus)
    except ValueError:
        return [0.0] * len(docs)
    sims = cosine_similarity(mat[0:1], mat[1:])[0]
    return [float(s) for s in sims]


def relevance(query: str, paper: Paper) -> float:
    """sklearn TfidfVectorizer: corpus = [query, title + ' ' + abstract]，
    query 与本文的 cosine similarity × 100。无 abstract 时只用标题（结果×0.6）。"""
    has_abstract = bool((paper.abstract or "").strip())
    doc = paper.title or ""
    if has_abstract:
        doc = f"{paper.title} {paper.abstract}"
    sim = _tfidf_cosine(query, [doc])[0]
    score = sim * 100.0
    if not has_abstract:
        score *= 0.6
    return _clamp(score)


def authority(paper: Paper, max_citations: int | None = None) -> float:
    """0-100 组合：
    - 引用数：log1p(citation_count)/log1p(max_citations_in_batch) × 50（None 记 0）
    - 来源权重 ×30：顶级 venue=30；有 venue=18；无=8
    - 新鲜度 ×20：近3年=20，近5年=15，近10年=10，更早=5
    结果 clamp 到 [0,100]。"""
    # 引用数分量
    if max_citations is None:
        max_citations = paper.citation_count or 0
    if paper.citation_count and max_citations > 0:
        cite_score = (math.log1p(paper.citation_count)
                      / math.log1p(max_citations)) * 50.0
    else:
        cite_score = 0.0

    # 来源权重分量
    venue_lower = (paper.venue or "").lower()
    if venue_lower and any(tv in venue_lower for tv in TOP_VENUES):
        venue_score = 30.0
    elif venue_lower:
        venue_score = 18.0
    else:
        venue_score = 8.0

    # 新鲜度分量
    if paper.year is None:
        fresh_score = 5.0
    else:
        age = date.today().year - paper.year
        if age <= 3:
            fresh_score = 20.0
        elif age <= 5:
            fresh_score = 15.0
        elif age <= 10:
            fresh_score = 10.0
        else:
            fresh_score = 5.0

    return _clamp(cite_score + venue_score + fresh_score)


def score_papers(query: str, papers: list[Paper]) -> None:
    """原地写入 relevance_score / authority_score（0-100）。"""
    if not papers:
        return
    # 相关度：一次性向量化整个批次
    has_abstract = [bool((p.abstract or "").strip()) for p in papers]
    docs = [(f"{p.title} {p.abstract}" if has else (p.title or ""))
            for p, has in zip(papers, has_abstract)]
    sims = _tfidf_cosine(query, docs)
    for p, sim, has in zip(papers, sims, has_abstract):
        score = sim * 100.0
        if not has:
            score *= 0.6
        p.relevance_score = _clamp(score)

    # 权威度：批次内最大引用数归一化
    max_citations = max((p.citation_count or 0) for p in papers)
    for p in papers:
        p.authority_score = authority(p, max_citations=max_citations)
