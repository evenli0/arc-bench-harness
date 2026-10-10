"""自产卷子的第一道体检：**它们能跑吗**（只收集，不执行）。

为什么官方卷子不需要这一道而自产卷子需要：官方卷子的文件名和内容是既定的、
跑了十几轮没变过；自产卷子是刚写出来的 TypeScript，**没有任何人编译过它**。

语法错、import 错、用了不存在的 API —— 会让整个文件一条用例都收集不到，
而收集失败在 `_accept` 里的下场是 `summary.error` → 「spec run failed」：
不修复、不回滚、节点直接判负。**同一个错误还会在每个节点上重演**，因为每个
节点都要跑自己那几份卷子 —— 二十几个节点各自失败一次，看起来像模型不行。

所以这一趟买的是「错误信息里有坏卷子的名字」，而不是「判据变松」：
坏卷子仍然会让节点失败（那是诚实的）。
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import acceptance as A  # noqa: E402


def _runner(tmp_path):
    runner = A.AcceptanceRunner(root=tmp_path / "pw", tests_dir=tmp_path / "tests",
                                work_dir=tmp_path / "work", log=lambda _: None)
    runner._prepare = lambda workers=None: tmp_path / "work" / "playwright.config.ts"
    return runner


def _fake_run(returncode=0, stdout="", stderr=""):
    def run(cmd, **kwargs):
        run.cmd = cmd
        run.kwargs = kwargs
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return run


def test_a_clean_suite_reports_how_many_tests_it_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(A.subprocess, "run",
                        _fake_run(stdout="Listing tests:\n  a.spec.ts:3:1 › creates one\n"
                                          "  a.spec.ts:9:1 › opens one\n\n"
                                          "  Total: 2 tests in 1 file\n"))
    total, error = _runner(tmp_path).collect(["a.spec.ts"])
    assert (total, error) == (2, "")


def test_ansi_is_stripped_from_the_error_text(tmp_path, monkeypatch):
    """Playwright 的输出带 ANSI 颜色码，直接进日志是一堆 `\\x1b[31m`。"""
    monkeypatch.setattr(A.subprocess, "run",
                        _fake_run(returncode=1, stderr="\x1b[31mError\x1b[0m: a.spec.ts:1:1 - "
                                                       "Unexpected token"))
    total, error = _runner(tmp_path).collect(["a.spec.ts"])
    assert total == 0
    assert "\x1b" not in error
    assert "Unexpected token" in error


def test_a_file_that_does_not_collect_is_reported_as_an_error(tmp_path, monkeypatch):
    """**这是这一趟存在的理由。**

    一个语法错的卷子收集不到任何用例。发现它的时机从「第一个验收它的节点」
    提前到「出卷刚结束」—— 前者已经吃掉了一个节点和它的一轮修复，而且会在
    每个节点上重演。
    """
    monkeypatch.setattr(A.subprocess, "run",
                        _fake_run(returncode=1, stderr="Error: a.spec.ts:3:20 - error TS2304: "
                                                       "Cannot find name 'exepct'."))
    total, error = _runner(tmp_path).collect(["a.spec.ts"])
    assert total == 0
    assert "Cannot find name" in error


def test_a_timeout_is_an_error_not_an_exception(tmp_path, monkeypatch):
    """体检本身绝不能打断一轮 —— 超时是「没测成」，不是「炸了」。"""
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 300)

    monkeypatch.setattr(A.subprocess, "run", run)
    total, error = _runner(tmp_path).collect(["a.spec.ts"])
    assert total == 0
    assert "exceeded" in error


def test_playwright_failing_to_start_is_an_error_not_an_exception(tmp_path, monkeypatch):
    def run(cmd, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(A.subprocess, "run", run)
    total, error = _runner(tmp_path).collect(["a.spec.ts"])
    assert total == 0
    assert "could not start" in error


def test_a_reporter_that_changed_its_wording_still_yields_a_count(tmp_path, monkeypatch):
    """退出码 0 但没解析出 `Total:` 时，退回去数 `›` 行（list reporter 的前缀）。

    这条是防「Playwright 换了个说法，我们就把 40 份卷子读成 0 份」——
    读成 0 份会让 `smoke_check_authored_suite` 喊一句假警报。
    """
    monkeypatch.setattr(A.subprocess, "run",
                        _fake_run(stdout="  a.spec.ts:3:1 › creates one\n"
                                         "  a.spec.ts:9:1 › opens one\n"))
    total, error = _runner(tmp_path).collect(["a.spec.ts"])
    assert (total, error) == (2, "")


def test_collecting_nothing_is_not_an_error(tmp_path, monkeypatch):
    """本函数只负责**观察** —— 「0 个用例算不算问题」的判据留在 `main.py`。"""
    called = []
    monkeypatch.setattr(A.subprocess, "run", lambda cmd, **kw: called.append(cmd))
    assert _runner(tmp_path).collect([]) == (0, "")
    assert called == [], "没有卷子就不该起 playwright"


@pytest.mark.skipif(sys.platform != "win32", reason="路径形状是平台相关的")
def test_windows_passes_relative_paths(tmp_path, monkeypatch):
    """**Windows 上必须传相对路径。**

    这条钉的是一个具体的坑：绝对路径里带盘符（`C:\\...`），Playwright 的
    过滤器按子串匹配 `testPathPattern`，盘符和反斜杠会让它一条都对不上 ——
    而那种失败看起来是「这些卷子一个用例都没有」，不是「路径写错了」。
    """
    run = _fake_run(stdout="Total: 1 test in 1 file\n")
    monkeypatch.setattr(A.subprocess, "run", run)
    _runner(tmp_path).collect(["sub/a.spec.ts"])
    passed = [a for a in run.cmd if a.endswith(".spec.ts")]
    assert passed == ["tests/sub/a.spec.ts"], f"传的不是相对路径：{passed}"
