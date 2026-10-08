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

from ...llm.provider import LLMClient, parallel_labelled
from ...llm.toolloop import run_tool_loop
from .. import prompts
from ..state import WriteState

MAX_TASKS = 15              # 任务数上限。测试阶段的硬边界，防止主 agent 切得过细
CONCURRENCY = 4             # 并发子 agent 上限
MAX_REWORK = 2              # 返修轮数上限
MAX_TOOL_ROUNDS = 3         # plan 取全文的工具循环上限
MAX_TOOL_INDICES = 30       # 单次工具调用最多取几条（超出的会在结果里明说，不静默丢）
TASK_MARK_RE = re.compile(r"<!--\s*task:(\S+)\s+section:(\S+)\s*-->")
# 子 agent 分节输出用的标记。`covers` 多于一节时必须逐节标注，否则无法归位。
SECTION_MARK_RE = re.compile(r"<!--\s*section:(\S+)\s*-->")
SEG_REF_RE = re.compile(r"\[S(\d+)·(\d+)\]")          # 段级引用 S3·5
SEG_REF_LOOSE_RE = re.compile(r"\[S\d+[^\]]*?\]")     # 自创写法
SRC_REF_RE = re.compile(r"\[S(\d+)\]")               # 旧的来源级写法，不允许

LLM = LLMClient()


# ================================================================ 规划

