"""RA_MODELS_PATH / RA_CKPT_DB 环境变量开关守门（离线，2026-11-04）。
用法：.venv/bin/python tests/test_env_overrides.py"""
from __future__ import annotations
import os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ARK_API_KEY", "stub-not-used")

FAILED = []
def check(l, c, d=""):
    print(("✅" if c else "❌"), l, (f"—— {d}" if d else ""))
    if not c: FAILED.append(l)

def main():
    # 不设：与现状完全一致
    os.environ.pop("RA_MODELS_PATH", None)
    os.environ.pop("RA_CKPT_DB", None)
    from research_agent.llm.provider import ModelRegistry
    r = ModelRegistry()
    check("不设 RA_MODELS_PATH → config/models.yaml",
          r.path == ROOT / "config" / "models.yaml", str(r.path))
    # runner.CKPT_DB 在 import 时定型——重载模块看两种取值
    import importlib
    import research_agent.graph.runner as R
    importlib.reload(R)
    check("不设 RA_CKPT_DB → data/checkpoints.db",
          R.CKPT_DB == ROOT / "data" / "checkpoints.db", str(R.CKPT_DB))
    # 设置：走环境变量
    tmp = tempfile.mktemp(suffix=".yaml")
    Path(tmp).write_text("defaults: {}\nproviders: {}\nstages: {}\n")
    os.environ["RA_MODELS_PATH"] = tmp
    r2 = ModelRegistry()
    check("设 RA_MODELS_PATH → 用变量", r2.path == Path(tmp), str(r2.path))
    explicit = tempfile.mktemp(suffix=".yaml")
    Path(explicit).write_text("defaults: {}\nproviders: {}\nstages: {}\n")
    check("显式 path 仍优先于 env",
          ModelRegistry(path=explicit).path == Path(explicit))
    ck = tempfile.mktemp(suffix=".db")
    os.environ["RA_CKPT_DB"] = ck
    importlib.reload(R)
    check("设 RA_CKPT_DB → 用变量", R.CKPT_DB == Path(ck), str(R.CKPT_DB))
    os.environ.pop("RA_MODELS_PATH"); os.environ.pop("RA_CKPT_DB")
    importlib.reload(R)
    print("\n" + ("全部通过" if not FAILED else f"失败 {FAILED}"))
    return 1 if FAILED else 0

if __name__ == "__main__":
    raise SystemExit(main())
