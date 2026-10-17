"""两件事的回归测试（2026-10-17）：

① **截断检测** `_is_truncated` —— 离线路径，验证它识别哪些形态
② **切回 xj189 后「关思考」是否真的还关着** —— 这是「换个平台静默多烧
   token」那个陷阱的守门测试。

为什么 ② 必须测而不是看配置：两个平台的「关思考」写法**正好相反**
（internai 发 `none`，xj189 不发字段）。配置写对了但翻译没接上，
结果就是 7 个环节**静默打开思考** —— 不报错、测试也照样过。

用法：.venv/bin/python tests/test_platform_switch.py
"""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from research_agent.llm.provider import (  # noqa: E402
    ModelRegistry, SSEAccum, _is_truncated, _parse_json,
)

fails: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    print(f"{'✅' if ok else '❌'} {tag}" + (f"\n   {detail}" if detail else ""))
    if not ok:
        fails.append(tag)


# ─────────────────────────── ① 截断检测 ───────────────────────────
print("=== ① 截断检测 ===")
cases = [
    ("finish_reason=length（最可靠）",
     {"completion_tokens": 500}, "length", 8192, True),
    ("顶满上限、平台不报 finish_reason",
     {"completion_tokens": 8192}, None, 8192, True),
    ("刚好差 1 token", {"completion_tokens": 8191}, None, 8192, False),
    ("正常完成", {"completion_tokens": 3966}, "stop", 8192, False),
    ("usage 没有 completion_tokens",
     {"prompt_tokens": 100}, "stop", 8192, False),
    ("usage 是 None", None, None, 8192, False),
    ("completion_tokens 是 None（xj189 见过 null）",
     {"completion_tokens": None}, None, 8192, False),
    ("completion_tokens 是字符串",
     {"completion_tokens": "8192"}, None, 8192, True),
    ("budget=0 不该误判",
     {"completion_tokens": 99999}, "stop", 0, False),
]
for tag, usage, fr, budget, want in cases:
    got = _is_truncated(usage, fr, budget)
    check(tag, got == want, f"期望 {want}｜实得 {got}")

# SSEAccum 必须真的抓到 finish_reason（流式与非流式形状一致）
acc = SSEAccum()
acc.feed({"choices": [{"delta": {"content": "x"}}]})
mid = acc.finish_reason
acc.feed({"choices": [{"delta": {}, "finish_reason": "length"}]})
check("SSEAccum 抓到 finish_reason",
      mid is None and acc.finish_reason == "length",
      f"中途={mid!r} 结束={acc.finish_reason!r}")

# ── 截断 + 抢救 = 静默丢字段（这就是它要拦的那一幕） ──
half = ('{"subtopics": [{"name": "A", "what": "…"}], '
        '"entities": [{"name": "宁德时代", "type": "org"}], '
        '"jargon": [{"term": "硫化物电解质", "en_equi')
salvaged = _parse_json(half)
check("截断的 JSON 会被抢救成合法对象（所以截断必须另判）",
      isinstance(salvaged, dict),
      f"抢救结果键: {list(salvaged.keys()) if isinstance(salvaged, dict) else salvaged}｜"
      f"⚠️ 能解析 ⇒ 只靠 json 解析失败抓不到它")

# ─────────────────────── ② 切回 xj189 是否还关思考 ───────────────────────
print("\n=== ② effort 语义化：切平台后「关思考」是否还关着 ===")
raw = yaml.safe_load((ROOT / "config" / "models.yaml").read_text(encoding="utf-8"))

# ⚠️ **从配置里读，不要手抄**（2026-10-18 改）。
# 原来是硬编码的 7 个环节名，`summarize` 从 `disabled` 改成 `low` 之后
# 它还留在名单里，于是这一项每次跑都失败 —— 而失败原因是**测试自己过期了**，
# 不是代码坏了。手抄的清单迟早跟不上配置。
raw_stages = raw.get("stages") or {}
OFF_STAGES = [n for n, s in raw_stages.items()
              if (s or {}).get("reasoning_effort") == "disabled"]
assert OFF_STAGES, "配置里一个 `reasoning_effort: disabled` 的环节都没有了 —— " \
                    "那么这段测试要测的「关思考」语义已经无处可测"