def node_plan_spec(state: WriteState) -> dict:
    """主 agent 第一步：只出写作规约，不出任务列表。

    为什么要拆：合成一次调用（规约 + 任务）在 effort=high 下**必然断连**——
    实测输入 2.4 万字符、输出 16,536 字符 / 7,633 out_tokens，
    连续三次 Server disconnected，把 max_output_tokens 翻倍到 32768 也救不回来。
    拆开后两次都能完成：规约 4,234 out、任务列表 7,135 out，各自都在安全区。
    """
    try:
        data = LLM.complete_json("write_plan", _clip(_dumps(_brief(state)), 40000),
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

    输入只有**段落概括**。原先这里还有一层 get_evidence 工具循环，让它
    按需取断言全文——那是断言当证据单位时的补救。现在证据单位是段落，
    概括与全文的落差本来就该由 write 阶段的三个工具解决，规划阶段不再重复一遍。
    """
    spec = state.get("spec") or {}
    outline = state.get("outline") or []
    known = {g.get("ref") for g in (state.get("on_topic") or [])}
    known.discard(None)
    prompt = _dumps({"研究问题": state.get("question", ""),
                     "写作规约（你上一轮定的，必须遵守）": spec,
                     "框架与轮廓": _brief(state)})

    try:
        data = LLM.complete_json("write_plan", _clip(prompt, 60000),
                                 system=prompts.WRITE_PLAN_TASKS)
    except Exception as e:
        return {"plan_failed": str(e)[:200],
                "events": [{"kind": "write_plan_error", "stage": "tasks",
                            "error": str(e)[:200]}]}

    raw_tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
    tasks = _normalize_tasks(raw_tasks, outline, known)
    _trace(state.get("session_id"), spec=spec, tasks=tasks,
           stats={"raw_tasks": len(raw_tasks), "kept": len(tasks),
                  "capped": len(tasks) < len(raw_tasks),
                  "segments_available": len(known)})
    return {
        "spec": spec, "tasks": tasks,
        "spec_capped": len(tasks) < len(raw_tasks),
        "events": [{"kind": "write_plan_tasks", "tasks": len(tasks),
                    "raw_tasks": len(raw_tasks),
                    "segments_available": len(known)}],
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


def _tool_results(calls: list[dict], ev: list[dict],
                  seen: set) -> list[dict]:
    """把 tool_use 转成 tool_result。

    两个要点：
      · 一轮可能有多个 tool_use，**每个都要回一个**，顺序一致，
        少一个就 400 `tool call and result not match`。
      · 单次取数有上限，但**不能静默截断**——没给的那些要在结果里
        说清楚，否则模型不知道自己漏了什么（这正是 need_full 被否掉的原因）。
    """
    out: list[dict] = []
    for c in calls:
        idxs = [i for i in ((c.get("input") or {}).get("indices") or [])
                if isinstance(i, int) and 0 <= i < len(ev)]
        idxs = [i for i in idxs if i not in seen][:MAX_TOOL_INDICES]
        fresh = [i for i in idxs if i not in seen]
        seen.update(fresh)
        payload = [{"编号": i, "全文": (ev[i].get("text") or ""),
                    "来源": ev[i].get("source"), "性质": ev[i].get("nature"),
                    "置信": ev[i].get("confidence"), "日期": ev[i].get("date")}
                   for i in fresh]
        note = ""
        want = len([i for i in ((c.get("input") or {}).get("indices") or [])
                    if isinstance(i, int) and 0 <= i < len(ev)])
        if want > len(fresh):
            note = (f"\n【注意】你这次要了 {want} 条，单次上限 {MAX_TOOL_INDICES} 条，"
                    f"只返回了 {len(fresh)} 条，其余未取。"
                    f"若确实需要，下一轮可以再调。")
        out.append({"type": "tool_result", "tool_use_id": c.get("id"),
                    "content": _dumps(payload) + note})
    return out


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
    for sec in state.get("outline") or []:
        sid = sec.get("id")
        chunks = []
        for tid in task_of_section.get(sid, []):
            body = _section_body(drafts.get(tid), sid)
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
            if _section_body(drafts.get(t["id"]), sec):
                used.add(t["id"])
    missing = [t["id"] for t in by_task.values()
               if drafts.get(t["id"]) and t["id"] not in used]
    return {
        "assembled": "\n\n".join(blocks),
        "sections_written": written,
        "uncovered_tasks": missing,
        "assemble_warnings": warnings,
        "headings_demoted": demoted,
        "events": [{"kind": "write_assemble", "sections": written,
                    "tasks": len(drafts), "failed": len(failed),
                    "warnings": len(warnings), "headings_demoted": demoted}],
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


def _section_body(draft: Any, sid: str) -> str:
    """取某个任务在某一节的正文（标题已降级）。旧格式（裸字符串）兜底兼容。"""
    return _demote_headings(_raw_body(draft, sid))


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

    读 `audit_history_json` 的**倒数第二条**——最后一条是上一轮刚写的，
    但那一轮的 targets 是「它当时要求改什么」，正是我们要的。
    若上一轮没有 high，返回空（模型就不必填 `rework_check`）。
    """
    if not session_id:
        return []
    try:
        from ...db.repo import get_repo
        h = get_repo().write_trace(session_id).get("audit_history") or []
    except Exception:
        return []
    for rec in reversed(h):
        t = rec.get("targets") or []
        if t:
            return [{"task": x.get("task"), "section": x.get("section"),
                     "问题": x.get("problem"), "要求": x.get("fix")}
                    for x in t if x.get("task")]
    return []


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
    earlier = state.get("earlier") or []
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

    if earlier:
        lines = [f"- **{c.get('date') or '日期未知'}**：{(c.get('summary') or '').strip()}"
                 for c in earlier if (c.get("summary") or "").strip()]
        if lines:
            parts.append("\n## 认知演进\n\n"
                         "以下是此前研究留下的相关结论，供你对照本次结果：\n\n"
                         + "\n".join(lines) + "\n")

    md = "".join(parts)
    # 定位标记是给审查看的，不能留在最终报告里
    md = TASK_MARK_RE.sub("", md)
    md = SECTION_MARK_RE.sub("", md)
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
              earlier: list | None = None, n_no_summary: int = 0) -> dict:
    """write 子图入口。返回 report_markdown 与事件。"""
    if not outline or not segments:
        return {"report_markdown":
                f"# {question}\n\n本轮没有可用的证据段落，未生成报告。\n",
                "events": [{"kind": "write", "note": "empty_input"}]}

    st: WriteState = {
        "session_id": session_id, "question": question, "outline": outline,
        "signals": signals, "segments": segments, "on_topic": on_topic,
        "src_lines": src_lines, "earlier": earlier or [],
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
