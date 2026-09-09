"""配置加载（SPEC §8）。

config.yaml 结构：
  zotero: {library_id, library_type: user|group, api_key}
  llm: {base_url, api_key, model}
  unpaywall_email: str
  download_pdfs: true
  max_pdf_mb: 50
"""

from __future__ import annotations

import logging
import os

import yaml

logger = logging.getLogger(__name__)

# 默认配置：文件缺失或字段缺省时以此为底
DEFAULT_CONFIG: dict = {
    "zotero": {
        "library_id": "",
        "library_type": "user",
        "api_key": "",
    },
    "llm": {
        "base_url": "",
        "api_key": "",
        "model": "",
    },
    "unpaywall_email": "",
    "download_pdfs": True,
    "max_pdf_mb": 50,
}


def _merge_defaults(cfg: dict) -> dict:
    """以 DEFAULT_CONFIG 为底深合并（仅一层嵌套），缺失键用默认值补齐。"""
    merged: dict = {}
    for key, default_val in DEFAULT_CONFIG.items():
        user_val = cfg.get(key)
        if isinstance(default_val, dict):
            merged[key] = dict(default_val)
            if isinstance(user_val, dict):
                merged[key].update({k: v for k, v in user_val.items() if v is not None})
        else:
            merged[key] = user_val if user_val is not None else default_val
    # 保留用户配置里的额外键
    for key, val in cfg.items():
        if key not in merged:
            merged[key] = val
    return merged


def load_config(path: str = "config.yaml") -> dict:
    """读 YAML；文件不存在则返回默认 dict 并警告。解析失败同样降级为默认值。"""
    if not os.path.exists(path):
        logger.warning("配置文件 %s 不存在，使用默认配置", path)
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001 - 配置损坏不应导致整体崩溃
        logger.warning("配置文件 %s 解析失败（%s），使用默认配置", path, exc)
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULT_CONFIG.items()}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        logger.warning("配置文件 %s 顶层不是 mapping，使用默认配置", path)
        raw = {}
    return _merge_defaults(raw)
