import type { ProgressEntry } from '../types'

/** 一个大环节。 */
export interface Phase {
  key: string
  label: string
  /** 摘要行：大环节永不折叠，一眼看到整体进度 */
  summary: string
  steps: Step[]
}

/** 一个子环节。**同名的可以重复出现——检索/评估本来就是多轮循环。 */
export interface Step {
  /** 稳定的身份，往上翻/定位靠它 */
  id: string
  label: string
  /** 一行摘要，永不折叠 */
  summary: string
  /** 需要用户注意（材料不足 / 审查发现问题 / 环节失败） */
  warn?: boolean
  /** 状态：运行中 / 已完成 */
  state?: 'running' | 'done'
  /** 收起后才看的明细。**没有就不渲染这一块** */
  detail?: string[]
  /** 事件 kind——内部用于把细粒度事件归组，不用于渲染语义 */
  kind?: string
  /** write_task 专用：true = 这是返修任务，组长是 write_revise 不是 write_dispatch */
  revise?: boolean
  /** 三级明细：这个环节内部跑出来的逐条产出（B1 细粒度事件）。
   *  环节完成时自动收拢成本行，点开才看。 */
  children?: Step[]
}

/**
 * 把 SSE 事件整理成「四个大环节 + 子环节」的工作流。
 *
 * ## 为什么只有四个大环节
 *
 * 用户要的是「知道在干活」，不是看内部有 11 个节点。
 * 11 个节点里只有四组是用户心智里的「阶段」，其余（探测、抽取、装配、
 * 收尾）都是某一步的细节，压成一行摘要就够了。
 *
 * ## 循环怎么表达
 *
 * **不单独设计「循环」这个东西**——检索/评估本来就是多轮的，
 * 让同名子环节重复出现就是最直白的表达：
 * 「检索（第 1 轮）… 检索（第 2 轮）… 评估（第 2 轮）」。
 * 硬造一个「循环 1/2/3」的层级反而更难读。
 *
 * ## 展示标准（用户定的）
 *
 * 1. 让用户知道**在干活**——每步都有摘要
 * 2. 让用户知道**怎么干的活**——检索词等明细可展开
 * 3. 让用户知道**核心信息**——⚠️ 只在需要判断时出现
 * 4. **不必要的信息一律不给**：没有明细就没有「收起后」，不塞占位符
 */

const PHASE_ORDER = ['规划', '审批', '写作', '收尾'] as const
type PhaseName = (typeof PHASE_ORDER)[number]

// ⚠️ 这两个辅助函数的参数**必须写 `ProgressEntry` 而不是 `ProgressEvent`**——
// 不 import 同名的 `ProgressEvent` 之后，裸写会解析到 DOM 内置的那个
// （`ProgressEvent<EventTarget>`），tsc 会报一屏「缺 lengthComputable/total」。
function n(e: ProgressEntry, k: string): number | undefined {
  return typeof e[k] === 'number' ? (e[k] as number) : undefined
}
function s(e: ProgressEntry, k: string): string | undefined {
  return typeof e[k] === 'string' ? (e[k] as string) : undefined
}

/** 当前节点属于哪个大环节。`assess`/`apply_revision` 都算规划。
 *
 *  ⚠️ 这个函数**只该拿「事件自带的 node」或「此刻正在跑的 node」来调**，
 *  绝不能用当前节点去回溯整段历史（那正是下面 `buildPhases` 原来的写法，
 *  会让写作阶段把规划步骤全吞掉）。 */
function phaseOf(node: string | null, view: string): PhaseName {
  if (view === 'awaiting') return '审批'
  // ⚠️ `summary` 必须跟 `finalize` 同属「收尾」——
  // 漏了它会 fall through 到「规划」，收尾时环节高亮闪回规划。
  if (node === 'write' || node === 'finalize' || node === 'summary')
    return node === 'write' ? '写作' : '收尾'
  return '规划'
}

