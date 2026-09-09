"""zotero_importer / pdf_fetcher / config 测试（SPEC §15 相关部分）。

覆盖：
- import_or_dump 无配置时走 pending_dump 分支
- build_item_template 字段映射正确性
- ZoteroImporter 行为（mock pyzotero 的 Zotero 类）
- pdf_fetcher：真实 arXiv PDF 下载（网络不可达则 skip）、大小限制、失败分类
"""

import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from paper_agent.config import load_config
from paper_agent.models import Paper
from paper_agent import pdf_fetcher
from paper_agent.pdf_fetcher import fetch_pdfs
from paper_agent.zotero_importer import (
    ZoteroError,
    ZoteroImporter,
    build_item_template,
    import_or_dump,
)

ARXIV_PDF_URL = "https://arxiv.org/pdf/1706.03762.pdf"


def make_paper(**kw) -> Paper:
    base = dict(
        id="arxiv:1706.03762", source="arxiv",
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        abstract="The dominant sequence transduction models...",
        year=2017, venue="", doi=None,
        url="https://arxiv.org/abs/1706.03762",
        pdf_url=None, citation_count=50000,
    )
    base.update(kw)
    return Paper(**base)


# ---------------- config ----------------

def test_load_config_missing_returns_defaults(tmp_path):
    cfg = load_config(str(tmp_path / "nope.yaml"))
    assert cfg["max_pdf_mb"] == 50
    assert cfg["download_pdfs"] is True
    assert cfg["zotero"]["library_type"] == "user"


