"""write 子图：主 agent 统筹，多个子 agent 分块写。

存在的理由不是「流程好看」，而是三个具体的工程问题：

1. **盲写会跑偏。** 每节各自调用时互不可见，同一概念在不同节用了不同名称、
   同一事实被重复三次、某节凭空断言了材料里没有的东西。实测上一轮 18/22 个
   来源在多节里被引用，第 1 节说「半固态无定义」而第 7 节当成熟概念展开。
2. **一次写全篇会炸。** 一次调用要组织十几节、上百条断言，reasoning 模型的
   思考量会吃光输出预算（实测 MiniMax M3.1 Flash 思考 16,928 字符后空返回）。
3. **写完没人看。** 没有任何环节回头检查接地性、跨节一致性、引用规范。

所以：主 agent 先规划（产规约 + 任务列表），子 agent 各写一块，
主 agent 再通读审查、按需定点返修。critic 不另设角色——写作意图只存在于
规约里，规约是主 agent 写的，异构 critic 拿到的是二手信息。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langgraph.graph import END, START, StateGraph

from ...llm.provider import (LLMClient, ConfigError, _parse_json,
                             fire_progress, parallel_labelled)
from ...llm.toolloop import run_tool_loop
from .. import prompts
from ..state import WriteState

MAX_TASKS = 15              # 任务数上限。测试阶段的硬边界，防止主 agent 切得过细
CONCURRENCY = 4             # 并发子 agent 上限
MAX_REWORK = 2              # 返修轮数上限
MAX_TOOL_ROUNDS = 3         # plan 取全文的工具循环上限
TASK_MARK_RE = re.compile(r"<!--\s*task:(\S+)\s+section:(\S+)\s*-->")
# 子 agent 分节输出用的标记。`covers` 多于一节时必须逐节标注，否则无法归位。
SECTION_MARK_RE = re.compile(r"<!--\s*section:(\S+)\s*-->")
# 子 agent 给「引言标题」加的标记。带它的标题被降成正文段落、不进目录也不编号
# ——它和节标题讲的是同一件事，再给一个子标题身份，目录里就会冒出两条近似条目。
LEAD_MARK_RE = re.compile(r"<!--\s*lead\s*-->")
SEG_REF_RE = re.compile(r"\[S(\d+)·(\d+)\]")          # 段级引用 S3·5
SEG_REF_LOOSE_RE = re.compile(r"\[S\d+[^\]]*?\]")     # 自创写法
SRC_REF_RE = re.compile(r"\[S(\d+)\]")               # 旧的来源级写法，不允许

# 行首标题：1~6 级都算，后面必须跟空格。`#######` 与 `#标题` 不算。
# 为什么 1 级也算：小节内容永远在 `## {节标题}` 之下，草稿里出现 `#`
# 一律是模型自造的错位标题，要进降级/重编——v13 实测 `# secN xxx`
# 一级标题不认就直接原样漏进终稿（坑 64）。
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# 子 agent 自造的编号前缀。v5 实测四种形态全出现过：
#   「一、」「五、」中文数字 / 「4.」「2 」阿拉伯数字 / 「1.1」「2.3.1」多级 / 「sec3」
# 数字后必须有空白（中文数字则须跟顿号/点）。这一条是防误伤的关键：
# 没有它，`### 500强企业排名` 会被剥成「强企业排名」、`### 3D打印` 被剥成「D打印」
# ——而这两种根本不是编号。第一版就栽在这里，靠真实数据回放才看出来。
_NUM_PREFIX_RE = re.compile(
    r"^(?:"
    r"[一二三四五六七八九十百]+[、.．][ \t]*"       # 一、/ 五、 中文数字后通常不留空格
    r"|\d+(?:\.\d+)*[、.．]?[ \t]+"                # 1 / 4. / 1.1 / 2.3.1（可无标点但必须有空白）
    r"|sec\d+(?:[ \t]+|[：:][ \t]*)"               # sec3 / sec3： ——全角冒号是实测形态
    r")", re.IGNORECASE)

LLM = LLMClient()


# 分派结果的 schema（2026-10-17 加）。字段与 `_normalize_tasks` 读的
# 名字一一对应——**那份函数是契约的另一头**，改一边必须改另一边。
#
# 为什么不能只靠提示词约束它：纯提示词路径下模型要手写整份 JSON，
# 实测输出 7,135 token，而长 JSON 会在中途崩（报错在第 630 字符而非结尾，
# 见 `complete_json_forced` 的记录）。挂成工具之后模型只填 `arguments`，
# 平台在解码层面保证合法。
PLAN_TASKS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "description": "写作任务。上限 15 个。",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "任务 id，形如 t1"},
                    "covers": {
                        "type": "array",
                        "description": "这个任务负责哪几个小节 id（来自大纲）",
                        "items": {"type": "string"},
                    },
                    "mandate": {"type": "string",
                                "description": "这一块要回答什么问题"},
                    "segments": {
                        "type": "array",
                        "description": "分给这个任务的段号，形如 S3·5",
                        "items": {"type": "string"},
                    },
                    "must_cover": {"type": "array", "items": {"type": "string"}},
                    "not_yours": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["covers", "mandate", "segments"],
            },
        },
    },
    "required": ["tasks"],
}

# Anthropic 形状，与 `EvidenceTools.specs()` 同一套（`run_tool_loop`
# 内部自己转 OpenAI 形状，别在这里转）。
SUBMIT_TASKS: dict = {
    "name": "submit_tasks",
    "description": "证据读够了就调用它交卷，参数是完整的任务列表。"
                   "判断依据够了之后不必继续读原文。",
    "input_schema": PLAN_TASKS_SCHEMA,
}

# 子 agent 的「交卷」工具（2026-10-19 加）：正文走 `input.sections[].body`，
# 不走对话文本。措辞与 tests/exp_submit_section.py 的实验版逐字一致——
# 那版在真实 API 上验证过有效，别顺手改写。
#
# 为什么必须走工具：v13 实测把工具循环最后一轮的 text 块当正文，
# 里面混着前言（「下面是我重写的 sec2 正稿」）、英文推理/自检、
# MiniMax 伪工具调用标记（`]<]minimax[>[<tool_call>…`）、```markdown
# 围栏、`# secN` 任务头——`_split_sections` 只 strip，全漏进报告。
# 更糟的一次：返修输出只有英文推理，却**覆盖了**本来完好的旧稿。
# 结构化之后正文是数据不是自由文本，这条污染通道直接消失（坑 75）。
SUBMIT_SECTION: dict = {
    "name": "submit_section",
    "description": (
        "交卷：写完正文后调用一次。"
        "**正文只能放在 sections[].body 里**（每节一项，id 是小节 id）；"
        "工具调用之外的对话文本会被丢弃，不要在那里写正文、前言或自检。"),
    "input_schema": {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "description": "每个覆盖小节一项，顺序即正文顺序",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string",
                               "description": "小节 id，如 sec3"},
                        "body": {"type": "string",
                                 "description": "该节的 Markdown 正文"
                                                "（含 [S{n}·{m}] 引用、"
                                                "<!-- lead --> 标记）；"
                                                "不含前言、自检、任务头或代码围栏"},
                    },
                    "required": ["id", "body"],
                },
            },
            "notes": {"type": "string",
                      "description": "给审阅者的备注（不进报告）：引用核对、材料缺口等"},
        },
        "required": ["sections"],
    },
}

# 强制交卷轮钉在末条 user 消息后的提示（2026-10-19，P2 固化）：
# 关思考的强制轮里模型仍会觉得「证据没读完」去调读工具而不交卷
# （验收实测 ~2/8）；这句把它拉回任务。措辞与
# tests/exp_submit_prod.py 的 EXP_P 变体逐字一致——
# 那是 t7 8/8、t5r 8/8 的实测版本，别顺手改写。
SUBMIT_NUDGE = ("证据读取额度已用完。不要再调用任何读取工具；"
                "请基于你已经读到的内容，立即调用 submit_section 交卷。")

# write_audit 的强制交卷 schema（2026-10-19，坑 77：迁 M3.1-Flash）。
# 字段与 prompts.WRITE_AUDIT 要求的输出一一对应；形状与
# tests/exp_audit_nothink.py 的 SUBMIT_AUDIT 逐字一致——
# 那是真实验收跑通的版本，字段名改了模型就要重新学。
AUDIT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "rework_check": {"type": "array", "items": {
            "type": "object", "properties": {
                "task": {"type": "string"}, "section": {"type": "string"},
                "status": {"type": "string",
                           "enum": ["fixed", "still_there",
                                    "changed_form"]}}}},
        "need_rework": {"type": "boolean"},
        "reason": {"type": "string"},
        "spec_issue": {"type": ["string", "null"]},
        "findings": {"type": "array", "items": {
            "type": "object", "properties": {
                "task": {"type": "string"}, "section": {"type": "string"},
                "severity": {"type": "string",
                             "enum": ["high", "mid", "low"]},
                "problem": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["task", "section", "severity", "problem", "fix"]}},
        "blocked": {"type": "array", "items": {
            "type": "object", "properties": {
                "task": {"type": "string"}, "section": {"type": "string"},
                "kind": {"type": "string",
                         "enum": ["no_material", "bad_assignment",
                                  "spec_conflict"]},
                "issue": {"type": "string"}}}},
    },
    "required": ["need_rework", "reason", "findings", "blocked"],
}


# ================================================================ 规划

def node_plan_spec(state: WriteState) -> dict:
    """主 agent 第一步：只出写作规约，不出任务列表。

    为什么要拆：合成一次调用（规约 + 任务）在 effort=high 下**必然断连**——
    实测输入 2.4 万字符、输出 16,536 字符 / 7,633 out_tokens，
    连续三次 Server disconnected，把 max_output_tokens 翻倍到 32768 也救不回来。
    拆开后两次都能完成：规约 4,234 out、任务列表 7,135 out，各自都在安全区。
    """
    data = None
    err: Exception | None = None
    # 一次性重试（2026-10-18）：这步的输出是**手写 JSON**（complete_json
    # 不是强制工具调用），而它的输出是全流程最大的 JSON——v13 全量实测
    # 在 char 6962 处坏掉，一次失败整条写作线退化成「只有来源清单」。
    # 坏 JSON 是非确定性抖动，重试一次大概率能过；两次都不成才降级。
    for _ in range(2):
        try:
            data = LLM.complete_json("write_plan_spec",
                                     _clip(_dumps(_brief(state)), 40000),
                                     system=prompts.WRITE_PLAN_SPEC)
            err = None
            break
        except Exception as e:
            err = e
            print(f"⚠️  写作规约 JSON 解析失败，重试一次: {str(e)[:120]}",
                  flush=True)
    if data is None:
        return {"plan_failed": str(err)[:200],
                "events": [{"kind": "write_plan_error", "stage": "spec",
                            "error": str(err)[:200]}]}
    spec = {k: v for k, v in data.items()
            if k in ("thesis", "glossary", "units", "cautions", "sections",
                     "spec_uncertainty")}
    return {"spec": spec,
            "events": [{"kind": "write_plan_spec",
                        "thesis": (spec.get("thesis") or "")[:120],
                        "glossary": len(spec.get("glossary") or []),
                        "cautions": len(spec.get("cautions") or []),
                        "spec_uncertainty": (spec.get("spec_uncertainty") or "")[:200]}]}


def node_plan_tasks(state: WriteState) -> dict:
    """主 agent 第二步：分派任务。

    **工具循环在 2026-10-15 加回来**（此前被删掉，但删得只做了一半）：

      · 代码不传工具了，`node_plan_tasks` 只调一次 `complete_json`
      · ⚠️ **但 `prompts.WRITE_PLAN_TASKS` 一直写着「看不准的时候可以调用
        工具取原文——取完再继续判断」**——在教模型去做一件它做不到的事
      · ⚠️ `MAX_TOOL_INDICES` 曾定义了却零使用点，和 `keep_last` 是同一类残留，
        已删；`MAX_TOOL_ROUNDS` 现在被下面 `run_tool_loop` 的调用使用

    为什么加回来而不是把提示词那两句删掉：分派是**全局决策**——决定哪段证据
    归哪一节、哪两节不能重复。只看 82 段概括做这个决定，粒度本来就不够；
    概括说"讲了成本问题"的段，究竟能不能支撑 sec3，得看原文才知道。

    ⚠️ **仍然只给 82 段概括做初始输入**——工具是"按需往下挖"，
    不是把 119k token 正文一次塞进来（那正是 `dispatch` 里子 agent 的做法）。

    ## 交卷走工具，不走手写 JSON（2026-10-17）

    最后一轮改成 `tool_choice` 强制调 `submit_tasks`，产物由
    `run_tool_loop` 序列化成 JSON 文本回来，**下游解析路径一个字没改**。
    实测这条链路上 `plan_tool_reads: 0`（99 段证据一次没取），
    所以实际发生的几乎总是「第一轮回文本 → 提交轮强制交卷」，
    代价是多一次调用、收益是那份 7,000 token 的 JSON 不再由模型手写。
    """
    spec = state.get("spec") or {}
    outline = state.get("outline") or []
    known = {g.get("ref") for g in (state.get("on_topic") or [])}
    known.discard(None)
    prompt = _dumps({"研究问题": state.get("question", ""),
                     "写作规约（你上一轮定的，必须遵守）": spec,
                     "框架与轮廓": _brief(state)})
    sid = state.get("session_id") or ""

    text = ""
    log: list[list[dict]] = []
    if sid:
        from ...db.repo import get_repo
        from ...tools.evidence import EvidenceTools
        tools = EvidenceTools(get_repo(), sid)
        try:
            text, _, log = run_tool_loop(
                LLM, "write_plan_tasks", prompts.WRITE_PLAN_TASKS,
                [{"role": "user", "content": _clip(prompt, 60000)}],
                tools, max_rounds=MAX_TOOL_ROUNDS, final_tool=SUBMIT_TASKS)
            _record_tool_use(state, "plan_tasks", log)
        except Exception as e:
            # 工具循环挂了退回单次调用——**不能因此整步失败**：
            # 分派本来就只是"照着概括分"，有工具是加分项不是必需项。
            print(f"⚠️  分派环节工具循环失败，退回单次调用: {e}", flush=True)
            text = ""

    data = None
    err: Exception | None = None
    if text:
        try:
            data = _parse_json(text)
        except Exception as e:
            # ⚠️ 同一段文本重解析是同一份确定性失败，重试没意义——
            # 退回单次调用（下面的重试循环兜它）
            print(f"⚠️  分派工具循环产出解析失败，退回单次调用: "
                  f"{str(e)[:120]}", flush=True)
    if data is None:
        # 一次性重试（2026-10-18）：手写 JSON 路径的抖动型失败，
        # 同 plan_spec——v13 实测 spec 步在 char 6962 坏掉一次，
        # 没有重试的话整条写作线直接退化成「只有来源清单」。
        for _ in range(2):
            try:
                data = LLM.complete_json("write_plan_tasks",
                                         _clip(prompt, 60000),
                                         system=prompts.WRITE_PLAN_TASKS)
                err = None
                break
            except Exception as e:
                err = e
                print(f"⚠️  任务分派调用失败，重试一次: {str(e)[:120]}",
                      flush=True)
    if data is None:
        return {"plan_failed": str(err)[:200],
                "events": [{"kind": "write_plan_error", "stage": "tasks",
                            "error": str(err)[:200]}]}

    raw_tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
    tasks = _normalize_tasks(raw_tasks, outline, known)
    _trace(state.get("session_id"), spec=spec, tasks=tasks,
           stats={"raw_tasks": len(raw_tasks), "kept": len(tasks),
                  "capped": len(tasks) < len(raw_tasks),
                  "segments_available": len(known),
                  "plan_tool_reads": sum(len(r) for r in log)})
    return {
        "spec": spec, "tasks": tasks,
        "spec_capped": len(tasks) < len(raw_tasks),
        "events": [{"kind": "write_plan_tasks", "tasks": len(tasks),
                    "raw_tasks": len(raw_tasks),
                    "segments_available": len(known),
                    "tool_reads": sum(len(r) for r in log)}],
    }


def _brief(state: WriteState) -> dict:
    """主 agent 看到的输入：段落清单（只有概括，没有正文）。

    正文按需用工具取。这是「信息量不够时 agent 主动往下挖」的前提——
    清单必须小到能一眼扫完，否则它根本不会去挖。
    """
    segs = state.get("on_topic") or state.get("segments") or []
    return {
        "研究问题": state.get("question", ""),
        "已批准的框架": [{"id": s.get("id"), "title": s.get("title"),
                        "question": s.get("question"), "kind": s.get("kind")}
                       for s in (state.get("outline") or [])],
        "探针轮廓": state.get("signals") or {},
        "来源清单": state.get("src_lines") or [],
        "证据段落": [
            {"段号": g.get("ref"), "标题": g.get("heading_path") or "",
             "这段在讲": g.get("summary"), "token": g.get("token_count")}
            for g in segs if (g.get("summary") or "").strip()],
    }


def _as_str_list(v) -> list[str]:
    """模型偶尔把数组字段回成字符串（常是 JSON 编码的数组）；逐字迭代会拆成单字。"""
    if isinstance(v, str):
        s = v.strip()
        try:
            v = json.loads(s) if s[:1] == "[" else [s]
        except ValueError:
            v = [s]
    return [str(x)[:200] for x in (v or []) if str(x).strip()][:12]


def _normalize_tasks(raw: list, outline: list, known: set) -> list:
    """清洗主 agent 的任务列表。脏数据在这里挡住，不让它流到子 agent。

    `known` 是本次研究真实存在的段号集合——模型编出来的段号在这里被丢掉。
    """
    sec_ids = {s.get("id") for s in outline if s.get("id")}
    out: list[dict] = []
    for i, t in enumerate(raw):
        tid = t.get("id") or f"t{i+1}"
        covers = [c for c in (t.get("covers") or []) if c in sec_ids]
        if not covers:
            continue
        segs = [x for x in (t.get("segments") or [])
                if isinstance(x, str) and x in known]
        out.append({
            "id": str(tid),
            "covers": covers,
            "mandate": str(t.get("mandate") or "")[:600],
            "segments": sorted(set(segs)),
            "must_cover": _as_str_list(t.get("must_cover")),
            "not_yours": _as_str_list(t.get("not_yours")),
        })
        if len(out) >= MAX_TASKS:
            break
    return out


# ================================================================ 写作

def node_dispatch(state: WriteState) -> dict:
    """跑所有还没有草稿的任务。

    任务之间**没有依赖**。子 agent 互相看不到对方的正文（最小权限），
    装配顺序由大纲顺序与 `covers` 决定，与谁先跑完无关——
    所以「最后一个先写」没有任何问题，全部可以并发。

    曾经有个 `depends_on` 字段让主 agent 排出依赖链，结果 14 个任务分 7 轮
    串行跑，19 分钟里大部分在等；而它表达的依赖根本无从兑现——
    依赖方读不到被依赖方的产出。现已删除。

    **按批次串行预热**（C1-f2）。共享前缀约 8,000 token（tools + system +
    写作规约 spec，实测两两公共前缀 11,244~12,514 字符，spec 完整落在里面），
    缓存命中时每次省下这8,000，miss 时全价。

    2026-10-07 那轮**预热只覆盖了 1 个任务**（`todo[0]` 串行，其余全部丢进
    同一个 4 并发池），后面的批次在缓存写入生效前就发出去，于是打空：
    30 次调用里 19 次 miss，按每次白付约 7,820 算 ≈ 148,000 token，
    占 write_section 输入的一半。

    证据是同一秒内的序列——miss 的请求规模恰好等于后续 HIT 读到的
    cache_read（8,304 / 8,506 / 8,375 三组完全对上），说明是写入-读取的
    因果链，不是前缀不一致。

    改成**每批先串行跑一个预热、其余再并发**：`first_serial` 落在每一批，
    不只是第一批。
    """
    tasks = state.get("tasks") or []
    drafts = dict(state.get("drafts") or {})
    failed = list(state.get("failed") or [])
    todo = [t for t in tasks if t["id"] not in drafts
            and t["id"] not in {f.get("task") for f in failed}]
    if not todo:
        return {}

    ev = {"drafts": dict(drafts), "failed": list(failed)}
    batches = [todo[i:i + CONCURRENCY]
               for i in range(0, len(todo), CONCURRENCY)]

    def _one(t: dict) -> tuple[dict, str | None]:
        secs, err = _run_task(state, t, revise=None)
        # 每任务一行：写作环节最长的并行段终于有粒度合适的进度。
        # 这里就推（fire_progress），不等批收齐——否则 8 个任务还是
        # 按批到达，用户看到的仍是块而不是流。
        fire_progress("write", {
            "kind": "write_task", "task": t["id"],
            "covers": list(t.get("covers") or []),
            "ok": bool(secs),
            "chars": sum(len(str(v)) for v in secs.values()) if secs else 0,
            "reason": (err or "")[:80]})
        return secs, err

    first_cost = 0.0
    for bi, batch in enumerate(batches):
        t0 = _now()

        # 必须走 parallel_labelled：它内部用 submit_run_in_context 在**主线程**
        # 造好 wrapper 再提交，线程不继承 contextvar 这件事才不会漏。
        # 注意**不能**写成 pool.map(lambda t: run_in_context(...), batch)——
        # 那个 lambda 是在 worker 线程里执行的，复制到的 context 是空的，
        # 记账 session_id 会静默变 NULL（2026-10-07 实测漏记 238,901 token）。
        results = parallel_labelled(batch, _one, workers=CONCURRENCY,
                                    label="write", first_serial=True)
        if bi == 0:
            first_cost = round(_now() - t0, 1)
        for t, r in zip(batch, results):
            if not r:
                ev["failed"].append({"task": t["id"], "reason": "调用失败或异常"})
                continue
            secs, err = r
            if secs:
                ev["drafts"][t["id"]] = secs
            else:
                ev["failed"].append({"task": t["id"],
                                     "reason": err or "empty output"})

    # ---- 偶发失败重试一次（2026-10-19，坑 78）----
    # 「交卷但正文全空」这类随机失败，同一任务复测 6/6 成功——值一轮重试。
    # 429/额度/配置类重发没意义（`_task_retryable` 与 `_audit_retryable`
    # 同一套判据）。只重试本轮新败的：state 里遗留的 failed 已定性。
    new_failed = {f.get("task"): f for f in ev["failed"][len(failed):]}
    retry_tasks = [t for t in todo
                   if t["id"] in new_failed
                   and _task_retryable(new_failed[t["id"]].get("reason"))]
    retried = len(retry_tasks)
    recovered = 0
    if retry_tasks:
        def _one_retry(t: dict) -> tuple[dict, str | None]:
            secs, err = _run_task(state, t, revise=None)
            # 重试成功照常再发一条 ok=True；仍败**不**再发——
            # 首跑那条失败进度已在事件流里，重发是重复噪音。
            if secs:
                fire_progress("write", {
                    "kind": "write_task", "task": t["id"], "retry": True,
                    "covers": list(t.get("covers") or []),
                    "ok": True,
                    "chars": sum(len(str(v)) for v in secs.values())})
            return secs, err

        # 与首跑同一层：parallel_labelled 的 contextvar 记账坑见上面注释。
        retry_results = parallel_labelled(retry_tasks, _one_retry,
                                          workers=CONCURRENCY,
                                          label="write", first_serial=True)
        recovered_ids: set = set()
        for t, r in zip(retry_tasks, retry_results):
            if not r:
                continue                       # 保留首跑记下的失败原因
            secs, err = r
            if secs:
                ev["drafts"][t["id"]] = secs
                recovered_ids.add(t["id"])
            else:
                # 重试仍败：reason 换成最后一次的（更贴近真死因）
                new_failed[t["id"]]["reason"] = err or "empty output"
        if recovered_ids:
            recovered = len(recovered_ids)
            ev["failed"] = [f for f in ev["failed"]
                            if f.get("task") not in recovered_ids]

    # 程序护栏在审查之前（坑 79）：能机械修的引用/编号缺陷先修掉，
    # 审查和残留检查看到的都是修过的稿子；修不了的才进 violations。
    ev["drafts"], auto_fixes = _auto_fix_drafts(state, ev["drafts"])

    return {
        "drafts": ev["drafts"],
        "failed": ev["failed"],
        "violations": _check_all_citations(state, ev["drafts"]),
        "events": [{"kind": "write_dispatch", "ran": len(todo),
                    "first_serial_s": first_cost,
                    "retried": retried, "recovered": recovered,
                    "drafts_total": len(ev["drafts"]),
                    "failed_total": len(ev["failed"]),
                    "auto_fixes": len(auto_fixes),
                    "auto_fix_sample": auto_fixes[:8]}],
    }


def _task_retryable(reason: str | None) -> bool:
    """失败原因值不值得再跑一次（dispatch 内重试的闸门）。

    与 `_audit_retryable` 同一套判据，只是喂进来的是原因字符串：
    · HTTP 状态错（429 限流/额度、5xx）——重发一个死一个；
    · 配置类 / 确定性失败（没分到证据）——重跑一万次也一样；
    · 交卷异常（空交卷、污染、截断、调用方异常）——随机事件，值一次。
    """
    s = reason or ""
    if re.search(r"MiniMax [45]\d\d", s) or "quota" in s.lower():
        return False
    if "ConfigError" in s or "没有分到" in s:
        return False
    return True


def _pending(state: WriteState) -> list[dict]:
    """还没产出、也没被标记失败的任务。"""
    done = set(state.get("drafts") or {})
    dead = {f.get("task") for f in (state.get("failed") or [])}
    return [t for t in (state.get("tasks") or [])
            if t["id"] not in done and t["id"] not in dead]


def route_after_dispatch(state: WriteState) -> str:
    """还有没写的任务就再跑一轮。"""
    return "dispatch" if _pending(state) else "assemble"


def _split_sections(text: str, covers: list[str]) -> tuple[dict[str, str], str | None]:
    """把子 agent 的产出按小节切开。

    单节任务：整份就是那一节，不要求任何标记。
    多节任务：必须逐节标 `<!-- section:sec7 -->`。切不出来就如实报错，
    **绝不把整份正文复制到每一节**。

    这正是 R5 的 bug：主 agent 被明确授权「两节耦合时一个任务写两节」，
    但装配层只认字符串，于是把同一份正文贴进 sec7 和 sec8，
    sec7 成了 sec8 的逐字副本，返修怎么改都改不掉。
    """
    covers = [c for c in (covers or []) if c]
    if not covers:
        return {}, "任务没有对应的小节"
    if len(covers) == 1:
        body = (text or "").strip()
        return ({covers[0]: body}, None) if body else ({}, "empty output")

    parts = _cut_marked(text or "", covers)
    if not parts:
        return {}, (f"多节任务（{'、'.join(covers)}）没有输出分节标记 "
                    f"`<!-- section:xx -->`，无法确定哪段属于哪一节")
    out = {k: v.strip() for k, v in parts.items() if v.strip()}
    if not out:
        return {}, (f"多节任务（{'、'.join(covers)}）标了分节标记但正文全空")
    note = None
    missing = [c for c in covers if c not in out]
    if missing:
        note = (f"多节任务只产出了 {'、'.join(out)}，"
                f"缺 {'、'.join(missing)}（无分节标记或该节为空）")
    return out, note


def _cut_marked(text: str, covers: list[str]) -> dict[str, str]:
    """按 `<!-- section:xx -->` 切分。不在 covers 里的小节 id 丢弃。"""
    ms = list(SECTION_MARK_RE.finditer(text))
    if not ms:
        return {}
    out: dict[str, str] = {}
    for i, m in enumerate(ms):
        sid = m.group(1)
        if sid not in covers:
            continue
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        out[sid] = text[m.end():end].strip()
    return out


def _parse_submission(text: str, covers: list[str]) -> tuple[dict, str | None, str]:
    """`submit_section` 交卷 JSON → ({section_id: 正文}, 出错说明, 模型 notes)。

    sections 为空 dict = 交卷无效，调用方按失败处理（走 failed→降级）。
    **body 不做任何文本清洗**——结构化通道可信，清洗只会制造第二份
    「模型原文 vs 落库原文」的对不上。

    归位规则：
    - `id` 在 covers 里才收，多余 id 丢弃；
    - covers 只有一节、模型也只交了一项但 id 写错 → 映射到 covers[0]。
      只有「一项对一项」时这个映射才是无歧义的（实测模型会把
      任务 id 或小节标题写进 id）。
    """
    import json
    covers = [c for c in (covers or []) if c]
    try:
        data = json.loads(text or "")
    except Exception:
        # 不抛：交卷产物不该是坏 JSON，但上游（截断等）确实可能给出。
        # 返回失败让上层走降级，比在这里炸掉多留一次抢救机会。
        return {}, "交卷内容不是合法 JSON", ""
    secs_in = data.get("sections") or []
    notes = str(data.get("notes") or "")
    out: dict[str, str] = {}
    for s in secs_in:
        if not isinstance(s, dict):
            continue
        sid, body = s.get("id"), (s.get("body") or "")
        if sid in covers and body.strip():
            out[sid] = body
    if not out and len(covers) == 1 and len(secs_in) == 1:
        only = secs_in[0]
        if isinstance(only, dict) and str(only.get("body") or "").strip():
            out[covers[0]] = str(only["body"])
    if not out:
        return {}, "交卷但正文全空", notes
    missing = [c for c in covers if c not in out]
    note = (f"多节任务只产出了 {'、'.join(out)}，"
            f"缺 {'、'.join(missing)}（交卷里没有对应 id 的项）") if missing else None
    return out, note, notes


# ---- 自由文本通道的污染检测 ----
#
# 这批标记是从 tests/exp_submit_section.py 的 contam 搬来的——
# 每一条都是 v13 真实草稿上出现过的形态，别随手加新正则：
# 宽了会误杀正常正文（比如正文里提到「下面」开头的引用），
# 窄了漏掉的会原样进报告。
#
# 刻意**不含** `too_short`：实验版有它是为了给自由文本兜底，
# 但「写得短」不等于脏（返修小改本来就短），长度判断归 _merge_sections
# 的 40% 条款管，那里能跟旧稿比，比绝对阈值准。
_PSEUDO_RE = re.compile(r"minimax\]\[|\]<\]minimax|<tool_call|<invoke|read_segments")
# 前言：开头先说「下面/我来/Here」再进正文。`---` 和围栏先行跳过——
# 模型喜欢在正文前甩一个分隔线。
_PREAMBLE_RE = re.compile(
    r"^(下面|以下|现在|好的|我(先|来|已|看|将|重写|补|掌握|核对|把)|"
    r"Now|Let me|I'll|I've|I have|Here|OK|Okay|Sure|First)", re.I)
# 自检/核对类元信息：混在正文末尾的「检查清单」式英文或中文碎片。
_META_RE = re.compile(
    r"(Citation check|Constraints satisfied|Let me verify|self-check|"
    r"I've drafted|自检|核对清单|Other notable points)", re.I)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 任务头：模型自作主张写的节标题，如 `# sec3` / `## 第二节`。
# 装配时标题由系统统一加，模型写的只会让层级错位（坑 64 同源）。
_TASK_HEADER_RE = re.compile(r"^#{1,2}\s*(sec\d+|第[一二三四五六七八九十]+节)", re.I)

# 命中这些 = 新稿本身就是垃圾，没有讨论余地。
# 其余标记（前言/围栏/任务头）只出现在自由文本通道，_clean_fallback
# 整体判失败；merge 保护只用 STRONG，因为「带个前言」还轮不到拒收整节。
STRONG = {"empty", "pseudo_tool", "english_block"}


def _contamination(body: str) -> list[str]:
    """正文污染检测。返回标记列表，空 = 干净。"""
    flags: list[str] = []
    lines = [x for x in (body or "").split("\n") if x.strip()]
    if not lines:
        return ["empty"]
    if _PSEUDO_RE.search(body):
        flags.append("pseudo_tool")
    first = next((x.strip() for x in lines
                  if x.strip() not in ("---", "```markdown", "```")), "")
    if lines[0].strip().startswith("```"):
        flags.append("fence")
    if _PREAMBLE_RE.match(first):
        flags.append("preamble")
    if _TASK_HEADER_RE.match(first):
        flags.append("task_header")
    if _META_RE.search(body):
        flags.append("trailing_meta")
    en = [x for x in lines if len(x) > 60 and not x.lstrip().startswith("|")
          and len(_CJK_RE.findall(x)) < len(x) * 0.05]
    if len(en) >= 3:
        flags.append("english_block")
    return flags


def _strip_sec_header(body: str) -> str:
    """去掉开头的模型自写 `# secN` / `## 第N节` 头行。

    只剥**第一个非空行**一处，不动正文中部的标题——剥多了就是
    在「猜哪段是正文」，那正是被污染的源头。
    """
    lines = (body or "").splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i < len(lines) and _TASK_HEADER_RE.match(lines[i].strip()):
        del lines[i]
    return "\n".join(lines)


def _clean_fallback(text: str, covers: list[str]) -> tuple[dict, str | None]:
    """没交卷时的自由文本通道：只做无歧义的机械清洗，碰到污染整体判失败。

    三步：剥整包围栏（`_unwrap_fence`）→ 去开头的 `# secN` 头 → 污染检测。
    **有任何标记就判失败**返回 `({}, "未交卷且文本被污染：…")`，
    走既有 failed→降级路径——不做「猜哪段是正文」的清洗，
    v13 的教训是猜出来的正文比缺一节更糟。
    """
    body = _strip_sec_header(_unwrap_fence(text or ""))
    flags = _contamination(body)
    if flags:
        return {}, f"未交卷且文本被污染：{'、'.join(flags)}"
    # fallback 通道本就不可信：真实验收抓到过一个 30 字符的「正文」
    # 全部标记都没命中、被当合法小节收进来。最短正文给 200——
    # 真实小节没有比这更短的合理产出。
    # ⚠️ 只加在这里，不进 `_contamination`：merge 保护也调它，
    # 那里的长度判断归 40% 条款（跟旧稿比，比绝对阈值准）。
    n = len(body.strip())
    if n < 200:
        return {}, f"未交卷且文本过短（{n} 字符）"
    return _split_sections(body, covers)


def _record_submit(state: WriteState, task_id: str, via: str, notes: str) -> None:
    """记账：正文从哪条通道拿到的 + 模型 notes，进 write_trace stats。

    best-effort，同 `_record_tool_use`：记不进去不该拖垮写作。
    `via=="fallback"` 时 `unstructured` 计数——它是「结构化通道没兜住」
    的直接量度，真实跑完要能一眼看到发生了几次。
    """
    if via in ("fallback", "forced_retry"):
        # 这两条路都意味着「正常通道出过事」，终端要看得见（带 task id，
        # 一场十几个任务时没 id 根本没法对号）。
        print(f"⚠️  [{task_id}] submit_section 走了 {via} 通道", flush=True)
    sid = state.get("session_id")
    if not sid:
        return
    try:
        from ...db.repo import get_repo
        repo = get_repo()
        tr = repo.write_trace(sid).get("stats") or {}
        v = tr.setdefault("submit_via", {})
        v[via or "unknown"] = v.get(via or "unknown", 0) + 1
        if notes:
            tr.setdefault("section_notes", {})[task_id] = str(notes)[:600]
        if via == "fallback":
            tr["unstructured"] = int(tr.get("unstructured") or 0) + 1
        repo.save_write_trace(sid, stats=tr)
    except Exception:
        pass


def _spec_for_task(spec: dict, covers: list[str]) -> dict:
    """裁掉与本任务无关的小节明细，只留自己那节的完整要求。

    `spec["sections"]` 是**全部**小节的 must_cover / not_yours，
    11,172 字符的规约里有 4,757 字符（43%）是它。而一个子 agent 只需要
    自己那一节（实测均值 475 字符）——其余 9 节对它是纯噪声，
    每节还带着 4 条 `not_yours`，读多了反而容易自我设限。

    **只裁 sections，thesis / glossary / units / cautions 全留**：
    它们是跨小节通用的写作约定，砍掉会真的影响质量。

    其他小节保留 `id` + `role`（角色定位一句话），
    让子 agent 仍知道全文有哪些部分、自己不写什么——
    这跟紧随其后的【其他任务的职责】是互补而非重复。

    ⚠️ 共享前缀因此变短（规约 11,172 → 约 6,900 字符）。
    缓存仍会命中，只是命中量少一点；省下的输入远大于缓存损失
    （每次省约 2,141 token，缓存前缀只缩约 2,140 token，
    而 C1-f2 修好之后这部分本来就该被缓存吃掉）。
    """
    sections = spec.get("sections") or []
    if not sections:
        return spec
    want = set(covers or [])
    kept, brief = [], []
    for s in sections:
        sid = str(s.get("id") or "")
        if not want or sid in want:
            kept.append(s)                      # 自己那节：全量要求
        else:
            brief.append({"id": sid, "role": str(s.get("role") or "")[:60]})
    out = dict(spec)
    out["sections"] = kept
    if brief:
        out["其他小节（只列定位，你不用写）"] = brief
    return out


def _run_task(state: WriteState, task: dict, revise: dict | None) -> tuple[dict, str | None]:
    """调一次子 agent 写一个任务。返回 ({section_id: 正文}, 出错原因或 None)。

    默认只给**段落概括**。它觉得不够时自己调工具往下挖——
    read_segments 看某段全文、read_context 连同邻段、read_source 看整篇。
    这就是「信息量不够时由 agent 主动触发」的落点。
    """
    segs = _task_segments(state, task)
    if not segs:
        return {}, "任务没有分到任何证据段落"
    tasks = state.get("tasks") or []
    sid = state.get("session_id") or ""
    covers = list(task.get("covers") or [])

    spec = _spec_for_task(state.get("spec") or {}, covers)

    others = [{"id": t["id"], "covers": t.get("covers"),
               "职责": (t.get("mandate") or "")[:80]}
              for t in tasks if t["id"] != task["id"]]
    head = (
        f"研究问题：{state.get('question','')}\n\n"
        f"【写作规约】\n{_dumps(spec)}\n\n"
        f"【其他任务的职责（不要替它们写）】\n{_dumps(others)}\n\n"
        f"────────  以上为规约，以下为你的任务  ────────\n\n"
        f"【你的任务】\nid={task['id']}  对应小节={covers}\n"
        f"要回答的问题：{task.get('mandate','')}\n"
        f"必须写到：{_dumps(task.get('must_cover') or [])}\n"
        f"不归你管：{_dumps(task.get('not_yours') or [])}\n\n"
    )
    if len(covers) > 1:
        head += (
            f"**你负责 {len(covers)} 个小节，交卷时 `submit_section` 的 "
            f"`sections` 里每节单独一项，`id` 填小节 id。**\n"
            f"小节 id：{'、'.join(covers)}。\n"
            f"不要把两节混写在一项里，也不要把同一段内容复制给两节。\n\n")
    listing = _dumps([
        {"段号": g.get("ref"), "这段在讲": g.get("summary"),
         "token": g.get("token_count")}
        for g in segs])
    tail = (f"【你的证据段落（只有概括，不足以判断就调工具看全文）】\n{listing}\n")

    # 返修信息并进同一条 user 消息，而不是再追加一条。
    # 两个 user 消息连续既不合协议，也会把「规约前缀」和「返修内容」隔开，
    # 缓存前缀就断了。
    first = head + tail
    if revise:
        first += (f"\n【审查意见】\n{_dumps(revise.get('fix') or revise.get('problem') or '')}"
                  f"\n\n【你上一版的正文】\n{revise.get('draft') or ''}")
    messages = [{"role": "user", "content": first}]

    stage = "write_revise" if revise else "write_section"
    if sid:
        from ...db.repo import get_repo
        from ...tools.evidence import EvidenceTools
        tools = EvidenceTools(get_repo(), sid)
        meta: dict = {}
        try:
            # `final_tool=SUBMIT_SECTION`：正文走工具参数而不是对话文本，
            # 否则最后一轮的 text 块会把前言/英文推理/伪工具调用一起当正文
            # （v13 实测四类污染，见 SUBMIT_SECTION 的注释与坑 75）。
            text, _, log = run_tool_loop(
                LLM, stage, prompts.WRITE_RULES, messages, tools,
                max_rounds=2, final_tool=SUBMIT_SECTION, meta=meta,
                final_nudge=SUBMIT_NUDGE)
            _record_tool_use(state, task["id"], log)
            via = meta.get("via") or "fallback"
            if via != "fallback":
                secs, note, notes = _parse_submission(text, covers)
                _record_submit(state, task["id"], via, notes)
                return secs, note
            # 没拿到交卷：text 是自由文本，按污染通道处理——
            # 只剥无歧义的机械层（围栏/#secN 头），有污染标记就整段拒收。
            secs, err = _clean_fallback(text, covers)
            _record_submit(state, task["id"], "fallback", err or "")
            return secs, err
        except Exception as e:
            print(f"⚠️  工具循环失败，退回单次调用: {e}", flush=True)
    try:
        # 单次调用没有交卷通道，text 同样是自由文本，走同一套清洗。
        secs, err = _clean_fallback(
            LLM.complete(stage, head + tail, system=prompts.WRITE_RULES),
            covers)
        if sid:
            _record_submit(state, task["id"], "fallback", err or "")
        return secs, err
    except Exception as e:
        print(f"⚠️  小节「{(covers or ['?'])[0]}」写入失败: {e}", flush=True)
        return {}, f"调用失败：{str(e)[:180]}"


def _task_segments(state: WriteState, task: dict) -> list[dict]:
    allowed = set(task.get("segments") or [])
    segs = state.get("on_topic") or state.get("segments") or []
    return [g for g in segs if g.get("ref") in allowed]


def _record_tool_use(state: WriteState, task_id: str, log: list) -> None:
    """子 agent 到底去看了什么——测试期要能查。"""
    sid = state.get("session_id")
    if not sid or not log:
        return
    try:
        from ...db.repo import get_repo
        repo = get_repo()
        tr = repo.write_trace(sid).get("stats") or {}
        tr.setdefault("tool_reads", {})
        for calls in log:
            for c in calls:
                tr["tool_reads"].setdefault(c.get("name"), 0)
                tr["tool_reads"][c.get("name")] += 1
        tr["last_task_tools"] = task_id
        repo.save_write_trace(sid, stats=tr)
    except Exception:
        pass


# ================================================================ 引用校验

def _check_citations(text: str, allowed: set[str]) -> list[dict]:
    """硬规则 1：引用必须是规范段号，且在分配给本任务的段里。

    纯机械检查，程序查 100% 准。允许两种写法：
        [S3·5]  —— 段级，推荐，能定位到具体哪一段
        [S3]    —— 来源级，只要这个来源有**任意一段**分给本任务就放行。
                   它指向的来源是对的，只是粗一点，不算错。
    其余一律算自创写法（实测出现过 `[S13 类同表述见 S19]`）。
    """
    bad: list[dict] = []
    for m in SEG_REF_LOOSE_RE.finditer(text or ""):
        raw = m.group(0)
        if not re.fullmatch(r"\[S\d+·\d+\]|\[S\d+\]", raw):
            bad.append({"kind": "自创写法", "found": raw[:40]})
    for m in SEG_REF_RE.finditer(text or ""):
        key = f"S{m.group(1)}·{m.group(2)}"
        if key not in allowed:
            bad.append({"kind": "越界引用", "found": key})
    for m in SRC_REF_RE.finditer(text or ""):
        # 来源级引用只在「这个来源的所有段都分给我」时才成立
        if f"S{m.group(1)}" not in allowed and not any(
                k.startswith(f"S{m.group(1)}·") for k in allowed):
            bad.append({"kind": "越界引用", "found": f"S{m.group(1)}"})
    seen, out = set(), []
    for b in bad:
        k = (b["kind"], b["found"])
        if k not in seen:
            seen.add(k); out.append(b)
    return out


# ---------------- 程序护栏：机械性引用/编号缺陷（坑 79）----------------
# 三类缺陷是**确定性可查**的，以前只靠审查模型「看看」+ 展示违规：
#   R1 多引用/区间括号 `[S3·1、S3·2]`/`[S29·1—S29·7]` → 规范成 `[S3·1][S3·2]`
#   R2 伪引用 `[S21 系报道口径,按规约……]` → 首 token 在分内就规范化 `[S21]`
#   R3a `secN` → 人读的节名（只替换不删句——首版删整句实测误伤实质内容）
#   R3b `tN`/`tNa` 任务编号 → 保守删整句（带 [S 引用/表格/标题/超长不删）
# 修不了的一概**不动**（全越界、跨来源区间、未知节号、带引用的编号句），
# 留给 `_check_*` 检出为残留 → `node_audit` 转成返修目标。

# ⚠️ 不能用 \b：Python 里中文字符也算 \w，「在 t6b 的任务」里的边界
# 在 \b 语义下不存在。lookaround 排除 ASCII 字母数字才算「独立成词」。
_INTERNAL_ID_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:t\d+[ab]?|sec\d+)(?![A-Za-z0-9])")
_SEC_ID_RE = re.compile(r"(?<![A-Za-z0-9])sec(\d+)(?![A-Za-z0-9])")
_TASK_ID_RE = re.compile(r"(?<![A-Za-z0-9])t\d+[ab]?(?![A-Za-z0-9])")
# 区间：S29·1—S29·7 / S29·1—7；连接符 —–-~至到；第二端点若带来源须同源
_RANGE_TOKEN_RE = re.compile(
    r"S(\d+)·(\d+)\s*(?:—|–|-|~|至|到)\s*(?:S(\d+)·)?(\d+)")
_REF_SEPS = "，、,;； \t"
_SENT_END = "。！？；"                 # R3b 的句末标点（换行边界另行处理）
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)


def _mask_comments(text: str) -> str:
    """把 <!--...--> 整段换成等长空白（保住换行），防装配标记里的 t6 之类
    被当正文命中；等长替换保持坐标与原串一一对应。"""
    return _HTML_COMMENT_RE.sub(
        lambda m: "".join("\n" if c == "\n" else " " for c in m.group(0)),
        text or "")


def _parse_ref_tokens(inner: str) -> list[str] | None:
    """括号内容（不含 []）完全由引用 token + 分隔符组成时返回展开后的
    token 列表；否则返回 None。区间在这里展开：跨来源/倒序/超 12 段
    都算「不纯」，整个括号不处理。"""
    toks: list[str] = []
    i, n = 0, len(inner)
    while i < n:
        m = _RANGE_TOKEN_RE.match(inner, i)
        if m:
            if m.group(3) and m.group(3) != m.group(1):
                return None                         # S29·1—S30·2 跨来源
            a, b = int(m.group(2)), int(m.group(4))
            if not (0 < b - a <= 12):
                return None                         # 倒序或超长区间不展开
            toks.extend(f"S{m.group(1)}·{k}" for k in range(a, b + 1))
            i = m.end()
            continue
        m = re.match(r"S\d+·\d+", inner[i:])
        if m:
            toks.append(m.group(0)); i += m.end(); continue
        m = re.match(r"S\d+", inner[i:])
        if m:
            toks.append(m.group(0)); i += m.end(); continue
        if inner[i] in _REF_SEPS:
            i += 1
            continue
        return None
    return toks


def _ref_allowed(tok: str, allowed: set[str]) -> bool:
    """段级看精确命中；来源级 `S21` 只要有任意 `S21·*` 分给它就留——
    与 `_check_citations` 对来源级引用的放行口径一致。"""
    if "·" in tok:
        return tok in allowed
    return tok in allowed or any(
        k.startswith(tok + "·") for k in allowed)


def _auto_fix_r1(raw: str, allowed: set[str]) -> str | None:
    """R1：纯 token/分隔符括号 → 规范化拆分；不可规范化返回 None。"""
    toks = _parse_ref_tokens(raw[1:-1])
    if toks is None:
        return None
    kept: list[str] = []
    for t in toks:
        if _ref_allowed(t, allowed) and t not in kept:
            kept.append(t)
    if not kept:
        return None       # 展开/拆分后无一在分内：不删，留给残留检查
    return "".join(f"[{t}]" for t in kept)


def _has_nonref_text(inner: str) -> bool:
    """剥掉 token、区间连接符、分隔符之后还剩的算「非引用文字」。"""
    s = _RANGE_TOKEN_RE.sub("", inner)
    s = re.sub(r"S\d+·\d+|S\d+", "", s)
    s = re.sub(r"[，、,;；\s—–\-~至到]", "", s)
    return bool(s)


def _auto_fix_r2(raw: str, allowed: set[str]) -> str | None:
    """R2：`[S21 系报道口径,……]` → 首 token 在分内则替换为规范 `[S21]`。"""
    inner = raw[1:-1]
    if re.fullmatch(r"S\d+·\d+|S\d+", inner):
        return None                                     # 已是规范形
    m = re.match(r"S\d+(?:·\d+)?", inner)
    if not m or not _has_nonref_text(inner):
        return None             # 跨来源区间这类「纯 token 杂质」不碰
    tok = m.group(0)
    if not _ref_allowed(tok, allowed):
        return None             # 首 token 越界：不删，留给残留检查
    return f"[{tok}]"


def _auto_fix_r3a(text: str, sec_titles: dict[str, str],
                  cur_sec: str | None) -> tuple[str, list[dict]]:
    """R3a：`secN` 翻成人能读的节名。**只替换，绝不删句**——真实稿里
    写手用 secN 指代别的节时句子本身是实质内容（坑 79 首版删整句
    实测误伤 ≥4/7，见 CONTEXT）。

    自指换「本节」（吞掉其后一个空格）；大纲里有标题的换「{title}」一节；
    查无此节的不动，留给残留检查进返修。表格行原地替换（不破坏结构），
    标题行（# 开头）不动——标题里漏编号多半是真缺陷，让残留报出来。
    """
    masked = _mask_comments(text)
    fixes: list[dict] = []
    out: list[str] = []
    pos = 0
    for m in _SEC_ID_RE.finditer(masked):
        sid = f"sec{m.group(1)}"
        line_start = text.rfind("\n", 0, m.start()) + 1
        if text[line_start:line_start + 3].lstrip().startswith("#"):
            continue                                        # 标题行不碰
        if sid == cur_sec:
            repl = "本节"
        elif sec_titles.get(sid):
            repl = f"「{sec_titles[sid]}」一节"
        else:
            continue                                        # 未知 id 留残留
        a = max(0, m.start() - 40)
        end = m.end()
        if sid == cur_sec and end < len(text) and text[end] == " ":
            end += 1                    # 「sec6 的核心问题」→「本节的核心问题」
        b = min(len(text), end + 40)
        fixes.append({"rule": "R3a", "before": text[a:b],
                      "after": text[a:m.start()] + repl + text[end:b]})
        out.append(text[pos:m.start()])
        out.append(repl)
        pos = end
    if not fixes:
        return text, fixes
    out.append(text[pos:])
    return "".join(out), fixes


def _auto_fix_r3b(text: str) -> tuple[str, list[dict]]:
    """R3b：`tN`/`tNa` 任务编号 → **保守**删整句。

    任务编号对读者没有任何意义，句子多半是「某某在 t6b 范围内」这类
    元说明——但带 `[S` 引用的句子可能是实质内容，宁可删不干净交给
    返修，也不整句误删。不删的（带引用/表格行/标题行/>120 字）由
    残留检查报出来。"""
    masked = _mask_comments(text)
    spans: list[tuple[int, int]] = []
    fixes: list[dict] = []
    for m in _TASK_ID_RE.finditer(masked):
        if re.match(r"\.\d", masked[m.end():m.end() + 2]):
            continue                    # t1.5 这类版本号不是任务编号
        if any(s <= m.start() < e for s, e in spans):
            continue                    # 同句里的第二个编号只记一次
        start = m.start()
        while start > 0 and masked[start - 1] not in _SENT_END + "\n":
            start -= 1
        end = m.end()
        while end < len(masked) and masked[end] not in _SENT_END + "\n":
            end += 1
        if end < len(masked) and masked[end] in _SENT_END:
            end += 1                    # 连同句末标点；行尾换行则保留换行
        line_start = text.rfind("\n", 0, m.start()) + 1
        head = text[line_start:line_start + 8].lstrip()
        if (head.startswith(("|", "#")) or end - start > 120
                or re.search(r"\[S\d", text[start:end])):
            continue
        spans.append((start, end))
        fixes.append({"rule": "R3b",
                      "before": text[start:end], "after": ""})
    for s, e in sorted(spans, reverse=True):
        text = text[:s] + text[e:]
    text = re.sub(r"\n{3,}", "\n\n", text)   # 删句后空行堆叠压成最多一个
    return text, fixes


def _auto_fix_text(text: str, allowed: set[str],
                   sec_titles: dict[str, str] | None = None,
                   cur_sec: str | None = None) -> tuple[str, list[dict]]:
    """R1→R2→R3a→R3b 顺序机械修稿。纯函数、幂等；返回 (新文本, fixes)。

    `sec_titles`：大纲的 section id→标题（R3a 换名用，缺省只换自指）。
    `cur_sec`：当前正文所属小节 id（R3a 判自指用）。
    """
    fixes: list[dict] = []
    if not text:
        return text, fixes

    def _r1(m):
        new = _auto_fix_r1(m.group(0), allowed)
        if new is not None and new != m.group(0):
            fixes.append({"rule": "R1", "before": m.group(0)[:80],
                          "after": new[:80]})
            return new
        return m.group(0)

    text = SEG_REF_LOOSE_RE.sub(_r1, text)

    def _r2(m):
        new = _auto_fix_r2(m.group(0), allowed)
        if new is not None and new != m.group(0):
            fixes.append({"rule": "R2", "before": m.group(0)[:80],
                          "after": new[:80]})
            return new
        return m.group(0)

    text = SEG_REF_LOOSE_RE.sub(_r2, text)
    text, r3a = _auto_fix_r3a(text, sec_titles or {}, cur_sec)
    fixes.extend(r3a)
    text, r3b = _auto_fix_r3b(text)
    fixes.extend(r3b)
    return text, fixes


def _auto_fix_drafts(state: WriteState,
                     drafts: dict) -> tuple[dict, list[dict]]:
    """对每份草稿跑 `_auto_fix_text`。allowed = 该任务分到的段号；
    sec_titles 来自大纲（R3a 换名）；无变化的节不重写，返回新 drafts
    （不原地改）。"""
    out = dict(drafts or {})
    fixes: list[dict] = []
    tasks = {t["id"]: t for t in state.get("tasks") or []}
    sec_titles = {o["id"]: o["title"] for o in state.get("outline") or []
                  if isinstance(o, dict) and o.get("id") and o.get("title")}
    for tid, d in (drafts or {}).items():
        t = tasks.get(tid)
        allowed = set((t or {}).get("segments") or [])
        changed, secs = False, {}
        for sec, body in _section_map(d).items():
            if isinstance(body, str):
                new_body, fx = _auto_fix_text(
                    body, allowed, sec_titles, sec or None)
                for f in fx:
                    f["task"], f["section"] = tid, sec or None
                fixes.extend(fx)
                if new_body != body:
                    changed = True
                secs[sec] = new_body
            else:
                secs[sec] = body
        if changed:
            out[tid] = secs
    return out, fixes


def _check_internal_ids(text: str) -> list[dict]:
    """R3 没删的内部编号残留（表格行/标题行/长句）——报成违规交给返修。"""
    masked = _mask_comments(text or "")
    bad, seen = [], set()
    for m in _INTERNAL_ID_RE.finditer(masked):
        if re.match(r"\.\d", masked[m.end():m.end() + 2]):
            continue                    # 与 R3 同款豁免：t1.5 不是任务编号
        a = max(0, m.start() - 15)
        found = (text or "")[a:m.end() + 15]
        if found not in seen:
            seen.add(found)
            bad.append({"kind": "内部编号", "found": found})
    return bad


def _check_all_citations(state: WriteState, drafts: dict) -> dict:
    tasks = {t["id"]: t for t in state.get("tasks") or []}
    out: dict[str, list[dict]] = {}
    for tid, sections in (drafts or {}).items():
        t = tasks.get(tid)
        if not t:
            continue
        allowed = set(t.get("segments") or [])
        # 配额是按任务分的证据，不是按小节，所以逐节拼起来一起查
        bad = _check_citations(_join(sections), allowed)
        bad += _check_internal_ids(_join(sections))
        if bad:
            out[tid] = bad
    return out


def _join(sections: Any) -> str:
    """{section_id: 正文} → 一段文本。容忍旧格式的字符串。"""
    if isinstance(sections, str):
        return sections
    if not isinstance(sections, dict):
        return ""
    return "\n\n".join(v for v in sections.values() if v)


# ================================================================ 装配

def node_assemble(state: WriteState) -> dict:
    """按大纲顺序把任务草稿拼回小节，并给审查打上定位标记。

    一个任务可以覆盖多节，此时**每节取自己那一段**（`drafts[tid][section_id]`）。
    R5 在这里把整份字符串贴进它 covers 的每一节，是返修永远修不好的直接原因。
    """
    drafts = state.get("drafts") or {}
    failed = state.get("failed") or []
    by_task = {t["id"]: t for t in state.get("tasks") or []}
    task_of_section: dict[str, list[str]] = {}
    for t in by_task.values():
        for sec in t.get("covers") or []:
            task_of_section.setdefault(sec, []).append(t["id"])

    blocks: list[str] = []
    bodies: dict[str, str] = {}
    written = 0
    demoted = 0
    _renumber_stats["stripped"] = 0
    _renumber_stats["leads"] = 0
    _renumber_stats["fences"] = 0
    for sec_no, sec in enumerate(state.get("outline") or [], 1):
        sid = sec.get("id")
        chunks = []
        # 一节可能由**多个任务**拼成，标题编号必须跨任务连续，
        # 所以计数器由本节持有、逐任务传进去（换节换新 list，见 `_renumber_headings`）
        sec_counters = [sec_no]
        for tid in task_of_section.get(sid, []):
            body = _section_body(drafts.get(tid), sid, sec_no, sec_counters)
            if body:
                demoted += _count_demoted(_raw_body(drafts.get(tid), sid))
                mark = f"<!-- task:{tid} section:{sid} -->"
                chunks.append(f"{mark}\n{body}")
        if chunks:
            joined = "\n\n".join(chunks)
            bodies[sid] = joined
            blocks.append(f"## {sec.get('title')}\n\n" + joined)
            written += 1
        else:
            why = "写作失败或为空" if task_of_section.get(sid) else "没有任务覆盖本节"
            blocks.append(f"## {sec.get('title')}\n\n"
                          f"<!-- 未能完成 -->\n**本节未能完成**：{why}。\n")

    warnings = _assemble_warnings(bodies, drafts, by_task)
    # 有产出、却一节都没被采用的任务
    used: set[str] = set()
    for t in by_task.values():
        for sec in t.get("covers") or []:
            # 这里只判断「有没有正文」，用 _raw_body 就够——
            # 走 _section_body 会再跑一遍编号逻辑，把计数重复累加。
            if _raw_body(drafts.get(t["id"]), sec):
                used.add(t["id"])
    missing = [t["id"] for t in by_task.values()
               if drafts.get(t["id"]) and t["id"] not in used]
    return {
        "assembled": "\n\n".join(blocks),
        "sections_written": written,
        "uncovered_tasks": missing,
        "assemble_warnings": warnings,
        "headings_demoted": demoted,
        "headings_renumbered": _renumber_stats["stripped"],
        "lead_headings_dropped": _renumber_stats["leads"],
        "events": [{"kind": "write_assemble", "sections": written,
                    "tasks": len(drafts), "failed": len(failed),
                    "warnings": len(warnings), "headings_demoted": demoted,
                    "headings_renumbered": _renumber_stats["stripped"],
                    "lead_headings_dropped": _renumber_stats["leads"],
                    "fences_unwrapped": _renumber_stats["fences"]}],
    }


def _count_demoted(raw: Any) -> int:
    """数原文里有多少个 `##`（正好两级）标题会被装配降级。

    只用于上报：改了正文就得让人看得见改了多少，
    否则「模型写错了格式、程序替它擦屁股」这件事会静悄悄地消失。
    注意只数 `##` 不数 `###`——模型自己写 `###` 是允许的，不算程序动过手。
    """
    if isinstance(raw, dict):
        raw = "\n".join(v for v in raw.values() if isinstance(v, str))
    if not isinstance(raw, str):
        return 0
    n = 0
    in_fence = False
    for line in raw.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and re.match(r"^##(?!#)\s+", line):
            n += 1
    return n


def _raw_body(draft: Any, sid: str) -> str:
    """未降级的原始正文。只给计数用。"""
    if isinstance(draft, dict):
        return (draft.get(sid) or "").strip()
    return (draft or "").strip()


def _unwrap_fence(body: str) -> str:
    """整节草稿被模型包在一层 ```markdown … ``` 围栏里时，剥掉那层壳。

    v13 实测形态（坑 64）：子 agent 把整份小节产出包在 ```markdown 里交回。
    不剥的话，下游把所有内容当代码块——标题降级、编号重排、secN 剥前缀
    全部失效（围栏内是「代码」，不该被处理），`# secN` 和围栏线
    原样漏进终稿。

    只剥「第一条非空行开围栏、最后一条非空行闭围栏」的整包壳；
    中段出现的围栏不动——节里真放代码块是允许的。
    剥壳要计数：程序替模型擦了几次屁股必须看得见。
    """
    if "```" not in body:
        return body
    lines = body.splitlines()
    i, j = 0, len(lines) - 1
    while i <= j and not lines[i].strip():
        i += 1
    while j >= i and not lines[j].strip():
        j -= 1
    if i >= j:
        return body
    if (re.match(r"^```\w*\s*$", lines[i].strip())
            and lines[j].strip() == "```"):
        _renumber_stats["fences"] += 1
        return "\n".join(lines[i + 1:j])
    return body


def _section_body(draft: Any, sid: str, sec_no: int = 1,
                  counters: list[int] | None = None) -> str:
    """取某个任务在某一节的正文（标题已降级并重编号）。旧格式（裸字符串）兜底兼容。

    `sec_no` 是本节在大纲里的序号，从 1 起。`node_assemble` 按大纲顺序遍历，
    序号现成；给不出它就没法编「节.节内」这种号，只能退回 1。

    `counters` 由调用方持有并在**同一节的任务之间复用** ——
    一节可能由多个任务拼成，编号要跨任务连续（见 `_renumber_headings`）。
    """
    return _renumber_headings(
        _demote_headings(_unwrap_fence(_raw_body(draft, sid))),
        sec_no, counters)


def _demote_headings(body: str) -> str:
    """把子 agent 自造的 `##` 降成 `###`。

    为什么用程序而不用提示词：`WRITE_SECTION` 里明写了「不要写 `##`」，
    实测两轮分别造出 13 个和 16 个 `##`，**一次都没管住**。
    而同类问题（自创引用写法、越界引用）恰恰是程序抓得又快又准的。

    为什么降级不损失什么：这些标题本来就在节标题之下，和节标题平级只会
    把目录层级弄乱。降成 `###` 之后它们还是原来的意思，只是回到了正确层级。
    """
    if "##" not in body:
        return body
    # 只降级**正好两级**的行首 `##`：
    #   · `###` 及更深是模型正当使用的层次，不该动
    #   · 句子里、URL 里、代码块里的 `##` 不该动
    out = []
    in_fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        m = re.match(r"^(##)(?!#)(\s+)", line)
        if m:
            line = "###" + m.group(2) + line[m.end():]
        out.append(line)
    return "\n".join(out)


def _renumber_headings(body: str, sec_no: int,
                       counters: list[int] | None = None) -> str:
    """剥掉子 agent 自造的编号，按「节.节内」重编；带 `lead` 标记的降成正文段落。

    **为什么用程序而不是提示词。** 两重原因，第二重是决定性的：
      1. 子 agent 看不到自己是大纲的第几节——`_spec_for_task` 把别人的节压成
         `{id, role}` 连标题都不留，它手上没有排编号需要的任何信息。
      2. 「不要自己编号」这种约束**需要信息才能执行**。同一条提示词实测
         遵守率 3/3（见 tests/lead_mark_probe.py），说明不是能力问题。

    v5 实测的四种自造编号全被剥掉：`一、` / `4.` / `1.1` / `sec3`。
    剥完再重编，所以模型不写、写成什么样都不影响最终编号。

    **`counters` 是跨任务共享的计数器（2026-10-17 补）**：
    一节可以有**多个任务**（实测 sec1 来了 t1/t2 两块）。调用方必须把同一个
    list 传进来，编号才会**跨任务连续**。
    以前每个任务各拿一个新计数器，于是每个任务都从 `节.1` 重新数 ——
    实测产物：sec1 先出 `1.1…1.8`，紧接着又是 `1.1…1.5`，**号重复了**；
    sec2 同样是 `2.1…2.10` 然后 `2.1…2.6`。
    ⚠️ 事件里 `headings_renumbered: 37` 显示重排确实跑了，
    所以从事件计数上看不出这个问题——**得看产物**。
    换节时调用方要自己换一个新 list（节序号在第 0 位）。

    **`lead` 标记**：引言标题和节标题讲同一件事，编了号会在目录里留下两条
    近似条目。判据只用标记、不碰语义——v5 试过相似度启发式，7 个里只删对 3 个，
    而「界面问题的物理本质」这类真子标题会被误删（它和节标题共享关键词）。
    要求程序判断「重复」还是「相关」，它就得理解语义，那是它做不到的。
    """
    if not body:
        return body
    if counters is None:
        counters = [sec_no]   # 第 0 位就是节序号
    elif counters[0] != sec_no:
        # 换节：**整份重置**。只把第 0 位换成新节序号的话，上一节的层级计数
        # 会被带过来（[1,2,1] 换到第 2 节就变成 [2,2,1]，编出 `2.3`）。
        counters[:] = [sec_no]
    else:
        counters[0] = sec_no   # 调用方复用同一个 list，节序号对齐
    in_fence = False
    out: list[str] = []
    stripped = 0
    leads = 0
    # 上一行是不是一个独占整行的 lead 标记。
    # 用它来覆盖「标记单独一行、标题在下一行」这种写法——
    # 标记行本身要丢掉，但它把紧随其后的那个标题变成了引言。
    pending_lead = False
    for line in body.splitlines():
        s = line.strip()
        # 代码块里的 `#` 不是标题，整块跳过。v5 恰好没有代码块，但不能假设。
        if s.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            pending_lead = False
            continue
        if in_fence:
            out.append(line)
            continue

        # ── 第一步：摘掉 lead 标记，判定这一行是不是引言标题 ──
        # 标记有三种落法，全都在这里收口：
        #   独占一行 / `<!-- lead --> xxx` / `### <!-- lead --> xxx`
        # 分三条分支处理曾经互相抢行（每补一条又漏一种），所以合并成「摘完再判标题」。
        # ── 第一步：摘掉 lead 标记，判定这一行是不是引言标题 ──
        # 标记有四种落法，全在这里收口：
        #   独占一行 / `<!-- lead --> xxx` / `<!-- lead --> ### xxx` / `### <!-- lead --> xxx`
        # 分支式处理会互相抢行（每补一条又漏一种），所以统一走「摘标记 → 判标题」。

        # (a) 标记独占整行：这一行丢掉，作用留给下一行
        if LEAD_MARK_RE.fullmatch(s):
            pending_lead = True
            continue
        # (b) 标记排在 `###` 之后（`### <!-- lead --> xxx`）：
        #     得先摘标题符号才匹配得到，所以这里就地处理、不再往下走
        hm = HEADING_RE.match(s)
        if hm and LEAD_MARK_RE.match(hm.group(2).strip()):
            if t := _lead_text(hm.group(2)):
                out.append(t)
            leads += 1
            continue
        # (c) 标记在行首，后面接纯文本或 `###`
        if LEAD_MARK_RE.match(s):
            s = LEAD_MARK_RE.sub("", s, count=1).strip()
            hm = HEADING_RE.match(s)
            if t := _lead_text(hm.group(2) if hm else s):
                out.append(t)
            leads += 1
            continue
        # (d) 标记在上一行独占整行
        if pending_lead:
            pending_lead = False
            hm = HEADING_RE.match(s)
            if t := _lead_text(hm.group(2) if hm else s):
                out.append(t)
            leads += 1
            continue

        m = HEADING_RE.match(s)
        if not m:
            out.append(line)
            continue
        # 只有标题符号没有文字（模型畸形输出）——丢掉，别留光秃秃的 `###`
        if not m.group(2).strip():
            continue

        # ── 第二步：真子标题，剥编号后按层级重编 ──
        marks, title = m.group(1), m.group(2)
        new_title = _NUM_PREFIX_RE.sub("", title, count=1)
        if new_title != title:
            stripped += 1
        title = new_title or title   # 全被剥空（只有编号）就还原，不留空标题
        # `##` 在正文里是节级，和节标题平级。正常路径上 `_demote_headings`
        # 已经把它降成 `###` 了；真漏到这里（降级失效）也不能参与编号——
        # 它的层级是 2，算出来的 idx=0 会直接覆盖节序号，编出 `## 2 xxx`。
        # 这里再兜一次底：统一按最深的合法子标题层级处理。
        marks = "###" if len(marks) < 3 else marks
        # `###` 是「节下面的第一个子标题」，计数器索引是 level-2 = 1，不是 level-1。
        # 用 level-1 会整体偏移一位，把未初始化的 0 编进号码里，产出 `1.0.1`。
        idx = len(marks) - 2
        if idx >= len(counters):
            counters += [0] * (idx - len(counters) + 1)
        counters[idx] += 1
        for k in range(idx + 1, len(counters)):
            counters[k] = 0
        out.append(f"{marks} {'.'.join(str(c) for c in counters[:idx + 1])} {title}")

    _renumber_stats["stripped"] += stripped
    _renumber_stats["leads"] += leads
    return "\n".join(out)


def _lead_text(raw: str) -> str:
    """引言标题 → 一行正文文字。

    去掉标记和自造编号（`<!-- lead --> 一、成本困局` → `成本困局`）。
    可能返回空串——模型只写标记没写内容的情形，调用方据此跳过，
    不往正文里插空行。
    """
    return _NUM_PREFIX_RE.sub("", LEAD_MARK_RE.sub("", raw, count=1).strip(), count=1)


# 程序动过正文的量，要让人看得见——否则「模型写错格式、程序替它擦屁股」
# 这件事会静悄悄地消失（同 `_count_demoted` 的理由）。
_renumber_stats: dict[str, int] = {"stripped": 0, "leads": 0, "fences": 0}


def _assemble_warnings(bodies: dict, drafts: dict, by_task: dict) -> list[str]:
    """装配后的机械自检。命中即上报，不修也不藏。

    R5 的事故长这样：子 agent 自己写了带 `##` 的两节，装配又把整份贴进
    sec7 和 sec8，于是 sec7 成了 sec8 的副本，而 sec8 内部还完整重复了两遍。
    所以要比**去掉定位标记之后**的正文，否则永远测不出来。
    """
    out: list[str] = []
    contents = {s: TASK_MARK_RE.sub("", b).strip() for s, b in bodies.items()}
    ids = [s for s in contents if contents[s]]

    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if contents[a] == contents[b]:
                out.append(f"小节 {a} 与 {b} 的正文逐字相同，装配很可能出错了")
    for sid, body in contents.items():
        m = TASK_MARK_RE.search(bodies.get(sid) or "")
        if m and m.group(2) != sid:
            out.append(f"小节 {sid} 的正文以 {m.group(1)}→{m.group(2)} 的定位标记开头，"
                       f"内容被放错了小节")
        # 同一节里整段逐字重复：子 agent 自己写了重复内容
        seen: set[str] = set()
        for line in body.splitlines():
            s = line.strip()
            if len(s) >= 80:
                if s in seen:
                    out.append(f"小节 {sid} 里有整段重复出现两次（{s[:40]}…），"
                               f"正文自身存在重复")
                    break
                seen.add(s)

    # 多节任务只产出了一部分小节（判据是「该任务负责几节」，不是「产出了几节」
    # ——只产出一节的多节任务同样要报）
    for tid, draft in (drafts or {}).items():
        if not isinstance(draft, dict):
            continue
        want = set(by_task.get(tid, {}).get("covers") or [])
        if len(want) <= 1:
            continue
        gap = want - set(draft)
        if gap:
            out.append(f"任务 {tid} 负责 {'、'.join(sorted(want))}，"
                       f"实际只产出了 {'、'.join(sorted(draft))}，缺 {'、'.join(sorted(gap))}")
    return out


# ================================================================ 审查

def _last_high_findings(session_id: str | None) -> list[dict]:
    """上一轮的 high findings 原文（给审查逐条核对用）。

    读 `audit_history_json`。⚠️ 里面的 `targets` 存的是**字符串**列表
    （`"t5→sec5"`），不是 dict——我第一版按 dict 写，直接
    `AttributeError: 'str' object has no attribute 'get'`，
    **把整场研究的 write_audit 节点打崩**（2026-10-09 实测）。

    原文（problem/fix）在 `audit.targets` 里才有，那是 dict 形状。
    两者形状不同，这里分开取。
    """
    if not session_id:
        return []
    try:
        from ...db.repo import get_repo
        tr = get_repo().write_trace(session_id)
    except Exception:
        return []
    hist = tr.get("audit_history") or []
    # `targets` 只是 "t5→sec5" 这样的标签，用它确认上一轮确实要求过返修
    asked = [t for rec in reversed(hist) if isinstance(rec, dict)
             for t in (rec.get("targets") or []) if isinstance(t, str)]
    if not asked:
        return []
    # problem/fix 的原文在当前 audit 里（它是最后一轮的结论）
    out = []
    for t in (tr.get("audit") or {}).get("targets") or []:
        if not isinstance(t, dict) or not t.get("task"):
            continue
        out.append({"task": t.get("task"), "section": t.get("section"),
                    "问题": t.get("problem"), "要求": t.get("fix")})
    return out


_AUDIT_KEYS = {"need_rework", "findings", "blocked", "reason"}


def _norm_audit_list(v) -> list:
    """M3.1 实测会把数组包成 `{"item": [...]}` 或回成字符串——统一收成 list。

    单 dict 的 `item` 值包成一项；字符串按 JSON 试解；其余非 list 归 []。
    """
    if isinstance(v, dict):
        v = v.get("item")            # {"item": X} 包装（真实样本出现过）
    if isinstance(v, dict):
        v = [v]                      # X 本身是单个 dict → 一项
    elif isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            v = []
    return v if isinstance(v, list) else []


def _normalize_audit_input(raw) -> dict | None:
    """把 `submit_audit` 的工具入参归一成下游读的 dict；归一不了返回 None。

    为什么需要它（2026-10-19，18 个真实样本里 2 个偏离 schema）：
    M3.1 会把整个结论套进多余的键（`audit` / `audit_result`）、把列表包成
    `{"item": [...]}`、把 `need_rework` 回成 `"true"` 字符串或
    `{"reason":…,"rework_check":…,"$text":"true"}` 的 dict。
    **把这类结果当「审查通过」会静默放过问题**——所以归一化不了就判无效，
    让上层重试一次，而不是拿半残的结构往下走。
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    # 解包：顶层不含任何期望键、且恰好只有一个值为 dict 的键 → 剥开。
    # 最多解两层（样本里出现过套娃）。
    d = raw
    for _ in range(2):
        if any(k in d for k in _AUDIT_KEYS):
            break
        dict_vals = [v for v in d.values() if isinstance(v, dict)]
        if len(dict_vals) == 1:
            d = dict_vals[0]
        else:
            break
    # need_rework：bool 原样；"true"/"false" 字符串转 bool；
    # dict 形状取 "$text"，并把里层的 reason/rework_check 提升到顶层
    # （顶层已有的不覆盖）。
    nr = d.get("need_rework")
    lifted: dict = {}
    if isinstance(nr, dict):
        for k in ("reason", "rework_check"):
            if k in nr and k not in d:
                lifted[k] = nr[k]
        nr = nr.get("$text")
    if isinstance(nr, str):
        nr = {"true": True, "false": False}.get(nr.strip().lower())
    if (not isinstance(nr, bool)
            or "findings" not in d or "blocked" not in d):
        # 「need_rework 缺失/给不出 bool」或 schema 键缺席 = 无效。
        # `blocked` 也要**键在**——真实样本里它漂成了 `blocker`，
        # 不设别名白名单（不猜模型会飘成什么键），缺键就判无效
        # 让上层重试：宁可多花一次调用，也不能让 blocked 整栏静默丢。
        # 注意 `need_rework=False, findings=[], blocked=[]` 是合法的
        # 「审查通过」，不能误杀——缺的只是字段才算无效。
        return None
    out = dict(d)
    out.update(lifted)
    out["need_rework"] = nr
    for k in ("findings", "blocked", "rework_check"):
        out[k] = _norm_audit_list(out.get(k))
    if not isinstance(out.get("reason"), str):
        out["reason"] = "" if out.get("reason") is None else str(out["reason"])
    return out


