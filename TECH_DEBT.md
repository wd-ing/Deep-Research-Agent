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

> **2026-10-27 更新（批 8）：机制层已做，根因未治，真实库尚未清理。**
> 已有：`research_agent/maintenance.py` + CLI `python -m research_agent.maintenance prune-checkpoints`
> （默认 dry-run；`--apply` 只裁剪已完成会话的历史 checkpoint，每个 thread 只留顶层最新一个；
> 孤儿线程需 `--delete-orphans`，孤儿占比>50% 或研究库 session 为 0 时需再加 `--yes-really`；
> `--vacuum` 需停服务）；会话成功完成后 server 自动 `prune_thread`（`RA_CKPT_AUTOPRUNE=0` 可关）。
> 在 /tmp 副本上演练：752MB → 4MB（-99.5%），保留 thread 的最终 state 仍可读。
> **未做**：①对真实 `data/checkpoints.db` 执行清理（不可逆，等用户确认并先备份）；②根因（全文塞进图状态）；
> ③僵尸会话的自动处理（只有 CLI 的 `--stale-days`）。

> **2026-11-04 复查（真跑 v31，关自动裁剪，n=1；详见 CONTEXT 坑 95）——下面「实测到的事实」是 10-17 的旧数字，根因的位置已经变了：**
> 一场标准档 42 个 checkpoint / blob 11.1MB（旧：20~27MB）；`pending_extract` 现在只占 7%；**67% 来自 write 子图 10 个 checkpoint 里重复携带的 `segments`（394KB）+`on_topic`（349KB，是 segments 的子集，文本存了两遍）**。序列化最大状态 ~0.1ms，运行时间影响可忽略；自动裁剪后每个 thread 只剩最终 state（现役库 26 个 thread 共 4.5MB blob）；仅失败/僵尸会话不会被自动裁剪（每个残留 ~12MB）。**结论：建议降级为观察项，暂不治根因**（收益小、要动 write 子图所有读正文的节点与测试夹具）；若要治，窄改法是 `on_topic` 只存 ref（约 -47%），进一步 `segments` 改 id + 节点入口读库。等用户定。

> **自动清理已做（2026-11-05）**：failed/僵尸会话每 6h 由 daemon 线程 `sweep` 清扫（启动 30s 后首扫），只 `keep_latest`，不删孤儿，不 VACUUM（文件体积不缩，只复用页）；进程内正在跑的 sid 走 `_runs` 注册表跳过；`RA_CKPT_SWEEP=0` 关。

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

## 2. ✅ 离线守门测试会污染默认库 `data/research.db`（2026-11-04 已修；遗留 714 条孤儿行未清）

> **2026-11-04 已修**：新增 `tests/_isolate.py`（导入时 `setdefault` 把 `RA_DB_PATH`/`RA_CKPT_DB` 指到 `tempfile.mkdtemp()`），26 个原先不设 `RA_DB_PATH` 的 `tests/test_*.py` 在第一个 `import research_agent` 之前 `import _isolate`（显式设了环境变量的脚本/外部覆盖不受影响）。**验证**：不设两个环境变量把全部 44 个 `test_*.py` 跑一遍，退出码全 0，`data/research.db` 19 张表逐表行数与 `data/checkpoints.db` 字节数前后完全一致。**未做**：默认库里已有的孤儿 `llm_call`（现 714 条：2026-10-17 实测 404，后续又涨；其中约 19 条是本次修复过程里改测试时未隔离的跑法写的）没有删——删行属破坏性操作，也不影响正确性（统计口径都带 `where session_id=?`），等用户定。**新增测试脚本时必须带 `import _isolate`**，否则会复发。

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

**修法（已做，见上方 2026-11-04 更新）**：守门测试统一把 `RA_DB_PATH` 指到临时库。

---

## 3. ✅ `pyproject.toml` 声明的 `ra` 入口点不存在（2026-11-04 已删）

> 已处置：全仓 grep 无任何 `research_agent.cli` / `ra` 命令引用，删掉 `[project.scripts]` 段。以后若真要 CLI 再补，别先声明。

```toml
[project.scripts]
ra = "research_agent.cli:main"
```

而 `research_agent/cli.py` **没有这个文件**。当前没人用这个入口，所以从没暴露。

**两条路**：要么补一个 CLI，要么把这行删掉。
**别留着** —— 「声明了但不存在」正是 `CONTEXT.md` 坑 36（删除只做了一半，
提示词还在承诺已经没有的功能）的同一形态。

