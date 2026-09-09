# SPEC.md — PaperHunter Agent（论文检索与知识管理 Agent）

## 1. 项目目标

命令行 Agent：输入自然语言检索需求（主题 + 数量 + 领域），自动完成：
1. 按领域路由到合适来源检索论文（arXiv / Semantic Scholar / Crossref / PubMed）
2. 相关度 + 权威度打分、去重、排序
3. 导入 Zotero 指定 collection（含条目元数据 + 网页链接 + 可获取的 PDF 原文；付费墙/机构认证失败的单独报告）
4. 生成独立 Obsidian 笔记文件夹（每篇论文一篇笔记 + 主题 MOC + wikilink 网状互链）
5. 生成 Excel 汇总报告（元数据 + 指标 + 条件格式）

语言：Python ≥ 3.10。包名 `paper_agent`。

## 2. 目录结构

```
paper_agent/
├── __init__.py
├── models.py            # 数据结构（§3）
├── config.py            # 配置加载（§8）
├── router.py            # 领域→来源路由（§4.1）
├── retrievers/
│   ├── __init__.py
│   ├── base.py          # BaseRetriever
│   ├── arxiv_retriever.py
│   ├── semantic_scholar.py
│   ├── crossref_retriever.py
│   └── pubmed_retriever.py
├── dedupe.py            # 跨来源去重（§5）
├── scoring.py           # 相关度/权威度打分（§6）
├── analysis.py          # 笔记内容生成（LLM 或离线启发式）（§7）
├── pdf_fetcher.py       # 开放获取 PDF 下载（§9）
├── zotero_importer.py   # Zotero Web API 导入（§10）
├── obsidian_gen.py      # Obsidian 笔记文件夹生成（§11）
├── excel_report.py      # Excel 汇总（§12）
└── agent.py             # CLI 主入口，串联全流程（§13）
requirements.txt
config.example.yaml
README.md
tests/                   # 各模块自测脚本（可用 pytest 或 plain asserts）
```

## 3. 核心数据结构（models.py）

```python
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
    cluster: str                 # 主题聚类标签（用于笔记互联，如 "GNN-分子表示"）
```

所有 Paper 必须能 `dataclasses.asdict()` 序列化为 JSON（运行中间产物 `papers.json`）。

## 4. 检索模块（retrievers/）

### 4.1 基类与路由

```python
# base.py
class BaseRetriever:
    name: str
    def search(self, query: str, limit: int) -> list[Paper]: ...
    # 子类实现；失败时 raise RetrieverError（自定义异常），由上层捕获降级
```

```python
# router.py
def select_retrievers(query: str, domain: str | None = None) -> list[BaseRetriever]:
    """
    domain 可为 None（自动从 query 关键词推断）或显式：
    'biomedical' -> [PubMed, SemanticScholar, Crossref]
    'cs'/'physics'/'math'/'eess' -> [ArXiv, SemanticScholar, Crossref]
    'general' -> [SemanticScholar, Crossref, ArXiv]
    判断 biomedical：query 含基因/蛋白/细胞/drug/clinical/protein/gene/disease 等词。
    """
```

- **arxiv_retriever.py**：用官方 Atom API（`http://export.arxiv.org/api/query?search_query=all:...&max_results=`），标准库 `urllib` + `xml.etree` 解析，不用第三方 arxiv 包。Paper: source='arxiv', pdf_url=`https://arxiv.org/pdf/{id}.pdf`, citation_count=None。
- **semantic_scholar.py**：Graph API `GET https://api.semanticscholar.org/graph/v1/paper/search?query=&limit=&fields=title,abstract,authors,year,venue,citationCount,externalIds,openAccessPdf,fieldsOfStudy`。无需 key，注意 429 时指数退避重试（最多 3 次）。id 用 `s2:{paperId}`；doi 从 externalIds 取；pdf_url 取 openAccessPdf.url。
- **crossref_retriever.py**：`GET https://api.crossref.org/works?query=&rows=&filter=type:journal-article`，带 `mailto` UA（polite pool）。citation_count 取 `is-referenced-by-count`。可覆盖 Nature/Science 等综合期刊元数据。
- **pubmed_retriever.py**：NCBI E-utilities（esearch + efetch，XML 解析），无需 key（建议 config 里可配 email/tool 参数）。id `pmid:{pmid}`，pdf_url 尝试 PMC 开放获取链接，否则 None。

所有 retriever 必须：超时 ≤ 30s；单来源失败不中断整体流程（log 警告后返回已得结果）。