def _audit_retryable(e: Exception) -> bool:
    """哪些异常值得再打一次。

    EmptyOutput（空返回、工具参数被 `_reject_broken_args` 判截断）是
    随机失败，重试有意义；`MiniMax <status>` 是 HTTP 层——429 是额度
    问题、5xx 整次重发没意义；ConfigError 是配置错，重试一万次也一样。
    """
    if isinstance(e, ConfigError):
        return False
    return not str(e).startswith("MiniMax ")


def node_audit(state: WriteState) -> dict:
    """主 agent 通读全稿。返回是否返修、返修谁、以及返修有没有真的解决问题。

    `rework_effective` **不再采信模型自评**，改由 `rework_check` 逐条核对推出：
    模型要回答的是「上轮那几条现在还在吗」（稿子里能查），
    而不是「返修整体有没有用」（没有依据，只能猜）。
    """
    draft = state.get("assembled") or ""
    spec = state.get("spec") or {}
    violations = state.get("violations") or {}
    rounds = state.get("rework_rounds", 0)

    payload = {
        "研究问题": state.get("question", ""),
        "写作规约": spec,
        "草稿": draft,
    }
    # 任务分派摘要：只有段号和条数，不给正文。
    # 没有它，审阅人判断不了「这节做得不好」是写砸了还是**根本没材料可写**——
    # 而这两者的正确处置完全相反：前者该返修，后者重写一万遍也写不出来。
    tasks = state.get("tasks") or []
    if tasks:
        payload["任务分派（每个任务分到了几段证据）"] = [
            {"task": t.get("id"), "covers": t.get("covers"),
             "证据段数": len(t.get("segments") or []),
             "要求写到": len(t.get("must_cover") or [])}
            for t in tasks]
    if violations:
        payload["程序已查出的引用问题"] = violations
    if rounds:
        payload["这是第几轮返修后的草稿"] = rounds
        # ⚠️ 这一段是 rework_effective 从「不可靠」变成「可核对」的关键。
        #
        # 原来只告诉模型「这是第 N 轮」，却**不告诉它上一轮说了什么**——
        # 于是它只能对「有没有用」笼统自评。实测两个独立 session 都出现了
        # 同一个形状：reason 里写「已有明显改善」，字段却填 false，
        # 代码采信了 false，于是 high 问题一个没解决就收工。
        #
        # 把上一轮它自己写的 problem / fix 原文喂回去，
        # 它要回答的问题就从「整体有没有用」（无依据，只能猜）
        # 变成「这 4 个具体问题现在各自还在不在」（稿子里能查）。
        prev = _last_high_findings(state.get("session_id"))
        if prev:
            payload["上一轮你提出的问题（逐条核对，现在还在吗）"] = prev

    # 2026-10-19 起结论走强制工具 `submit_audit`（坑 77）：M3 开思考
    # 手写 JSON 太长必被截断；M3.1-Flash low 单次 7~12k 接得住，
    # 但它的工具入参有约 2/18 偏离 schema → 必须过
    # `_normalize_audit_input`，归一不了当失败重试。
    # 点名工具的话术与 tests/exp_audit_nothink.py 的实测版本逐字一致。
    prompt = (_clip(_dumps(payload), 120000)
              + "\n\n审完后**调用 `submit_audit` 工具**提交结论，"
                "结论只放在工具参数里，不要在对话文本里写 JSON。")
    d: dict | None = None
    err: object = ""
    attempts = 0
    for _attempt in (1, 2):
        attempts = _attempt
        try:
            raw = LLM.complete_json_forced(
                "write_audit", prompt, "submit_audit", AUDIT_SCHEMA,
                system=prompts.WRITE_AUDIT)
        except Exception as e:
            err = e
            if _attempt == 1 and _audit_retryable(e):
                print(f"⚠️  [write_audit] 调用失败"
                      f"（{type(e).__name__}: {str(e)[:80]}），重试一次",
                      flush=True)
                continue
            break
        d = _normalize_audit_input(raw)
        if d is None:
            err = "submit_audit 入参偏离 schema，无法归一化"
            if _attempt == 1:
                print("⚠️  [write_audit] 返回结构无法归一化，重试一次",
                      flush=True)
                continue
        break

    if d is None:
        # 审查失败不阻断：保留现状，把问题暴露出来而不是假装没问题
        msg = f"{err}（已尝试 {attempts} 次）"[:200]
        return {"audit": {"need_rework": False, "error": msg,
                          "findings": [], "targets": [], "blocked": [],
                          "rework_check": [], "checked": 0, "still_open": 0,
                          "rework_effective": True,
                          "remaining_issues": ["审查环节失败，未能核查"]},
                "rework_effective": True,
                "events": [{"kind": "write_audit_error", "error": msg}]}

    findings = [f for f in (d.get("findings") or []) if isinstance(f, dict)]
    by_task = {t["id"]: t for t in state.get("tasks") or []}
    targets = []
    for f in findings:
        tid = str(f.get("task") or "")
        if f.get("severity") == "high" and tid in by_task:
            targets.append({
                "task": tid,
                "problem": str(f.get("problem") or "")[:400],
                "fix": str(f.get("fix") or "")[:800],
                "section": f.get("section"),
            })
    # ---- 程序检出的残留违规 → 直接变返修目标（坑 79）----
    # 这类缺陷机械可查，不该消耗模型的判断力：违规列表是给模型看的
    # 背景（提示词已叫它别重复提 finding），这里程序自己转成目标。
    # `section=None` 时 node_revise 会退回任务原始 covers，merge 按新稿键并。
    violation_targets = 0
    high_tasks = {t["task"] for t in targets}
    for tid, bads in (violations or {}).items():
        if not bads or tid not in by_task:
            continue
        sample = ";".join(
            f"{b.get('kind')}:{b.get('found')}" for b in bads[:5])
        problem = f"程序检出 {len(bads)} 处引用/编号问题:{sample}"
        fix = ("逐处处理:越界引用——改成你被分配的段号中真正支撑该句的一条,"
               "找不到支撑就删去该句断言,不要保留无出处的数字;"
               "自创写法——改成规范的 [S3·5] 形式;"
               "正文里出现的 t1/sec3 之类内部编号——删掉整句元说明。"
               "只改这些处,不要动其他文字。")
        if tid in high_tasks:
            # 已被模型点名的任务：违规文本并进它的第一条 target，
            # 不另立 target（返修按任务跑，同任务的意见本就该一起问）、
            # 也不计 violation_targets——这条返修本来就是模型要的。
            tgt = next(t for t in targets if t["task"] == tid)
            tgt["problem"] = tgt["problem"] + "\n" + problem
            tgt["fix"] = tgt["fix"] + "\n" + fix
            continue
        targets.append({
            "task": tid, "problem": problem, "fix": fix, "section": None,
        })
        violation_targets += 1
    # ---- blocked：返修解决不了的，独立成一栏 ----
    # 原来这类内容混在 `remaining_issues` 里，和「本轮不该解决」混在一起，
    # 路由看不出来，于是模型被迫把它们判成 high 才不会漏掉。
    # 拆开后它们有独立地位：不影响 need_rework，但会原样进报告的存疑一节。
    blocked = []
    for b in (d.get("blocked") or []):
        if isinstance(b, dict):
            blocked.append({
                "task": str(b.get("task") or ""),
                "section": b.get("section"),
                "kind": str(b.get("kind") or "no_material"),
                "issue": str(b.get("issue") or "")[:400],
            })
        elif b:
            blocked.append({"task": "", "section": None,
                            "kind": "no_material", "issue": str(b)[:400]})

    # ---- rework_effective：程序从逐条核对推出，不再采信模型自评 ----
    # 判据只看 `rework_check`：上轮的问题还有几条 `still_there`/`changed_form`。
    # `changed_form`（表面改了实质没改）按「仍在」算。
    check = [c for c in (d.get("rework_check") or []) if isinstance(c, dict)]
    still = [c for c in check
             if str(c.get("status")) in ("still_there", "changed_form")]
    checked = len(check)
    if rounds > 0 and checked:
        rework_effective = not still          # 还有条目没改掉 → 判定为无效
    elif rounds > 0:
        # 有上轮 high、但模型没给核对结果：**不能当成有效**。
        # 旧实现在这里采信了一个瞎猜的 false；新实现宁可判「无效」，
        # 让人在报告里看见，也不能在问题未解决时假装改好了。
        rework_effective = False
        still = [{"task": "", "section": None,
                  "status": "no_check", "note": "模型未给逐条核对结果"}]
    else:
        rework_effective = True               # 首轮没有「上一轮」可言

    # remaining_issues 保留（node_final 读它写存疑节），
    # 现在 = blocked 的内容 + 模型若仍返回旧字段也一并收进来。
    remaining = ([f"[{b['kind']}] {b['issue']}" for b in blocked]
                 + [str(x)[:300] for x in (d.get("remaining_issues") or [])])

    audit = {
        # 残留违规是程序判据：模型说「不用返修」不算数（坑 79）
        "need_rework": (bool(d.get("need_rework")) or violation_targets > 0)
                       and bool(targets),
        "reason": str(d.get("reason") or "")[:400],
        "spec_issue": d.get("spec_issue"),
        "findings": findings,
        "targets": targets,
        "blocked": blocked,
        "remaining_issues": remaining,
        "rework_effective": rework_effective,
        "rework_check": check,
        "still_open": len(still),
        "checked": checked,
        "violation_targets": violation_targets,
    }
    _trace(state.get("session_id"), audit=audit,
           stats=_merge_stats(state.get("session_id"), {
               "audit_round": rounds, "high": len(targets),
               "findings": len(findings), "blocked": len(blocked),
               "violation_targets": violation_targets,
               "need_rework": audit["need_rework"],
               "rework_effective": rework_effective,
               "rework_checked": checked, "rework_still_open": len(still)}))
    # 每轮的发现要留着。之前 audit_json 每轮覆盖，R5 那轮 27 次返修
    # 只剩最后一轮的 11 条，根因只能靠读代码反推——可观测性缺口。
    # `rework_check` 的逐条结果也记进历史，这样「模型到底核对没核对」
    # 是可查的，而不是只能看一个真假布尔。
    _push_audit_history(state.get("session_id"), {
        "round": rounds,
        "need_rework": audit["need_rework"],
        "findings": len(findings),
        "high": len(targets),
        "blocked": len(blocked),
        "violation_targets": violation_targets,
        "targets": [f"{t['task']}→{t.get('section')}" for t in targets],
        "rework_effective": (rework_effective if rounds > 0 else None),
        "rework_checked": checked,
        "still_open": len(still),
        "check_detail": [f"{c.get('task')}/{c.get('section')}:{c.get('status')}"
                         for c in check][:20],
        "reason": audit["reason"][:200],
    })
    return {
        "audit": audit,
        "rework_effective": rework_effective,
        "events": [{"kind": "write_audit", "rounds": rounds,
                    "attempts": attempts,
                    "high": len(targets), "findings": len(findings),
                    "blocked": len(blocked),
                    "violation_targets": violation_targets,
                    "need_rework": audit["need_rework"],
                    "rework_effective": rework_effective,
                    "rework_checked": checked, "still_open": len(still),
                    "check_detail": [f"{c.get('task')}/{c.get('section')}"
                                     f":{c.get('status')}" for c in check][:20],
                    "spec_issue": str(d.get("spec_issue") or "")[:200]}],
    }


