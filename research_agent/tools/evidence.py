"""三个全局证据工具：往下挖证据。

写作阶段所有角色默认只看**段落概括**。信息不够时自己往下挖：

    read_segments  按段号看段落全文        —— 「这句概括说的到底是什么意思」
    read_context   连同前后邻段一起看      —— 「这段的前因后果是什么」
    read_source    看整篇正文              —— 「这个来源整体是什么货色」

为什么需要三个而不是一个：
  · 段（约 700 token）是主力粒度，展开便宜，随便读
  · 邻段是「读一段」的天然延伸，否则模型得自己知道相邻段号
  · 全文中位 2,895 字符（≈1,450 token），25 个来源里 24 个都负担得起；
    只有超长来源（实测最高 46,549 字符）才需要留意

刻意不设配额。限制总量会让 agent 不知道自己漏了什么，
「看似完整实则残缺」比多读几段糟得多。只防循环（MAX_ROUNDS），不防总量。
"""

from __future__ import annotations

import json
import re

MAX_SOURCE_CHARS = 60000     # 全文工具的单次上限，超出截断并明说


def _clip(text: str, cap: int) -> tuple[str, str]:
    if len(text) <= cap:
        return text, ""
    elided = len(text) - cap
    return (text[: int(cap * 0.8)]
            + f"\n\n【中间省略 {elided} 字符：超长来源保护】\n\n"
            + text[-int(cap * 0.2):],
            f"（该来源正文 {len(text):,} 字符，此处只给了头尾）")


