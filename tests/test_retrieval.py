"""检索 / 去重 / 打分模块测试（SPEC §15）。

- 每个 retriever 真实 API 小样本测试（limit=3），网络不可达时 skip。
- dedupe / scoring 用构造的假数据测试。
"""
from __future__ import annotations

import socket

import pytest

from paper_agent.dedupe import dedupe
from paper_agent.models import Paper
from paper_agent.retrievers import (
    ArxivRetriever,
    CrossrefRetriever,
    PubMedRetriever,
    SemanticScholarRetriever,
)
from paper_agent.retrievers.base import BaseRetriever, RetrieverError
from paper_agent.router import select_retrievers
from paper_agent.scoring import authority, relevance, score_papers


def _network_available() -> bool:
    try:
        socket.setdefaulttimeout(5)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except OSError:
        return False


NETWORK = _network_available()
needs_network = pytest.mark.skipif(not NETWORK, reason="网络不可达，跳过真实 API 测试")


def _real_search(retriever, query, limit=3):
    """真实 API 调用；网络/服务端失败（超时、限流等）时 skip 而非 fail。"""
    try:
        return retriever.search(query, limit)
    except RetrieverError as exc:
        pytest.skip(f"{retriever.name} API 暂不可用: {exc}")


def _check_paper_fields(p: Paper) -> None:
    assert isinstance(p, Paper)
    assert p.id and isinstance(p.id, str)
    assert p.source in {"arxiv", "semantic_scholar", "crossref", "pubmed"}
    assert p.title.strip()
    assert isinstance(p.authors, list)
    assert isinstance(p.abstract, str)
    assert p.year is None or isinstance(p.year, int)
    assert isinstance(p.venue, str)
    assert p.url.strip()
    assert p.pdf_url is None or isinstance(p.pdf_url, str)
    assert p.citation_count is None or isinstance(p.citation_count, int)
    assert isinstance(p.keywords, list)


# ---------------- 真实 API 测试 ----------------

@needs_network
def test_arxiv_real():
    papers = _real_search(ArxivRetriever(), "graph neural network", 3)
    assert len(papers) >= 1
    for p in papers:
        _check_paper_fields(p)
        assert p.source == "arxiv"
        assert p.id.startswith("arxiv:")
        assert p.pdf_url and p.pdf_url.startswith("https://arxiv.org/pdf/")
        assert p.citation_count is None


@needs_network
def test_semantic_scholar_real():
    papers = _real_search(SemanticScholarRetriever(), "transformer", 3)
    assert len(papers) >= 1
    for p in papers:
        _check_paper_fields(p)
        assert p.source == "semantic_scholar"
        assert p.id.startswith("s2:")


@needs_network
def test_crossref_real():
    papers = _real_search(CrossrefRetriever(), "climate", 3)
    assert len(papers) >= 1
    for p in papers:
        _check_paper_fields(p)
        assert p.source == "crossref"
        assert p.id.startswith("doi:")
        assert p.doi


@needs_network
def test_pubmed_real():
    papers = _real_search(PubMedRetriever(), "CRISPR", 3)
    assert len(papers) >= 1
    for p in papers:
        _check_paper_fields(p)
        assert p.source == "pubmed"
        assert p.id.startswith("pmid:")


# ---------------- 降级行为 ----------------

def test_safe_search_degrades():
    class Broken(BaseRetriever):
        name = "broken"

        def search(self, query, limit):
            raise RetrieverError("boom")

    assert Broken().safe_search("x", 3) == []


# ---------------- 路由测试 ----------------

def test_router_explicit_domains():
    names = [r.name for r in select_retrievers("anything", "biomedical")]
    assert names == ["pubmed", "semantic_scholar", "crossref"]
    for d in ("cs", "physics", "math", "eess"):
        names = [r.name for r in select_retrievers("anything", d)]
        assert names == ["arxiv", "semantic_scholar", "crossref"]
    names = [r.name for r in select_retrievers("anything", "general")]
    assert names == ["semantic_scholar", "crossref", "arxiv"]


def test_router_auto_biomedical():
    names = [r.name for r in select_retrievers("CRISPR gene editing in cancer cells")]
    assert names[0] == "pubmed"
    names = [r.name for r in select_retrievers("蛋白质结构预测")]
    assert names[0] == "pubmed"


