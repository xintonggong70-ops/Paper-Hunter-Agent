"""CLI 主入口（SPEC §13）：串联 检索→去重→打分→PDF→分析→Zotero→Obsidian→Excel。

用法：
    python -m paper_agent.agent "graph neural networks for drug discovery" \
        --n 20 --domain auto --zotero-collection "GNN药物发现" \
        --config config.yaml --out ./runs/run1
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time

from .analysis import analyze_papers
from .config import load_config
from .dedupe import dedupe
from .excel_report import write_excel
from .models import Paper
from .notify import notify_done
from .obsidian_gen import generate_vault
from .pdf_fetcher import fetch_pdfs
from .router import infer_domain, select_retrievers
from .scoring import score_papers
from .zotero_importer import ZoteroError, import_or_dump

logger = logging.getLogger("paper_agent")


def composite(p: Paper) -> float:
    return round(0.6 * p.relevance_score + 0.4 * p.authority_score, 2)


def _write_run_report(out_dir: str, query: str, domain: str, n: int,
                      papers: list[Paper], zotero_result: dict,
                      elapsed: float) -> str:
    """写 run_report.md：统计 + 成功/失败清单（含无法获取原文的原因）。"""
    by_source: dict[str, int] = {}
    for p in papers:
        by_source[p.source] = by_source.get(p.source, 0) + 1

    pdf_ok = [p for p in papers if p.pdf_status == "ok"]
    pdf_paywalled = [p for p in papers if p.pdf_status == "paywalled"]
    pdf_no = [p for p in papers if p.pdf_status == "no_pdf"]
    pdf_err = [p for p in papers if p.pdf_status == "error"]

    lines = [
        f"# 检索运行报告",
        "",
        f"- 检索需求：{query}",
        f"- 领域路由：{domain}",
        f"- 目标数量：{n}，最终入选：{len(papers)}",
        f"- 来源分布：" + ("，".join(f"{k} {v} 篇" for k, v in by_source.items()) or "无"),
        f"- 耗时：{elapsed:.1f}s",
        "",
        "## 输出文件",
        f"- Obsidian 笔记库：{out_dir}/obsidian_vault/",
        f"- Excel 汇总：{out_dir}/检索报告.xlsx",
        f"- PDF 原文目录：{out_dir}/pdfs/（成功 {len(pdf_ok)} 篇）",
        "",
        "## Zotero 导入",
    ]
    if "pending_dump" in zotero_result:
        lines.append(f"- 未配置 Zotero 凭证，待导入清单：{zotero_result['pending_dump']}")
    else:
        lines.append(f"- 成功导入：{len(zotero_result.get('imported', []))} 篇")
        failed = zotero_result.get("failed", [])
        if failed:
            lines.append(f"- 导入失败：{len(failed)} 篇")
            for p, reason in failed:
                lines.append(f"  - {p.title}（{reason}）")

    lines += ["", "## 原文获取情况",
              f"- 成功下载 PDF：{len(pdf_ok)} 篇"]
    if pdf_paywalled:
        lines.append(f"- **需要机构认证 / 付费订阅，未能获取原文（{len(pdf_paywalled)} 篇）**：")
        for p in pdf_paywalled:
            lines.append(f"  - {p.title} — {p.url}（{p.pdf_note or '需要机构认证或付费订阅'}）")
    if pdf_no:
        lines.append(f"- 无开放获取 PDF 链接（{len(pdf_no)} 篇）：")
        for p in pdf_no:
            lines.append(f"  - {p.title} — {p.url}")
    if pdf_err:
        lines.append(f"- 下载出错（{len(pdf_err)} 篇）：")
        for p in pdf_err:
            lines.append(f"  - {p.title}（{p.pdf_note}）")

    path = os.path.join(out_dir, "run_report.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def run(query: str, n: int, domain: str | None, zotero_collection: str | None,
        config_path: str, out_dir: str, notify: bool = True) -> int:
    t0 = time.time()
    os.makedirs(out_dir, exist_ok=True)
    cfg = load_config(config_path)

    # 1. 路由 + 检索
    eff_domain = domain if domain and domain != "auto" else infer_domain(query)
    retrievers = select_retrievers(query, eff_domain)
    logger.info("领域=%s，启用来源：%s", eff_domain,
                ", ".join(r.name for r in retrievers))
    all_papers: list[Paper] = []
    for r in retrievers:
        got = r.safe_search(query, limit=n)  # 单来源失败不中断
        logger.info("  %s 返回 %d 篇", r.name, len(got))
        all_papers.extend(got)
    if not all_papers:
        logger.error("所有来源均未返回结果，请检查网络或更换查询词")
        return 1

    # 2. 去重 + 打分 + 取 top-n
    all_papers = dedupe(all_papers)
    logger.info("去重后 %d 篇", len(all_papers))
    score_papers(query, all_papers)
    all_papers.sort(key=composite, reverse=True)
    papers = all_papers[:n]
    logger.info("入选 top-%d（综合分 %.1f ~ %.1f）", len(papers),
                composite(papers[-1]) if papers else 0,
                composite(papers[0]) if papers else 0)

    # 中间产物
    with open(os.path.join(out_dir, "papers.json"), "w", encoding="utf-8") as f:
        json.dump([dataclasses.asdict(p) for p in papers], f,
                  ensure_ascii=False, indent=2)

    # 3. PDF 抓取（先于内容分析，使分析可利用全文结构）
    if cfg.get("download_pdfs", True):
        fetch_pdfs(papers, os.path.join(out_dir, "pdfs"), cfg)
        ok = sum(1 for p in papers if p.pdf_status == "ok")
        logger.info("PDF 下载成功 %d/%d", ok, len(papers))
    else:
        logger.info("配置为不下载 PDF")

    # 4. 笔记内容分析（此时 PDF 已就位，可精确到章节/图表）
    analyses = analyze_papers(query, papers, cfg)

    # 5. Zotero
    clusters = {pid: a.cluster for pid, a in analyses.items()}
    zotero_result: dict = {}
    if zotero_collection:
        try:
            zotero_result = import_or_dump(papers, zotero_collection, cfg,
                                           out_dir, clusters=clusters)
        except ZoteroError as exc:
            logger.error("Zotero 导入失败：%s", exc)
            zotero_result = {"imported": [], "failed": [(p, str(exc)) for p in papers]}
    else:
        logger.info("未指定 --zotero-collection，跳过 Zotero 导入")

    # 6. Obsidian
    vault_dir = os.path.join(out_dir, "obsidian_vault")
    generate_vault(query, papers, analyses, vault_dir)
    logger.info("Obsidian 笔记库已生成：%s", vault_dir)

    # 7. Excel
    excel_path = os.path.join(out_dir, "检索报告.xlsx")
    write_excel(query, papers, analyses, excel_path)
    logger.info("Excel 报告已生成：%s", excel_path)

    # 8. 运行报告
    report = _write_run_report(out_dir, query, eff_domain, n, papers,
                               zotero_result, time.time() - t0)
    logger.info("运行报告：%s", report)

    # 9. 完成总结 + 通知
    _print_final_summary(query, papers, zotero_result, out_dir,
                         time.time() - t0)
    if notify:
        pdf_ok = sum(1 for p in papers if p.pdf_status == "ok")
        notify_done(
            "PaperHunter 检索完成 ✅",
            f"「{query[:40]}」入选 {len(papers)} 篇，PDF {pdf_ok} 篇，"
            f"结果在 {os.path.basename(os.path.abspath(out_dir))}/")
    return 0


def _print_final_summary(query: str, papers: list[Paper],
                         zotero_result: dict, out_dir: str,
                         elapsed: float) -> None:
    """终端打印醒目的完成总结框。"""
    pdf_ok = sum(1 for p in papers if p.pdf_status == "ok")
    pdf_paywalled = sum(1 for p in papers if p.pdf_status == "paywalled")
    if "pending_dump" in zotero_result:
        zline = "未配置凭证（清单已存 zotero_pending.json）"
    elif not zotero_result:
        zline = "未指定文件夹，已跳过"
    else:
        zline = (f"导入 {len(zotero_result.get('imported', []))} 篇，"
                 f"失败 {len(zotero_result.get('failed', []))} 篇")
    lines = [
        "",
        "=" * 56,
        "  ✅ 检索完成",
        f"  需求：{query}",
        f"  入选：{len(papers)} 篇    耗时：{elapsed:.0f} 秒",
        f"  PDF 原文：成功 {pdf_ok} 篇，需机构认证 {pdf_paywalled} 篇",
        f"  Zotero：{zline}",
        "  输出：",
        f"    📓 Obsidian 笔记库  {out_dir}/obsidian_vault/",
        f"    📊 Excel 汇总       {out_dir}/检索报告.xlsx",
        f"    📄 运行报告         {out_dir}/run_report.md",
        "=" * 56,
        "",
    ]
    print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="paper_agent",
        description="论文检索 Agent：检索→打分→Zotero 导入→Obsidian 笔记→Excel 汇总")
    parser.add_argument("query", help="检索需求（自然语言，中英文均可）")
    parser.add_argument("--n", type=int, default=20, help="目标论文数量（默认 20）")
    parser.add_argument("--domain", default="auto",
                        choices=["auto", "biomedical", "cs", "physics", "math",
                                 "eess", "general"],
                        help="领域（默认 auto 自动推断）")
    parser.add_argument("--zotero-collection", default=None,
                        help="目标 Zotero 文件夹名（不提供则跳过导入）")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--out", default="./run_output", help="输出目录")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    parser.add_argument("--no-notify", action="store_true",
                        help="禁用完成时的桌面通知和响铃")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    return run(query=args.query, n=args.n, domain=args.domain,
               zotero_collection=args.zotero_collection,
               config_path=args.config, out_dir=args.out,
               notify=not args.no_notify)


if __name__ == "__main__":
    sys.exit(main())
