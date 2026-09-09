"""论文笔记内容分析（SPEC §7）。

若配置了 OpenAI 兼容 LLM（config.yaml 的 llm 段），逐篇生成
内容大意 / 精读建议 / 略读建议 / 进一步思考，并统一聚类命名；
否则离线启发式降级，保证全流程可用。

v2 升级：
- 语言跟随论文原文（英文论文 → 英文输出，不翻译成中文）。
- 若 PDF 已下载，先用 pdf_parser 解析章节骨架与图表，
  精读/略读建议精确到真实章节编号与图表标签，绝不编造。
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter

import requests

from .models import Paper, PaperAnalysis
from .pdf_parser import ParsedPdf, SectionInfo, detect_language, parse_pdf

logger = logging.getLogger(__name__)

_LLM_TIMEOUT = 60


def _llm_configured(cfg: dict) -> bool:
    llm = cfg.get("llm") or {}
    return bool(str(llm.get("base_url") or "").strip()
                and str(llm.get("api_key") or "").strip()
                and str(llm.get("model") or "").strip())


def _chat(cfg: dict, prompt: str) -> str:
    """调用 OpenAI 兼容 chat completions 接口，返回文本内容。"""
    llm = cfg["llm"]
    url = str(llm["base_url"]).rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {llm['api_key']}",
               "Content-Type": "application/json"}
    payload = {
        "model": llm["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=_LLM_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _extract_json(text: str) -> dict:
    """从 LLM 回复中提取 JSON 对象（容忍 ```json 包裹）。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("LLM 回复中未找到 JSON")
    return json.loads(m.group(0))


# ---------------- 语言与全文结构 ----------------

def _get_parsed(paper: Paper) -> ParsedPdf | None:
    """若本地 PDF 存在则解析结构；任何失败返回 None。"""
    path = paper.local_pdf or ""
    if not path or not os.path.isfile(path):
        return None
    try:
        return parse_pdf(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("PDF 解析失败（%s）：%s", paper.id, exc)
        return None


def _paper_language(paper: Paper, parsed: ParsedPdf | None) -> str:
    """论文语言：优先 PDF 解析结果，否则按标题+摘要检测；unknown 时默认中文。"""
    if parsed and parsed.language in ("en", "zh"):
        return parsed.language
    lang = detect_language((paper.title or "") + "\n" + (paper.abstract or ""))
    return lang if lang in ("en", "zh") else "zh"


def _find_section(parsed: ParsedPdf | None, *keywords: str) -> SectionInfo | None:
    if not parsed:
        return None
    for s in parsed.sections:
        low = s.title.lower()
        if any(k in low for k in keywords):
            return s
    return None


# ---------------- LLM 模式 ----------------

_FIELDS_SPEC_ZH = """{
  "summary": "内容大意，150-300字，概括研究问题、方法、主要结果与结论",
  "deep_read": "建议精读的部分及理由，必须引用真实章节编号/图表标签（如 Section 3.2、Figure 1），100字左右",
  "skim": "建议略读或可跳过的部分及理由，同样引用真实章节编号/图表标签，80字左右",
  "questions": ["2-4条基于本文的进一步思考或可延伸的研究问题"]
}"""

_FIELDS_SPEC_EN = """{
  "summary": "paper summary, 150-300 words: problem, method, key results, conclusion",
  "deep_read": "sections/figures worth a deep read WITH reasons; must cite real section numbers and figure/table labels (e.g. Section 3.2, Figure 1)",
  "skim": "sections/figures safe to skim or skip, citing real section numbers/labels",
  "questions": ["2-4 follow-up thoughts or research questions inspired by this paper"]
}"""


def _structure_block(parsed: ParsedPdf | None) -> str:
    """把解析出的章节/图表清单渲染成 prompt 文本。"""
    if not parsed:
        return ""
    lines: list[str] = []
    if parsed.sections:
        lines.append("Full-text sections (number | title | page | first-200-chars preview):")
        for s in parsed.sections:
            label = f"Section {s.number}" if s.number else "(unnumbered)"
            lines.append(f"  - {label} | {s.title} | p.{s.page} | {s.preview[:200]}")
    if parsed.figures:
        lines.append("Figures: " + "; ".join(f"{l}: {c[:120]}" for l, c in parsed.figures))
    if parsed.tables:
        lines.append("Tables: " + "; ".join(f"{l}: {c[:120]}" for l, c in parsed.tables))
    return "\n".join(lines)


def _single_prompt(query: str, paper: Paper, parsed: ParsedPdf | None,
                   lang: str) -> str:
    structure = _structure_block(parsed)
    if lang == "en":
        return f"""You are a research assistant. Read the paper information below and output ONE JSON object (nothing else):
{_FIELDS_SPEC_EN}

Rules:
- The paper is written in English. Write ALL fields in English (keep original terminology; do NOT translate into Chinese).
- In deep_read/skim, only cite section numbers and figure/table labels that appear in the provided lists. Never invent. If no structure is provided, give generic advice without any numbering.

Search topic: {query}
Title: {paper.title}
Authors: {", ".join(paper.authors[:5])}
Year: {paper.year or "unknown"}  Venue: {paper.venue or "unknown"}
Abstract: {paper.abstract[:3000] or "(no abstract)"}

{structure or "(full text unavailable; base advice on the abstract only)"}
"""
    return f"""你是一名科研助理。请阅读以下论文信息，用中文输出一个 JSON 对象（不要输出其他内容）：
{_FIELDS_SPEC_ZH}

要求：
- 精读/略读建议只能引用下方清单中真实存在的章节编号和图表标签（Only cite section numbers and figure/table labels that appear in the provided lists. Never invent.）；若无结构信息则给笼统建议，不要编造编号。

检索主题：{query}
标题：{paper.title}
作者：{", ".join(paper.authors[:5])}
年份：{paper.year or "未知"}  期刊/会议：{paper.venue or "未知"}
摘要：{paper.abstract[:3000] or "（无摘要）"}

{structure or "（全文不可用，仅基于摘要给出建议）"}
"""


def _analyze_one_llm(query: str, paper: Paper, cfg: dict,
                     parsed: ParsedPdf | None, lang: str) -> PaperAnalysis:
    prompt = _single_prompt(query, paper, parsed, lang)
    data = _extract_json(_chat(cfg, prompt))
    questions = data.get("questions") or []
    if isinstance(questions, str):
        questions = [questions]
    return PaperAnalysis(
        paper_id=paper.id,
        summary=str(data.get("summary") or "").strip(),
        deep_read=str(data.get("deep_read") or "").strip(),
        skim=str(data.get("skim") or "").strip(),
        questions=[str(q).strip() for q in questions if str(q).strip()],
        cluster="",  # 由聚类阶段统一填
    )


_CLUSTER_PROMPT = """你是一名科研助理。下面是一次文献检索（主题：{query}）命中的论文列表（编号. 标题）。
请将它们按研究子主题聚成 2-6 类，输出 JSON：{{"clusters": {{"类别名（中文，10字内）": [编号...]}}}}
每篇论文必须且只能属于一个类。不要输出其他内容。

{listing}
"""


def _assign_clusters_llm(query: str, papers: list[Paper],
                         analyses: dict[str, PaperAnalysis], cfg: dict) -> None:
    listing = "\n".join(f"{i}. {p.title}" for i, p in enumerate(papers, 1))
    data = _extract_json(_chat(cfg, _CLUSTER_PROMPT.format(query=query, listing=listing)))
    clusters = data.get("clusters") or {}
    assigned: dict[int, str] = {}
    for name, idxs in clusters.items():
        for i in idxs:
            try:
                assigned[int(i)] = str(name)
            except (TypeError, ValueError):
                continue
    for i, paper in enumerate(papers, 1):
        analyses[paper.id].cluster = assigned.get(i, "综合")


# ---------------- 离线启发式降级 ----------------

_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")


def _first_sentences(text: str, n: int = 3, max_chars: int = 400) -> str:
    sents = _SENT_SPLIT.split(text.strip())
    out = ""
    for s in sents[:n]:
        if len(out) + len(s) > max_chars:
            break
        out += (" " if out else "") + s
    return out.strip()


def _first_words(text: str, min_words: int = 150, max_words: int = 300) -> str:
    """按句累加，凑够 min_words 即停，硬上限 max_words（用于英文 summary）。"""
    words_total = len(text.split())
    if words_total <= max_words:
        return text.strip()
    out: list[str] = []
    count = 0
    for sent in _SENT_SPLIT.split(text.strip()):
        w = len(sent.split())
        if out and count >= min_words:
            break
        if count + w > max_words and out:
            break
        out.append(sent)
        count += w
    return " ".join(out).strip()


_METHOD_KW = ("method", "approach", "propose", "framework", "model", "algorithm",
              "architecture", "方法", "模型", "算法")
_EXP_KW = ("experiment", "result", "evaluation", "benchmark", "dataset", "ablation",
           "实验", "结果", "评估")
_REVIEW_KW = ("review", "survey", "overview", "综述")

# 章节标题分类关键词（小写匹配）
_SEC_METHOD = ("method", "approach", "model", "architecture", "framework",
               "algorithm", "proposed", "design", "implementation", "system",
               "方法", "模型", "算法", "架构", "设计")
_SEC_EXP = ("experiment", "result", "evaluation", "ablation", "benchmark",
            "setup", "dataset", "实验", "结果", "评估", "消融")
_SEC_BG = ("related work", "background", "introduction", "preliminaries",
           "motivation", "引言", "绪论", "背景", "相关工作")
_SEC_SKIP = ("appendix", "appendices", "supplementary", "proof", "acknowledg",
             "reference", "limitations", "broader impact", "ethics",
             "附录", "证明", "致谢", "参考文献", "局限")

_FIG_DEEP = ("architecture", "framework", "pipeline", "diagram", "overview",
             "架构", "框架", "流程", "结构")
_FIG_SKIM = ("qualitative", "example", "visualization", "visualisation",
             "示例", "可视化")


def _classify_section(s: SectionInfo) -> str:
    """'method' | 'exp' | 'bg' | 'skip' | 'other'"""
    low = s.title.lower()
    if any(k in low for k in _SEC_SKIP):
        return "skip"
    if any(k in low for k in _SEC_EXP):
        return "exp"
    if any(k in low for k in _SEC_METHOD):
        return "method"
    if any(k in low for k in _SEC_BG):
        return "bg"
    return "other"


def _fmt_sec_en(s: SectionInfo) -> str:
    if s.number:
        return f"Section {s.number} '{s.title}' (p.{s.page})"
    return f"the '{s.title}' section (p.{s.page})"


def _fmt_sec_zh(s: SectionInfo) -> str:
    if s.number:
        return f"第 {s.number} 节《{s.title}》（第 {s.page} 页）"
    return f"「{s.title}」部分（第 {s.page} 页）"


def _split_figures(parsed: ParsedPdf) -> tuple[list[tuple[str, str]],
                                               list[tuple[str, str]]]:
    deep, skim = [], []
    for label, cap in parsed.figures:
        low = cap.lower()
        if any(k in low for k in _FIG_DEEP):
            deep.append((label, cap))
        elif any(k in low for k in _FIG_SKIM):
            skim.append((label, cap))
    return deep, skim


def _offline_deep_skim_en(query: str, paper: Paper,
                          parsed: ParsedPdf | None) -> tuple[str, str]:
    """英文精读/略读建议：有结构则精确到章节号/图表，否则通用建议。"""
    if parsed and parsed.sections:
        groups = {"method": [], "exp": [], "bg": [], "skip": [], "other": []}
        for s in parsed.sections:
            if s.title.lower() in ("abstract", "摘要"):
                continue
            groups[_classify_section(s)].append(s)
        deep_secs = groups["method"] + groups["exp"]
        if not deep_secs:  # 综述等无方法/实验章节：退到未分类章节
            deep_secs = groups["other"]
        skim_secs = groups["bg"]
        skip_secs = groups["skip"]

        parts: list[str] = []
        if deep_secs:
            cited = "; ".join(_fmt_sec_en(s) for s in deep_secs[:4])
            parts.append(f"Deep-read {cited}: the core method and experimental "
                         f"evidence live here.")
            first = deep_secs[0]
            if first.number:
                parts.append(f"Paragraph-level: focus on the first 2 paragraphs "
                             f"of Section {first.number} for the problem setup "
                             f"and key definitions.")
        fig_deep, fig_skim = _split_figures(parsed)
        if fig_deep:
            parts.append("Also study "
                         + "; ".join(f"{l} ({c[:60]})" for l, c in fig_deep[:3])
                         + " for the overall architecture/framework.")
        deep = " ".join(parts) or ("Read the introduction and conclusion "
                                   "carefully to pin down the contributions.")

        skim_parts: list[str] = []
        if skim_secs:
            skim_parts.append("Skim "
                              + "; ".join(_fmt_sec_en(s) for s in skim_secs[:3])
                              + " — background material; revisit only if needed.")
        if fig_skim:
            skim_parts.append("Figures to skim: "
                              + ", ".join(l for l, _ in fig_skim[:3])
                              + " (qualitative examples).")
        if skip_secs:
            skim_parts.append("Safe to skip on first pass: "
                              + "; ".join(_fmt_sec_en(s) for s in skip_secs[:3]) + ".")
        skim = " ".join(skim_parts) or "No clearly skippable sections identified."
        return deep, skim

    # 无 PDF 结构：按标题/摘要给通用建议（不编造编号）
    low = (paper.title + " " + paper.abstract).lower()
    if any(k in low for k in _REVIEW_KW):
        return ("This is a survey: deep-read the introduction and the "
                "taxonomy/classification part to build a map of the field.",
                "Skim the per-topic descriptions of individual works; go back "
                "to the cited originals as needed.")
    if any(k in low for k in _METHOD_KW) and any(k in low for k in _EXP_KW):
        return ("Deep-read the method and experimental-setup parts; focus on "
                "the core innovation and the baselines it is compared against.",
                "Skim related work and background; skip lengthy proofs on the "
                "first pass.")
    if any(k in low for k in _METHOD_KW):
        return ("Deep-read the method description and algorithm pipeline to "
                "grasp the core idea.",
                "Skim the application-background build-up in the introduction.")
    return ("Deep-read the introduction and conclusion first to pin down the "
            "research question and main contributions.",
            "Skim the middle details selectively as needed.")


def _offline_deep_skim_zh(query: str, paper: Paper,
                          parsed: ParsedPdf | None) -> tuple[str, str]:
    if parsed and parsed.sections:
        groups = {"method": [], "exp": [], "bg": [], "skip": [], "other": []}
        for s in parsed.sections:
            if s.title.lower() in ("abstract", "摘要"):
                continue
            groups[_classify_section(s)].append(s)
        deep_secs = groups["method"] + groups["exp"]
        if not deep_secs:
            deep_secs = groups["other"]
        skim_secs = groups["bg"]
        skip_secs = groups["skip"]

        parts: list[str] = []
        if deep_secs:
            cited = "；".join(_fmt_sec_zh(s) for s in deep_secs[:4])
            parts.append(f"建议精读{cited}：核心方法与实验证据集中于此。")
            first = deep_secs[0]
            if first.number:
                parts.append(f"段落级建议：重点读第 {first.number} 节前 2 段，"
                             f"抓住问题设定与关键定义。")
        fig_deep, fig_skim = _split_figures(parsed)
        if fig_deep:
            parts.append("另请细看 "
                         + "；".join(f"{l}（{c[:60]}）" for l, c in fig_deep[:3])
                         + "，把握整体架构/框架。")
        deep = "".join(parts) or "建议精读引言与结论，先明确研究问题与主要贡献。"

        skim_parts: list[str] = []
        if skim_secs:
            skim_parts.append("可略读"
                              + "；".join(_fmt_sec_zh(s) for s in skim_secs[:3])
                              + "，属背景材料，需要时再回看。")
        if fig_skim:
            skim_parts.append("可略读的图："
                              + "、".join(l for l, _ in fig_skim[:3])
                              + "（定性示例）。")
        if skip_secs:
            skim_parts.append("第一遍可跳过："
                              + "；".join(_fmt_sec_zh(s) for s in skip_secs[:3]) + "。")
        skim = "".join(skim_parts) or "未发现明显可跳过的章节。"
        return deep, skim

    low = (paper.title + " " + paper.abstract).lower()
    if any(k in low for k in _REVIEW_KW):
        return ("本文为综述，建议精读引言与分类框架部分，快速建立领域全貌。",
                "各细分方向的具体工作描述可略读，按需回查引用的原文。")
    if any(k in low for k in _METHOD_KW) and any(k in low for k in _EXP_KW):
        return ("建议精读方法（Method/Approach）与实验设置部分，关注核心创新与对比基线。",
                "相关工作与背景介绍可略读；冗长的理论证明可先跳过，需要时再回看。")
    if any(k in low for k in _METHOD_KW):
        return ("建议精读方法描述与算法流程，理解核心思路。",
                "引言中的应用背景铺垫可略读。")
    return ("建议精读引言与结论，先明确研究问题与主要贡献。",
            "中间细节可按需选读。")


def _offline_summary_en(paper: Paper, parsed: ParsedPdf | None) -> str:
    abstract = paper.abstract.strip()
    if not abstract and parsed:
        abs_sec = _find_section(parsed, "abstract")
        if abs_sec and abs_sec.preview:
            abstract = abs_sec.preview
    if abstract:
        return _first_words(abstract, min_words=150, max_words=300)
    bits = [f"Based on the title, this paper ({paper.venue or 'unknown venue'}"
            f"{', ' + str(paper.year) if paper.year else ''}) studies "
            f"'{paper.title}'."]
    if paper.keywords:
        bits.append("Listed keywords: " + ", ".join(paper.keywords[:8]) + ".")
    bits.append("The title suggests the work addresses this topic; read the "
                "introduction to confirm scope and claims. "
                "(based on title/metadata only; abstract unavailable)")
    return " ".join(bits)


def _offline_summary_zh(paper: Paper, parsed: ParsedPdf | None) -> str:
    abstract = paper.abstract.strip()
    if not abstract and parsed:
        abs_sec = _find_section(parsed, "abstract", "摘要")
        if abs_sec and abs_sec.preview:
            abstract = abs_sec.preview
    if abstract:
        body = _first_sentences(abstract)
        if len(abstract) > 400:
            body += "……"
        return "本文要点（取自摘要）：" + body
    bits = [f"本文题为《{paper.title}》"
            f"（{paper.venue or '期刊/会议未知'}"
            f"{('，' + str(paper.year) + ' 年') if paper.year else ''}）。"]
    if paper.keywords:
        bits.append("关键词：" + "、".join(paper.keywords[:8]) + "。")
    bits.append("从标题看，论文围绕上述主题展开；建议先读引言确认研究范围与贡献。"
                "（仅基于标题/元数据，未获取到摘要）")
    return "".join(bits)


def _offline_questions_en(query: str, paper: Paper,
                          parsed: ParsedPdf | None) -> list[str]:
    qs = [f"Compared with mainstream approaches on '{query}', what are the "
          f"strengths and limitations of this paper's method?"]
    concl = _find_section(parsed, "conclusion", "discussion") if parsed else None
    if concl and concl.preview:
        qs.append(f"The conclusion section mentions: \"{concl.preview[:120]}...\" "
                  f"— what follow-up experiment would most strengthen this claim?")
    else:
        qs.append("Are there reproducibility or generalization risks in the "
                  "paper's experimental/data setup?")
    qs.append("Could the ideas be transferred to your own problem or dataset? "
              "What adaptation would be required?")
    return qs


def _offline_questions_zh(query: str, paper: Paper,
                          parsed: ParsedPdf | None) -> list[str]:
    qs = [f"本文方法与检索主题「{query}」中的主流路线相比，优势与局限分别是什么？"]
    concl = _find_section(parsed, "conclusion", "结论", "总结") if parsed else None
    if concl and concl.preview:
        qs.append(f"结论部分提到：「{concl.preview[:80]}……」——"
                  f"哪项后续实验最能加强这一论断？")
    else:
        qs.append("本文的实验/数据设置是否存在可复现性或泛化性方面的隐患？")
    qs.append("能否将本文思路迁移到你手头的问题或数据集上？需要哪些改造？")
    return qs


def _analyze_one_offline(query: str, paper: Paper,
                         parsed: ParsedPdf | None = None,
                         lang: str = "zh") -> PaperAnalysis:
    if lang == "en":
        summary = _offline_summary_en(paper, parsed)
        deep, skim = _offline_deep_skim_en(query, paper, parsed)
        questions = _offline_questions_en(query, paper, parsed)
    else:
        summary = _offline_summary_zh(paper, parsed)
        deep, skim = _offline_deep_skim_zh(query, paper, parsed)
        questions = _offline_questions_zh(query, paper, parsed)
    return PaperAnalysis(paper_id=paper.id, summary=summary, deep_read=deep,
                         skim=skim, questions=questions, cluster="")


_STOPWORDS = set("""a an the and or of for in on to with by from as at is are was were be been
we our this that these those it its via based using use study analysis approach
between over under into their his her not no do does did""".split())


def _title_keywords(title: str) -> set[str]:
    words = re.findall(r"[A-Za-z]{4,}|[一-鿿]{2,}", title.lower())
    return {w for w in words if w not in _STOPWORDS}


def _assign_clusters_offline(papers: list[Paper],
                             analyses: dict[str, PaperAnalysis]) -> None:
    """贪心关键词重叠聚类 + 以簇内最高权威度论文的高频词命名。"""
    kws = [(_title_keywords(p.title) | _title_keywords(p.abstract[:200]))
           for p in papers]
    n = len(papers)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            a, b = kws[i], kws[j]
            if not a or not b:
                continue
            overlap = len(a & b) / max(1, min(len(a), len(b)))
            if overlap >= 0.3:
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    for members in groups.values():
        # 命名：簇内出现次数最多的关键词（排除只在单篇出现的）
        freq: Counter = Counter()
        for i in members:
            for w in kws[i]:
                freq[w] += 1
        common = [w for w, c in freq.most_common() if c >= 2][:3]
        name = "-".join(common) if common else f"主题-{members[0] + 1}"
        for i in members:
            analyses[papers[i].id].cluster = name


def analyze_papers(query: str, papers: list[Paper],
                   cfg: dict) -> dict[str, PaperAnalysis]:
    """SPEC §7：返回 {paper.id: PaperAnalysis}。
    LLM 配置齐全则逐篇生成 + 聚类命名；任何 LLM 环节失败自动整体降级离线模式。
    每篇论文先尝试解析本地 PDF 结构，使建议精确到章节/图表；语言跟随论文原文。
    """
    parsed_map: dict[str, ParsedPdf | None] = {}
    lang_map: dict[str, str] = {}
    for paper in papers:
        parsed = _get_parsed(paper)
        parsed_map[paper.id] = parsed
        lang_map[paper.id] = _paper_language(paper, parsed)
        if parsed and parsed.sections:
            logger.info("全文解析：%s → %d 节 / %d 图 / %d 表，语言=%s",
                        paper.title[:30], len(parsed.sections),
                        len(parsed.figures), len(parsed.tables),
                        parsed.language)

    analyses: dict[str, PaperAnalysis] = {}

    if _llm_configured(cfg):
        try:
            for i, paper in enumerate(papers, 1):
                logger.info("LLM 分析 [%d/%d] %s", i, len(papers), paper.title[:40])
                try:
                    analyses[paper.id] = _analyze_one_llm(
                        query, paper, cfg,
                        parsed_map[paper.id], lang_map[paper.id])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("单篇 LLM 分析失败（%s），该篇降级离线：%s",
                                   paper.id, exc)
                    analyses[paper.id] = _analyze_one_offline(
                        query, paper, parsed_map[paper.id], lang_map[paper.id])
            try:
                _assign_clusters_llm(query, papers, analyses, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("LLM 聚类失败，降级离线聚类：%s", exc)
                _assign_clusters_offline(papers, analyses)
            return analyses
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 分析整体失败，全部降级离线模式：%s", exc)

    logger.info("使用离线启发式生成笔记内容")
    for paper in papers:
        analyses[paper.id] = _analyze_one_offline(
            query, paper, parsed_map[paper.id], lang_map[paper.id])
    _assign_clusters_offline(papers, analyses)
    return analyses
