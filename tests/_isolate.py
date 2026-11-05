"""离线测试隔离：不设 RA_DB_PATH/RA_CKPT_DB 的脚本一 import research_agent，
第一次 get_repo()/CKPT_DB 求值就会指向 data/research.db / data/checkpoints.db，
把 llm_call/会话/checkpoint 写进默认库（TECH_DEBT #2，实测 404→695 条孤儿行）。

用法：测试脚本在 `sys.path.insert(0, str(ROOT))` 之后、
第一个 `import research_agent...` 之前写 `import _isolate`。
`setdefault`：显式设了变量的脚本/外部环境不受影响。
"""
import os
import tempfile

_isodir = tempfile.mkdtemp(prefix="ra_test_iso_")
os.environ.setdefault("RA_DB_PATH", os.path.join(_isodir, "research.db"))
os.environ.setdefault("RA_CKPT_DB", os.path.join(_isodir, "checkpoints.db"))
os.environ.setdefault("RA_INTENT", "0")
os.environ.setdefault("RA_CKPT_SWEEP", "0")
os.environ.setdefault("RA_AUTH", "0")
