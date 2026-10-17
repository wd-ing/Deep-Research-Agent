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
import re
from typing import Any

from langgraph.graph import END, START, StateGraph

from ...llm.provider import LLMClient, _parse_json, parallel_labelled
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

# 行首标题：只认 2~6 级，后面必须跟空格。`#######` 与 `#标题` 都不算。
HEADING_RE = re.compile(r"^(#{2,6})\s+(.*)$")
# 子 agent 自造的编号前缀。v5 实测四种形态全出现过：
#   「一、」「五、」中文数字 / 「4.」「2 」阿拉伯数字 / 「1.1」「2.3.1」多级 / 「sec3」
# 数字后必须有空白（中文数字则须跟顿号/点）。这一条是防误伤的关键：
# 没有它，`### 500强企业排名` 会被剥成「强企业排名」、`### 3D打印` 被剥成「D打印」
# ——而这两种根本不是编号。第一版就栽在这里，靠真实数据回放才看出来。
_NUM_PREFIX_RE = re.compile(
    r"^(?:"
    r"[一二三四五六七八九十百]+[、.．][ \t]*"       # 一、/ 五、 中文数字后通常不留空格
    r"|\d+(?:\.\d+)*[、.．]?[ \t]+"                # 1 / 4. / 1.1 / 2.3.1（可无标点但必须有空白）
    r"|sec\d+[ \t]+"                               # sec3
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


# ================================================================ 规划

def node_plan_spec(state: WriteState) -> dict:
    """主 agent 第一步：只出写作规约，不出任务列表。

    为什么要拆：合成一次调用（规约 + 任务）在 effort=high 下**必然断连**——
    实测输入 2.4 万字符、输出 16,536 字符 / 7,633 out_tokens，
    连续三次 Server disconnected，把 max_output_tokens 翻倍到 32768 也救不回来。
    拆开后两次都能完成：规约 4,234 out、任务列表 7,135 out，各自都在安全区。
    """
    try:
        data = LLM.complete_json("write_plan_spec",
                                 _clip(_dumps(_brief(state)), 40000),
                                 system=prompts.WRITE_PLAN_SPEC)
    except Exception as e:
        return {"plan_failed": str(e)[:200],
                "events": [{"kind": "write_plan_error", "stage": "spec",
                            "error": str(e)[:200]}]}
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

    try:
        data = (_parse_json(text) if text
                else LLM.complete_json("write_plan_tasks", _clip(prompt, 60000),
                                       system=prompts.WRITE_PLAN_TASKS))
    except Exception as e:
        return {"plan_failed": str(e)[:200],
                "events": [{"kind": "write_plan_error", "stage": "tasks",
                            "error": str(e)[:200]}]}

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
            "must_cover": [str(x)[:200] for x in (t.get("must_cover") or [])][:12],
            "not_yours": [str(x)[:200] for x in (t.get("not_yours") or [])][:12],
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
    first_cost = 0.0
    for bi, batch in enumerate(batches):
        t0 = _now()

        def _one(t: dict, _b=batch) -> tuple[dict, str | None]:
            return _run_task(state, t, revise=None)

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

    return {
        "drafts": ev["drafts"],
        "failed": ev["failed"],
        "violations": _check_all_citations(state, ev["drafts"]),
        "events": [{"kind": "write_dispatch", "ran": len(todo),
                    "first_serial_s": first_cost,
                    "drafts_total": len(ev["drafts"]),
                    "failed_total": len(ev["failed"])}],
    }


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
            f"**你负责 {len(covers)} 个小节，每一节都要单独成段输出，"
            f"用 `<!-- section:小节id -->` 开头标明这是哪一节。**\n"
            f"小节 id：{'、'.join(covers)}。\n"
            f"不要在一个标记里混写两节，也不要把同一段内容复制给两节。\n\n")
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
        try:
            text, _, log = run_tool_loop(LLM, stage, prompts.WRITE_RULES,
                                        messages, tools, max_rounds=2)
            _record_tool_use(state, task["id"], log)
            return _split_sections(text, covers)
        except Exception as e:
            print(f"⚠️  工具循环失败，退回单次调用: {e}", flush=True)
    try:
        return _split_sections(
            LLM.complete(stage, head + tail, system=prompts.WRITE_RULES), covers)
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
                    "lead_headings_dropped": _renumber_stats["leads"]}],
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
    return sum(1 for line in raw.splitlines() if re.match(r"^##(?!#)\s+", line))


