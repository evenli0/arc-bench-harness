"""执行侧的第三道药：文件级隔离（2026-10-07）。

背景形状：一批卷子共用一个服务器跑，一份卷子留下没还原的状态（REQ-1-3 改完
密码不还原）会成为**同批后面每一份文件**的错误起点 —— 之前那份卷子自己没错，
红却记在它头上。文件级隔离让每份文件开工前，应用和工作区都回到快照原点。

这里钉的判据（不测 Playwright，全部假件）：
    · 多份文件 → 每份一次 server start/stop、runner 一次只收一份文件
    · 卷子工作目录整批只准备一次（reprepare 只对第一份为 True）
    · 每份文件之间工作区还原，污染清单按文件并集上报
    · 单份文件的基建错（收集不到用例）只进 load_errors，其余文件照跑
    · runner 被杀 → 停批、标记 killed（交给调用方既有的减 worker 重试）
    · 单份文件与关闭开关时走原来的整批一次跑（逐字不动）
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402
from acceptance import RunSummary, TestOutcome  # noqa: E402


def _summary(passed, total, error=None, killed=False, file="a.spec.ts"):
    results = [TestOutcome(title=f"t{i}", ok=i < passed,
                           status="passed" if i < passed else "failed",
                           duration_ms=1, file=file) for i in range(total)]
    return RunSummary(passed=passed, total=total, results=results,
                      error=error, killed=killed)


class _Server:
    def __init__(self):
        self.builds = 0
        self.starts = 0
        self.stops = 0

    def build(self):
        self.builds += 1
        return None

    def start(self):
        self.starts += 1
        return None

    def stop(self):
        self.stops += 1


class _Runner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []
        self.reprepares: list[bool] = []
        self.fail_fasts: list[bool] = []

    def run(self, specs, base_url, workers=None, wall_timeout=900, reprepare=True,
            fail_fast=False):
        self.calls.append(list(specs))
        self.reprepares.append(reprepare)
        self.fail_fasts.append(fail_fast)
        return self.outcomes.pop(0)


class _Flow:
    """`run_specs` / `run_specs_isolated` 真正伸手要的那一面。"""

    smoke_port = 4321

    def __init__(self, outcomes, mutated=None):
        self.server = _Server()
        self.runner = _Runner(outcomes)
        self.runtime = types.SimpleNamespace(
            git=types.SimpleNamespace(run=lambda args, check=False: None))
        self._mutated = list(mutated or [])
        self.restores = 0
        self.snapshots = 0

    def app_server(self, grader_like):
        return self.server

    run_specs = M.Flow.run_specs
    run_specs_isolated = M.Flow.run_specs_isolated


@pytest.fixture(autouse=True)
def _isolate_fs(monkeypatch):
    """工作区三件套全部换成假的：这一层测的是调用序，不是 git。"""
    monkeypatch.setattr(M, "snapshot_worktree", lambda git_run: None)
    monkeypatch.setattr(M, "mutated_by_tests",
                        lambda git_run: list(FLOW._mutated.pop(0)) if FLOW._mutated else [])
    monkeypatch.setattr(M, "restore_worktree", lambda git_run: setattr(FLOW, "restores", FLOW.restores + 1))


FLOW: _Flow | None = None


def _flow(monkeypatch, outcomes, mutated=None):
    global FLOW
    FLOW = _Flow(outcomes, mutated)
    return FLOW


def test_a_batch_runs_one_file_per_server_start(monkeypatch):
    flow = _flow(monkeypatch, [_summary(2, 2, file="a.spec.ts"),
                               _summary(1, 2, file="b.spec.ts")])
    summary = flow.run_specs(["a.spec.ts", "b.spec.ts"])
    assert flow.runner.calls == [["a.spec.ts"], ["b.spec.ts"]], "每份文件单独一次"
    assert flow.runner.reprepares == [True, False], "卷子工作目录整批只准备一次"
    assert flow.server.builds == 1, "构建只做一次"
    assert flow.server.starts == 2 and flow.server.stops >= 2, "每份文件一个新服务器"
    assert (summary.passed, summary.total) == (3, 4)
    assert not summary.error


def test_files_start_from_a_restored_worktree(monkeypatch):
    flow = _flow(monkeypatch, [_summary(1, 1), _summary(1, 1)],
                 mutated=[["a.spec.ts"], ["b.spec.ts"]])
    summary = flow.run_specs(["a.spec.ts", "b.spec.ts"])
    assert summary.stores_written == ["a.spec.ts", "b.spec.ts"], "污染清单按文件并集"
    assert flow.restores >= 2, "每份文件之后都还原工作区"


def test_one_broken_file_does_not_sink_the_batch(monkeypatch):
    """收集不到用例（error+0 结果）只进 load_errors —— 其余文件的结果仍然有效。"""
    flow = _flow(monkeypatch, [
        RunSummary(error="Playwright collected 0 tests from broken.spec.ts"),
        _summary(2, 2, file="b.spec.ts")])
    summary = flow.run_specs(["broken.spec.ts", "b.spec.ts"])
    assert summary.error is None, "单份的基建错不是整批的基建错"
    assert any("broken.spec.ts" in e for e in summary.load_errors)
    assert (summary.passed, summary.total) == (2, 2)


def test_a_killed_runner_stops_the_batch(monkeypatch):
    flow = _flow(monkeypatch, [
        _summary(0, 0, error="playwright was killed (rc=-9)", killed=True),
        _summary(1, 1)])
    summary = flow.run_specs(["a.spec.ts", "b.spec.ts"])
    assert summary.killed
    assert summary.error, "被杀要带出原因，调用方据此减 worker 重试"
    assert flow.runner.calls == [["a.spec.ts"]], "被杀之后不再往下跑"


def test_a_single_file_keeps_the_one_shot_path(monkeypatch):
    flow = _flow(monkeypatch, [_summary(1, 1)])
    summary = flow.run_specs(["only.spec.ts"])
    assert flow.runner.calls == [["only.spec.ts"]]
    assert flow.server.starts == 1, "单份文件本来就是隔离的，走原路径"
    assert (summary.passed, summary.total) == (1, 1)


def test_the_isolation_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("OCTOS_ARC_FILE_ISOLATION", "0")
    flow = _flow(monkeypatch, [_summary(2, 2)])
    summary = flow.run_specs(["a.spec.ts", "b.spec.ts"])
    assert flow.runner.calls == [["a.spec.ts", "b.spec.ts"]], "关掉就整批一次跑"
    assert (summary.passed, summary.total) == (2, 2)


def test_fail_fast_rides_into_every_file_and_defaults_off(monkeypatch):
    """#2 判红即停（2026-10-08 提速批）：挂上时逐份文件的测量都带着它；默认
    不挂 —— 净胜/成绩的测量要看得见**每一条**红（少看一条就是假净胜）。"""
    flow = _flow(monkeypatch, [_summary(1, 2, file="a.spec.ts"),
                               _summary(2, 2, file="b.spec.ts")])
    flow.run_specs(["a.spec.ts", "b.spec.ts"], fail_fast=True)
    assert flow.runner.fail_fasts == [True, True], "每一份文件的测量都挂"

    quiet = _flow(monkeypatch, [_summary(2, 2, file="a.spec.ts"),
                                _summary(2, 2, file="b.spec.ts")])
    quiet.run_specs(["a.spec.ts", "b.spec.ts"])
    assert quiet.runner.fail_fasts == [False, False], "默认不挂"