/** 单个 progress 事件 → 一个子环节。没有就返回 null（不留空条目）。 */
function toStep(e: ProgressEntry, seq: number): Step | null {
  const id = `${e.kind}-${seq}`
  switch (e.kind) {
    case 'probe_light':
      return { id, label: '轻扫检索', summary: `命中 ${n(e, 'hits') ?? '?'}` }
    case 'probe_deep':
      return { id, label: '深读', summary: `${n(e, 'read')}/${n(e, 'tried')} 篇 · ${n(e, 'chars')} 字` }
    case 'probe_outline':
      return {
        id, label: '归纳领域轮廓',
        summary: `${n(e, 'subtopics')} 个子话题 · ${n(e, 'entities')} 实体 · ${n(e, 'controversies')} 争议`,
      }
    case 'outline':
      return { id, label: '定框架', summary: `${n(e, 'sections') ?? '?'} 个小节` }
    case 'search_query': {
      // 环节内实时进度（fire_progress，B1）：每条查询返回即发一行，
      // 检索那几秒不再是黑箱。失败的要标出来——命中 0 和查挂了是两回事。
      const err = s(e, 'error')
      return {
        id, label: `查询 ${n(e, 'i')}/${n(e, 'of')}`,
        summary: err
          ? `「${s(e, 'query')}」失败`
          : `「${s(e, 'query')}」→ 命中 ${n(e, 'hits') ?? '?'}`,
        warn: !!err,
        detail: err ? [err] : undefined,
      }
    }
    case 'retrieve': {
      const rd = n(e, 'round') ?? 0
      const lines: string[] = []
      const q = e.queries as { zh?: string[]; en?: string[] } | undefined
      if (q?.zh?.length) lines.push(`中文：${q.zh.join(' / ')}`)
      if (q?.en?.length) lines.push(`英文：${q.en.join(' / ')}`)
      return {
        id, label: `检索（第 ${rd} 轮）`,
        summary: `搜 ${(q?.zh?.length ?? 0) + (q?.en?.length ?? 0)} 组词 · 命中 ${n(e, 'hits') ?? '?'} · 采用 ${n(e, 'accepted') ?? n(e, 'new_sources') ?? '?'}`,
        detail: lines,
      }
    }
    case 'summarize_batch': {
      // 概括的批级进度：done < of 是部分失败（下标错位被丢），
      // failed 是整批重试仍不成——后者那些段落进不了写作阶段。
      if (e.failed === true) {
        return {
          id, label: `段落概括 ${n(e, 'batch')}/${n(e, 'batches')}`,
          summary: `批次失败，${n(e, 'of')} 段没进写作材料`, warn: true,
        }
      }
      const done = n(e, 'done') ?? 0
      const of = n(e, 'of') ?? 0
      return {
        id, label: `段落概括 ${n(e, 'batch')}/${n(e, 'batches')}`,
        summary: `${done}/${of} 段`, warn: done < of,
      }
    }
    case 'extract_source': {
      const title = s(e, 'title') ?? ''
      const nature = s(e, 'nature') ?? ''
      return {
        id, label: '来源判定',
        summary: `${title.length > 42 ? title.slice(0, 42) + '…' : title} — ${nature}`,
      }
    }
    case 'nature_batch': {
      // 只有「有缺口」或「整批失败」才发（缺口的来源会归到「未判」组）
      const missing = n(e, 'missing') ?? 0
      return {
        id, label: `来源判定批次 ${n(e, 'batch')}/${n(e, 'batches')}`,
        summary: e.failed === true
          ? `批次失败${s(e, 'error') ? `：${s(e, 'error')}` : ''}`
          : `${missing} 个来源没判出性质`,
        warn: true,
      }
    }
    case 'gate_batch': {
      if (e.failed === true) {
        return {
          id, label: `闸门判定 ${n(e, 'batch')}/${n(e, 'batches')}`,
          summary: `批次失败，回退逐段补判`, warn: true,
        }
      }
      return {
        id, label: `闸门判定 ${n(e, 'batch')}/${n(e, 'batches')}`,
        summary: `留 ${n(e, 'on_topic') ?? '?'} · 毙 ${n(e, 'off_topic') ?? '?'}`
          + ((n(e, 'missing') ?? 0) > 0 ? ` · 漏判 ${n(e, 'missing')}` : ''),
      }
    }
    case 'gate_retry':
      return {
        id, label: '闸门补判',
        summary: `${n(e, 'count')} 段回退逐段判定`, warn: true,
      }
    case 'write_task': {
      const covers = ((e.covers as string[] | undefined) ?? []).join('、')
      const ok = e.ok === true
      return {
        id, label: e.revise === true ? '小节返修' : '小节写作',
        summary: `${s(e, 'task') ?? ''} → ${covers} · ${n(e, 'chars') ?? 0} 字${ok ? '' : ' · 失败'}`,
        warn: !ok,
        detail: s(e, 'reason') ? [s(e, 'reason') as string] : undefined,
        revise: e.revise === true,
      }
    }
    case 'write_audit': {
      // 审查结论原来整段被吞（default → null）——自检→返修→复检是
      // 写作叙事最关键的一环，界面看不到等于「写完就交稿」。
      const rounds = n(e, 'rounds') ?? 0
      const still = n(e, 'still_open') ?? 0
      const needRework = e.need_rework === true
      // check_detail 的 status 词是内部码，翻成人话
      const STATUS_CN: Record<string, string> = {
        changed_form: '换了措辞，问题还在',
        still_there: '没改',
        resolved: '已闭合',
      }
      const checks = (e.check_detail as string[] | undefined) ?? []
      return {
        id, label: rounds > 0 ? '复审' : '审查',
        summary: rounds > 0
          ? `复查 ${n(e, 'rework_checked') ?? '?'} 项 · ${still} 项未闭合`
          : `${n(e, 'findings') ?? 0} 项发现 · 高严重 ${n(e, 'high') ?? 0}`
            + (needRework ? ' · 需返修' : ''),
        warn: needRework || still > 0,
        detail: checks.map((c) => {
          const [where, st] = c.split(':')
          return `${where} — ${STATUS_CN[st] ?? st}`
        }),
      }
    }
    case 'write_revise': {
      const tasks = (e.tasks as string[] | undefined) ?? []
      return {
        id, label: '返修',
        summary: `${tasks.length} 节返修（${((e.targets as string[] | undefined) ?? []).join(' · ')}）`,
      }
    }
    case 'extract': {
      // 两件独立的事，都得说：「概括」批次失败 → 段落没进写作材料；
      // 「性质」没判出来 → 来源库里归到「未判」组、写作时看到的是「性质未判」。
      // 性质这一条原来后端根本没报（整批被静默丢弃），2026-10-14 才补上 `unjudged_sources`。
      const sumFail = n(e, 'summarize_failed_batches') ?? 0
      const unjudged = n(e, 'unjudged_sources') ?? 0
      const detail = [
        ...(sumFail > 0
          ? [`${sumFail} 批概括失败，这些段落的要点没进写作材料`] : []),
        ...(unjudged > 0
          ? [`${unjudged} 个来源没判出性质，来源库里会归到「未判」组`] : []),
      ]
      return {
        id, label: `抽取（第 ${n(e, 'round') ?? '?'} 轮）`,
        summary: `${n(e, 'sources')} 来源 → ${n(e, 'segments')} 段 · 概括成功 ${n(e, 'summarized')}`,
        warn: detail.length > 0,
        detail: detail.length ? detail : undefined,
      }
    }
    case 'gate': {
      // 段级相关性闸门（2026-10-18 从 write 头部前移到每轮抽取后）。
      // by_method 告诉用户多少段是规则免费放行的、多少是 LLM 判的。
      const bm = (e.gate as { by_method?: Record<string, number> } | undefined)
        ?.by_method ?? {}
      const ruleFree = (bm.rule ?? 0) + (bm.rule_weak ?? 0)
      const judged = (bm.llm ?? 0) + (bm.fallback ?? 0)
      return {
        id, label: `证据闸门（第 ${n(e, 'round') ?? '?'} 轮）`,
        summary: `${n(e, 'segments_kept') ?? '?'}/${n(e, 'segments_total') ?? '?'} 段相关`
          + ` · 规则放行 ${ruleFree} · LLM 判 ${judged}`,
      }
    }
    case 'assess': {
      const thin = (e.thin_sections as string[] | undefined) ?? []
      const gaps = e.gaps as number | undefined
      const urg = e.gaps_by_urgency as Record<string, number> | undefined
      const parts = [`累计 ${n(e, 'segments_total')} 段`, `本轮新增 ${n(e, 'new_segments')}`]
      if (gaps) parts.push(`缺口 ${gaps}${urg ? `（${Object.entries(urg).map(([k, v]) => k + ' ' + v).join(' ')}）` : ''}`)
      return {
        id, label: `评估（第 ${n(e, 'round')} 轮）`,
        summary: parts.join(' · '),
        warn: thin.length > 0,
        detail: [
          ...(thin.length ? [`材料不足：${thin.join('、')}`] : []),
          ...((e.gap_detail as string[] | undefined) ?? []),
          ...(s(e, 'stop_would_be') ? [`停止判据：${s(e, 'stop_would_be')}（来自 ${s(e, 'stop_layer')}）`] : []),
        ],
      }
    }
    case 'outline_revision': {
      const nar = e.narrowed as { id: string; to: string }[] | undefined
      const rem = e.removed as string[] | undefined
      const add = e.added as string[] | undefined
      const detail: string[] = []
      nar?.forEach((x) => detail.push(`${x.id} 收窄为「${x.to}」`))
      rem?.forEach((x) => detail.push(`删除 ${x}`))
      add?.forEach((x) => detail.push(`新增 ${x}`))
      if (e.reason) detail.push(`理由：${String(e.reason)}`)
      return {
        id, label: '校准框架',
        summary: `收窄 ${nar?.length ?? 0} 节 · 新增 ${add?.length ?? 0} · 删除 ${rem?.length ?? 0}`,
        detail,
      }
    }
    case 'write_plan_spec': {
      const thesis = s(e, 'thesis')
      return {
        id, label: '写作规约',
        summary: thesis ? `论点：${thesis.length > 60 ? thesis.slice(0, 60) + '…' : thesis}` : '规约已生成',
        warn: !!s(e, 'spec_uncertainty'),
        detail: s(e, 'spec_uncertainty') ? [`规约存疑：${s(e, 'spec_uncertainty')}`] : undefined,
      }
    }
    case 'write_plan_tasks':
      return { id, label: '任务分派', summary: `${n(e, 'kept') ?? n(e, 'tasks')} 个子 agent · ${n(e, 'segments_available')} 段可用` }
    case 'write_dispatch':
      return { id, label: '并行写作', summary: `${n(e, 'ran')} 个子 agent · 首个 ${n(e, 'first_serial_s')}s`, state: 'done' }
    case 'write_assemble':
      return {
        id, label: '装配成文',
        summary: `${n(e, 'sections')} 节${(n(e, 'headings_renumbered') ?? 0) > 0 ? ` · 重编号 ${n(e, 'headings_renumbered')} 处` : ''}`,
        warn: (n(e, 'warnings') ?? 0) > 0,
      }
    case 'write_audit_error':
      return { id, label: '审查', summary: '环节失败，未拿到审查结论', warn: true, detail: [s(e, 'error') ?? ''] }
    case 'write': {
      // ⚠️ **`kind:"write"` 有两个来源，语义完全不同**（2026-10-14 用真实录制
      // 数据跑出来才发现）：
      //   · `write.py:1250` —— 写作子图的产出（chars/tasks/drafts/failed/violations/…）
      //   · `nodes.py:1262` —— 成稿之后的**证据闸门**（segments_total/kept/gate/degraded）
      // 两者原来都走 `labelOfWrite`，于是**都叫「报告完成」**：界面上并排两行
      // 同名、摘要几乎一样的条目（`13045 字 · 4 节` / `13045 字 · 29 节`），
      // 用户根本看不出这是两步。第二条根本不是「报告完成」，是成稿后的证据质量检查。
      // 判据用 `segments_kept`——只有闸门那条事件带这个字段。
      if (e.segments_kept !== undefined) {
        const kept = n(e, 'segments_kept')
        const total = n(e, 'segments_total')
        return {
          id, label: '证据闸门',
          summary: kept !== undefined && total !== undefined
            ? `${kept}/${total} 段进入写作`
            : `${n(e, 'chars') ?? '?'} 字`,
          // ⚠️ `degraded` 的含义别猜：`nodes.py:1201` 里它是 `bool(head)`，
          // head 来自 `probe_degraded` —— **轻量检索判定这个主题的公开信息稀薄**，
          // 报告开头会被加上 DEGRADED_HEADER。不是运行失败，但值得说一句。
          warn: e.degraded === true,
          detail: e.degraded === true
            ? ['轻量检索判定这个主题的公开信息稀薄，报告开头已加了说明']
            : undefined,
        }
      }
      return {
        id, label: labelOfWrite(e),
        summary: `${n(e, 'chars') ?? '?'} 字 · ${n(e, 'sections_written') ?? n(e, 'sources') ?? '?'} 节`
          + ((n(e, 'rework_rounds') ?? 0) > 0 ? ` · 返修 ${n(e, 'rework_rounds')} 轮` : ''),
        warn: (n(e, 'failed') ?? 0) > 0 || (n(e, 'violations') ?? 0) > 0,
        detail: [
          ...(s(e, 'spec_uncertainty') ? [`规约存疑：${s(e, 'spec_uncertainty')}`] : []),
        ],
      }
    }
    case 'summary':
      // 摘要成功不占版面（4 秒的活儿，用户不需要盯着）；
      // `skipped`（没报告 / 已有摘要）也是正常的。
      // 但**失败要报**——否则用户回列表发现没有摘要，不知道出了什么事。
      // 这与 write_audit_error 是同一个约定：只显示出问题的那次。
      if (e.ok !== false) return null
      return {
        id, label: '生成会话摘要',
        summary: '环节失败，列表里不会有摘要',
        warn: true,
        detail: [s(e, 'error') ?? ''].filter(Boolean),
      }
    default:
      return null
  }
}

