"""JSON 修复逻辑的回归测试。

这里**直接用 provider 里的实现**，不再抄一份。
早先这里是独立副本，两边各自演化：provider 那边只处理「被 max_tokens 截断」，
测试这边也只测截断，于是尾逗号和行注释两种失误两边都没有用例，一路漏到线上
——探针连续两次拿不到判断，整场研究塌成单节。
"""

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from research_agent.llm.provider import _repair_truncated_json  # noqa: E402


def repair(text: str) -> Any:
    """跑 provider 的真实实现。"""
    return _repair_truncated_json(text)


CASES = [
    ('{"a":1,"b":[1,2,3',              "顶层数组未闭合"),
    ('{"items":[{"a":1},{"b":"未闭合',    "字符串与结构都未闭合"),
    ('{"x":1,"y":2}',                  "本就完整"),
    ('```json\n{"ok":true}\n```',        "带代码围栏"),
    ('{"a":1,"b":',                     "停在键名"),
    ('{"sections":[{"name":"x","w":',    "深层截断"),
    ('[1,2,3',                          "顶层数组"),
    ('{"a":"引号内 } 字符不闭合',         "字符串内有括号字符"),
    ('{"claims":[{"text":"x"}',          "数组元素少一半"),
    ('{"a":[1,2,',                      "尾逗号"),
    ('{"a":1',                          "只差一个花括号"),
    ('{"a":"未闭合字符串',               "只有字符串没闭合"),
    ('{"a":"带 \\" 转义的字符串',         "含转义引号"),
    ('乱码完全不是 json',                 "不可修复"),
    # ---- 非截断失误：尾逗号与行注释 ----
    # 这两种报出来的错和截断几乎一样，曾被一起当截断处理而救不回来，
    # 探针因此连续两次拿不到 depth 判断，整场研究塌成单节。
    ('{"ambiguity":{"is_ambiguous":false,},"depth":{"quality":"thin",}}',
     "对象尾逗号"),
    ('{"a":[],"b":{},}',                  "空容器后的尾逗号"),
    ('{\n  // 先判歧义\n  "depth":{"q":"thin"}\n}', "行注释"),
    ('{"a":1,\n// 备注\n"b":2}',          "行注释夹在中间"),
    ('{"a":[1,2,],}',                     "数组内尾逗号"),
    # ---- 下面两条是反向保护：不能误删字符串里的内容 ----
    ('{"url":"https://x.com/a//b","q":"thin"}', "URL 里的双斜杠"),
    ('{"理由":"见 // 注释 与 https://a.b","q":"thin"}', "中文串里的双斜杠"),
]

if __name__ == "__main__":
    ok = 0
    for text, desc in CASES:
        expect_none = "不可修复" in desc
        got = repair(text)
        good = (got is None) if expect_none else (got is not None)
        ok += good
        mark = "✓" if good else "✗"
        print(f"{mark} {desc:<24} {got}")
    print(f"\n{ok}/{len(CASES)} 通过")
