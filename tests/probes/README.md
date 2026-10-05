# 探测脚本存档

这些脚本**不是回归测试**，跑一次得出一个结论就完成了使命。
它们的价值在于**结论**——每个决策背后都有一段实测数据，脚本就是复现它们的方式。
放在这里而不是删除，是因为"当初凭什么这么定"需要可查证。

需要重复运行的测试在 `tests/` 根目录：`smoke.py`（7 项冒烟）、
`write_struct.py`（write 子图 7 场景结构自检）、`json_repair.py`（21 例）、
`e2e_run.py` / `e2e_resume.py` / `write_real.py`（端到端与子图真数据验证）。

大部分脚本依赖 `/tmp/*.db` 里的历史数据或需要 API key，重跑前先看脚本头的注释。

---

## 思考控制与缓存（R1）

| 脚本 | 测了什么 | 结论 |
| --- | --- | --- |
| `probe_cache.py` | 缓存命中、并发惊群 | 自动模式隐式生效，无须 `cache_control`；冷缓存 8 并发只 3 命中，预热后 7/8 |
| `probe_cache_concurrent.py` | 并发建缓存行为 | 冷缓存下多数请求全价，**必须串行先建** |
| `probe_cache_extend.py` | 缓存能否沿只追加对话延用 | **能**（9,262 → 9,351）。此前我断言"对话变长就是新钥匙"是错的 |
| `thinking_effort.py` | `output_config.effort` 真实效果 | `budget_tokens` 对 MiniMax 完全无效；effort=low 思考压到 520 字符 |
| `write_budget.py` | 增大 max_output_tokens 是否有用 | **反而更容易断**。16,384 本来就够（实际用 6,859） |
| `write_outlet.py` | 换模型 / 砍输入 / 显式断点 | 只有换模型有效；砍输入无效 |
| `write_repeat.py` | 候选模型连跑稳定性 | M3 连跑 3 次全成 |
| `preflight_models.py` | 各环节真实提示词的 JSON 可解析性 | 8/8 通过（换模型前必跑） |
| `probe_flake.py` / `probe_models.py` / `probe_raw.py` | 探针输出稳定性与原始响应 | 探针轻扫 6/6 正常 |

## 切分与段落（R5）

| 脚本 | 测了什么 | 结论 |
| --- | --- | --- |
| `probe_para_split.py` | 按空行切段 | **零空行**——trafilatura 把整篇压成一行 |
| `probe_para_fmt.py` | `include_formatting` 的效果 | 空行 0 → 100；多出的全是 `#`/`**` 标记，**不是噪声，保留** |
| `probe_para_merge.py` | 段落合并到 1000 token | 5 个来源可用样本全部达标 |
| `probe_para_real.py` | 23 个真实来源全量测 | 672 段 → 53 块，压缩比 12.7，中位 760 token |
| `r5_verify.py` | 真实语料跑完整切分 + 概括 | 26 块，块长 364–998，概括 26–63 字 |

## 协议与工具

| 脚本 | 测了什么 | 结论 |
| --- | --- | --- |
| `probe_tools.py` | tool calling 支持与协议坑 | 支持；一轮可能发**多个** tool_use，**每个都要回 tool_result**，少一个就 400 |
| `diag_plan.py` | plan 单次调用的输出规模 | 合成一次在 effort=high 下**必然断连**；拆成规约 + 任务列表后两次都通 |
| `diag_stages.py` | 各环节单次负载体检 | audit 在 30k 输入 + high 下也断 |
| `diag_audit_flaky.py` | audit 预算与子 agent 断连复测 | 16,384 够用；子 agent 4/4 正常，断连是偶发 |
| `probe_only.py` | 探针子图单跑（可注入故障） | 判不出来与判为稀薄必须分开（`judged` 字段） |

## 其他

| 脚本 | 说明 |
| --- | --- |
| `searxng_idle.py` | SearXNG 空闲自停三段实测：进程内通过、**进程退出后不生效**（催生了看门狗方案） |
| `batch_ab.py` | 批量抽取的批次参数 A/B |
| `replay_r5_dup_section.py` | 用 R5 真实报告复现「sec7 与 sec8 逐字相同」，并验证 R6 修好了 |
| `probe_agnes.py` | Agnes 两模型基础可用性（第一版，撞限流，后被下面几个取代） |
| `probe_agnes_thinking.py` | Agnes thinking 参数的精确对比：7 种变体输出全同，**参数不分区只分开关** |
| `probe_agnes3_think_budget.py` | 3.0/2.5 开思考的预算曲线：**2048 会烧光，4096 起正常** |
| `probe_agnes_rate.py` | 慢速复测（补齐 JSON/工具/Messages）+ 限流窗口。**结论：免费额度 RPM 10** |
| `probe_agnes_speed.py` | 用真实任务形态测速度（summarize / 歧义 / 相关性 / 短 JSON）。**3.0 整体更快** |
| `probe_agnes_rpm_window.py` | 限流窗口语义：按发送时刻还是按在途。**按发送时刻，慢请求不占配额槽位** |
| `probe_agnes_limit.py` | 二分搜真实限额。8→8、12→9、20→15、30→0，**不是干净形态**（后由官方文档解释为两级配额） |
| `probe_agnes_write_quality.py` | write_plan / write_audit 能力初测。**有严重缺陷，结论不可用**（读错稿子、截断输入、标准答案跨轮混用） |
| `probe_agnes_audit_quality.py` | 上面那个的修正版。发现真问题：R5 的装配 bug 被 Agnes 独立诊断出来 |
| `probe_agnes_audit_ab.py` | **最终版**。R6c 稿子 + R6c 规约，4/4 题目先验证再评分，双轨对比 |

> Agnes 探针共 9 个，其中后 3 个是同一个问题的三次迭代——
> **每一次的缺陷都是「前提没核对」**（读错文件、截断输入、标准答案跨轮混用）。
> 保留前两个是因为它们记录了错在哪，删掉会让后来的人重蹈覆辙。
> 结论汇总在 `DESIGN_R7_SECONDARY_MODELS.md`（含修订节）。

---

## 一条方法论教训

这几个脚本里有**一半以上是失败的实验**——`probe_cache_extend.py` 改了三版才对，
`write_budget.py` 测的是一个错误前提，`probe_para_split.py` 直接推翻了「按段落切」这个方案。

它们留着，是为了提醒后来的人：**结论必须能复现，推翻过什么也要能查。**