function labelOfWrite(e: ProgressEntry): string {
  // ⚠️ 不能叫「返修」：write_revise 事件已经占了这个名字。
  // 这条是写作**子图**的收口（write.py:1250，带 rework_rounds），
  // 后面 nodes.py 还会发一条真正的「报告完成」。
  return n(e, 'rework_rounds') ? '写作汇总' : '报告完成'
}

/**
 * 三级归组：细粒度事件（B1）收进它们所属环节的汇总行下面。
 *
 * 两种挂法，对应两种时序：
 * - **forward**：组长最后落地（retrieve/extract/gate 都是「干完才发汇总」）。
 *   之前的同类明细先攒着，组长一到就收为 children。
 * - **backward**：组长先落地（write_dispatch 在任务流之前）。
 *   后续 write_task 直接挂到最近一个 dispatch 身上。
 *
 * 组里成员在「等组长期间」先按平铺渲染（运行中本来就该逐条看见），
 * 组长落地那一刻自然收拢——这正是「跑完收起来」的交互。
 */
const CHILD_TO_PARENT: Record<string, string> = {
  search_query: 'retrieve',
  extract_source: 'extract',
  nature_batch: 'extract',
  summarize_batch: 'extract',
  gate_batch: 'gate',
  gate_retry: 'gate',
}