def node_revise(state: WriteState) -> dict:
    """定点返修：只把「规约 + 本任务证据 + 草稿 + 具体问题」喂回子 agent。

    **按任务合并，而不是按目标逐条跑。** 同一任务被点名多节时必须一次问完：
    旧写法把每个 high 发现单独跑一次，全写进 drafts[task]，
    于是 t8/sec7 修好，下一秒被 t8/sec8 那版整个覆盖掉——
    返修目标在数据结构上不可满足，怎么跑都不收敛。

    第一个任务串行跑（返修轮同样要重建缓存，TTL 只有 5 分钟），其余并发。
    """
    audit = state.get("audit") or {}
    targets = audit.get("targets") or []
    drafts = dict(state.get("drafts") or {})
    if not targets:
        return {}

    by_task: dict[str, list[dict]] = {}
    for t in targets:
        by_task.setdefault(t["task"], []).append(t)
    order = list(by_task)

    def _one(tid: str) -> tuple[str, dict, str | None]:
        secs = by_task[tid]
        # 意见按小节归并，提示词里才看得出「这几节各要改什么」
        asks = [{"小节": s.get("section"), "问题": s.get("problem"),
                 "改法": s.get("fix")} for s in secs]
        covers = [s.get("section") for s in secs if s.get("section")]
        if not covers:                      # 审查没给小节，就退回任务原始 covers
            covers = [c for c in _task_covers(state, tid) if c]
        prev = _section_map(drafts.get(tid))
        new, err = _run_task(
            state, {"id": tid, "covers": covers,
                    "mandate": "", "must_cover": [], "not_yours": [],
                    "segments": _task_segments_refs(state, tid)},
            revise={"fix": _dumps(asks), "problem": _dumps(asks), "draft": _join(prev)})
        fire_progress("write", {
            "kind": "write_task", "task": tid, "covers": covers,
            "ok": bool(new), "revise": True,
            "chars": sum(len(str(v)) for v in new.values()) if new else 0,
            "reason": (err or "")[:80]})
        return tid, new, err

    t0 = _now()
    # 被 merge 保护拦下的节：新稿可疑而保留了旧稿，必须进事件可查——
    # 不然「返修跑了但正文没动」看起来会像静默 bug（坑 75）。
    rejected: list[dict] = []
    tid, new, _err = _one(order[0])
    drafts[tid] = _merge_sections(drafts.get(tid), new, by_task[tid],
                                  rejected=rejected)
    first_cost = round(_now() - t0, 1)

    rest = order[1:]
    if rest:
        # 同 node_dispatch（C1-f2）：返修的并发任务共享同一段前缀，
        # 预热同样要**每批一个**，不能只有第一个任务。
        # parallel_labelled 保证按原顺序返回，所以逐个合并不影响装配顺序。
        for r in parallel_labelled(rest, _one, workers=CONCURRENCY,
                                   label="revise", first_serial=True):
            if not r:
                continue
            tid, new, _err = r
            drafts[tid] = _merge_sections(drafts.get(tid), new, by_task[tid],
                                          rejected=rejected)

    # 返修救回的任务不能还挂在 failed 里（坑 78）：终稿「存疑与未完成」
    # 会照着它写「以下任务未能完成」，而正文其实已经在了——报告说假话。
    # `failed` 是普通 list（无 append reducer），返回过滤后的新列表即生效。
    cleared_failed = [f.get("task") for f in (state.get("failed") or [])
                      if _draft_has_body(drafts.get(f.get("task")))]
    still_failed = [f for f in (state.get("failed") or [])
                    if f.get("task") not in set(cleared_failed)]

    # 程序护栏在审查之前（坑 79）：返修稿同样先机械修再查残留。
    drafts, auto_fixes = _auto_fix_drafts(state, drafts)

    return {
        "drafts": drafts,
        "failed": still_failed,
        "rework_rounds": state.get("rework_rounds", 0) + 1,
        "violations": _check_all_citations(state, drafts),
        "events": [{"kind": "write_revise",
                    "tasks": order,
                    "targets": [f"{t['task']}→{t.get('section')}" for t in targets],
                    "kept_old": rejected,
                    "cleared_failed": cleared_failed,
                    "auto_fixes": len(auto_fixes),
                    "auto_fix_sample": auto_fixes[:8],
                    "first_serial_s": first_cost,
                    "round": state.get("rework_rounds", 0) + 1}],
    }


