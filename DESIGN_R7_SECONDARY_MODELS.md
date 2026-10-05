# DESIGN R7：第二套模型（Agnes）搭配方案

写于 2026-10-06。基线数据来自 `/tmp/ra_r5_e2e.db`（session `s_a9f5bc761016`，
真实一轮 66.5 分钟）与 `tests/probes/` 下四个 Agnes 探针。

---

## 零、先说结论：这套方案的价值比预期小得多

用户的原始意图是「给第二套模型做降级通道」。实测下来：

一轮 66.5 分钟的用时分布：

| 环节 | 调用数 | 用时 | 占总量 | Agnes 能否接 |
|---|---:|---:|---:|---|
| relevance | 577 | 28.3 分钟 | 43% | ❌ RPM 10 下要 57.7 分钟 |
| write_plan / section / revise / audit | 43 | 32.1 分钟 | 48% | ❌ 配额 + 单次 100–130 秒占死槽位 |
| summarize | 26 | 2.2 分钟 | 3% | ⚠️ 可以，省一半 |
| probe_outline / outline | 2 | 2.6 分钟 | 4% | ✅ 可以 |
| probe_light / extract | 4 | 1.1 分钟 | 2% | ✅ 可以 |
| **合计** | **652** | **66.5** | | |

**91% 的用时在 Agnes 接不了的环节上。** 它能接的那部分（3.8 分钟）换过去
只能省 1.3 分钟，占总量 2%。

所以这套方案的定位必须修正：

> **不是「分流主力调用」，是「轻量环节的备份 + 故障兜底」。**
> 日常加速的收益只有 2%，别指望它。

而且降级通道有个结构性尴尬：**降级是在主模型出故障时触发的，而主模型出故障
往往伴随调用量激增**——正是配额最紧张的时候。Agnes 救得了「有报告」，
救不了「好报告」。

---

## 一、实测到的 Agnes 事实

四个探针：`probe_agnes.py` / `probe_agnes_thinking.py` / `probe_agnes_rate.py` /
`probe_agnes3_think_budget.py` / `probe_agnes_speed.py`。

### 能用的

| 项 | 结果 |
|---|---|
| `agnes-2.5-flash` / `agnes-3.0-flash` | 都通，1.5–6.6 秒 |
| 严格 JSON | **裸发就能 parse**，`{"city": "北京"}`；`response_format` 也接受 |
| 工具调用 | **能用**，参数完全正确：`read_segments({"segments": ["S1·5","S2·3"]})` |
| Anthropic Messages 格式 | **也通**，标准 `content: [{type:"text"}]` |
| 上下文 | 512K（MiniMax 之外的优势） |
| 最大输出 | 65,536 token（MiniMax 的 16,384 的 4 倍） |
| 价格 | 三项全免 |

### 三个必须写进配置的坑

**1. `max_tokens` 必须 ≥ 4096，否则开思考会返回空。**

```
3.0 开思考 max=2048  →  正文 0 字，reason 2048，text 0，finish=length
3.0 开思考 max=4096  →  正文 724 字，reason 2705，text 450，finish=stop
2.5 开思考 max=2048  →  正文 0 字，reason 2048，text 0，finish=length
2.5 开思考 max=8192  →  正文 858 字，reason 1348，text 523，finish=stop
```

两个模型都会烧光。**这跟 MiniMax 是同一个失效模式**（thinking 吃光预算），
现有的 `retry_on_empty` + 翻倍预算正好能治。但别去赌——直接配够。

**2. JSON 合规率随「开思考」显著变化。**

4 次采样（每种配置 4 个真实任务形态）：

| 配置 | JSON 一次成功 |
|---|---|
| `3.0` + 开思考 | **4/4** |
| `3.0` + 关思考 | 3/4 |
| `2.5` + 关思考 | 3/4 |
| `2.5` + 开思考 | **0/4** |

**2.5 开思考一条都不合格。** 单次采样不足以下定论，但足以说明：
要用 JSON 就开思考，代价是慢 3–4 倍。
（项目已有 `json_repair` 21 例兜底，25% 失败率不至于致命，但没必要主动制造。）

**3. thinking 只有开关，没有档位。**

`reasoning_effort` 的 low / high 的 `out_tok` 都是 21，**完全不分档**。
真正生效的是 `chat_template_kwargs.enable_thinking`（Anthropic 格式下是
`thinking.type`）。**默认是关闭的。**

这意味着**不能照抄 MiniMax 的 T0/T1/T2 分档**——MiniMax 靠 effort 分三档，
Agnes 只能按模型分。

### 速度（真实任务形态，max_tokens 见各任务）

| 任务 | 输入 | 3.0 关 | 3.0 开 | 2.5 关 | 2.5 开 |
|---|---:|---:|---:|---:|---:|
| A summarize 段落概括 | 4,016 字 | **2.11s** | 8.96s | 5.42s | 14.93s |
| B probe_light 歧义判定 | 253 字 | **2.03s** | 18.11s | 2.57s | 3.30s |
| C relevance 长输入判定 | 2,534 字 | 6.71s | 8.01s | **1.51s** | 15.04s |
| D 纯 JSON 输出 | 64 字 | **3.68s** | 7.65s | 5.90s | 4.19s |