def resolve_with(target: str) -> ModelRegistry:
    """把 defaults.provider 换成 target，写临时文件，真跑一遍注册表。"""
    data = copy.deepcopy(raw)
    data.setdefault("defaults", {})["provider"] = target
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)
        tmp = f.name
    return ModelRegistry(tmp)


reg_i = resolve_with("internai")
reg_x = resolve_with("xj189")

for st in OFF_STAGES:
    ei = reg_i.stage(st)
    ex = reg_x.stage(st)
    check(f"{st}: internai→{ei.reasoning_effort!r} / xj189→{ex.reasoning_effort!r}",
          ei.reasoning_effort == "none" and ex.reasoning_effort is None,
          f"internai={ei.reasoning_effort!r}（发 none=关思考）｜"
          f"xj189={ex.reasoning_effort!r}（不发字段=关思考）")

# 反向陷阱：如果环节写死字面量 none，切到 xj189 会怎样
print("\n=== ②b 反向陷阱：写死字面量 none 会发生什么 ===")
data = copy.deepcopy(raw)
for st in OFF_STAGES:
    data["stages"][st]["reasoning_effort"] = "none"      # 绕过语义层
with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                 encoding="utf-8") as f:
    yaml.safe_dump(data, f, allow_unicode=True)
    tmp = f.name
reg_literal = ModelRegistry(tmp).stage("relevance")
check("写死 none 时，在 xj189 上会**保留 none**（= 开思考，静默多烧 token）",
      reg_literal.reasoning_effort == "none",
      f"xj189 侧拿到 {reg_literal.reasoning_effort!r} —— "
      f"xj189 实测 none = 开思考（140~456 字 reasoning），"
      f"这正是语义化要消灭的陷阱")

# 平台档案自检：两个平台的 effort_off 必须不同，否则语义化白做
prof = raw.get("providers") or {}
ei_off = (prof.get("internai") or {}).get("effort_off", "<未声明>")
ex_off = (prof.get("xj189") or {}).get("effort_off", "<未声明>")
check("两平台档案的 effort_off 声明符合实测",
      ei_off == "none" and ex_off == "<未声明>",
      f"internai: {ei_off!r}（实测省略=开思考，要关得显式发 none）｜"
      f"xj189: {ex_off!r}（实测省略=关思考，不声明即不发字段）")

# 单点切换：所有阶段都不该再硬写 provider
hardcoded = [k for k, v in raw["stages"].items() if v.get("provider")]
check("14 个阶段都不再硬写 provider（单点切换生效）",
      not hardcoded,
      f"仍硬写的: {hardcoded or '无'}"
      f"｜defaults.provider = {(raw.get('defaults') or {}).get('provider')!r}")

# ── YAML 布尔陷阱必须被拦住（2026-10-17 真踩过） ──
print("\n=== ②c YAML 布尔陷阱：`reasoning_effort: off` 必须报错而不是静默 ===")
import yaml as _yaml  # noqa: E402
parsed = _yaml.safe_load("v: off\nw: on\nx: yes\nz: no\n")
check("PyYAML 确实把裸 off/on/yes/no 解析成布尔",
      (parsed["v"], parsed["w"], parsed["x"], parsed["z"]) == (False, True, True, False),
      f"解析结果: {parsed} ← 这就是坑的根源")

from research_agent.llm.provider import ConfigError  # noqa: E402


def expect_config_error(tag: str, effort_value) -> bool:
    data = copy.deepcopy(raw)
    data["stages"]["relevance"]["reasoning_effort"] = effort_value
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as f:
        _yaml.safe_dump(data, f, allow_unicode=True)
        tmp = f.name
    try:
        ModelRegistry(tmp).stage("relevance")
    except ConfigError as e:
        print(f"✅ {tag}\n   正确报错：{str(e)[:150]}")
        return True
    print(f"❌ {tag}\n   **没有报错** ⇒ 会静默变成「思考默认打开」")
    return False


ok = expect_config_error("`off`（YAML 解析成 False）被拦下", False)
ok &= expect_config_error("未知档位 `turbo` 被拦下", "turbo")
check("两类非法值都会响（而不是静默多烧 token）", ok)

print("\n" + "=" * 60)
print(f"共跑完，失败 {len(fails)} 项" + ("：" + "、".join(fails) if fails else ""))
sys.exit(1 if fails else 0)