---

## 4. ✅ `providers.opencode` 档案缺 `model` 字段（2026-11-04 已删档案）

> 已处置：确认没有任何 stage/测试引用该档案后，删除 `config/models.yaml` 的 `providers.opencode` 档案与 `alternatives:` 里三条 blocked 免费模型；provider 代码里的 `_opencode` 方法名与 `OPENAI_COMPAT` 里的 `"opencode"` 字符串是 OpenAI 兼容协议的**实现名**，故意没动（改名是纯重构，收益小）。

`config/models.yaml` 的 `opencode` 档案只有 `provider` / `base_url` /
`api_key_env`，**没有 `model`**。选中它会直接失败。

另见 `alternatives:` 里三个 `status: blocked` 的 OpenCode 免费模型
（直连 403 FreeTierError）—— 这个 provider 实际已经不可用，
**档案留着是误导**。要么补全，要么连同那三条一起清掉。

---

## 5. ✅ `research_agent/docs/` 是个空包（2026-11-04 已删）

> 已处置：确认零 import、`pyproject` 的 hatch `packages=["research_agent"]` 不依赖，`git rm -r`；`import research_agent.server` 正常。

只有一个 0 字节的 `__init__.py`。没有任何代码 import 它。

---

## 6. 🟡 参考库不在 git 里（已有本机备份，未做异地）

`data/refs/` 下的 6 份参考库是**不可再生的资产**（重造一份 25~40 分钟 +
几十万 token），而 `.gitignore` 同时排除 `data/` 和 `*.db`。

2026-10-17 把它们从 `/tmp` 搬进来，只解决了「`/tmp` 被清」，
**没解决「`data/` 被删」**。详见 `CONTEXT.md` 第四节「参考库」。

**2026-11-04 重打（41 个库，integrity 全 ok）**：推荐包 `~/Documents/deep-research-refs-backup-20261103-pure.tar.gz`（纯 .db、无侧车，50,049,763B，sha256 `c8355b42…b30264`）；同目录另有带空侧车的 `…20261103.tar.gz`（覆盖了上一版，内容为超集，详见 CONTEXT 坑 97 后「备份已重打」）。仍是同盘备份，非异地。

**（历史）2026-11-03 已做一次仓库外备份**：`~/Documents/deep-research-refs-backup-20261103.tar.gz`（40MB，34 个库含 v27–v30 Ark 对照库，用 `sqlite3 .backup` 取一致快照、`integrity_check` 全部 ok，sha256 `da78c151…d16e6`；同名文件已覆盖旧版）。仍在同一块磁盘上——**不是异地备份**；`data/refs/` 新增/变更后要重打。待定：是否再拷一份到网盘/外置盘。

---

## 7. ✅ `config/search.yaml` 没有任何代码加载，且值已和真值矛盾（2026-11-04 用户决定删除，已删）

> 已处置：`git rm config/search.yaml`；设计依据留存于 `CONTEXT.md` 坑 61 末尾，原文可 `git show acccd62:config/search.yaml` 取回，`tavily.py` docstring 已内联禁用 /research 端点的理由。以下为历史记录。

**2026-10-17 阶段 0 排查发现**（详见 `CONTEXT.md` 坑 61）：后端唯一的
`yaml.safe_load` 读的是 `models.yaml`，`search.yaml` 是纯文档。
它的 `loop.max_rounds: 8` / `loop.timeout_minutes: 20` /
`chunking.max_tokens: 500` 都和代码里写死的值（4 / 30 / 2000）矛盾，
`loop.max_tokens_per_session` 对应的判据已被删除。

已做的处置：文件头加了显眼警告，标为「文档、不生效」。**2026-11-04 复核结论：不属于「高把握的小问题」——接上与删除是二选一的产品决定，故这一档不动，留给用户定**（现状无害：文件头警告在，没有代码读它）。

**待定（动手前要定的）**：接上（改成真正被加载、代码默认值改从它读）
还是删掉（设计说明挪进 `CONTEXT.md`）。接上的话要先核对每条配置项
对应的代码读法 —— 否则只是把一个说谎的文件换成另一种说法。

---

## 关联文档

- `CONTEXT.md` 第十节「已知缺口」—— **研究层面**的未验项（病因未知、质量未评估等），
  和本文的工程欠账是两回事，别混。
- `CONTEXT.md` 第六节「已知地雷」—— 那些是**会咬人的现状**，不是债：
  它们不打算修，是必须记住的约束。
