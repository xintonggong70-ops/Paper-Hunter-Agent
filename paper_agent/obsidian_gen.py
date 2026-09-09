"""Obsidian 笔记文件夹生成（SPEC §11）。

generate_vault() 生成：
  out_dir/
  ├── 00-MOC-{安全化query}.md   # 按 cluster 分组的 Map of Content
  └── papers/
      └── {safe_title}.md       # 每篇论文一篇笔记

保证：frontmatter 为合法 YAML；所有 wikilink 目标与实际文件名一致（零死链）。
"""

from __future__ import annotations

import os
import re
from datetime import datetime

import yaml

from .models import Paper, PaperAnalysis

# Obsidian / 文件系统非法字符
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')
_WS = re.compile(r"\s+")

NOTE_DIR = "papers"


def _safe_name(text: str, maxlen: int = 80) -> str:
    """文件名 safe 化：去 Obsidian 非法字符，压缩空白，截断 maxlen 字符。"""
    name = _ILLEGAL_CHARS.sub("", text or "")
    name = _WS.sub(" ", name).strip().strip(".")
    if not name:
        name = "untitled"
    return name[:maxlen].rstrip(" .")


def _short_id(paper_id: str) -> str:
    """从 paper.id 提取短标识用于文件名冲突消歧。"""
    tail = paper_id.split(":", 1)[-1] if ":" in paper_id else paper_id
    tail = _safe_name(tail, 24)
    return tail or "x"


def assign_note_names(papers: list[Paper]) -> dict[str, str]:
    """paper.id -> 笔记名（不含 .md）。冲突时追加短 id。"""
    names: dict[str, str] = {}
    used: set[str] = set()
    for p in papers:
        base = _safe_name(p.title, 80)
        name = base
        if name in used:
            suffix = _short_id(p.id)
            name = f"{base}-{suffix}"
            i = 2
            while name in used:
                name = f"{base}-{suffix}-{i}"
                i += 1
        used.add(name)
        names[p.id] = name
    return names


def _cluster_slug(cluster: str) -> str:
    slug = _WS.sub("-", (cluster or "").strip())
    slug = _ILLEGAL_CHARS.sub("", slug)
    return slug or "uncategorized"


def composite_score(paper: Paper) -> float:
    return 0.6 * paper.relevance_score + 0.4 * paper.authority_score


def _front_matter(paper: Paper, analysis: PaperAnalysis | None) -> str:
    cluster = analysis.cluster if analysis else ""
    data = {
        "title": paper.title,
        "source": paper.source,
        "year": paper.year,
        "authors": list(paper.authors),
        "doi": paper.doi,
        "url": paper.url,
        "relevance": round(paper.relevance_score),
        "authority": round(paper.authority_score),
        "cluster": cluster,
        "tags": ["paper", _cluster_slug(cluster), paper.source],
    }
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False).strip()


def _fmt(value, default: str = "—") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _info_callout(paper: Paper) -> str:
    zotero = "已导入" if paper.zotero_key else "未导入"
    pdf = paper.pdf_status
    if paper.pdf_note:
        pdf = f"{pdf}（{paper.pdf_note}）"
    rows = [
        ("作者", "; ".join(paper.authors) if paper.authors else "—"),
        ("年份", _fmt(paper.year)),
        ("期刊/会议", _fmt(paper.venue)),
        ("来源", paper.source),
        ("DOI", _fmt(paper.doi)),
        ("链接", f"[{paper.url}]({paper.url})" if paper.url else "—"),
        ("引用数", _fmt(paper.citation_count)),
        ("相关度", f"{paper.relevance_score:.1f}"),
        ("权威度", f"{paper.authority_score:.1f}"),
        ("PDF 状态", pdf),
        ("Zotero 状态", zotero),
    ]
    lines = ["> [!info] 元信息", "> | 项目 | 内容 |", "> | --- | --- |"]
    for k, v in rows:
        v = str(v).replace("|", "\\|").replace("\n", " ")
        lines.append(f"> | {k} | {v} |")
    return "\n".join(lines)


def _one_sentence(text: str, maxlen: int = 60) -> str:
    """取摘要第一句并截断，用于 MOC 行内简介。"""
    text = _WS.sub(" ", (text or "")).strip()
    if not text:
        return ""
    for sep in ("。", "；"):
        idx = text.find(sep)
        if 0 < idx:
            return text[: idx + 1]
    idx = text.find(". ")
    if 0 < idx:
        return text[:idx + 1]
    if len(text) > maxlen:
        text = text[: maxlen - 1].rstrip() + "…"
    return text