## 5. 去重（dedupe.py）

```python
def dedupe(papers: list[Paper]) -> list[Paper]:
    """按优先级 DOI 小写 > 标题归一化（小写去标点）> arXiv id 去重。
    重复时合并：保留 citation_count 非空的、abstract 更长的版本；source 保留首个。"""
```

## 6. 打分（scoring.py）

```python
def score_papers(query: str, papers: list[Paper]) -> None:
    """原地写入 relevance_score / authority_score（0-100）。"""

def relevance(query: str, paper: Paper) -> float:
    """sklearn TfidfVectorizer: corpus = [query] + [title + ' ' + abstract for each paper]，
    query 与各文的 cosine similarity × 100。无 abstract 时只用标题（结果×0.6）。"""

def authority(paper: Paper) -> float:
    """0-100 组合：
    - 引用数：log1p(citation_count)/log1p(max_citations_in_batch) × 50（None 记 0）
    - 来源权重 ×30：Nature/Science/Cell/NEJM/Lancet/PRL/CVPR/NeurIPS/ICML 等顶级 venue（内置名单，大小写不敏感子串匹配）=30；有 venue=18；无=8
    - 新鲜度 ×20：近3年=20，近5年=15，近10年=10，更早=5
    结果 clamp 到 [0,100]。"""
```

## 7. 笔记内容分析（analysis.py）

```python
def analyze_papers(query: str, papers: list[Paper], cfg: dict) -> dict[str, PaperAnalysis]:
    """返回 {paper.id: PaperAnalysis}。
    若 cfg['llm'] 配置齐全（base_url/api_key/model，OpenAI 兼容接口）：
        对每篇调用 LLM 生成 summary/deep_read/skim/questions（中文 prompt），
        再对所有标题+摘要做一次聚类命名调用得到 cluster。
    否则离线降级：
        summary = 摘要前 2-3 句（或标题改写）；deep_read/skim 用启发式模板
        （如含 'experiment'/'results' 关键词则建议精读实验部分等）；
        questions 由标题+摘要关键词模板生成；cluster 用关键词重叠聚类命名。
    LLM 调用失败自动降级离线模式并 log。"""
```

## 8. 配置（config.py）

```python
def load_config(path: str = "config.yaml") -> dict:
    """读 YAML；文件不存在则返回默认 dict 并警告。
    结构：
      zotero: {library_id, library_type: user|group, api_key}
      llm: {base_url, api_key, model}
      unpaywall_email: str
      download_pdfs: true
      max_pdf_mb: 50
    """
```

## 9. PDF 抓取（pdf_fetcher.py）

```python
def fetch_pdfs(papers: list[Paper], out_dir: str, cfg: dict) -> None:
    """对每篇：已有 pdf_url 直接尝试下载（requests，stream，限 cfg['max_pdf_mb']）；
    无 pdf_url 且有 doi 且配置了 unpaywall_email：查 https://api.unpaywall.org/v2/{doi}?email=
    取 best_oa_location.url_for_pdf。
    成功：local_pdf 写入 {out_dir}/{safe_title}.pdf，pdf_status='ok'；
    失败分类：HTTP 401/403 -> 'paywalled'（pdf_note='需要机构认证或付费订阅'），
    找不到 OA 链接 -> 'no_pdf'，其他异常 -> 'error'（note 记录原因）。
    全文不 raise。"""
```

## 10. Zotero 导入（zotero_importer.py）

依赖 `pyzotero`（requirements 列入）。

```python
class ZoteroImporter:
    def __init__(self, library_id: str, library_type: str, api_key: str): ...
    def ensure_collection(self, name: str) -> str:
        """按名字查找 collection，不存在则创建，返回 collection key。"""
    def import_papers(self, papers: list[Paper], collection_name: str,
                      pdf_dir: str | None = None) -> dict:
        """
        对每篇 Paper：
        - itemType: 有 venue 用 'journalArticle' 否则 'preprint'（arxiv）或 'report'
        - 字段: title, creators(authors), abstractNote, date(year), DOI, url,
                extra 里写 'arXiv: ...' / 来源, tags 加 cluster 和 source
        - 加入指定 collection；若 local_pdf 存在，作为 attachment 上传
        返回 {'imported': [Paper...], 'failed': [(Paper, reason)...], 'collection_key': str}
        单条失败不中断。异常（认证失败等）raise ZoteroError 由上层处理。
        """

def import_or_dump(papers: list[Paper], collection_name: str, cfg: dict,
                   out_dir: str) -> dict:
    """便捷入口：zotero 配置齐全 -> ZoteroImporter 导入；
    未配置 -> 把待导入清单写 {out_dir}/zotero_pending.json 并返回
    {'imported': [], 'failed': [], 'pending_dump': path}。"""
```

