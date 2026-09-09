# PaperHunter Agent — 论文检索与知识管理 Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)

输入一句检索需求，自动完成：**多源检索 → 相关度/权威度打分 → 导入 Zotero 指定文件夹（含 PDF 原文）→ 生成 Obsidian 网状笔记库 → 生成 Excel 汇总报告**。

## 项目结构

```
paper_agent/
├── agent.py             # CLI 主入口（串联全流程 + 完成通知）
├── router.py            # 领域 → 检索来源智能路由
├── retrievers/          # arXiv / Semantic Scholar / Crossref / PubMed 适配器
├── dedupe.py            # 跨来源去重（DOI / 标题 / arXiv 号）
├── scoring.py           # 相关度（TF-IDF）+ 权威度（引用/期刊/年份）打分
├── pdf_fetcher.py       # 开放获取 PDF 抓取（含魔数校验、付费墙识别）
├── pdf_parser.py        # PDF 全文结构解析（章节/图表/语言检测）
├── analysis.py          # 笔记内容生成（LLM 或离线启发式，语言跟随原文）
├── zotero_importer.py   # Zotero Web API 导入（collection + 附件）
├── obsidian_gen.py      # Obsidian 笔记库生成（wikilink 网状互链 + MOC）
├── excel_report.py      # Excel 汇总报告（条件格式 + 超链接）
├── notify.py            # 跨平台完成通知（桌面通知 + 响铃）
└── config.py            # 配置加载
tests/                   # 53 项测试（pytest）
config.example.yaml      # 配置模板
```

## 功能一览

| 环节 | 说明 |
|---|---|
| 智能路由 | 根据查询自动判断领域：生物医学 → PubMed + Semantic Scholar + Crossref；计算机/物理/数学等 → arXiv + Semantic Scholar + Crossref；综合 → 全源 |
| 去重打分 | DOI/标题/arXiv号 三级去重；相关度（TF-IDF 语义匹配）+ 权威度（引用数、期刊/会议等级、发表年份）双指标，0-100 |
| Zotero | 按你指定的文件夹名自动创建/定位 collection，导入条目元数据 + 链接 + 已抓取的 PDF 附件；**付费墙/需机构认证的原文会在运行报告中单独列出并说明原因** |
| Obsidian | 每篇论文一篇笔记：内容大意、建议精读、建议略读、进一步思考、元信息表格；**若能获取 PDF 原文，精读/略读建议可精确到具体章节编号与图表**（如 "Deep-read Section 3.2、Figure 1"），且笔记语言跟随论文原文（英文论文用英文，不翻译）；笔记间按主题聚类 wikilink 互链 + 一篇 MOC 总目录，导入 Obsidian 后即可在关系图谱（Graph View）中呈现网状结构 |
| Excel | 全部元数据 + 相关度/权威度/综合分 + 主题聚类 + PDF 状态 + Zotero 状态 + 内容大意；三色阶条件格式、状态着色、超链接、冻结首行 |

## 安装

```bash
git clone https://github.com/<你的用户名>/paperhunter-agent.git
cd paperhunter-agent
pip install -r requirements.txt
cp config.example.yaml config.yaml   # 然后按需填写（config.yaml 已在 .gitignore 中，不会泄露 Key）
```

## 配置（config.yaml）

```yaml
zotero:
  library_id: "12345678"     # https://www.zotero.org/settings/keys 页面的 Your userID
  library_type: "user"       # 或 group
  api_key: "你的API Key"      # 同页创建，勾选写权限

llm:                          # 可选。不配则用内置离线启发式生成笔记内容
  base_url: "https://api.openai.com/v1"
  api_key: "..."
  model: "gpt-4o-mini"

unpaywall_email: "you@real-email.com"  # 建议填真实邮箱，用于查询付费论文的开放获取版本
download_pdfs: true
max_pdf_mb: 50
```

> 不配 Zotero 凭证也能运行：待导入清单会写入 `zotero_pending.json` 供你之后导入。
> 不配 LLM 也能运行：笔记内容用启发式模板生成（配置 LLM 后质量更高）。

## 使用

```bash
python -m paper_agent.agent "graph neural networks for drug discovery" \
    --n 20 \
    --domain auto \
    --zotero-collection "GNN药物发现" \
    --config config.yaml \
    --out ./runs/run1
```

参数：
- `query`：自然语言检索需求（中英文均可）
- `--n`：目标论文数量（默认 20）
- `--domain`：`auto`（默认，自动推断）/ `biomedical` / `cs` / `physics` / `math` / `eess` / `general`
- `--zotero-collection`：目标 Zotero 文件夹名，不存在会自动创建；不提供则跳过导入
- `--out`：输出目录
- `--no-notify`：禁用完成时的桌面通知与响铃（默认开启）

运行结束会自动弹出系统桌面通知（macOS / Windows / Linux），并在终端打印完成总结框。

## 输出结构

```
runs/run1/
├── papers.json             # 结构化中间产物
├── pdfs/                   # 成功抓取的 PDF 原文
├── obsidian_vault/         # 整个文件夹复制进你的 Obsidian 库即可
│   ├── 00-MOC-*.md         # 主题总目录（按聚类分组）
│   └── papers/*.md         # 每篇论文一篇笔记（互链成网）
├── 检索报告.xlsx            # Excel 汇总
├── zotero_pending.json     # （仅未配置凭证时）待导入清单
└── run_report.md           # 运行报告：统计 + 成功/失败清单 + 需机构认证的论文列表
```

## 常见问题

- **Semantic Scholar 报 429/超时**：公共 API 对共享 IP 限流，程序会自动退避重试和降级，其他来源不受影响；稍后重跑即可。
- **某些论文没有 PDF**：出版商付费墙或需机构认证，运行报告会明确列出，请通过学校 VPN/图书馆渠道手动下载后放入 Zotero。
- **某些论文"内容大意"较简略**：Crossref 很多条目不提供摘要；配置 LLM 或换用 arXiv/PubMed 丰富的查询可改善。
- **建议没有精确到章节/图表**：需要成功下载 PDF 原文（`download_pdfs: true`）才能解析全文结构；配置 LLM 后建议质量更高。无法获取 PDF 时会自动降级为基于摘要的建议，且不会编造章节号。

## 测试

```bash
python -m pytest tests/ -q    # 54 项测试
```
