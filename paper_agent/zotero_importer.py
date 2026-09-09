"""Zotero Web API 导入（SPEC §10）。依赖 pyzotero。

字段映射方案（build_item_template）：
- itemType：有 venue -> 'journalArticle'（publicationTitle=venue）；
  无 venue 且 source=='arxiv' -> 'preprint'（repository='arXiv', archiveID=arXiv id）；
  其他 -> 'report'。
- title <- paper.title
- creators <- [{'creatorType': 'author', 'firstName': ..., 'lastName': ...}]
  （按最后一个空格拆分；单名作者只填 lastName）
- abstractNote <- paper.abstract；date <- str(paper.year)；DOI <- paper.doi；url <- paper.url
- extra <- 逐行写 'arXiv: <id>'（arxiv 来源）与 'Source: <source>'
- tags <- [{'tag': cluster}（如提供）, {'tag': source}]
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os

from .models import Paper

logger = logging.getLogger(__name__)


class ZoteroError(Exception):
    """Zotero 导入致命错误（如认证失败），由上层捕获处理。"""


def _split_author(name: str) -> dict:
    """'Yann LeCun' -> {'creatorType': 'author', 'firstName': 'Yann', 'lastName': 'LeCun'}"""
    parts = name.strip().split()
    if len(parts) >= 2:
        return {"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]}
    return {"creatorType": "author", "firstName": "", "lastName": name.strip()}


def build_item_template(paper: Paper, cluster: str | None = None,
                        collection_key: str | None = None) -> dict:
    """由 Paper 构造 Zotero item 模板 dict（不依赖网络，可独立测试）。"""
    venue = (paper.venue or "").strip()
    arxiv_id = paper.id.split(":", 1)[1] if paper.source == "arxiv" and ":" in paper.id else None
    if venue:
        item_type = "journalArticle"
    elif paper.source == "arxiv":
        item_type = "preprint"
    else:
        item_type = "report"

    item: dict = {
        "itemType": item_type,
        "title": paper.title,
        "creators": [_split_author(a) for a in paper.authors if a and a.strip()],
        "abstractNote": paper.abstract or "",
        "date": str(paper.year) if paper.year else "",
        "DOI": paper.doi or "",
        "url": paper.url or "",
        "tags": [],
    }
    if item_type == "journalArticle":
        item["publicationTitle"] = venue
    elif item_type == "preprint":
        item["repository"] = "arXiv"
        if arxiv_id:
            item["archiveID"] = arxiv_id

    extra_lines = []
    if arxiv_id:
        extra_lines.append(f"arXiv: {arxiv_id}")
    extra_lines.append(f"Source: {paper.source}")
    if paper.citation_count is not None:
        extra_lines.append(f"Citations: {paper.citation_count}")
    item["extra"] = "\n".join(extra_lines)

    if cluster:
        item["tags"].append({"tag": cluster})
    item["tags"].append({"tag": paper.source})
    if collection_key:
        item["collections"] = [collection_key]
    return item


class ZoteroImporter:
    """Zotero Web API 导入器。"""

    def __init__(self, library_id: str, library_type: str, api_key: str):
        from pyzotero import zotero  # 延迟导入，便于无凭证环境使用其他模块

        self.zot = zotero.Zotero(library_id, library_type, api_key)
        self.library_id = library_id
        self.library_type = library_type

    def ensure_collection(self, name: str) -> str:
        """按名字查找 collection，不存在则创建，返回 collection key。"""
        try:
            for coll in self.zot.collections():
                if coll.get("data", {}).get("name") == name:
                    return coll["data"]["key"]
            resp = self.zot.create_collections([{"name": name}])
        except Exception as exc:  # noqa: BLE001
            raise ZoteroError(f"获取/创建 collection 失败：{exc}") from exc
        success = resp.get("success") or {}
        if success:
            return next(iter(success.values()))
        raise ZoteroError(f"创建 collection '{name}' 失败：{resp.get('failed')}")

    def import_papers(self, papers: list[Paper], collection_name: str,
                      pdf_dir: str | None = None,
                      clusters: dict[str, str] | None = None) -> dict:
        """
        逐条导入 Paper 到指定 collection；有 local_pdf 则上传 attachment。
        返回 {'imported': [Paper...], 'failed': [(Paper, reason)...], 'collection_key': str}。
        单条失败不中断；认证失败等致命异常 raise ZoteroError。
        """
        collection_key = self.ensure_collection(collection_name)
        imported: list[Paper] = []
        failed: list[tuple[Paper, str]] = []

        for i, paper in enumerate(papers, 1):
            cluster = (clusters or {}).get(paper.id)
            try:
                item = build_item_template(paper, cluster=cluster,
                                           collection_key=collection_key)
                resp = self.zot.create_items([item])
            except Exception as exc:  # noqa: BLE001
                if _is_auth_error(exc):
                    raise ZoteroError(f"Zotero 认证失败：{exc}") from exc
                reason = f"创建条目异常：{exc}"
                logger.warning("[%d/%d] %s 导入失败：%s", i, len(papers), paper.id, reason)
                failed.append((paper, reason))
                continue

            success = resp.get("success") or {}
            if not success:
                fail_info = resp.get("failed") or {}
                reason = json.dumps(fail_info, ensure_ascii=False) or "未知错误"
                logger.warning("[%d/%d] %s 导入失败：%s", i, len(papers), paper.id, reason)
                failed.append((paper, reason))
                continue

            item_key = next(iter(success.values()))
            paper.zotero_key = item_key

            pdf_path = paper.local_pdf
            if pdf_path and os.path.exists(pdf_path):
                if pdf_dir and not os.path.isabs(pdf_path):
                    pdf_path = os.path.join(pdf_dir, pdf_path)
                try:
                    self.zot.attachment_simple([pdf_path], item_key)
                except Exception as exc:  # noqa: BLE001 - 附件失败不影响条目本体
                    logger.warning("附件上传失败 %s：%s", paper.id, exc)
            imported.append(paper)

        return {"imported": imported, "failed": failed, "collection_key": collection_key}


def _is_auth_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    if "auth" in name or "forbidden" in name or "permission" in name:
        return True
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return code in (401, 403)


def _zotero_configured(cfg: dict) -> bool:
    z = cfg.get("zotero") or {}
    return bool(str(z.get("library_id") or "").strip()
                and str(z.get("api_key") or "").strip())


def import_or_dump(papers: list[Paper], collection_name: str, cfg: dict,
                   out_dir: str, clusters: dict[str, str] | None = None) -> dict:
    """便捷入口：zotero 配置齐全 -> ZoteroImporter 导入；
    未配置 -> 待导入清单写 {out_dir}/zotero_pending.json，返回
    {'imported': [], 'failed': [], 'pending_dump': path}。"""
    if _zotero_configured(cfg):
        z = cfg["zotero"]
        importer = ZoteroImporter(
            library_id=str(z["library_id"]),
            library_type=str(z.get("library_type") or "user"),
            api_key=str(z["api_key"]),
        )
        return importer.import_papers(papers, collection_name,
                                      pdf_dir=os.path.join(out_dir, "pdfs"),
                                      clusters=clusters)

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "zotero_pending.json")
    payload = {
        "collection": collection_name,
        "papers": [dataclasses.asdict(p) for p in papers],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    logger.info("未配置 Zotero 凭证，待导入清单已写入 %s", path)
    return {"imported": [], "failed": [], "pending_dump": path}
