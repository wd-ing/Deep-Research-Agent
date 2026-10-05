"""向量化封装。

固定用 multilingual-e5-small（384 维）。

硬规则：e5 系列必须在输入前加前缀，query 加 "query: "，文档加 "passage: "。
漏掉前缀质量会显著下降，这不是可选项。

存储用 float16（体积减半），算余弦时转 float32。
"""

from __future__ import annotations

import os
import threading
from typing import Iterable, Sequence

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


def cosine_topk(
    query: np.ndarray,
    matrix: np.ndarray,
    k: int = 10,
) -> tuple[np.ndarray, np.ndarray]:
    """余弦相似度 Top-K。

    入库时向量已 L2 归一化，所以点积即余弦。
    向量按需从 SQLite 读出，不常驻。
    """
    if matrix.size == 0:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float32)
    k = min(k, matrix.shape[0])
    q = np.asarray(query, dtype=np.float32)
    qn = np.linalg.norm(q)
    if qn > 0:
        q = q / qn
    sims = matrix @ q
    idx = np.argpartition(-sims, k - 1)[:k]
    idx = idx[np.argsort(-sims[idx])]
    return idx, sims[idx]


def stack_blobs(blobs: Iterable[bytes]) -> np.ndarray:
    mats = [from_blob(b) for b in blobs]
    if not mats:
        return np.zeros((0, DIM), dtype=np.float32)
    return np.vstack(mats)


# ---------- 时间衰减 ----------

def time_decay(source_date: str | None, now: str, half_life_days: float = 180.0) -> float:
    """source_date 为空表示时效未知，不衰减（返回 1.0）。"""
    from datetime import datetime, timezone

    if not source_date:
        return 1.0
    try:
        d = datetime.fromisoformat(source_date.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        age = (datetime.fromisoformat(now.replace("Z", "+00:00")) - d).total_seconds() / 86400
    except Exception:
        return 1.0
    if age <= 0:
        return 1.0
    return float(0.5 ** (age / half_life_days))
