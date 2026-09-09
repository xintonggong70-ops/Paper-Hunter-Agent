"""开放获取 PDF 下载（SPEC §9）。

失败分类（必须精确）：
- HTTP 401/403 -> pdf_status='paywalled', pdf_note='需要机构认证或付费订阅'
- 找不到 OA 链接 -> 'no_pdf'
- 其他异常（含超大小限制、超时、非 2xx 其他状态码） -> 'error'，note 记录原因
全文不 raise。
"""

from __future__ import annotations

import logging
import os
import re

import requests

from .models import Paper

logger = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
TIMEOUT = 30
PAYWALLED_NOTE = "需要机构认证或付费订阅"
_UA = {"User-Agent": "PaperHunter/0.1 (mailto:paperhunter@example.com)"}

_FILENAME_ILLEGAL = re.compile(r'[\\/:*?"<>|#^\[\]]')


def safe_pdf_name(title: str, paper_id: str, max_len: int = 80) -> str:
    """由标题生成安全文件名（不含扩展名）：去非法字符、压缩空白、截断，冲突时调用方可追加 id。"""
    name = _FILENAME_ILLEGAL.sub("", title)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name:
        name = paper_id.replace(":", "_") or "untitled"
    return name[:max_len].rstrip()


def _find_oa_pdf_url(paper: Paper, cfg: dict) -> str | None:
    """无 pdf_url 时通过 Unpaywall 查询 OA PDF 直链。查不到/查询失败返回 None。"""
    email = (cfg.get("unpaywall_email") or "").strip()
    if not paper.doi or not email:
        return None
    api = f"https://api.unpaywall.org/v2/{paper.doi}"
    try:
        resp = requests.get(api, params={"email": email}, headers=_UA, timeout=TIMEOUT)
        if resp.status_code != 200:
            logger.warning("Unpaywall 查询 %s 返回 HTTP %s", paper.doi, resp.status_code)
            return None
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Unpaywall 查询 %s 失败：%s", paper.doi, exc)
        return None
    best = data.get("best_oa_location") or {}
    url = best.get("url_for_pdf")
    if not url:
        for loc in data.get("oa_locations") or []:
            if loc.get("url_for_pdf"):
                url = loc["url_for_pdf"]
                break
    return url


def _download(url: str, dest_path: str, max_bytes: int) -> str | None:
    """流式下载到 dest_path，返回 None 表示成功；失败时返回 (分类, 原因) 中的原因字符串，
    分类信息通过抛出带状态码的 ValueError 表达——见 _fetch_one。
    为简单起见，paywalled 判定直接在此返回特殊标记字符串。"""
    resp = requests.get(url, headers=_UA, timeout=TIMEOUT, stream=True, allow_redirects=True)
    try:
        if resp.status_code in (401, 403):
            return "paywalled"
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}"
        content_length = resp.headers.get("Content-Length")
        if content_length and content_length.isdigit() and int(content_length) > max_bytes:
            return f"PDF 超过大小限制（{int(content_length) / 1e6:.1f} MB）"
        written = 0
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    f.close()
                    os.unlink(dest_path)
                    return f"PDF 超过大小限制（>{max_bytes // (1024 * 1024)} MB）"
                f.write(chunk)
        if written == 0:
            os.unlink(dest_path)
            return "下载内容为空"
        # 魔数校验：防止把登录墙/错误 HTML 页面存成 PDF
        with open(dest_path, "rb") as f:
            magic = f.read(5)
        if magic != b"%PDF-":
            os.unlink(dest_path)
            if b"<" in magic[:5] or magic[:1] in (b"<", b"{"):
                return "返回内容不是 PDF（可能是登录墙或 HTML 页面）"
            return "返回内容不是 PDF（魔数校验失败）"
        return None
    finally:
        resp.close()


def _fetch_one(paper: Paper, out_dir: str, cfg: dict, max_bytes: int) -> None:
    url = paper.pdf_url or _find_oa_pdf_url(paper, cfg)
    if not url:
        paper.pdf_status = "no_pdf"
        paper.pdf_note = "未找到开放获取 PDF 链接"
        return
    dest = os.path.join(out_dir, safe_pdf_name(paper.title, paper.id) + ".pdf")
    try:
        err = _download(url, dest, max_bytes)
    except Exception as exc:  # noqa: BLE001 - 全文不 raise
        paper.pdf_status = "error"
        paper.pdf_note = f"下载异常：{exc}"
        logger.warning("PDF 下载异常 %s：%s", paper.id, exc)
        return
    if err is None:
        paper.local_pdf = dest
        paper.pdf_status = "ok"
        paper.pdf_note = ""
    elif err == "paywalled":
        paper.pdf_status = "paywalled"
        paper.pdf_note = PAYWALLED_NOTE
    else:
        paper.pdf_status = "error"
        paper.pdf_note = err
        logger.warning("PDF 下载失败 %s：%s", paper.id, err)


def fetch_pdfs(papers: list[Paper], out_dir: str, cfg: dict) -> None:
    """对每篇 Paper 尝试获取 OA PDF（原地更新 local_pdf / pdf_status / pdf_note）。"""
    if not cfg.get("download_pdfs", True):
        logger.info("download_pdfs=false，跳过 PDF 下载")
        return
    os.makedirs(out_dir, exist_ok=True)
    max_bytes = int(cfg.get("max_pdf_mb", 50)) * 1024 * 1024
    for i, paper in enumerate(papers, 1):
        if paper.pdf_status == "ok" and paper.local_pdf and os.path.exists(paper.local_pdf):
            continue
        logger.info("[%d/%d] 下载 PDF: %s", i, len(papers), paper.title[:60])
        _fetch_one(paper, out_dir, cfg, max_bytes)
