# TECH_DEBT · 技术债

> 这份文档记**已经知道、但有意押后**的债。
>
> 和 `CONTEXT.md` 的分工：
> - `CONTEXT.md` = 架构 / 仍然有效的决策 / 踩过的坑 / **研究层面的已知缺口**（第十节）
> - 本文 = **工程层面的欠账**：知道它坏在哪、知道怎么修、但**现在故意不修**
>
> 每条必须写清三件事：**实测到的事实**、**根因**、**动手前要先定的东西**。
> 只写「这里以后要优化」而不写依据的条目，等于没写 —— 那正是
> `DECISIONS.md` / `HANDOVER.md` 被删掉的原因。

---

## 1. 🔴 `checkpoints.db` 无清理机制（押后到上线前）

**决定（用户，2026-10-17）**：等项目优化得差不多了再统一清理，**上线前定清理机制**。
在那之前不动它 —— 它现在不影响正确性，只占磁盘。

### 实测到的事实

`data/checkpoints.db` 当前 **302.1 MB**（73,745 页 × 4,096 B），另有 5.4 MB WAL。

| 表 | 行数 | blob 字节 |
|---|---|---|
| `checkpoints` | 1,111 | **240.6 MB** |
| `writes` | 4,954 | 57.8 MB |

里面是 **34 个 thread（= 34 场研究）**，单场最大 27.5 MB：

```
s_d72dffee0257   27.5 MB / 50 个 checkpoint（平均每个 0.55 MB）
s_f1a69ee4b319   25.6 MB / 73 个 checkpoint
s_702124d34f9d   22.1 MB / 59 个 checkpoint
```

⇒ **一场研究约 20~27 MB**。单个 checkpoint blob 最大 2.12 MB。

### 根因：全文被塞进图状态，然后每个 checkpoint 重新序列化一遍

按 state 通道统计 `writes` 表的字节：

| 通道 | 行数 | 字节 | 装的是什么 |
|---|---|---|---|
| `pending_extract` | 156 | **24.0 MB** | **整页正文 + 每个分块的正文（同一份文本存了两遍）** |
| `segments` | 15 | 7.8 MB | WriteState 里的全部段落正文 |
| `batches` | 73 | 7.1 MB | ExtractBatchState 的批次（同样含正文） |
| `on_topic` | 15 | 5.6 MB | 过闸门的段落正文 |
| `report_markdown` | 39 | 2.3 MB | 报告（每次写都是全文快照）|

`node_retrieve` 往 `pending_extract` 里放的是
（`nodes.py:580-597`）：

```python
pending.append({"source_id": …, "title": …, "url": …, "published": …,
                "text": p.text})          # ← 整页正文
…
it["segments"] = segments                  # ← 每个分块的正文，又一遍
```

而 LangGraph **每次节点转移都把整个 state 序列化一次**。`pending_extract`
在被清空前要跨好几个节点，于是同一批正文被反复写进 checkpoint ——
**单场内近似 O(节点数 × 正文量)**。

⚠️ **关键点：这些正文本来就已经在 SQLite 里了**（`repo.add_chunk` 存了
`content`），state 里这一份是**纯冗余**。按 id 传、让下游自己读库，
这一项就基本消失。

### 动手前要先定的东西

1. **清理机制的语义**：按会话删（删了就不能续跑）/ 按时间留窗口 /
   只留每个 thread 最后 N 个 checkpoint。三者对「研究跑一半能恢复」
   这个能力的损害完全不同，而那是 checkpointer 存在的唯一理由。
2. **先治根因还是先治症状**：把全文从 state 里拿掉（治根因）会顺带
   减小序列化开销；只做定期 VACUUM/删除（治症状）治标不治本。
   **建议先治根因** —— 它同时属于「成本优化」方向。
3. **两个图共写一个库**（`get_graph()` 同步 / `get_agraph()` 异步，
   见 `CONTEXT.md` 第七节）。清理动作必须考虑有没有进程正持有它。
4. `data/` 整个被 `.gitignore` 排除，删错了没有 git 可回滚。

---

## 2. 🟡 离线守门测试会污染默认库 `data/research.db`

**2026-10-17 实测**（这是查「僵尸 server 往哪写」时顺带发现的，
一开始归错因了，见下）：

```
data/research.db 的 llm_call：跑 tests/test_plan_tasks_forced.py 之前 401 条
                              → 跑一次 → 404 条（+3）
```

`tests/test_plan_tasks_forced.py` **不设 `RA_DB_PATH`**，于是落到默认的
`data/research.db`。该库现在是 **0 个 session 配 404 条 `llm_call`** 的孤儿状态。

⚠️ **归因更正**：我最初说这些孤儿记录是那个跑了 21 小时的 uvicorn 写的
（因为 `ps` 的命令行里看不到 `RA_DB_PATH`）。**错了** —— 环境变量压根不出现在
`ps` 输出里。`lsof` 看它真正打开的文件，是 `/tmp/fe_test.db`。
**判断「进程在写哪个库」要看 `lsof`，不要看 `ps`。**

**影响**：不致命（统计口径都带 `where session_id=?`，见 `CONTEXT.md` 第六节），
但它是个真实污染源，且让默认库单向变大。

**修法（未做）**：守门测试统一在文件头把 `RA_DB_PATH` 指到临时库。
`tests/test_summarize_retry.py` 已经这么做了（`tempfile.gettempdir()`），
可以照抄。**属于结构性改动（动的是全部守门测试的前置），按约定先说方案。**

---

## 3. 🟢 `pyproject.toml` 声明的 `ra` 入口点不存在

```toml
[project.scripts]
ra = "research_agent.cli:main"
```

而 `research_agent/cli.py` **没有这个文件**。当前没人用这个入口，所以从没暴露。

**两条路**：要么补一个 CLI，要么把这行删掉。
**别留着** —— 「声明了但不存在」正是 `CONTEXT.md` 坑 36（删除只做了一半，
提示词还在承诺已经没有的功能）的同一形态。

---

## 4. 🟢 `providers.opencode` 档案缺 `model` 字段

`config/models.yaml` 的 `opencode` 档案只有 `provider` / `base_url` /
`api_key_env`，**没有 `model`**。选中它会直接失败。

另见 `alternatives:` 里三个 `status: blocked` 的 OpenCode 免费模型
（直连 403 FreeTierError）—— 这个 provider 实际已经不可用，
**档案留着是误导**。要么补全，要么连同那三条一起清掉。

---

## 5. 🟢 `research_agent/docs/` 是个空包

只有一个 0 字节的 `__init__.py`。没有任何代码 import 它。

---

## 6. 🟢 参考库不在 git 里

`data/refs/` 下的 6 份参考库是**不可再生的资产**（重造一份 25~40 分钟 +
几十万 token），而 `.gitignore` 同时排除 `data/` 和 `*.db`。

2026-10-17 把它们从 `/tmp` 搬进来，只解决了「`/tmp` 被清」，
**没解决「`data/` 被删」**。详见 `CONTEXT.md` 第四节「参考库」。

**待定**：要不要做仓库外备份，以及备份到哪。

---

## 关联文档

- `CONTEXT.md` 第十节「已知缺口」—— **研究层面**的未验项（病因未知、质量未评估等），
  和本文的工程欠账是两回事，别混。
- `CONTEXT.md` 第六节「已知地雷」—— 那些是**会咬人的现状**，不是债：
  它们不打算修，是必须记住的约束。