def test_load_config_merge(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("max_pdf_mb: 5\nzotero:\n  library_id: '123'\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg["max_pdf_mb"] == 5
    assert cfg["zotero"]["library_id"] == "123"
    assert cfg["zotero"]["api_key"] == ""  # 默认补齐


# ---------------- item 模板 ----------------

def test_template_preprint_for_arxiv():
    item = build_item_template(make_paper(), cluster="Transformer",
                               collection_key="ABC123")
    assert item["itemType"] == "preprint"
    assert item["repository"] == "arXiv"
    assert item["archiveID"] == "1706.03762"
    assert item["title"] == "Attention Is All You Need"
    assert item["date"] == "2017"
    assert item["abstractNote"].startswith("The dominant")
    assert item["collections"] == ["ABC123"]
    assert {"tag": "Transformer"} in item["tags"]
    assert {"tag": "arxiv"} in item["tags"]
    assert "arXiv: 1706.03762" in item["extra"]
    assert "Source: arxiv" in item["extra"]


def test_template_journal_article_with_venue():
    p = make_paper(id="doi:10.1038/nature14539", source="crossref",
                   venue="Nature", doi="10.1038/nature14539", year=2015)
    item = build_item_template(p)
    assert item["itemType"] == "journalArticle"
    assert item["publicationTitle"] == "Nature"
    assert item["DOI"] == "10.1038/nature14539"
    assert "repository" not in item


def test_template_report_fallback_and_author_split():
    p = make_paper(id="s2:CorpusId:1", source="semantic_scholar",
                   authors=["Madonna", "Yann LeCun"])
    item = build_item_template(p)
    assert item["itemType"] == "report"
    c0, c1 = item["creators"]
    assert c0 == {"creatorType": "author", "firstName": "", "lastName": "Madonna"}
    assert c1 == {"creatorType": "author", "firstName": "Yann", "lastName": "LeCun"}


# ---------------- import_or_dump pending 分支 ----------------

def test_import_or_dump_pending(tmp_path):
    papers = [make_paper()]
    cfg = load_config(str(tmp_path / "none.yaml"))  # 全默认，无 zotero 凭证
    result = import_or_dump(papers, "测试集合", cfg, str(tmp_path))
    assert result["imported"] == []
    assert result["failed"] == []
    dump = result["pending_dump"]
    assert dump == str(tmp_path / "zotero_pending.json")
    payload = json.loads(open(dump, encoding="utf-8").read())
    assert payload["collection"] == "测试集合"
    assert len(payload["papers"]) == 1
    assert payload["papers"][0]["id"] == "arxiv:1706.03762"


def test_import_or_dump_partial_config_still_pending(tmp_path):
    cfg = load_config(str(tmp_path / "none.yaml"))
    cfg["zotero"]["library_id"] = "123"  # 缺 api_key
    result = import_or_dump([make_paper()], "c", cfg, str(tmp_path))
    assert "pending_dump" in result


# ---------------- ZoteroImporter（mock pyzotero） ----------------

def _mock_zotero(collections=None, create_resp=None):
    zot = mock.MagicMock()
    zot.collections.return_value = collections or []
    zot.create_collections.return_value = {"success": {"0": "NEWCOLL"}, "failed": {}}
    zot.create_items.return_value = create_resp or {"success": {"0": "ITEMKEY1"}, "failed": {}}
    return zot


def test_ensure_collection_find_existing():
    with mock.patch("pyzotero.zotero.Zotero") as ZotCls:
        zot = _mock_zotero(collections=[{"data": {"name": "已有", "key": "EXIST1"}}])
        ZotCls.return_value = zot
        imp = ZoteroImporter("123", "user", "key")
        assert imp.ensure_collection("已有") == "EXIST1"
        zot.create_collections.assert_not_called()


def test_ensure_collection_create_when_missing():
    with mock.patch("pyzotero.zotero.Zotero") as ZotCls:
        zot = _mock_zotero()
        ZotCls.return_value = zot
        imp = ZoteroImporter("123", "user", "key")
        assert imp.ensure_collection("新集合") == "NEWCOLL"
        zot.create_collections.assert_called_once_with([{"name": "新集合"}])


def test_import_papers_uploads_pdf_and_records_failures(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    ok = make_paper(local_pdf=str(pdf))
    bad = make_paper(id="arxiv:0000.00000", title="Bad")
    # 第一篇成功，第二篇 failed
    zot = _mock_zotero(collections=[{"data": {"name": "c", "key": "CK"}}])
    zot.create_items.side_effect = [
        {"success": {"0": "ITEMKEY1"}, "failed": {}},
        {"success": {}, "failed": {"0": {"message": "invalid DOI"}}},
    ]
    with mock.patch("pyzotero.zotero.Zotero") as ZotCls:
        ZotCls.return_value = zot
        imp = ZoteroImporter("123", "user", "key")
        result = imp.import_papers([ok, bad], "c",
                                   clusters={"arxiv:1706.03762": "Transformer"})
    assert result["collection_key"] == "CK"
    assert [p.id for p in result["imported"]] == ["arxiv:1706.03762"]
    assert ok.zotero_key == "ITEMKEY1"
    assert len(result["failed"]) == 1 and result["failed"][0][0] is bad
    assert "invalid DOI" in result["failed"][0][1]
    zot.attachment_simple.assert_called_once_with([str(pdf)], "ITEMKEY1")
    # 传给 create_items 的模板带 cluster tag 且属于 collection
    sent_item = zot.create_items.call_args_list[0][0][0][0]
    assert sent_item["collections"] == ["CK"]
    assert {"tag": "Transformer"} in sent_item["tags"]


def test_import_papers_auth_error_raises():
    class FakeAuthError(Exception):
        status_code = 403

    zot = _mock_zotero(collections=[{"data": {"name": "c", "key": "CK"}}])
    zot.create_items.side_effect = FakeAuthError("forbidden")
    with mock.patch("pyzotero.zotero.Zotero") as ZotCls:
        ZotCls.return_value = zot
        imp = ZoteroImporter("123", "user", "bad-key")
        with pytest.raises(ZoteroError):
            imp.import_papers([make_paper()], "c")


# ---------------- pdf_fetcher ----------------

class _FakeResp:
    def __init__(self, status_code=200, body=b"%PDF-1.4 fake-body", headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def iter_content(self, chunk_size=65536):
        yield self._body

    def json(self):
        return {}

    def close(self):
        pass


def _cfg(tmp_path, **kw):
    cfg = {"download_pdfs": True, "max_pdf_mb": 50, "unpaywall_email": ""}
    cfg.update(kw)
    return cfg


def test_fetch_pdf_paywalled_on_403(tmp_path):
    p = make_paper(pdf_url="https://example.com/paywalled.pdf")
    with mock.patch.object(pdf_fetcher.requests, "get",
                           return_value=_FakeResp(status_code=403)):
        fetch_pdfs([p], str(tmp_path), _cfg(tmp_path))
    assert p.pdf_status == "paywalled"
    assert p.pdf_note == "需要机构认证或付费订阅"
    assert p.local_pdf is None


def test_fetch_pdf_paywalled_on_401(tmp_path):
    p = make_paper(pdf_url="https://example.com/x.pdf")
    with mock.patch.object(pdf_fetcher.requests, "get",
                           return_value=_FakeResp(status_code=401)):
        fetch_pdfs([p], str(tmp_path), _cfg(tmp_path))
    assert p.pdf_status == "paywalled"


def test_fetch_pdf_no_pdf_when_no_url_and_no_doi(tmp_path):
    p = make_paper(pdf_url=None, doi=None)
    fetch_pdfs([p], str(tmp_path), _cfg(tmp_path))
    assert p.pdf_status == "no_pdf"


def test_fetch_pdf_size_limit(tmp_path):
    p = make_paper(pdf_url="https://example.com/big.pdf")
    big = _FakeResp(status_code=200, body=b"x" * 2048,
                    headers={"Content-Length": str(50 * 1024 * 1024)})
    with mock.patch.object(pdf_fetcher.requests, "get", return_value=big):
        fetch_pdfs([p], str(tmp_path), _cfg(tmp_path, max_pdf_mb=1))
    assert p.pdf_status == "error"
    assert "大小限制" in p.pdf_note
    assert not list(tmp_path.glob("*.pdf"))  # 超限不落盘


def test_fetch_pdf_network_error_classified(tmp_path):
    p = make_paper(pdf_url="https://example.com/x.pdf")
    with mock.patch.object(pdf_fetcher.requests, "get",
                           side_effect=pdf_fetcher.requests.Timeout("boom")):
        fetch_pdfs([p], str(tmp_path), _cfg(tmp_path))
    assert p.pdf_status == "error"
    assert "boom" in p.pdf_note


def test_fetch_pdf_download_pdfs_false_skips(tmp_path):
    p = make_paper(pdf_url="https://example.com/x.pdf")
    fetch_pdfs([p], str(tmp_path), _cfg(tmp_path, download_pdfs=False))
    assert p.pdf_status == "pending"


def test_fetch_pdf_real_arxiv_download(tmp_path):
    """真实下载 arXiv 开放获取 PDF（Attention Is All You Need）；网络不可达则 skip。"""
    import requests as rq
    try:
        head = rq.head(ARXIV_PDF_URL, timeout=15, allow_redirects=True)
        if head.status_code >= 500:
            pytest.skip("arXiv 暂不可用")
    except Exception as exc:
        pytest.skip(f"网络不可达：{exc}")
    p = make_paper(pdf_url=ARXIV_PDF_URL)
    fetch_pdfs([p], str(tmp_path), _cfg(tmp_path))
    assert p.pdf_status == "ok", p.pdf_note
    assert p.local_pdf and os.path.exists(p.local_pdf)
    assert os.path.getsize(p.local_pdf) > 100 * 1024  # 真实 PDF 应 >100KB
    with open(p.local_pdf, "rb") as f:
        assert f.read(5) == b"%PDF-"


def test_fetch_pdf_real_arxiv_size_limit(tmp_path):
    """真实 URL + 极小 max_pdf_mb 验证超限分类（网络不可达则 skip）。"""
    import requests as rq
    try:
        rq.head(ARXIV_PDF_URL, timeout=15, allow_redirects=True)
    except Exception as exc:
        pytest.skip(f"网络不可达：{exc}")
    p = make_paper(pdf_url=ARXIV_PDF_URL)
    cfg = _cfg(tmp_path)
    cfg["max_pdf_mb"] = 0  # 0 字节限制 -> 必然超限
    fetch_pdfs([p], str(tmp_path), cfg)
    assert p.pdf_status == "error"
    assert "大小限制" in p.pdf_note
