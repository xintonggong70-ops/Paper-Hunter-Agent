"""内容分析模块测试：PDF 结构解析 + 语言跟随 + 章节/图表级建议。"""
from __future__ import annotations

import re

import fitz  # PyMuPDF
import pytest

from paper_agent.analysis import analyze_papers
from paper_agent.models import Paper
from paper_agent.pdf_parser import detect_language, parse_pdf


def _make_pdf(path, pages_lines, fontname="helv"):
    """程序化生成一个带结构的假 PDF。"""
    doc = fitz.open()
    for lines in pages_lines:
        page = doc.new_page()
        y = 72
        for ln in lines:
            page.insert_text((72, y), ln, fontsize=11, fontname=fontname)
            y += 18
    doc.save(str(path))
    doc.close()


_EN_PAGES = [
    [
        "Abstract",
        "We propose a novel graph encoder architecture for molecule property "
        "prediction. Our approach combines message passing with a transformer "
        "decoder and achieves state of the art results on several benchmarks.",
        "1 Introduction",
        "Graph neural networks have become a standard tool for molecular "
        "modeling in drug discovery and materials science applications.",
        "2 Related Work",
        "Prior work on message passing neural networks and graph transformers "
        "has explored many variants of aggregation functions and attention.",
    ],
    [
        "3 Method",
        "Our framework consists of a graph encoder followed by a task head. "
        "The encoder iteratively refines node representations over layers.",
        "Figure 1: The overall architecture of our framework, showing the "
        "graph encoder and the prediction pipeline.",
        "3.1 Data",
        "We use public benchmark datasets for all experiments reported here.",
        "4 Experiments",
        "Table 1: Results on benchmark datasets compared with baselines.",
        "We compare against strong baselines and report mean and standard "
        "deviation over five random seeds for each dataset.",
        "5 Conclusion",
        "We presented a graph encoder that improves property prediction and "
        "plan to extend it to larger molecular systems in future work.",
    ],
]

_ZH_PAGES = [
    [
        "摘要",
        "本文提出一种面向分子性质预测的图编码器架构，结合消息传递与注意力机制，"
        "在多个公开基准数据集上取得了领先的效果，验证了方法的有效性。",
        "1 引言",
        "图神经网络已经成为分子建模与药物发现中的标准工具，"
        "近年来在性质预测任务上取得了显著进展，受到学界广泛关注。",
        "2 相关工作",
        "已有工作在消息传递网络与图Transformer方面提出了多种聚合与注意力变体，"
        "为本文方法提供了重要基础。",
    ],
    [
        "3 方法",
        "本文框架由图编码器与任务头组成，编码器逐层迭代更新节点表示，"
        "并通过注意力机制捕获长程依赖关系，提升模型表达能力。",
        "4 实验",
        "我们在公开基准数据集上与多个强基线方法进行了对比，"
        "实验结果表明本文方法在多数指标上均取得最优或次优表现。",
        "5 结论",
        "本文提出的图编码器有效提升了性质预测精度，未来将扩展到更大规模的"
        "分子体系与更多下游任务中，进一步验证其泛化能力。",
    ],
]


def _paper(pid: str, title: str, abstract: str = "",
           local_pdf: str | None = None) -> Paper:
    return Paper(
        id=pid, source="arxiv", title=title, authors=["Alice", "Bob"],
        abstract=abstract, year=2024, venue="TestConf", doi=None,
        url="https://example.org", pdf_url=None, citation_count=10,
        local_pdf=local_pdf,
        pdf_status="ok" if local_pdf else "no_pdf",
    )


# ---------------- pdf_parser ----------------