/** 合成组长的行标签——真组长还没落地时，明细先挂在这行下面缩进显示。 */
const LIVE_GROUP_LABEL: Record<string, string> = {
  retrieve: '检索进行中',
  extract: '抽取进行中',
  gate: '闸门判定中',
  write_revise: '返修进行中',
}

function groupSteps(steps: Step[]): Step[] {
  const out: Step[] = []
  /** pk → 正在收明细的合成组长（**已经在 `out` 里**，明细一来就缩进挂上，
   *  不再是「先平铺、组长落地再收进去」——后者会让行在执行中跳位）。 */
  const liveGroup = new Map<string, Step>()
  let lastDispatch: Step | undefined
  const flush = (pk: string, parent: Step) => {
    const g = liveGroup.get(pk)
    if (g) {
      if (g.children?.length) parent.children = g.children
      const idx = out.indexOf(g)
      if (idx >= 0) out.splice(idx, 1)   // 真组长原位替换合成组长
      liveGroup.delete(pk)
    }
  }
  const groupFor = (pk: string, seq: number): Step => {
    let g = liveGroup.get(pk)
    if (!g) {
      g = {
        id: `live-${pk}-${seq}`, label: LIVE_GROUP_LABEL[pk] ?? '处理中',
        summary: '', children: [],
        // ⚠️ 不能给 state:'running'：lastReal 只认非 running 步骤，
        // 给了它这行就进不了「最新产出行」——live/green 会错落到前一行。
        // 用 kind 标记，PhaseBlock 拿它认「这环节有组在收明细 = 在跑」。
        kind: '_live',
      }
      liveGroup.set(pk, g)
      out.push(g)
    }
    return g
  }
  for (const st of steps) {
    const k = st.kind ?? ''
    if (k === 'write_task') {
      if (st.revise) {
        groupFor('write_revise', out.length).children!.push(st)
      } else if (lastDispatch) {
        ;(lastDispatch.children ??= []).push(st)
      } else {
        out.push(st)
      }
      continue
    }
    const pk = CHILD_TO_PARENT[k]
    if (pk) {
      groupFor(pk, out.length).children!.push(st)
      continue
    }
    flush(k, st)
    if (k === 'write_dispatch') lastDispatch = st
    out.push(st)
  }
  // 组长没等到的孤儿组留着不动：中途断掉的 run 宁可显示也不藏，
  // 合成组长行（`live-*`）本身就是「这轮没收口」的如实呈现。
  return out
}