class EvidenceTools:
    """按 session 作用域提供三个读取工具。只读，不改任何东西。

    段号方案：来源按**入库顺序**编号 S1、S2…，段按来源内顺序编号 S3·5。
    用 rowid（插入顺序）而不是 fetched_at——后者同秒插入会撞。
    写作阶段必须用**同一套编号**，否则引用和工具对不上。
    """

    def __init__(self, repo, session_id: str) -> None:
        self.repo = repo
        self.session_id = session_id
        self.src_no: dict[str, int] = {}
        self.seg_no: dict[str, int] = {}       # chunk_id -> 段号整数
        self.chunk_of: dict[str, str] = {}    # "3·5" -> chunk_id
        self.src_of: dict[str, str] = {}      # "3" -> source_id
        self._build_index()

    def _build_index(self) -> None:
        srcs = self.repo.q(
            "SELECT id FROM source WHERE session_id=? ORDER BY rowid",
            (self.session_id,))
        for i, s in enumerate(srcs, 1):
            self.src_no[s["id"]] = i
            self.src_of[str(i)] = s["id"]
        segs = self.repo.q(
            "SELECT c.id, c.source_id FROM chunk c JOIN source s ON s.id=c.source_id "
            "WHERE c.session_id=? ORDER BY s.rowid, c.rowid", (self.session_id,))
        per: dict[str, int] = {}
        for c in segs:
            n = per.get(c["source_id"], 0) + 1
            per[c["source_id"]] = n
            no = self.src_no.get(c["source_id"], 0)
            self.seg_no[c["id"]] = n
            self.chunk_of[f"S{no}·{n}"] = c["id"]

    # ---------- schema ----------

    def specs(self) -> list[dict]:
        return [
            {
                "name": "read_segments",
                "description": "按段号查看段落全文。段号来自你手上的证据清单（形如 S3·5）。"
                               "当概括不足以支撑判断时用它。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "segments": {
                            "type": "array", "items": {"type": "string"},
                            "description": "段号列表，如 [\"S3·5\", \"S3·6\"]，可一次给多个",
                        }
                    },
                    "required": ["segments"],
                },
            },
            {
                "name": "read_context",
                "description": "查看某一段的全文，以及它前后各若干个相邻段。"
                               "当你怀疑「这句话是承上启下、不能单独看」时用它。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "segment": {"type": "string", "description": "段号，如 \"S3·5\""},
                        "before": {"type": "integer", "description": "向前取几段，默认 1"},
                        "after": {"type": "integer", "description": "向后取几段，默认 1"},
                    },
                    "required": ["segment"],
                },
            },
            {
                "name": "read_source",
                "description": "查看某来源的完整正文（分块给出，带各自概括）。"
                               "当你需要判断「这个来源整体是什么货色、"
                               "哪些部分可信」时用它。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string",
                                   "description": "来源号，如 \"S3\""},
                    },
                    "required": ["source"],
                },
            },
        ]

    # ---------- 执行 ----------

    def dispatch(self, calls: list[dict]) -> list[dict]:
        """一轮里的每个 tool_use 都必须有一个对应的 tool_result。"""
        out: list[dict] = []
        for c in calls:
            payload, err = self._one(c)
            out.append({
                "type": "tool_result",
                "tool_use_id": c.get("id"),
                "content": (json.dumps(payload, ensure_ascii=False, indent=1)
                            if not err else f"调用失败：{err}"),
            })
        return out

    def _one(self, call: dict) -> tuple[dict | str, str]:
        name = call.get("name")
        args = call.get("input") or {}
        try:
            if name == "read_segments":
                return self._read_segments(args.get("segments") or []), ""
            if name == "read_context":
                return self._read_context(args.get("segment"),
                                          _int(args.get("before"), 1),
                                          _int(args.get("after"), 1)), ""
            if name == "read_source":
                return self._read_source(args.get("source")), ""
            return {}, f"未知工具 {name}"
        except Exception as e:
            return {}, f"{type(e).__name__}: {e}"

    def _read_segments(self, refs) -> dict:
        ids, missing = [], []
        for r in refs or []:
            cid = self._chunk_id(str(r))
            (ids if cid else missing).append(cid or str(r))
        rows = self._chunks(ids)
        got = {r["id"]: r for r in rows}
        out = [self._seg_view(got[i]) for i in ids if i in got]
        res = {"段落": out}
        if missing or len(got) < len(ids):
            res["没找到"] = missing + [i for i in ids if i not in got]
        return res

    def _read_context(self, ref, before: int, after: int) -> dict:
        cid = self._chunk_id(str(ref)) if ref else None
        if not cid:
            return {"说明": f"段号 {ref!r} 无法解析", "格式": "形如 S3·5"}
        base = self.repo.q1(
            "SELECT id, source_id FROM chunk WHERE id=? AND session_id=?",
            (cid, self.session_id))
        if not base:
            return {"说明": f"段 {ref} 不在本次研究里"}
        sibs = self.repo.q(
            "SELECT id, heading_path, content, summary, token_count "
            "FROM chunk WHERE source_id=? AND session_id=? ORDER BY rowid",
            (base["source_id"], self.session_id))
        idx = next((i for i, r in enumerate(sibs) if r["id"] == cid), None)
        if idx is None:
            return {"说明": f"段 {ref} 不在本次研究里"}
        lo, hi = max(0, idx - before), min(len(sibs), idx + after + 1)
        return {
            "中心段": self._seg_view(self.repo.q1(
                "SELECT id, source_id, heading_path, content, summary, token_count "
                "FROM chunk WHERE id=?", (cid,))),
            "上下文": [self._seg_view(r) for r in sibs[lo:hi]],
            "该来源共几段": len(sibs),
            "取的范围": f"第 {lo+1} 到 {hi} 段（中心是第 {idx+1} 段）",
        }

    def _read_source(self, ref) -> dict:
        sid = self._source_id(str(ref)) if ref else None
        if not sid:
            return {"说明": f"来源号 {ref!r} 无法解析", "格式": "形如 S3"}
        s = self.repo.q1("SELECT id, title, url, domain, source_type, source_date, "
                          "source_nature, nature_reason, char_count "
                          "FROM source WHERE id=?", (sid,))
        if not s:
            return {"说明": f"来源 {ref} 不存在"}
        rows = self.repo.q(
            "SELECT id, heading_path, content, summary, token_count, on_topic "
            "FROM chunk WHERE source_id=? AND session_id=? ORDER BY rowid",
            (sid, self.session_id))
        total = sum(len(r["content"] or "") for r in rows)
        body, note = "", ""
        if total > MAX_SOURCE_CHARS:
            # 超长：给每段的头尾，中间明说省略了多少
            per = max(400, MAX_SOURCE_CHARS // max(1, len(rows)))
            body = "\n\n".join(_clip(r["content"] or "", per)[0] for r in rows)
            note = f"（该来源正文 {total:,} 字符，分 {len(rows)} 段，每段只给了头尾）"
        else:
            body = "\n\n".join(r["content"] or "" for r in rows)
        return {
            "来源": {k: s[k] for k in ("title", "url", "domain", "source_type",
                                        "source_date", "source_nature",
                                        "nature_reason", "char_count")},
            "段数": len(rows),
            "提示": note,
            "正文": body,
            "各段概括": [{"段号": self._ref(r["id"]), "概括": r["summary"],
                          "相关": r["on_topic"]} for r in rows],
        }

    # ---------- 内部 ----------

    def _seg_view(self, r) -> dict:
        if r is None:
            return {}
        r = dict(r)
        cid = r.pop("id", "")
        sid = r.pop("source_id", "")
        out = {"段号": self._ref(cid)}
        if sid:
            out["来源号"] = self._src_ref(sid)
        if r.get("heading_path"):
            out["标题"] = r["heading_path"]
        out["全文"] = r.get("content") or ""
        if r.get("summary"):
            out["概括"] = r["summary"]
        if "token_count" in r:
            out["token"] = r["token_count"]
        return out

    def _chunks(self, ids: list[str]) -> list:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.repo.q(
            f"SELECT id, source_id, heading_path, content, summary, token_count "
            f"FROM chunk WHERE session_id=? AND id IN ({marks})",
            [self.session_id] + ids)

    def _chunk_id(self, ref: str) -> str | None:
        r = (ref or "").strip().replace(".", "·")
        if r in self.chunk_of:
            return self.chunk_of[r]
        # 容错：省略来源号的纯段号，只在全库唯一时接受
        if "·" not in r and r.isdigit():
            hit = [cid for k, cid in self.chunk_of.items() if k.split("·")[1] == r]
            return hit[0] if len(hit) == 1 else None
        return None

    def _source_id(self, ref: str) -> str | None:
        return self.src_of.get((ref or "").strip().lstrip("S"))

    def _ref(self, chunk_id: str) -> str:
        """chunk id → 报告里用的段号。"""
        n = self.seg_no.get(chunk_id)
        r = self.repo.q1("SELECT source_id FROM chunk WHERE id=?", (chunk_id,))
        s = self.src_no.get(r["source_id"]) if r else None
        return f"S{s}·{n}" if s and n else chunk_id

    def _src_ref(self, source_id: str) -> str:
        n = self.src_no.get(source_id)
        return f"S{n}" if n else source_id


def _int(v, default: int) -> int:
    try:
        return max(0, min(8, int(v)))
    except Exception:
        return default