def test_router_auto_general():
    names = [r.name for r in select_retrievers("graph neural networks")]
    assert names == ["semantic_scholar", "crossref", "arxiv"]


# ---------------- 假数据 ----------------

def _mk(**kw) -> Paper:
    defaults = dict(
        id="x:1", source="arxiv", title="Some paper", authors=["A"],
        abstract="an abstract", year=2020, venue="", doi=None,
        url="http://example.com", pdf_url=None, citation_count=None,
    )
    defaults.update(kw)
    return Paper(**defaults)


# ---------------- 去重测试 ----------------

def test_dedupe_by_doi_case_insensitive():
    a = _mk(id="doi:10.1/ABC", source="crossref", doi="10.1/ABC", citation_count=None)
    b = _mk(id="s2:zzz", source="semantic_scholar", doi="10.1/abc",
            citation_count=42, abstract="a much longer abstract here")
    out = dedupe([a, b])
    assert len(out) == 1
    assert out[0].source == "crossref"          # source 保留首个
    assert out[0].citation_count == 42          # 保留 citation_count 非空版本


def test_dedupe_by_normalized_title():
    a = _mk(id="arxiv:1", title="Graph Neural Networks: A Survey!", doi=None)
    b = _mk(id="s2:2", source="semantic_scholar",
            title="graph neural networks a survey", doi=None, citation_count=10)
    out = dedupe([a, b])
    assert len(out) == 1
    assert out[0].citation_count == 10


def test_dedupe_by_arxiv_id_version():
    a = _mk(id="arxiv:2301.00001v1", doi=None, title="")
    b = _mk(id="arxiv:2301.00001v2", doi=None, title="")
    out = dedupe([a, b])
    # 标题为空时按 arXiv id（去版本号）去重
    assert len(out) == 1


def test_dedupe_keeps_distinct():
    a = _mk(id="doi:10.1/a", doi="10.1/a")
    b = _mk(id="doi:10.1/b", doi="10.1/b", title="Another paper")
    assert len(dedupe([a, b])) == 2


# ---------------- 打分测试 ----------------

def test_relevance_ordering():
    p_rel = _mk(title="graph neural networks",
                abstract="graph neural networks for molecule graphs")
    p_unrel = _mk(title="banana farming", abstract="tropical fruit cultivation")
    assert relevance("graph neural networks", p_rel) > relevance("graph neural networks", p_unrel)


def test_relevance_no_abstract_penalty():
    p_full = _mk(title="graph neural networks", abstract="graph neural networks methods")
    p_noabs = _mk(title="graph neural networks", abstract="")
    r_full = relevance("graph neural networks", p_full)
    r_noabs = relevance("graph neural networks", p_noabs)
    assert 0 <= r_noabs <= 100 and 0 <= r_full <= 100
    # 标题相同时无摘要应打折
    assert r_noabs < r_full


def test_authority_components():
    top = _mk(venue="Nature", citation_count=1000, year=__import__("datetime").date.today().year)
    plain = _mk(venue="Some Journal", citation_count=None, year=2000)
    empty = _mk(venue="", citation_count=None, year=None)
    a_top = authority(top, max_citations=1000)
    a_plain = authority(plain, max_citations=1000)
    a_empty = authority(empty, max_citations=1000)
    assert a_top > a_plain > a_empty
    assert 0 <= a_empty <= 100 and 0 <= a_top <= 100
    # 顶级 venue + 满引用 + 最新 ≈ 满分
    assert a_top == pytest.approx(100.0)


def test_score_papers_in_place():
    papers = [
        _mk(id="1", title="graph neural networks", abstract="gnn methods",
            venue="NeurIPS", citation_count=500, year=2023),
        _mk(id="2", title="banana farming", abstract="", venue="",
            citation_count=None, year=1990),
    ]
    score_papers("graph neural networks", papers)
    for p in papers:
        assert 0 <= p.relevance_score <= 100
        assert 0 <= p.authority_score <= 100
    assert papers[0].relevance_score > papers[1].relevance_score
    assert papers[0].authority_score > papers[1].authority_score


def test_score_papers_empty():
    score_papers("anything", [])  # 不应抛异常