def test_parse_pdf_sections_figures_language(tmp_path):
    pdf = tmp_path / "fake_en.pdf"
    _make_pdf(pdf, _EN_PAGES)
    parsed = parse_pdf(str(pdf))
    assert parsed is not None
    numbers = {s.number for s in parsed.sections}
    assert {"1", "2", "3", "3.1", "4", "5"} <= numbers
    titles = {s.title for s in parsed.sections}
    assert "Method" in titles and "Conclusion" in titles
    fig_labels = [l for l, _ in parsed.figures]
    assert "Figure 1" in fig_labels
    tab_labels = [l for l, _ in parsed.tables]
    assert "Table 1" in tab_labels
    assert parsed.language == "en"
    assert parsed.n_pages == 2
    # preview 取自正文而非标题本身
    method = next(s for s in parsed.sections if s.number == "3")
    assert "encoder" in method.preview.lower()


def test_parse_pdf_robustness(tmp_path):
    assert parse_pdf(str(tmp_path / "missing.pdf")) is None
    bad = tmp_path / "not_a_pdf.pdf"
    bad.write_text("this is not a pdf", encoding="utf-8")
    assert parse_pdf(str(bad)) is None
    blank = tmp_path / "blank.pdf"
    _make_pdf(blank, [[""]])  # 无文本层
    assert parse_pdf(str(blank)) is None


def test_detect_language():
    assert detect_language("Graph neural networks are powerful models "
                           "for structured data across domains.") == "en"
    assert detect_language("本文研究图神经网络在分子性质预测中的应用与改进方法。") == "zh"
    assert detect_language("") == "unknown"


# ---------------- 离线分析：英文论文 + 全文结构 ----------------

@pytest.fixture()
def en_pdf(tmp_path):
    pdf = tmp_path / "en.pdf"
    _make_pdf(pdf, _EN_PAGES)
    return str(pdf)


def test_offline_analysis_cites_real_sections(en_pdf):
    p = _paper("arxiv:1", "A Graph Encoder for Molecule Property Prediction",
               abstract="We propose a graph encoder architecture with strong "
                        "experimental results on molecular benchmarks.",
               local_pdf=en_pdf)
    out = analyze_papers("graph neural networks", [p], cfg={})
    a = out[p.id]
    parsed = parse_pdf(en_pdf)
    real_secs = {s.number for s in parsed.sections if s.number}
    real_figs = {l for l, _ in parsed.figures}

    # deep_read/skim 引用的 Section 编号必须真实存在
    for field in (a.deep_read, a.skim):
        for num in re.findall(r"Section\s+(\d+(?:\.\d+)*)", field):
            assert num in real_secs, f"编造了不存在的章节号 {num}"
        for lab in re.findall(r"(Figure\s+\d+)", field):
            assert lab in real_figs, f"编造了不存在的图 {lab}"
    # 确实引用了具体章节与图表
    assert re.search(r"Section\s+3\b", a.deep_read)
    assert "Figure 1" in a.deep_read
    # 英文论文输出为英文（不含连续 4 个以上中文字符）
    for field in (a.summary, a.deep_read, a.skim, " ".join(a.questions)):
        assert not re.search(r"[一-鿿]{4,}", field), field
    assert a.cluster  # 聚类已赋值


def test_offline_analysis_no_pdf_honest_summary():
    p = _paper("arxiv:2", "Sparse Mixture-of-Experts Routing for Long Context")
    out = analyze_papers("mixture of experts", [p], cfg={})
    a = out[p.id]
    assert a.summary and a.deep_read and a.skim and a.questions
    assert "建议先阅读标题" not in a.summary
    assert "未获取到摘要，建议先阅读" not in a.summary
    assert "abstract unavailable" in a.summary  # 诚实注明依据


def test_offline_analysis_chinese_pdf(tmp_path):
    pdf = tmp_path / "zh.pdf"
    _make_pdf(pdf, _ZH_PAGES, fontname="china-s")
    parsed = parse_pdf(str(pdf))
    assert parsed is not None
    assert parsed.language == "zh"
    p = _paper("arxiv:3", "面向分子性质预测的图编码器研究",
               local_pdf=str(pdf))
    out = analyze_papers("图神经网络", [p], cfg={})
    a = out[p.id]
    assert "精读" in a.deep_read
    assert re.search(r"第\s*3\s*节", a.deep_read)
    assert re.search(r"[一-鿿]{4,}", a.summary + a.deep_read + a.skim)
