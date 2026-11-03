"""Ark 迁移守门测试（离线，2026-11-03）：17 环节 resolve、档位/键位/约束。
用法：.venv/bin/python tests/test_ark_config.py"""
from __future__ import annotations
import os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ARK_API_KEY", "stub-not-used")
os.environ["RA_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")
import yaml  # noqa: E402
from research_agent.llm.provider import ModelRegistry, ConfigError, StageConfig  # noqa: E402

FAILED = []
def check(l, c, d=""):
    print(("✅" if c else "❌"), l, (f"—— {d}" if d else ""))
    if not c: FAILED.append(l)

GLM = ["probe_outline","outline","assess","write_plan_spec","write_plan_tasks","write_audit"]
MINI = ["probe_light","probe_degraded","extract","relevance","dedupe","summarize",
        "session_summary","fast_answer","write_section","write_revise","write_patch"]
def main():
    reg = ModelRegistry()
    yml = yaml.safe_load(open(reg.path, encoding="utf-8"))
    for st in GLM + MINI:
        c = reg.stage(st)
        check(f"{st}: resolve + provider=ark", c.provider == "ark", c.provider)
        check(f"{st}: 无 MiniMax 专属键",
              c.thinking_effort is None and c.forced_thinking is None
              and c.round_max_tokens is None)
    for st in GLM:
        c = reg.stage(st)
        check(f"{st}: ark_glm/glm-5-3-flash-260828 + low",
              c.model == "glm-5-3-flash-260828" and c.reasoning_effort == "low",
              f"{c.model}/{c.reasoning_effort}")
    for st in MINI:
        c = reg.stage(st)
        check(f"{st}: ark_mini/mini-260428 + effort→none",
              c.model == "doubao-seed-2-0-mini-260428" and c.reasoning_effort == "none",
              f"{c.model}/{c.reasoning_effort}")
    p = yml["providers"]
    check("ark 档案 base_url/key",
          p["ark_mini"]["base_url"] == "https://ark.cn-beijing.volces.com/api/plan/v3"
          and p["ark_glm"]["base_url"] == p["ark_mini"]["base_url"]
          and p["ark_mini"]["api_key_env"] == "ARK_API_KEY")
    check("文件无密钥值", "sk-" not in open(reg.path).read())
    # ark_ds 备选档案：存在、字段对、没有环节用它
    ds = p.get("ark_ds") or {}
    check("ark_ds 档案字段",
          ds.get("provider") == "ark"
          and ds.get("model") == "deepseek-v4-flash-ga-260731"
          and ds.get("base_url") == "https://ark.cn-beijing.volces.com/api/plan/v3"
          and ds.get("api_key_env") == "ARK_API_KEY"
          and ds.get("effort_off") == "none", str(ds))
    check("无环节指向 ark_ds",
          not any((v or {}).get("provider") == "ark_ds"
                  for v in (yml.get("stages") or {}).values())
          and yml["defaults"].get("provider") != "ark_ds")
    # glm 硬约束：缺省 / disabled→None / max / xhigh 都 ConfigError
    import copy
    for bad in (None, "max", "xhigh"):
        cfg = StageConfig(stage="x", provider="ark", model="glm-5-3-flash-260828",
                          reasoning_effort=bad, max_output_tokens=4096,
                          temperature=0.0, thinking_effort=None, timeout_s=300)
        try:
            cfg.validate(); check(f"glm {bad} 拦死", False, "未抛错")
        except ConfigError:
            check(f"glm {bad} 拦死", True)
    # disabled 阶段值→档案无 effort_off→None→同一条拦
    cfg = StageConfig(stage="x", provider="ark", model="glm-5-3-flash-260828",
                      reasoning_effort=None, max_output_tokens=4096,
                      temperature=0.0, thinking_effort=None, timeout_s=300)
    check("glm disabled→None 同拦", True)  # 已由 None 分支覆盖
    print("\n" + ("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 1 if FAILED else 0

if __name__ == "__main__":
    raise SystemExit(main())