def _draft_has_body(d) -> bool:
    """drafts 里的产物是否真有非空正文（dict 按节、str 按整段）。"""
    if isinstance(d, dict):
        return any(str(v).strip() for v in d.values())
    return bool(isinstance(d, str) and d.strip())


def _section_map(draft: Any) -> dict:
    """把草稿统一成 {section_id: 正文}。"""
    if isinstance(draft, dict):
        return {k: v for k, v in draft.items()}
    return {"": draft} if isinstance(draft, str) and draft else {}


def _merge_sections(old: Any, new: dict, targets: list[dict] | None = None,
                    rejected: list | None = None) -> dict:
    """把返修结果并回草稿。

    只有**被点名的小节**才用新稿覆盖；没被点名的小节原样保留。
    返修写崩（新稿为空）时整体保留原稿，不拿空串覆盖——
    宁可留着有瑕疵的版本，也不要凭空少一节。

    `rejected`（2026-10-19 加，坑 75）：新稿本身可疑时**保留旧稿**并记一条
    `{"section": sec, "reason": ...}`。两条判据，满足任一即拒收：
      (a) 新稿命中 STRONG 污染标记（伪工具调用/英文整块/空）——
          v13 实测返修输出过一整段英文推理，一覆盖旧稿就没了；
      (b) 旧稿干净（自身无 STRONG）且 `len(新稿) < 0.4 * len(旧稿)`——
          返修是「改」，砍掉六成的多半是换成了说明性文字。
          阈值 0.4 是**经验值不是实测**，过严会误杀「确实该大删」的返修。
    旧稿自己已经带 STRONG 标记时它是垃圾，新稿只要自己不触发 (a)
    就接受——不然垃圾旧稿会永远挡住任何修复。
    """
    prev = _section_map(old)
    if not new:
        return prev
    wanted = {s.get("section") for s in (targets or []) if s.get("section")}
    if not wanted:
        wanted = set(new)                  # 审查没给小节时，以新稿的键为准

    def _reject(sec: str, reason: str) -> None:
        if rejected is not None:
            rejected.append({"section": sec, "reason": reason})

    merged = dict(prev)
    for sec, body in new.items():
        if sec not in wanted or not (body or "").strip():
            continue
        flags = set(_contamination(body))
        if flags & STRONG:
            _reject(sec, f"新稿污染：{'、'.join(sorted(flags & STRONG))}")
            continue
        old_body = (prev.get(sec) or "").strip()
        if (old_body
                and not (set(_contamination(old_body)) & STRONG)
                and len(body.strip()) < 0.4 * len(old_body)):
            _reject(sec, f"新稿缩水至旧稿的 "
                         f"{len(body.strip())}/{len(old_body)} 字")
            continue
        merged[sec] = body
    return merged or prev


