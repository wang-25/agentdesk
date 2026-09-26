# -*- coding: utf-8 -*-
"""RAG 检索增强模块。

    loader.py     文档加载与切分
    embedder.py   文本向量化（可切换后端）
    store.py      向量存储、向量检索、BM25、混合检索融合
    pipeline.py   串起全链路 + RAG 问答 + 召回率评测
"""