def _raw_body(draft: Any, sid: str) -> str:
    """未降级的原始正文。只给计数用。"""
    if isinstance(draft, dict):
        return (draft.get(sid) or "").strip()
    return (draft or "").strip()


def _section_body(draft: Any, sid: str, sec_no: int = 1,
                  counters: list[int] | None = None) -> str:
    """取某个任务在某一节的正文（标题已降级并重编号）。旧格式（裸字符串）兜底兼容。

    `sec_no` 是本节在大纲里的序号，从 1 起。`node_assemble` 按大纲顺序遍历，
    序号现成；给不出它就没法编「节.节内」这种号，只能退回 1。

    `counters` 由调用方持有并在**同一节的任务之间复用** ——
    一节可能由多个任务拼成，编号要跨任务连续（见 `_renumber_headings`）。
    """
    return _renumber_headings(_demote_headings(_raw_body(draft, sid)),
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
    for line in body.splitlines():
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
_renumber_stats: dict[str, int] = {"stripped": 0, "leads": 0}


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

    try:
        d = LLM.complete_json("write_audit", _clip(_dumps(payload), 120000),
                               system=prompts.WRITE_AUDIT)
    except Exception as e:
        # 审查失败不阻断：保留现状，把问题暴露出来而不是假装没问题
        return {"audit": {"need_rework": False, "error": str(e)[:200],
                          "findings": [], "targets": [], "blocked": [],
                          "rework_check": [], "checked": 0, "still_open": 0,
                          "rework_effective": True,
                          "remaining_issues": ["审查环节失败，未能核查"]},
                "rework_effective": True,
                "events": [{"kind": "write_audit_error", "error": str(e)[:200]}]}

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
        "need_rework": bool(d.get("need_rework")) and bool(targets),
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
    }
    _trace(state.get("session_id"), audit=audit,
           stats=_merge_stats(state.get("session_id"), {
               "audit_round": rounds, "high": len(targets),
               "findings": len(findings), "blocked": len(blocked),
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
                    "high": len(targets), "findings": len(findings),
                    "blocked": len(blocked),
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
        return tid, new, err

    t0 = _now()
    tid, new, _err = _one(order[0])
    drafts[tid] = _merge_sections(drafts.get(tid), new, by_task[tid])
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
            drafts[tid] = _merge_sections(drafts.get(tid), new, by_task[tid])

    return {
        "drafts": drafts,
        "rework_rounds": state.get("rework_rounds", 0) + 1,
        "violations": _check_all_citations(state, drafts),
        "events": [{"kind": "write_revise",
                    "tasks": order,
                    "targets": [f"{t['task']}→{t.get('section')}" for t in targets],
                    "first_serial_s": first_cost,
                    "round": state.get("rework_rounds", 0) + 1}],
    }


def _section_map(draft: Any) -> dict:
    """把草稿统一成 {section_id: 正文}。"""
    if isinstance(draft, dict):
        return {k: v for k, v in draft.items()}
    return {"": draft} if isinstance(draft, str) and draft else {}


def _merge_sections(old: Any, new: dict, targets: list[dict] | None = None) -> dict:
    """把返修结果并回草稿。

    只有**被点名的小节**才用新稿覆盖；没被点名的小节原样保留。
    返修写崩（新稿为空）时整体保留原稿，不拿空串覆盖——
    宁可留着有瑕疵的版本，也不要凭空少一节。
    """
    prev = _section_map(old)
    if not new:
        return prev
    wanted = {s.get("section") for s in (targets or []) if s.get("section")}
    if not wanted:
        wanted = set(new)                  # 审查没给小节时，以新稿的键为准
    merged = dict(prev)
    for sec, body in new.items():
        if sec in wanted and (body or "").strip():
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