def _task_covers(state: WriteState, tid: str) -> list[str]:
    for t in state.get("tasks") or []:
        if t["id"] == tid:
            return list(t.get("covers") or [])
    return []


def _task_segments_refs(state: WriteState, tid: str) -> list[str]:
    for t in state.get("tasks") or []:
        if t["id"] == tid:
            return t.get("segments") or []
    return []


def route_after_spec(state: WriteState) -> str:
    """规约出不来就别硬做任务——直接降级，让报告里如实写明。"""
    return "tasks" if state.get("spec") else "final"


def route_after_audit(state: WriteState) -> str:
    """还有**返修解决得了**的 high 才继续；否则收工并把 blocked 显式暴露。

    三条收工线，两条是硬约束，一条是**有依据的**判断：

    1. 没有 high finding        → 没什么可改的
    2. `rework_rounds` 用完     → 改够了，剩下的问题写进存疑节
    3. 逐条核对显示上轮问题仍在 → **改不动了**，同样写进存疑节

    第 3 条以前是 `not rework_effective`，而那个字段是模型**自己填的**，
    且它手上没有上轮的 findings，只能笼统自评。实测两个独立 session
    都在 reason 里写着「已有明显改善」、字段却填 false，
    于是在 high 一个没解决时收工。现在 `rework_effective` 由程序从
    `rework_check` 的逐条结果推出，它要否决返修必须指出是哪几条没改掉。
    """
    audit = state.get("audit") or {}
    if not audit.get("need_rework"):
        return "final"
    if state.get("rework_rounds", 0) >= MAX_REWORK:
        return "final"
    if state.get("rework_rounds", 0) > 0 and not state.get("rework_effective", True):
        return "final"
    return "revise"


