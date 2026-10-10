"""自检：测试函数名必须落在 pytest 的收集规则里。

**为什么需要这条。** 内核的 Rust 单测命名很杂 —— 有规范的
`should_<expected>_when_<condition>`，也有大量 `solo_xxx` / `with_workspace_xxx` /
`classify_xxx`。逐字移植这些名字时，凡是不以 `test_` 或 `should_` 开头的函数，
pytest 会**静默跳过**：不报错、不警告，只是少几条。于是「移植了 17 条、全绿」
实际上是「移植了 17 条、跑了 0 条」。

这个坑在写这个项目时真实发生过两次（`test_scope.py` 整份被跳过、
`test_truncate.py` 漏掉一条）。约定本身没错，错在没有人守它 —— 所以守在这里。

规则：`tests/test_*.py` 里的模块级 `def`，名字要么以 `_` 开头（私有辅助函数），
要么以 `test_` 或 `should_` 开头（可收集）。其余一律视为违规。
"""

from __future__ import annotations

import ast
from pathlib import Path

#: 与 `pytest.ini` 的 `python_functions` 保持一致。改一处就要改两处。
COLLECTABLE_PREFIXES = ("test_", "should_")

TESTS_DIR = Path(__file__).resolve().parent


def test_every_module_level_test_function_uses_a_collectable_prefix():
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            name = node.name
            if name.startswith("_"):
                continue  # 私有辅助函数，本就不该被收集
            if not name.startswith(COLLECTABLE_PREFIXES):
                offenders.append(f"{path.name}:{node.lineno} {name}")
    assert not offenders, (
        "这些模块级函数既不是私有辅助函数、也不会被 pytest 收集 —— "
        "它们会**静默不运行**。加 `test_` 前缀（可把 Rust 原名放进 docstring）：\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_actually_has_teeth():
    """守则本身也要有牙：确认判定逻辑能认出违规名字，而不是恒真。"""
    sample = ast.parse(
        "def solo_classify_path_in_workspace(): pass\n"
        "def test_ok(): pass\n"
        "def should_do_thing_when_condition(): pass\n"
        "def _helper(): pass\n"
    )
    verdict = {}
    for node in sample.body:
        if isinstance(node, ast.FunctionDef):
            verdict[node.name] = (
                node.name.startswith("_") or node.name.startswith(COLLECTABLE_PREFIXES)
            )
    assert verdict == {
        "solo_classify_path_in_workspace": False,  # 会被静默跳过 → 违规
        "test_ok": True,
        "should_do_thing_when_condition": True,
        "_helper": True,  # 私有辅助，合法
    }


def test_pytest_ini_still_declares_both_prefixes():
    """`pytest.ini` 与上面的常量必须同步 —— 否则守则守的是错的东西。"""
    ini = (TESTS_DIR.parent / "pytest.ini").read_text(encoding="utf-8")
    for prefix in COLLECTABLE_PREFIXES:
        assert f"{prefix}*" in ini, f"pytest.ini 的 python_functions 缺少 {prefix}*"