/** 把事件流整理成四个大环节。 */
export function buildPhases(
  progress: ProgressEntry[],
  currentNode: string | null,
  view: string,
): Phase[] {
  const phases: Record<string, Phase> = {}
  for (const p of PHASE_ORDER) phases[p] = { key: p, label: p, summary: '', steps: [] }

  // ⚠️ **按事件自带的 `node` 归类，一个都不能用 `currentNode` 推。**
  // 原来这里写的是
  //   `/write|finalize/.test(e.node) || currentNode === 'write' ? currentNode : ''`
  // ——但 `e.node` 当时根本不存在（`useSession` 只存了内层 `ev.event`，
  // 把外层的 node 丢了），所以正则是**恒 false**，整个表达式退化成
  // 「渲染那一刻的当前节点」。后果：一进 write，全部历史步骤塌进「写作」、
  // 「规划」整个消失；一到 finalize 又全部弹回「规划」。
  // （v5 里 write 占整轮 58%，所以这个错法全程可见。）
  progress.forEach((e, i) => {
    const st = toStep(e, i)
    if (!st) return
    st.kind = e.kind
    phases[phaseOf(e.node, view)].steps.push(st)
  })
  for (const p of PHASE_ORDER) phases[p].steps = groupSteps(phases[p].steps)


  // 当前正在跑的大环节：**只在确实有后续事件时才加**。
  // 审批时不要塞「审批环节 / 进行中」——那时规划已经跑完，
  // 审批页本身就是结论，再叠一层反而让人以为还有东西在跑。
  if (view === 'running' && currentNode) {
    const cur = phaseOf(currentNode, view)
    const last = phases[cur].steps[phases[cur].steps.length - 1]
    if (!last || last.state === 'running') {
      phases[cur].steps.push({
        // ⚠️ 摘要**留空**：原来写「进行中…」，于是同一行里「进行中」出现了三次
        // （大环节摘要 + 步骤摘要 + 右侧标签）。右侧那个标签已经够了。
        id: `run-${cur}`, label: phaseRunningLabel(cur), summary: '',
        state: 'running',
      })
    }
  }

  for (const p of PHASE_ORDER) phases[p].summary = summarize(p, phases[p].steps)
  return PHASE_ORDER.map((p) => phases[p]).filter((p) => p.steps.length > 0)
}