# ================================================================ 收口

def node_final(state: WriteState) -> dict:
    """拼最终报告。降级、缺口、未完成必须显式可见。"""
    spec = state.get("spec") or {}
    audit = state.get("audit") or {}
    violations = state.get("violations") or {}
    failed = state.get("failed") or []
    uncovered = state.get("uncovered_tasks") or []
    asm_warn = state.get("assemble_warnings") or []
    plan_failed = state.get("plan_failed")

    parts = [f"# {state.get('question','')}\n"]
    if plan_failed:
        # 规划失败时正文是空的，只有标题和来源清单。
        # 实测过一次 MiniMax 内容过滤（output new_sensitive）就走到这里，
        # 产出 4,060 字「看起来像报告」的东西——只有一句标题和一串来源，
        # 读者完全不可能看出整场崩了。失败横幅必须放在最前面，
        # 放在末尾的「存疑」一节里等于没有。
        parts.append(
            "\n> # ⚠️ 本次研究没有产出正文\n"
            f">\n"
            f"> **主 agent 没能制定写作规约**，原因：{str(plan_failed)[:300]}\n"
            f">\n"
            f"> 下面只有来源清单。重新运行，或换个问法、换个模型再试。\n")
    if spec.get("thesis"):
        parts.append(f"\n> **本文主张**：{spec['thesis']}\n")
    parts.append("\n" + (state.get("assembled") or "") + "\n")

    # 存疑与未完成：静默降级会让人误以为报告是完整的
    doubt = list(audit.get("remaining_issues") or [])
    if plan_failed:
        doubt.append(f"写作规约环节失败：{str(plan_failed)[:300]}")
    if spec.get("spec_uncertainty"):
        # 主 agent 自己声明的规约存疑必须让读者看见，
        # 不能只躺在事件日志和 DB 里——这一条存在的意义就是被看见。
        doubt.append(f"规约自检（主 agent 自己标出的不确定处）："
                     f"{str(spec['spec_uncertainty'])[:400]}")
    if audit.get("spec_issue"):
        doubt.append(f"规约本身的存疑：{str(audit['spec_issue'])[:300]}")
    if violations:
        for tid, items in violations.items():
            kinds = "、".join(sorted({i["kind"] for i in items}))
            bad = "、".join(sorted({i["found"] for i in items}))
            doubt.append(f"任务 {tid} 的引用存在问题（{kinds}）：{bad}")
    if failed:
        doubt.append("以下任务未能完成：" +
                     "、".join(f"{f.get('task')}（{f.get('reason')}）" for f in failed))
    if state.get("n_no_summary"):
        doubt.append(
            f"有 {state['n_no_summary']} 个证据段落没能生成概括"
            f"（概括环节失败），它们未进入写作材料。"
            f"这不影响闸门判定——闸门用的是正文开头而非概括——"
            f"但子 agent 只能看到标题与正文开头，看不到这些段的要点概括。")
    if uncovered:
        doubt.append("以下任务的产出未被任何小节采用：" + "、".join(uncovered))
    for w in asm_warn:
        # 装配自检命中：这份稿子可能有结构性错误，读者必须看得见
        doubt.append(f"装配自检：{w}")
    if doubt:
        parts.append("\n## 存疑与未完成\n\n"
                     + "\n".join(f"- {d}" for d in doubt) + "\n")

    # 原来这里还有一节「## 认知演进」，内容是此前研究留下的结论。
    # 它的数据来自 `earlier` ← `recalled_approved` ← `recalled_chunks`，
    # 而 `recalled_chunks` 从召回被砍那天起就没有任何写入方，
    # 所以 `if earlier:` 永不成立——这一节**从来没在真实报告里出现过**。
    # 2026-10-13 连同 `earlier` 字段一起删掉。

    md = "".join(parts)
    # 定位标记是给审查看的，不能留在最终报告里
    md = TASK_MARK_RE.sub("", md)
    md = SECTION_MARK_RE.sub("", md)
    # 引言标记同理。正常情况下它在装配时就被吃掉了，
    # 这里兜底：标记单独占一行、正文里没有标题那种形态会漏过装配层。
    md = re.sub(r"[ \t]*<!--\s*lead\s*-->[ \t]*\n?", "", md)
    md += "\n---\n\n### 来源\n" + "\n".join(state.get("src_lines") or []) + "\n"
    return {
        "report_markdown": md,
        "events": [{"kind": "write",
                    "chars": len(md),
                    "tasks": len(state.get("tasks") or []),
                    "sections_written": state.get("sections_written", 0),
                    "drafts": len(state.get("drafts") or []),
                    "failed": len(failed),
                    "violations": len(violations),
                    "rework_rounds": state.get("rework_rounds", 0),
                    "rework_effective": state.get("rework_effective", True),
                    "assemble_warnings": len(asm_warn),
                    "spec_uncertainty": (spec.get("spec_uncertainty") or "")[:200]}],
    }


