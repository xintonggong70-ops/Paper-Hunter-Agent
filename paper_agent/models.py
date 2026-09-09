"""核心数据结构（SPEC §3）。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Paper:
    id: str                      # "arxiv:2301.00001" / "s2:CorpusId:..." / "doi:10.xxxx" / "pmid:..."
    source: str                  # 'arxiv' | 'semantic_scholar' | 'crossref' | 'pubmed'
    title: str
    authors: list[str]
    abstract: str                # 可能为空字符串
    year: int | None
    venue: str                   # 期刊/会议名，可空
    doi: str | None
    url: str                     # 落地页
    pdf_url: str | None          # 开放获取 PDF 直链，可空
    citation_count: int | None
    keywords: list[str] = field(default_factory=list)
    relevance_score: float = 0.0   # 0-100
    authority_score: float = 0.0   # 0-100
    local_pdf: str | None = None   # 已下载 PDF 本地路径
    pdf_status: str = "pending"    # 'ok' | 'paywalled' | 'no_pdf' | 'error' | 'pending'
    pdf_note: str = ""             # 失败原因（如 "需要机构认证"）
    zotero_key: str | None = None


@dataclass
class PaperAnalysis:
    paper_id: str
    summary: str                 # 内容大意（中文，150-300字）
    deep_read: str               # 建议精读的部分及理由
    skim: str                    # 建议略读/可跳过的部分及理由
    questions: list[str]         # 进一步思考/研究问题 2-4 条
    cluster: str                 # 主题聚类标签（用于笔记互联）
