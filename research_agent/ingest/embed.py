"""向量化封装。

固定用 multilingual-e5-small（384 维）。

硬规则：e5 系列必须在输入前加前缀，query 加 "query: "，文档加 "passage: "。
漏掉前缀质量会显著下降，这不是可选项。

存储用 float16（体积减半），算余弦时转 float32。
"""

from __future__ import annotations

import os
import threading
from typing import Sequence

import numpy as np

MODEL_NAME = os.environ.get("RA_EMBED_MODEL", "intfloat/multilingual-e5-small")
DIM = 384

QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "


class Embedder:
    """惰性加载的句向量模型，单例复用。"""

    _instance: "Embedder | None" = None
    _lock = threading.Lock()

    def __init__(self, model_name: str = MODEL_NAME) -> None:
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    @classmethod
    def instance(cls) -> "Embedder":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name, device="cpu")
            getter = getattr(self._model, "get_embedding_dimension", None) \
                or self._model.get_sentence_embedding_dimension
            dim = getter()
            if dim and dim != DIM:
                raise RuntimeError(
                    f"模型维度是 {dim}，与配置 DIM={DIM} 不符。"
                    f"换模型时必须同步改 DIM，否则存量向量无法比较。"
                )
        return self._model

    # ---------- 向量化 ----------

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray:
        """文档侧。加 passage: 前缀。返回 float32 矩阵 (n, dim)。"""
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        payload = [PASSAGE_PREFIX + t for t in texts]
        return self._encode(payload)

    def embed_queries(self, texts: Sequence[str]) -> np.ndarray:
        """查询侧。加 query: 前缀。返回 float32 矩阵 (n, dim)。"""
        if not texts:
            return np.zeros((0, DIM), dtype=np.float32)
        payload = [QUERY_PREFIX + t for t in texts]
        return self._encode(payload)

    def _encode(self, payload: Sequence[str]) -> np.ndarray:
        model = self._load()
        vecs = model.encode(
            list(payload),
            batch_size=16,
            convert_to_numpy=True,
            normalize_embeddings=True,  # 归一化后余弦相似度退化为点积
            show_progress_bar=False,
        )
        return np.asarray(vecs, dtype=np.float32)

    def embed_document(self, summary: str | None, content: str) -> np.ndarray:
        """单条入库用。概括句在前当索引前缀，与正文拼接后向量化。"""
        text = f"{summary}\n{content}" if summary else content
        return self.embed_passages([text])[0]


# ---------- 序列化与相似度 ----------

def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float16).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float16).astype(np.float32)


# ⚠️ 这里原来有 `cosine_topk()` 和 `stack_blobs()`（2026-10-13 删）。
# 它俩是一套东西的两半：`stack_blobs` 把 BLOB 还原成矩阵，
# `cosine_topk` 在矩阵上按余弦取 Top-K。这正是「按相似度召回段落」
# 的实现——而**这个环节从来没被接上**：`node_finalize` 算完向量就写库，
# 全库没有一条查询去读它。删之前它们唯一的使用者是 `tests/smoke.py` 的
# `t_embed`，那是拿一个从没有真实调用方的函数去测它自己。
#
# 上面的 `time_decay()` 同理（2026-10-13 删）：来源越新权重越高的指数衰减
# （每 180 天减半），从初始提交 fe5129c 就在，一次都没被调用过，因为它要
# 服务的库内检索排序从来没被实现过。它不是「没写完」，是给一个不存在的
# 环节预留的零件。留着它会误导——HANDOVER 里那句「时间衰减接不到排序」
# 就会一直暗示「排序已经存在、只差接一下」。将来真要做库内检索时，
# 衰减公式是标准做法，重写只要几分钟。
#
# ⚠️ 注意别把 `source_date` 字段一起删了——那个**在大量使用**：
# 来源库显示日期、`read_source` 工具让模型看得到来源日期、抓取时写入。
# 删掉的只是「怎么用日期算权重」这个策略，不是时间信息本身。
#
# ⚠️ `source_date` 同理别误删。上面删的三样都只是**策略**，
# 数据（`chunk.embedding` 列、`source_date` 字段、`content`/`summary` 正文）
# 一个都没动。`content` 和 `summary` 是写作阶段的证据入口，必不可删。

# 📌 本模块现在**零生产调用方**（2026-10-13 起）：向量化已停，
#    节点里最后那处 `EMB.embed_document()` 随 node_finalize 一起删了。
#    `embed_passages` / `embed_document` / `to_blob` / `from_blob` 留着，
#    因为「库里按语义找段落」是这个工具迟早要长出来的功能，重写一个
#    句向量封装（含 e5 系列的 query/passage 前缀硬规则）不值当。
#    代价是 sentence_transformers 这个重依赖还挂在依赖树里。