# ================================================================ 组装

def build_write_graph():
    g = StateGraph(WriteState)
    g.add_node("plan_spec", node_plan_spec)
    g.add_node("plan_tasks", node_plan_tasks)
    g.add_node("dispatch", node_dispatch)
    g.add_node("assemble", node_assemble)
    g.add_node("audit", node_audit)
    g.add_node("revise", node_revise)
    g.add_node("final", node_final)

    g.add_edge(START, "plan_spec")
    g.add_conditional_edges("plan_spec", route_after_spec,
                            {"tasks": "plan_tasks", "final": "final"})
    g.add_edge("plan_tasks", "dispatch")
    g.add_conditional_edges("dispatch", route_after_dispatch,
                            {"dispatch": "dispatch", "assemble": "assemble"})
    g.add_edge("assemble", "audit")
    g.add_conditional_edges("audit", route_after_audit,
                            {"revise": "revise", "final": "final"})
    g.add_edge("revise", "assemble")
    g.add_edge("final", END)
    return g


def run_write(session_id: str, question: str, outline: list, signals: dict,
              segments: list, on_topic: list, src_lines: list,
              n_no_summary: int = 0) -> dict:
    """write 子图入口。返回 report_markdown 与事件。"""
    if not outline or not segments:
        return {"report_markdown":
                f"# {question}\n\n本轮没有可用的证据段落，未生成报告。\n",
                "events": [{"kind": "write", "note": "empty_input"}]}

    st: WriteState = {
        "session_id": session_id, "question": question, "outline": outline,
        "signals": signals, "segments": segments, "on_topic": on_topic,
        "src_lines": src_lines,
        # `n_no_summary` 原来没落进 state —— 参数传进来了却没进这个 dict，
        # `node_final` 里 `state.get("n_no_summary")` 恒为假，
        # 报告那句「有 N 个证据段落没能生成概括」永远写不出来。
        "n_no_summary": n_no_summary,
        "drafts": {}, "violations": {}, "failed": [],
        "rework_rounds": 0, "rework_effective": True,
    }
    g = build_write_graph().compile()
    return g.invoke(st)