**3.0 整体更快。** 2.5 只在 C 上快（单次采样，不当定论）。

**输入越短、开思考越不划算**：B 只有 253 字输入，却烧了 744 token 思考
（text 只有 99）。判歧义这种任务不开思考就够。

---

## 二、推荐搭配

对应 MiniMax 的三档，但**分档维度不同**——Agnes 按模型 + 思考开关分，
不是按 effort 分。

| 档 | 配置 | 用途 | 依据 |
|---|---|---|---|
| **A0** | `agnes-3.0-flash` + `enable_thinking: true` | 需要判断力的轻量环节：`probe_light`、`probe_outline`、`outline` | JSON 4/4 是唯一全对配置；这三个环节本来就是判断题 |
| **A1** | `agnes-3.0-flash` + `enable_thinking: false` | 提取/格式化类：`summarize` | 2.1s vs MiniMax 5.2s，省一半；JSON 3/4 有 `json_repair` 兜 |
| **A2** | `agnes-2.5-flash` + `enable_thinking: true` | **不主动使用**。仅 3.0 不可用时的三级备份 | 开思考 JSON 0/4，不可信 |

**每个环节的 `max_output_tokens` 一律 ≥ 4096**，这是第一条坑的直接后果。

### 逐环节建议

| 环节 | 一轮次数 | 建议 | 理由 |
|---|---:|---|---|
| `probe_light` | 1 | **A0** | 判断题；输入短，开思考反而烧 token，但 JSON 要准 |
| `probe_outline` | 1 | **A0** | 判断题 |
| `outline` | 1 | **A0** | 判断题 |
| `summarize` | 26 | **A1** | 纯提取；2.1s vs 5.2s |
| `extract` | 3 | A1（未实测） | 纯提取 |
| `relevance` | 577 | **不接入** | 577 次 ÷ RPM 10 = 57.7 分钟，替掉 MiniMax 的 28.3 分钟是负优化 |
| `assess` | 0 | 不接入 | 一轮没用上 |
| `write_*` | 43 | **不接入** | 配额不够；且单次 100–130 秒会把并发槽占死 |

---

## 三、并发必须限速

项目现在 `CONCURRENCY = 4`。**在 RPM 10 下，4 并发会在几秒内打满配额。**
所有走 Agnes 的环节必须：

1. 串行执行（`summarize` 26 次 × 2.1s ≈ 55 秒），或
2. 限到并发 2，并**带 429 退避**

实测踩过：一次连打 8+ 个请求立刻 429
（`You've reached the API rate limit for free users`）。
14 秒间隔单发、1.5 秒间隔连发 3 次都正常。

**429 必须当成传输错误处理**（`EmptyOutput` 之外单独一路重试），
不能当内容过滤。

---

## 四、降级路径（架构改动，本方案未实施）

当前架构**没有 provider 切换路径**。`node_plan_spec` 失败就是
`plan_failed` → 直接降级出报告。

要做降级通道需要：

1. `StageConfig` 增加可选的 `fallback`（另一个 provider + model + 参数）
2. `LLMClient` 在主 provider 抛出**可降级错误**时切到 fallback
   - 可降级：传输错误、5xx、EmptyOutput
   - **不可降级**：内容过滤（重发同样内容大概率还是被拦，实测只观测到一次，
     证据不足以决定要不要重试）、鉴权失败
3. 产出必须标注「本环节由降级模型产出」——不然读者不知道质量降级了
4. **只在轻量环节启用降级**，write 阶段不配（配了也跑不动）

这一项是架构改动，按约定 5 先确认方案再改。

---

## 五、实现工作量

| 项 | 工作量 | 风险 |
|---|---|---|
| Agnes provider 适配 | 小。两种 API 格式都通，复用 `_minimax` 的骨架即可；`usage.completion_tokens_details` 分开报 reasoning/text，护栏能直接接 | 低 |
| models.yaml 第二套配置 | 小。分两档 + `max_output_tokens` 调大 | 低 |
| `summarize` 走 A1 | 中。要给 summarize 限速（串行或 2 并发 + 429 退避） | **中**——限流处理不对会拖垮 |
| 降级路径 | 大。动 `LLMClient` 的错误分类 | 高，要先确认 |
| 探针合并成一个 | 小。五个探针有重叠，可以合成 `probe_agnes_all.py` | 低 |

**建议先做前两项**（provider 适配 + 配置），跑通 `probe_light` / `outline`
两个环节验证，再决定要不要动 `summarize` 和降级路径。

---

## 六、诚实的代价

- **日常收益 2%。** 别当成性能优化。
- **降级只覆盖 3–4 个轻量环节。** write 阶段（48% 用时）救不了，
  relevance（43%）也救不了。
- **免费额度和主模型的额度是两回事。** 真出故障时，Agnes 那边
  也可能正在限流。
- **2.5 目前是负资产**：开思考 JSON 0/4 关思考也更慢，
  唯一优势（C 上 1.51s）是单次采样。留着当三级备份可以，
  但别主动分配。
