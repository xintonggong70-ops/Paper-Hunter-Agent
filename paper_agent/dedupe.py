"""跨来源去重（SPEC §5）。"""
from __future__ import annotations

import re

from .models import Paper

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def _norm_title(title: str) -> str:
    """标题归一化：小写、去标点、压缩空白。"""
    text = _PUNCT_RE.sub(" ", (title or "").lower())
    return _WS_RE.sub(" ", text).strip()


def _norm_arxiv_id(paper: Paper) -> str | None:
    """arXiv id 归一化（去版本号 vN）。"""
    if paper.source == "arxiv" and paper.id.startswith("arxiv:"):
        return re.sub(r"v\d+$", "", paper.id[len("arxiv:"):])
    # 其它来源 url 里可能带 arxiv abs 链接
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", paper.url or "")
    if m:
        return m.group(1)
    return None


def _dedupe_key(paper: Paper) -> tuple[str, str]:
    """按优先级 DOI 小写 > 标题归一化 > arXiv id 生成去重键。"""
    if paper.doi:
        return ("doi", paper.doi.lower().strip())
    norm = _norm_title(paper.title)
    if norm:
        return ("title", norm)
    ax = _norm_arxiv_id(paper)
    if ax:
        return ("arxiv", ax)
    return ("id", paper.id)


def _merge_into(keep: Paper, other: Paper) -> None:
    """把 other 中缺失的有价值字段补进 keep。"""
    if keep.citation_count is None:
        keep.citation_count = other.citation_count
    if not keep.abstract and other.abstract:
        keep.abstract = other.abstract
    if keep.pdf_url is None:
        keep.pdf_url = other.pdf_url
    if keep.doi is None:
        keep.doi = other.doi
    if keep.year is None:
        keep.year = other.year
    if not keep.venue:
        keep.venue = other.venue
    if not keep.url:
        keep.url = other.url
    for kw in other.keywords:
        if kw not in keep.keywords:
            keep.keywords.append(kw)


def _version_rank(p: Paper) -> tuple[bool, int]:
    """版本质量：citation_count 非空优先，其次 abstract 更长。"""
    return (p.citation_count is not None, len(p.abstract or ""))


def dedupe(papers: list[Paper]) -> list[Paper]:
    """按优先级 DOI 小写 > 标题归一化（小写去标点）> arXiv id 去重。
    重复时合并：保留 citation_count 非空的、abstract 更长的版本；source 保留首个。"""
    groups: dict[tuple[str, str], list[Paper]] = {}
    order: list[tuple[str, str]] = []
    for p in papers:
        key = _dedupe_key(p)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(p)

    result: list[Paper] = []
    for key in order:
        group = groups[key]
        first = group[0]
        # 选最优版本（citation_count 非空、abstract 更长）
        best = max(group, key=_version_rank)
        if best is not first:
            # source/id 保留首个，其余字段取最优版本
            merged = Paper(
                id=first.id, source=first.source,
                title=best.title or first.title,
                authors=best.authors or first.authors,
                abstract=best.abstract,
                year=best.year if best.year is not None else first.year,
                venue=best.venue or first.venue,
                doi=best.doi or first.doi,
                url=first.url or best.url,
                pdf_url=best.pdf_url or first.pdf_url,
                citation_count=best.citation_count,
                keywords=list(dict.fromkeys(best.keywords + first.keywords)),
            )
            keep = merged
        else:
            keep = first
        for other in group:
            if other is not keep and other is not best:
                _merge_into(keep, other)
        result.append(keep)
    return result