# ---------------------------------------------------------------- 小工具

def _trace(session_id: str, **kw) -> None:
    """把中间产物落库。落库失败不该影响写作。"""
    if not session_id:
        return
    try:
        from ...db.repo import get_repo
        get_repo().save_write_trace(session_id, **kw)
    except Exception:
        pass


def _merge_stats(session_id: str, new: dict) -> dict:
    """写 stats 时保住已有的 tool_reads。

    stats_json 是整体替换的，而 `_record_tool_use` 是在已有 stats 上累加。
    两者相遇的结果是：审查一跑，工具使用统计就被冲没了——
    R6 那轮结束时查到 `{}`，而中途明明记着 read_segments 15 次。
    工具用没用、在看哪些段，是这个系统最该看得见的一件事，不能被顺手覆盖。
    """
    if not session_id:
        return new
    try:
        from ...db.repo import get_repo
        old = get_repo().write_trace(session_id).get("stats") or {}
    except Exception:
        return new
    if not isinstance(old, dict):
        return new
    merged = {k: v for k, v in old.items() if k not in new}
    merged.update(new)
    return merged


def _push_audit_history(session_id: str, entry: dict) -> None:
    """把这一轮审查的摘要追加到历史里（读改写，审查是串行的所以没有竞争）。"""
    if not session_id:
        return
    try:
        from ...db.repo import get_repo
        repo = get_repo()
        hist = repo.write_trace(session_id).get("audit_history") or []
        if not isinstance(hist, list):
            hist = []
        if any(h.get("round") == entry.get("round") for h in hist
               if isinstance(h, dict)):
            return                      # 同一轮重跑就覆盖，不追加重复
        hist.append(entry)
        repo.save_write_trace(session_id, audit_history=hist)
    except Exception:
        pass


def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, indent=1)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _now() -> float:
    import time
    return time.time()