function phaseRunningLabel(p: PhaseName): string {
  return { 规划: '规划中', 审批: '待你批准', 写作: '写作中', 收尾: '收尾中' }[p]
}

/** 大环节摘要：一句话说清这组干了什么。 */
function summarize(name: string, steps: Step[]): string {
  const done = steps.filter((x) => x.state !== 'running')
  // 全是这个环节还没开始的合成步骤 → 摘要留空。「进行中」在大环节标题
  // （绿字 + 呼吸点）和步骤右侧标签各出现一次，已经够了。
  if (!done.length) return ''
  let base = `${done.length} 步`
  if (name === '规划') {
    const rd = done.filter((x) => x.label.startsWith('检索')).length
    const ex = done.filter((x) => x.label.startsWith('抽取')).length
    const as = done.filter((x) => x.label.startsWith('评估')).length
    const bits: string[] = []
    if (rd) bits.push(`检索 ${rd} 轮`)
    if (ex) bits.push(`抽取 ${ex} 轮`)
    if (as) bits.push(`评估 ${as} 轮`)
    if (bits.length) base += ' · ' + bits.join(' · ')
  }
  // ⚠️ 这里原来还追加 `· ${warn} 处需注意`，而标题行右侧已经挂了一个
  // 琥珀色 badge 写同样的数 —— 同一行里同一句话说两遍。badge 留下（更显眼、
  // 能扫读），摘要里去掉。
  return base
}