def _render_note(paper: Paper, analysis: PaperAnalysis | None,
                 papers: list[Paper], analyses: dict[str, PaperAnalysis],
                 note_names: dict[str, str], moc_name: str) -> str:
    cluster = analysis.cluster if analysis else ""
    parts: list[str] = []
    parts.append("---")
    parts.append(_front_matter(paper, analysis))
    parts.append("---")
    parts.append("")
    parts.append(f"# {paper.title}")
    parts.append("")
    parts.append(_info_callout(paper))
    parts.append("")

    parts.append("## 内容大意")
    parts.append("")
    parts.append(analysis.summary if analysis and analysis.summary else "（暂无分析）")
    parts.append("")

    parts.append("## 建议精读")
    parts.append("")
    parts.append(analysis.deep_read if analysis and analysis.deep_read else "（暂无建议）")
    parts.append("")

    parts.append("## 建议略读")
    parts.append("")
    parts.append(analysis.skim if analysis and analysis.skim else "（暂无建议）")
    parts.append("")

    parts.append("## 进一步思考")
    parts.append("")
    if analysis and analysis.questions:
        for q in analysis.questions:
            parts.append(f"- {q}")
    else:
        parts.append("- （暂无）")
    parts.append("")

    parts.append("## 相关论文")
    parts.append("")
    related = [
        rp for rp in papers
        if rp.id != paper.id
        and cluster
        and analyses.get(rp.id) is not None
        and analyses[rp.id].cluster == cluster
    ]
    related.sort(key=composite_score, reverse=True)
    if related:
        for rp in related[:8]:
            parts.append(f"- [[{note_names[rp.id]}]] — 同属「{cluster}」主题")
    else:
        parts.append("- （同主题暂无其他论文）")
    parts.append("")
    parts.append("---")
    parts.append("")
    parts.append(f"返回 [[{moc_name}|📑 返回目录]]")
    parts.append("")
    return "\n".join(parts)


def _render_moc(query: str, papers: list[Paper],
                analyses: dict[str, PaperAnalysis],
                note_names: dict[str, str]) -> str:
    clusters: dict[str, list[Paper]] = {}
    for p in papers:
        a = analyses.get(p.id)
        clusters.setdefault(a.cluster if a else "未分类", []).append(p)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []
    fm = {
        "title": f"{query} — 论文检索总览",
        "type": "MOC",
        "query": query,
        "paper_count": len(papers),
        "generated": now,
        "tags": ["MOC", "paper-agent"],
    }
    lines.append("---")
    lines.append(yaml.safe_dump(fm, allow_unicode=True, sort_keys=False).strip())
    lines.append("---")
    lines.append("")
    lines.append(f"# 📑 {query} — 论文检索 MOC")
    lines.append("")
    lines.append(f"- 检索时间：{now}")
    lines.append(f"- 论文数量：{len(papers)}")
    lines.append(f"- 主题聚类：{len(clusters)} 个")
    lines.append("")
    for cluster in sorted(clusters):
        members = sorted(clusters[cluster], key=composite_score, reverse=True)
        lines.append(f"## {cluster}（{len(members)} 篇）")
        lines.append("")
        for p in members:
            brief = _one_sentence(analyses[p.id].summary) if p.id in analyses else ""
            seg = f"- [[{note_names[p.id]}]]"
            if brief:
                seg += f" - {brief}"
            seg += f" - 综合分 {composite_score(p):.1f}"
            lines.append(seg)
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> 本目录由 PaperHunter Agent 自动生成。")
    lines.append("")
    return "\n".join(lines)


def generate_vault(query: str, papers: list[Paper],
                   analyses: dict[str, PaperAnalysis], out_dir: str) -> None:
    """生成 Obsidian 笔记文件夹（SPEC §11）。"""
    papers_dir = os.path.join(out_dir, NOTE_DIR)
    os.makedirs(papers_dir, exist_ok=True)

    moc_name = f"00-MOC-{_safe_name(query, 60)}"
    note_names = assign_note_names(papers)

    for p in papers:
        content = _render_note(p, analyses.get(p.id), papers, analyses,
                               note_names, moc_name)
        path = os.path.join(papers_dir, f"{note_names[p.id]}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    moc_path = os.path.join(out_dir, f"{moc_name}.md")
    with open(moc_path, "w", encoding="utf-8") as f:
        f.write(_render_moc(query, papers, analyses, note_names))
