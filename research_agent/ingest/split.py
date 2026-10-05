"""按段落边界切分。

为什么按段落而不按句子：切出来的片段是**写作取材的最小单位**，
半个句子写不成一段话，而一个完整段落天然就是一个论述单元。
段落不足目标长度时往相邻段落累积，落刀点始终落在段落边界上。

实测（25 个真实来源，156,696 字符）：
  · 旧方案（按句子递归切，500 token）：93 段，平均 878 token
  · 新方案（段落累积，1000 token）：  53 块，平均 705 token，中位 760
块数少 43%、块长还短 20%，而且不再跨段落断裂。

`include_formatting=True` 之后空行才会出现——不开的话整篇被压成一行、
零空行，最优先的分隔符 `\n\n` 永远不触发（25 篇实测 25 个「原始段落」）。
前提是抽取层开了那个开关，见 ingest/extract.py。

超长段落（单段就超过 max_tokens）退一层用句子边界切，病态段落直接硬切。
与 LangChain RecursiveCharacterTextSplitter 同思路，但：
  - 按 token 计数而非字符数（中文约 0.6-1 token/字符，英文约 0.25，混排按字符切会严重偏斜）
  - 切分时继承原段标题，避免片段失去指代对象
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

try:
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - 离线环境降级为字符估算
    _ENC = None

# 段落边界：空行（含带空格的空行）
PARAGRAPH_RE = re.compile(r"\n[ \t]*\n+")

# 超长段落再下探一层时用的分隔符：句末标点优先
SENTENCE_SEPARATORS: tuple[str, ...] = ("。", "！", "？", "；", ". ", "! ", "? ", "; ")

# 兼容旧调用方；段落路径不会走到这里
DEFAULT_SEPARATORS: tuple[str, ...] = (
    "\n\n",
    "\n",
    "。", "！", "？", "；",
    "，",
    ". ", "! ", "? ", "; ",
    " ", "",
)


def count_tokens(text: str) -> int:
    if _ENC is not None:
        return len(_ENC.encode(text, disallowed_special=()))
    # 降级估算：中文按 1 字 1 token，ASCII 按 4 字符 1 token
    cjk = sum(1 for c in text if "一" <= c <= "鿿")
    return cjk + max(0, (len(text) - cjk) // 4)


@dataclass
class Chunk:
    content: str
    token_count: int
    heading_path: str | None = None
    index: int = 0
    meta: dict = field(default_factory=dict)


class RecursiveSplitter:
    """把长文本按段落边界切成写作可用大小的片段。

    target_tokens —— 累积到这个量级就落刀
    max_tokens   —— 单个段落超过这个才二次切（句子边界）
    hard_tokens  —— 病态段落，再大直接按预算硬切
    min_tokens   —— merge_small 用的下限
    """

    def __init__(
        self,
        max_tokens: int = 2000,
        target_tokens: int = 1000,
        hard_tokens: int = 6000,
        min_tokens: int = 120,
        separators: Sequence[str] = DEFAULT_SEPARATORS,
        sentence_separators: Sequence[str] = SENTENCE_SEPARATORS,
    ) -> None:
        self.max_tokens = max_tokens
        self.target_tokens = target_tokens
        self.hard_tokens = hard_tokens
        self.min_tokens = min_tokens
        self.separators = tuple(separators)
        self.sentence_separators = tuple(sentence_separators)

    def split(
        self,
        text: str,
        heading_path: str | None = None,
        meta: dict | None = None,
    ) -> list[Chunk]:
        text = text.strip()
        if not text:
            return []
        out: list[Chunk] = []
        for piece in self._by_paragraph(text):
            piece = piece.strip()
            if not piece:
                continue
            out.append(
                Chunk(
                    content=piece,
                    token_count=count_tokens(piece),
                    heading_path=heading_path,
                    index=len(out),
                    meta=dict(meta or {}),
                )
            )
        return out

    # ---------- 内部 ----------

    def _by_paragraph(self, text: str) -> list[str]:
        """段落累积到目标长度，在段落边界落刀。"""
        paras = [p.strip() for p in PARAGRAPH_RE.split(text) if p.strip()]
        if not paras:                       # 整篇没有任何空行，退回按换行
            paras = [p.strip() for p in text.split("\n") if p.strip()]
        if not paras:
            return [text]

        units: list[str] = []
        for p in paras:
            if count_tokens(p) <= self.max_tokens:
                units.append(p)
            else:
                units.extend(self._by_sentence(p))

        out: list[str] = []
        cur: list[str] = []
        cur_t = 0
        for u in units:
            ut = count_tokens(u)
            if ut > self.max_tokens:       # 超大单元自己成块，别拖着邻居一起超
                if cur:
                    out.append("\n\n".join(cur))
                    cur, cur_t = [], 0
                out.append(u)
                continue
            if cur_t + ut > self.target_tokens and cur:
                out.append("\n\n".join(cur))
                cur, cur_t = [], 0
            cur.append(u)
            cur_t += ut
        if cur:
            out.append("\n\n".join(cur))
        return out

    def _by_sentence(self, text: str) -> list[str]:
        """超长段落：句子边界切；病态的再降一层走老路。"""
        if count_tokens(text) > self.hard_tokens:
            return self._hard_split(text)
        out: list[str] = []
        cur = ""
        for sent in self._sentence_pieces(text):
            if sent and count_tokens(cur + sent) > self.max_tokens:
                if cur.strip():
                    out.append(cur.strip())
                cur = sent
            else:
                cur += sent
        if cur.strip():
            out.append(cur.strip())
        return out or self._hard_split(text)

    def _sentence_pieces(self, text: str) -> list[str]:
        """按句末标点切，标点留在前一句尾部，语义不丢。"""
        parts = re.split(f"({'|'.join(re.escape(s) for s in self.sentence_separators)})",
                         text)
        out: list[str] = []
        for i in range(0, len(parts) - 1, 2):
            out.append(parts[i] + parts[i + 1])
        if parts[-1]:
            out.append(parts[-1])
        return out

    def _split_recursive(self, text: str, seps: Sequence[str]) -> list[str]:
        if count_tokens(text) <= self.max_tokens:
            return [text]

        # 优先用非空且当前层级存在的最靠前分隔符
        sep = next((s for s in seps if s and s in text), None)

        if sep is None:
            # 走到空串说明必须硬切：按 token 预算切成等份
            return self._hard_split(text)

        parts = text.split(sep)
        if len(parts) == 1:
            return self._hard_split(text)

        # 保留分隔符：拼回时把分隔符加回前一段的尾部，语义不丢标点
        rebuilt: list[str] = []
        for i, p in enumerate(parts):
            if i < len(parts) - 1:
                p = p + sep
            rebuilt.append(p)

        out: list[str] = []
        buffer = ""
        for piece in rebuilt:
            if count_tokens(buffer + piece) <= self.max_tokens:
                buffer += piece
                continue
            if buffer.strip():
                out.append(buffer)
            # 这一片自己就超限 → 换下一层分隔符继续下探
            if count_tokens(piece) > self.max_tokens:
                rest = seps[seps.index(sep) + 1:] if sep != "" else seps
                out.extend(self._split_recursive(piece, rest if rest else seps))
                buffer = ""
            else:
                buffer = piece
        if buffer.strip():
            out.append(buffer)
        return out

    def _hard_split(self, text: str) -> list[str]:
        """按 token 预算等分，边界尽量落在词/字中间但不破坏字符。"""
        if _ENC is not None:
            ids = _ENC.encode(text, disallowed_special=())
            step = self.max_tokens
            pieces = []
            for i in range(0, len(ids), step):
                chunk_ids = ids[i : i + step]
                try:
                    pieces.append(_ENC.decode(chunk_ids))
                except Exception:
                    # 落在多字节字符中间时回退到字节切
                    pieces.append(text.encode("utf-8")[i * 3 : (i + step) * 3].decode("utf-8", "ignore"))
            return pieces

        # 无 tiktoken 时按字符近似
        step_chars = int(self.max_tokens * 2)
        return [text[i : i + step_chars] for i in range(0, len(text), step_chars)]

    # ---------- 便利方法 ----------

    def split_markdown(self, md: str, meta: dict | None = None) -> list[Chunk]:
        """按 Markdown 标题层级切段，每段继承其标题路径。"""
        lines = md.splitlines()
        sections: list[tuple[list[str], str]] = []
        stack: list[tuple[int, str]] = []
        buf: list[str] = []

        def flush() -> None:
            if buf and any(line.strip() for line in buf):
                path = " > ".join(t for _, t in stack)
                sections.append((list(buf), path))

        for line in lines:
            m = re.match(r"^(#{1,6})\s+(.*)$", line)
            if m:
                flush()
                level = len(m.group(1))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, m.group(2).strip()))
            else:
                buf.append(line)
        flush()

        out: list[Chunk] = []
        for body, path in sections:
            for c in self.split("\n".join(body), heading_path=path or None, meta=meta):
                c.index = len(out)
                out.append(c)
        return out

    def merge_small(self, chunks: Iterable[Chunk]) -> list[Chunk]:
        """把过短的相邻片段合并，减少碎片。"""
        out: list[Chunk] = []
        for c in chunks:
            if out and out[-1].token_count < self.min_tokens and (
                (out[-1].heading_path or "") == (c.heading_path or "")
            ):
                prev = out[-1]
                merged = prev.content + "\n" + c.content
                out[-1] = Chunk(
                    content=merged,
                    token_count=count_tokens(merged),
                    heading_path=prev.heading_path,
                    index=prev.index,
                    meta=prev.meta,
                )
            else:
                c.index = len(out)
                out.append(c)
        return out