## 11. Obsidian 生成（obsidian_gen.py）

```python
def generate_vault(query: str, papers: list[Paper],
                   analyses: dict[str, PaperAnalysis], out_dir: str) -> None:
    """
    输出结构（out_dir 即用户之后导入 Obsidian 库的文件夹）：
      out_dir/
      ├── 00-MOC-{安全化query}.md      # Map of Content：按 cluster 分组列出所有论文 wikilink，
      │                                # 每篇一行：[[笔记名]] - 一句话大意 - 评分
      └── papers/
          └── {safe_title}.md        # 每篇一篇
    每篇笔记模板：
      ---
      title: "..."
      source: arxiv
      year: 2024
      authors: [a, b]
      doi: "..."
      url: "..."
      relevance: 87
      authority: 65
      cluster: "..."
      tags: [paper, {cluster_slug}, {source}]
      ---
      # {title}
      > [!info] 元信息表格（作者/年份/期刊/DOI/链接/评分/Zotero 状态）
      ## 内容大意  (analysis.summary)
      ## 建议精读  (analysis.deep_read)
      ## 建议略读  (analysis.skim)
      ## 进一步思考 (analysis.questions 列表)
      ## 相关论文
      同 cluster 的其他论文 wikilink 列表（最多 8 条），格式：- [[笔记名]] — 关系说明（同属{cluster}主题）
      文末：返回 [[00-MOC-...|📑 返回目录]]
    wikilink 目标必须用与文件名一致的 safe_title。文件名 safe 化：去 Obsidian 非法字符
    [\\\\/:*?"<>|#^\\[\\]]，截断 80 字符，冲突时追加短 id。
    """
```

## 12. Excel 报告（excel_report.py）

依赖 `openpyxl`。

```python
def write_excel(query: str, papers: list[Paper],
                analyses: dict[str, PaperAnalysis], out_path: str) -> None:
    """单 sheet '检索结果'，冻结首行，自动筛选，列宽合理：
    序号 | 标题 | 作者(前3+et al) | 年份 | 期刊/会议 | 来源 | DOI | 链接(超链接样式) |
    引用数 | 相关度(0-100) | 权威度(0-100) | 综合分(0.6*相关+0.4*权威) | 主题聚类 |
    PDF状态(ok/paywalled/no_pdf/error) | Zotero导入(已导入/失败/未配置) | 内容大意(摘要,列宽60换行)
    条件格式：相关度/权威度/综合分用 3 色阶；PDF状态 paywalled 标黄、error 标红、ok 标绿。
    首行表头加粗、填充浅灰。按综合分降序排列。"""
```

## 13. CLI 主入口（agent.py）

```
python -m paper_agent.agent "graph neural networks for drug discovery" \
    --n 20 --domain auto --zotero-collection "GNN药物发现" \
    --config config.yaml --out ./runs/run1
```

流程（每步打印进度）：
1. load_config → select_retrievers → 并行/串行 search（每来源 limit = n，汇总后去重）
2. dedupe → score_papers → 按综合分取 top-n
3. analyze_papers
4. fetch_pdfs（out/pdfs/）
5. import_or_dump Zotero
6. generate_vault（out/obsidian_vault/）
7. write_excel（out/检索报告.xlsx）
8. 写 out/run_report.md：检索统计、成功/失败清单（含需要机构认证而无法获取 PDF 的论文列表及原因）、输出文件路径

## 14. 依赖（requirements.txt）

```
requests
pyyaml
scikit-learn
openpyxl
pyzotero
```

（尽量只用这些；XML 解析用标准库。）

## 15. 测试要求

- 每个 retriever：真实 API 调用小样本测试（limit=3），断言返回非空且字段完整；网络不可达时 skip 不 fail。
- dedupe/scoring/obsidian_gen/excel_report 用构造的假数据测试。
- obsidian_gen 测试：生成的 wikilink 目标与实际文件名一一对应（不得有死链）；frontmatter 合法 YAML。
- excel_report 测试：openpyxl 重新打开验证行列数、表头、条件格式存在。
- zotero_importer：无凭证时 import_or_dump 走 pending_dump 分支的测试。
