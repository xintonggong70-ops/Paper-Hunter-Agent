"""PDF 全文结构解析（基于 PyMuPDF）。

从论文 PDF 中提取章节骨架（编号+标题+正文预览+页码）、图表 caption、
正文语言，供 analysis 模块生成精确到章节/图表的阅读建议。

所有入口均不抛异常：加密 PDF、扫描版（无文本层）、损坏文件等
返回 None 或部分结果。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

try:  # PyMuPDF，import 名为 fitz
    import fitz  # type: ignore
except Exception:  # noqa: BLE001
    fitz = None  # type: ignore
    logger.warning("PyMuPDF (fitz) 不可用，PDF 全文解析将被跳过")


@dataclass
class SectionInfo:
    number: str      # "3.2"，无编号则为 ""
    title: str       # "Methodology"
    preview: str     # 该节正文前 ~400 字符（去多余空白）
    page: int        # 所在页码（1-based）


@dataclass
class ParsedPdf:
    sections: list[SectionInfo] = field(default_factory=list)
    figures: list[tuple[str, str]] = field(default_factory=list)  # ("Figure 3", caption≤200)
    tables: list[tuple[str, str]] = field(default_factory=list)   # ("Table 1", caption)
    language: str = "unknown"   # 'en' | 'zh' | 'unknown'
    n_pages: int = 0


# ---------------- 语言检测 ----------------

_CJK = re.compile(r"[一-鿿]")
_ASCII_LETTER = re.compile(r"[A-Za-z]")


def detect_language(text: str) -> str:
    """前 2000 字符中：中文占比 >20% → 'zh'；ASCII 字母占比 >70% → 'en'。"""
    sample = (text or "")[:2000]
    if not sample.strip():
        return "unknown"
    n = len(sample)
    zh = len(_CJK.findall(sample))
    en = len(_ASCII_LETTER.findall(sample))
    if zh / n > 0.2:
        return "zh"
    if en / n > 0.7:
        return "en"
    return "unknown"


# ---------------- 章节标题识别 ----------------

# 阿拉伯数字编号： "1 Introduction" / "1. Introduction" / "3.2.1 Sub"
_NUM_HEAD = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(\S.{0,90})$")
# 罗马数字编号： "I. INTRODUCTION" / "IV. EXPERIMENTS"
_ROMAN_HEAD = re.compile(r"^\s*([IVXLC]{1,7})\.\s+([A-Z][\s\S]{0,90})$")

_ROMAN_VAL = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def _roman_to_int(s: str) -> int | None:
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN_VAL.get(ch)
        if v is None:
            return None
        if v < prev:
            total -= v
        else:
            total += v
            prev = v
    return total if 0 < total < 100 else None


# 常见无编号标题（小写、去冒号后精确匹配）
_KNOWN_TITLES = {
    "abstract", "introduction", "background", "related work", "preliminaries",
    "methodology", "methods", "method", "materials and methods", "approach",
    "experiments", "experimental setup", "experimental results", "results",
    "results and discussion", "discussion", "evaluation", "analysis",
    "conclusion", "conclusions", "conclusion and future work", "future work",
    "limitations", "acknowledgments", "acknowledgements", "references",
    "bibliography", "appendix", "appendices", "supplementary material",
    "摘要", "引言", "绪论", "相关工作", "背景", "方法", "实验", "结果",
    "讨论", "结论", "总结", "致谢", "参考文献", "附录",
}


def _match_heading(line: str) -> tuple[str, str] | None:
    """保守判定一个文本行是否为章节标题，返回 (编号, 标题) 或 None。"""
    s = line.strip()
    if not s or len(s) > 100:
        return None

    m = _NUM_HEAD.match(s)
    if m:
        number, title = m.group(1), m.group(2).strip()
        # 排除表格数值行：章节首段编号不超过 20；标题不含 [引用]/(年份)
        if int(number.split(".")[0]) > 20:
            return None
        if re.search(r"\[\d+\]|\(\d{4}\)", title):
            return None
        # 保守：标题须以字母开头（大写或中文）、不以句号结尾、含字母
        if (title and title[0].isalpha() and not title.endswith(".")
                and any(c.isalpha() for c in title)):
            first = title[0]
            if first.isupper() or title.isupper() or "一" <= first <= "鿿":
                return number, title.rstrip(":")
        return None

    m = _ROMAN_HEAD.match(s)
    if m:
        val = _roman_to_int(m.group(1))
        title = m.group(2).strip()
        if (val is not None and title and not title.endswith(".")
                and any(c.isalpha() for c in title)):
            return str(val), title.rstrip(":")
        return None

    key = s.rstrip(":").strip().lower()
    if key in _KNOWN_TITLES and len(s) <= 60:
        return "", s.rstrip(":")
    return None


# ---------------- 图表 caption ----------------

_FIG_LINE = re.compile(r"^\s*((?:Figure|Fig\.?)\s*\d+[A-Za-z]?)\b[.:]?\s*(.*)$")
_TAB_LINE = re.compile(r"^\s*((?:TABLE|Table)\s*\d+[A-Za-z]?)\b[.:]?\s*(.*)$")


def _norm_fig_label(raw: str) -> str:
    parts = raw.replace(".", "").split()
    return " ".join(parts[:2])


def _dedupe_key(label: str) -> str:
    return re.sub(r"[.\s]+", " ", label.lower()).strip()


# ---------------- 主入口 ----------------

_WS = re.compile(r"\s+")

# 独占一行的编号（标题编号常被排版为单独一行，如 "3.1" 下一行才是标题）
_LONE_NUM = re.compile(r"^\d{1,2}(?:\.\d{1,2}){0,3}\.?$")


def parse_pdf(path: str, max_sections: int = 40) -> ParsedPdf | None:
    """用 PyMuPDF 解析 PDF 结构。解析失败/非 PDF/无文本层返回 None（不 raise）。"""
    if fitz is None:
        return None
    try:
        doc = fitz.open(path)
    except Exception as exc:  # noqa: BLE001
        logger.debug("parse_pdf 打开失败 %s: %s", path, exc)
        return None
    try:
        if getattr(doc, "is_encrypted", False) and not doc.authenticate(""):
            logger.debug("parse_pdf: 加密 PDF 跳过 %s", path)
            return None
        result = ParsedPdf(n_pages=doc.page_count)

        # 逐页收集文本行（保留页码）
        page_lines: list[list[str]] = []
        full_text_parts: list[str] = []
        for pno in range(doc.page_count):
            try:
                text = doc[pno].get_text("text") or ""
            except Exception:  # noqa: BLE001
                text = ""
            full_text_parts.append(text)
            page_lines.append(text.splitlines())

        full_text = "\n".join(full_text_parts)
        if not full_text.strip():
            return None  # 扫描版/无文本层
        result.language = detect_language(full_text)

        # 拉平为 (page, line) 序列，并标记每页最后一个非空行（多为页脚页码）
        flat: list[tuple[int, str]] = []
        page_last_idx: set[int] = set()
        for pno, lines in enumerate(page_lines, 1):
            for ln in lines:
                flat.append((pno, ln))
            for j in range(len(flat) - 1, -1, -1):
                if flat[j][0] != pno:
                    break
                if flat[j][1].strip():
                    page_last_idx.add(j)
                    break

        # 识别标题行；编号独占一行时与下一行合并再判定
        heads: list[tuple[int, str, str, int]] = []  # (flat_idx, 编号, 标题, 占用行数)
        seen_numbers: set[str] = set()   # 编号唯一（同号不同题视为误判，留首个）
        seen_titles: set[str] = set()    # 无编号标题按题名去重
        for i, (_, ln) in enumerate(flat):
            span = 1
            cand = ln
            if (_LONE_NUM.match(ln.strip()) and i + 1 < len(flat)
                    and i not in page_last_idx):  # 页脚页码不参与合并
                nxt = flat[i + 1][1].strip()
                if 0 < len(nxt) <= 60:  # 页脚页码+长正文行不合并，降低误判
                    cand = ln.strip() + " " + nxt
                    span = 2
            m = _match_heading(cand)
            if not m:
                continue
            number, title = m
            if number:
                if number in seen_numbers:
                    continue
                seen_numbers.add(number)
                seen_titles.add(title.lower())
            else:
                if title.lower() in seen_titles:
                    continue
                seen_titles.add(title.lower())
            heads.append((i, number, title, span))

        for h_idx, (flat_i, number, title, span) in enumerate(heads[:max_sections]):
            end = heads[h_idx + 1][0] if h_idx + 1 < len(heads) else len(flat)
            body = " ".join(ln.strip() for _, ln in flat[flat_i + span:end]
                            if ln.strip())
            preview = _WS.sub(" ", body).strip()[:400]
            result.sections.append(SectionInfo(
                number=number, title=title, preview=preview,
                page=flat[flat_i][0]))

        # 图表 caption
        seen_fig: set[str] = set()
        seen_tab: set[str] = set()
        for _, ln in flat:
            mf = _FIG_LINE.match(ln)
            if mf:
                label = _norm_fig_label(mf.group(1))
                cap = _WS.sub(" ", ln.strip())[:200]
                if _dedupe_key(label) not in seen_fig and cap:
                    seen_fig.add(_dedupe_key(label))
                    result.figures.append((label, cap))
                continue
            mt = _TAB_LINE.match(ln)
            if mt:
                label = _norm_fig_label(mt.group(1))
                cap = _WS.sub(" ", ln.strip())[:200]
                if _dedupe_key(label) not in seen_tab and cap:
                    seen_tab.add(_dedupe_key(label))
                    result.tables.append((label, cap))

        return result
    except Exception as exc:  # noqa: BLE001
        logger.debug("parse_pdf 解析异常 %s: %s", path, exc)
        return None
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
