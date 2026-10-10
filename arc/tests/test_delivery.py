"""交付保底三闸的测试：快照、成绩卡、封箱。

这三样存在的理由只有一个：**预算用光/被掐停的那一刻，交付物必须是整个 run
量到过的最好版本**，而不是盘上恰好剩下的版本。它们的失效方式也同一个形状 ——
不报错，只是悄悄交出更差的树。所以这里钉的不是「跑得通」，是三个判据：

  · 快照        未提交改动+新建文件都在提交对象里；快照失败返回 None（绝不误退）
  · 成绩卡      严格更好才登记；登记时快照钉住被测的那棵树；落盘+ref 双保险
  · 封箱        量过的树才配留在盘上；量不动/更差 → 退回成绩卡；提交说明写明成绩

宿主对象是自己搭的（`Host`），但被测的是 `main.Flow` 的真方法 —— 不是复刻。
"""

import subprocess
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import main as M
from acceptance import RunSummary
from acceptance import TestOutcome


def _summary(passed, total=None, error=None, results=None):
    if results is None:
        results = [TestOutcome(title=f"t{i}", ok=i < passed, status="passed" if i < passed else "failed",
                               duration_ms=1, file="a.spec.ts") for i in range(total or passed)]
    return RunSummary(passed=passed, total=total if total is not None else passed,
                      results=results, error=error)


class Host:
    """`Flow` 三个方法真正伸手要的那一面。多一个都不给。"""

    def __init__(self, tmp_path, specs=("a.spec.ts",)):
        self.output_dir = tmp_path
        self.tests_dir = tmp_path / "tests"
        self.tests_dir.mkdir(exist_ok=True)
        for s in specs:
            (self.tests_dir / s).write_text("// spec", encoding="utf-8")
        self.spec_map = {"REQ-1": [f"tests/{s}" for s in specs]}
        self.runner = object()
        self.mem_limit = None
        self.remaining_seconds = 100000.0

        self._head = "head0"
        self._best_state = None
        self._best_summary = None
        self._best_grouped = None
        self._last_full_measure = None
        self._tree_maybe_edited = True

        self.commits = []
        self.restores = []
        self.git_calls = []
        self.recorded = []
        self.runs = []
        self.spec_results = []  # 每次 run_specs 返回一个 summary，弹一个
        self.snapshot_result = "snap1"
        self.parents = {}       # 快照 sha → 父提交（`rev-parse <sha>^` 的回答）

        self.runtime = types.SimpleNamespace(git=types.SimpleNamespace(run=self._git_run))

    def _git_run(self, args, check=True):
        self.git_calls.append(list(args))
        if args[:1] == ["rev-parse"] and str(args[1]).endswith("^"):
            parent = self.parents.get(str(args[1])[:-1], "")
            return SimpleNamespace(stdout=parent + ("\n" if parent else ""),
                                   returncode=0 if parent else 128)
        return SimpleNamespace(stdout="", returncode=0)

    # -- Flow 用到的那一面 --
    def head(self):
        return self._head

    def commit(self, message):
        self.commits.append(message)
        self._head = f"sha{len(self.commits)}"
        return True

    def restore_app(self, sha):
        self.restores.append(sha)

    def record_full_suite(self, summary, grouped):
        self.recorded.append((summary, grouped))

    def run_specs(self, specs, workers=None, grader_like=False, fail_fast=False):
        self.runs.append((list(specs), workers, grader_like))
        return self.spec_results.pop(0) if self.spec_results else _summary(0, 0)

    def remaining(self):
        return self.remaining_seconds

    # -- 被测的真方法 --
    def bind(self, *names):
        for name in names:
            setattr(self, name, types.MethodType(getattr(M.Flow, name), self))

    def snapshot_state(self, note="snapshot"):
        self.git_calls.append(["<snapshot>", note])
        return self.snapshot_result


# ---------------------------------------------------------------- 快照

class RealGit:
    """真 shell 出去跑 git —— 快照这一条必须用真仓库验，假的验不了 tree。"""

    def __init__(self, cwd):
        self.cwd = cwd

    def run(self, args, check=True):
        import os
        env = os.environ.copy()
        env.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        p = subprocess.run(["git", *args], cwd=self.cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", check=False, env=env)
        if check and p.returncode != 0:
            raise RuntimeError(p.stderr)
        return SimpleNamespace(stdout=p.stdout, returncode=p.returncode, stderr=p.stderr)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "frontend").mkdir(parents=True)
    (repo / "frontend" / "app.js").write_text("v1\n", encoding="utf-8")
    git = RealGit(repo)
    git.run(["init"])
    git.run(["add", "-A"])
    git.run(["commit", "-m", "init"], check=False)
    return repo, git


def test_snapshot_pins_worktree_including_untracked(tmp_path):
    """快照的判据：未提交改动和新建文件都进提交对象，HEAD 不动。"""
    repo, git = _repo(tmp_path)
    (repo / "frontend" / "app.js").write_text("v2-uncommitted\n", encoding="utf-8")
    (repo / "frontend" / "new.js").write_text("brand new\n", encoding="utf-8")

    host = Host(tmp_path)
    host.runtime = types.SimpleNamespace(git=git)
    host.bind("snapshot_state")

    head_before = git.run(["rev-parse", "HEAD"]).stdout.strip()
    host._head = head_before  # 宿主返回真 head —— 快照的 parent 必须是真对象
    sha = host.snapshot_state("unit test")
    assert sha, "a real repo must produce a snapshot sha"
    assert git.run(["rev-parse", "HEAD"]).stdout.strip() == head_before, "HEAD must not move"

    # 未提交的改动在快照里 —— 这正是「退回这一刀」要的东西。
    assert git.run(["show", f"{sha}:frontend/app.js"]).stdout == "v2-uncommitted\n"
    # 新建（未跟踪）文件也在 —— 改了一半留下的新文件同样得能退掉。
    tree = git.run(["ls-tree", "-r", "--name-only", sha]).stdout
    assert "frontend/new.js" in tree
    # 快照挂在 refs/arc/last-snapshot 上，防 gc。
    assert git.run(["rev-parse", "refs/arc/last-snapshot"]).stdout.strip() == sha


def test_snapshot_returns_none_when_git_fails(tmp_path):
    """快照失败必须返回 None：调用方据此**不退回**（退回才是灾难）。"""
    host = Host(tmp_path)
    host.runtime = types.SimpleNamespace(
        git=types.SimpleNamespace(run=lambda args, check=True: SimpleNamespace(stdout="", returncode=128)))
    host.bind("snapshot_state")
    assert host.snapshot_state("will fail") is None


# ---------------------------------------------------------------- 成绩卡

def test_record_best_only_on_strict_improvement(tmp_path):
    host = Host(tmp_path)
    host.bind("record_best")
    host.snapshot_result = "snap-good"

    sha = host.record_best(_summary(10, 30), "round 1")
    assert sha == "snap-good"
    assert host._best_state["passed"] == 10

    assert host.record_best(_summary(9, 30), "round 2") is None, "worse must not overwrite"
    assert host._best_state["passed"] == 10

    assert host.record_best(_summary(10, 30), "round 3") is None, "tie must not re-register"

    host.snapshot_result = "snap-better"
    assert host.record_best(_summary(12, 30), "round 4") == "snap-better"


def test_record_best_carpers_card_on_disk_and_ref(tmp_path):
    """卡片落盘（含恢复命令）并钉 refs/arc/best —— 进程被杀后的两条恢复线索。"""
    host = Host(tmp_path)
    host.bind("record_best")
    host.snapshot_result = "snapX"
    assert host.record_best(_summary(7, 9), "unit") == "snapX"

    import json
    card = json.loads((tmp_path / ".arc" / "best_state.json").read_text(encoding="utf-8"))
    assert (card["passed"], card["total"], card["sha"]) == (7, 9, "snapX")
    assert "checkout snapX" in card["recover"]
    assert ["update-ref", "refs/arc/best", "snapX"] in host.git_calls


@pytest.mark.parametrize("summary", [
    _summary(0, 3, error="app did not start"),
    RunSummary(passed=0, total=0),
])
def test_record_best_skips_broken_measurements(tmp_path, summary):
    """没跑起来/零结果的测量不配进成绩卡 —— 卡片是交付锚点，不是日志。"""
    host = Host(tmp_path)
    host.bind("record_best")
    assert host.record_best(summary, "broken") is None
    assert host._best_state is None


# ---------------------------------------------------------------- 封箱

def _sealed_host(tmp_path, card_passed=5, card_sha="cardsha"):
    host = Host(tmp_path)
    host.bind("seal_delivery", "record_best", "snapshot_is_current")
    host.snapshot_result = "snap-measure"
    if card_passed is not None:
        host._best_state = {"passed": card_passed, "total": 30, "sha": card_sha, "recover": f"checkout {card_sha}"}
        host._best_summary = _summary(card_passed, 30)
        host._best_grouped = {}
        host.parents = {card_sha: host._head}   # 默认：卡片正钉在当前 HEAD 上（新鲜）
    return host


def test_seal_measures_dirty_tree_and_keeps_it_when_better(tmp_path, monkeypatch):
    """自上次量过后动过手 → 先量当前树；更好 → 当前树就是交付物。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    host = _sealed_host(tmp_path)
    host._tree_maybe_edited = True
    host.spec_results = [_summary(8, 30)]

    host.seal_delivery()

    assert host.runs, "a dirty tree must be measured before delivery"
    assert host.runs[0][0] == ["a.spec.ts"]
    assert host.runs[0][2] is True, "the seal must measure grader-like"
    assert host._tree_maybe_edited is False
    assert host._last_full_measure == {"passed": 8, "total": 30}
    assert host._best_state["passed"] == 8
    assert any(c.startswith("chore: final measurement 8/30") for c in host.commits)
    assert host.commits[-1] == "chore: final delivery: best verified state 8/30"
    assert host.restores == [], "a measured-better tree must not be rolled back"


def test_seal_restores_card_when_measurement_is_worse(tmp_path, monkeypatch):
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    host = _sealed_host(tmp_path, card_passed=5)
    host._tree_maybe_edited = True
    host.spec_results = [_summary(3, 30)]

    host.seal_delivery()

    assert host.restores == ["cardsha"], "a worse-than-card tree must be rolled back"
    assert host._best_state["passed"] == 5, "the card must survive a bad measurement"
    assert host.commits[-1] == "chore: final delivery: best verified state 5/30"
    assert host.recorded, "verdicts must be re-recorded for the restored tree"


def test_seal_retries_halved_workers_after_kill(tmp_path, monkeypatch):
    """显式提速后全卷被杀（OOM）→ 减半 worker 重试一次；再失败就退回卡片。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    monkeypatch.setenv("OCTOS_ARC_FINAL_WORKERS", "4")
    host = _sealed_host(tmp_path, card_passed=5)
    host.spec_results = [_summary(0, 0, error="runner killed: oom", results=[]),
                         _summary(6, 30)]
    host.spec_results[0].killed = True

    host.seal_delivery()

    assert len(host.runs) == 2
    assert [w for _, w, _ in host.runs] == [4, 2], "the retry must use no more workers than the first try"
    assert host._best_state["passed"] == 6


def test_seal_at_the_grader_count_has_nothing_to_halve(tmp_path, monkeypatch):
    """默认口径（判分同款 workers=1）被杀 → 不空转重试，直接退回成绩卡。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    monkeypatch.delenv("OCTOS_ARC_FINAL_WORKERS", raising=False)
    host = _sealed_host(tmp_path, card_passed=5)
    killed = _summary(0, 0, error="runner killed: oom", results=[])
    killed.killed = True
    host.spec_results = [killed]

    host.seal_delivery()

    assert [w for _, w, _ in host.runs] == [1], "已经在判分口径的地板上，重试同一个数没有意义"
    assert host.restores == ["cardsha"], "量不动 → 交付的是成绩卡那一棵树"
    assert host.commits[-1] == "chore: final delivery: best verified state 5/30"


def test_seal_never_measures_when_not_allowed(tmp_path, monkeypatch):
    """异常路径（allow_measure=False）：只退快照+提交，几秒级，不跑卷。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    host = _sealed_host(tmp_path, card_passed=5)
    host._tree_maybe_edited = True

    host.seal_delivery(allow_measure=False)

    assert host.runs == [], "the abort path must not spend minutes on a suite run"
    assert host.restores == ["cardsha"]
    assert host.commits[-1] == "chore: final delivery: best verified state 5/30"


def test_seal_skips_clean_tree_without_measurement(tmp_path, monkeypatch):
    """没动过手（且上次测量就是卡上成绩）→ 一次多余的全卷都不跑。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    host = _sealed_host(tmp_path, card_passed=5, card_sha="cardsha")
    host._tree_maybe_edited = False
    host._last_full_measure = {"passed": 5, "total": 30}

    host.seal_delivery()

    assert host.runs == []
    assert host.restores == []
    assert host.commits[-1] == "chore: final delivery: best verified state 5/30"


def test_seal_without_card_delivers_as_is(tmp_path, monkeypatch):
    """一次全卷都没量成过（没有卡片）→ 交as-is，但提交说明不吹牛。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    host = _sealed_host(tmp_path, card_passed=None)
    host._tree_maybe_edited = True

    host.seal_delivery()

    assert host.restores == []
    assert host.commits[-1] == "chore: final delivery (no verified state on record)"


def test_seal_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOS_SEAL_DELIVERY", "0")
    host = _sealed_host(tmp_path)
    host.seal_delivery()
    assert host.commits == [] and host.runs == [] and host.restores == []


def test_seal_runs_before_postflight_removes_playwright():
    """封箱的「先量一次」必须发生在 postflight 之前 —— postflight 会把私有
    装的 Playwright 删掉给评分器腾内存，删了之后封箱就再也量不动，脏树只能
    直接退回旧快照（最后一段时间的改进被静默丢掉）。

    真机实测（2026-10-06 冒烟）：封箱写在 `finally: postflight()` 之后时，
    日志是「removed private Playwright install → sealing: could not run
    (WinError 2)」。这条钉的是两者的先后，不是各自的实现。
    """
    import inspect
    src = inspect.getsource(M.Flow.run)
    seal = src.index("self.seal_delivery()")
    postflight = src.index("self.postflight()")
    assert seal < postflight, "seal_delivery 必须在 postflight 之前调用（那时 Playwright 还在）"


def test_seal_budget_too_low_skips_measurement_but_still_restores(tmp_path, monkeypatch):
    """不够量一次（默认 600s）→ 不量，直接退卡片。封箱永远不冒险超支。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    host = _sealed_host(tmp_path, card_passed=5)
    host._tree_maybe_edited = True
    host.remaining_seconds = 300.0

    host.seal_delivery()

    assert host.runs == []
    assert host.restores == ["cardsha"]


# ------------------------------------------------------- 旧卡护栏（2026-10-08）

def test_snapshot_is_current_only_when_head_is_the_parent(tmp_path):
    """卡片新鲜的判据：快照的父提交 == 当前 HEAD；说不清一律 False（不赌）。"""
    host = Host(tmp_path)
    host.bind("snapshot_is_current")
    host._head = "head0"
    host.parents = {"fresh": "head0", "stale": "head-old"}

    assert host.snapshot_is_current("fresh") is True
    assert host.snapshot_is_current("stale") is False, "卡片之后落过提交"
    assert host.snapshot_is_current("") is False
    assert host.snapshot_is_current("unknown") is False, "指认不出父提交 → 不赌"


def test_seal_will_not_roll_back_to_a_card_pinned_before_head(tmp_path, monkeypatch):
    """当前树量不动、卡片又钉在更早的 HEAD 上 → **不退**：退回就是拿卡片那棵树
    覆盖之后落盘的每一刀提交（中途武装的早期卡可能差着几小时的工作），而当前
    树的好坏未知 —— 那是纯赌。按现状交付，提交说明如实写明。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    host = _sealed_host(tmp_path, card_passed=5)
    host._tree_maybe_edited = True
    host.parents = {"cardsha": "head-old"}   # 卡片之后落了提交
    host.remaining_seconds = 300.0           # 量不动

    host.seal_delivery()

    assert host.runs == []
    assert host.restores == [], "卡片不是现在这棵树钉的 → 不退"
    assert host._best_state["passed"] == 5, "卡片原样留着（进程被杀后仍是恢复线索）"
    assert host.commits[-1] == "chore: final delivery (card pinned before HEAD; current tree unmeasured)"


def test_seal_still_rolls_back_to_a_card_pinned_at_the_current_head(tmp_path, monkeypatch):
    """卡片正钉在当前 HEAD 上（半途中断留下的一棵树）→ 照旧退回：卡描述的就是
    盘上这棵树的已验证版本，「能退就退」的设计本意不变。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    host = _sealed_host(tmp_path, card_passed=5)
    host._tree_maybe_edited = True
    host.remaining_seconds = 300.0

    host.seal_delivery()

    assert host.restores == ["cardsha"]
    assert host.commits[-1] == "chore: final delivery: best verified state 5/30"


def test_a_measured_worse_tree_still_rolls_back_to_a_stale_card(tmp_path, monkeypatch):
    """这次量过了、当前树就是更差（3/30 < 卡 5/30）→ 卡的时点无关紧要：
    已知当前更差，退回那一刻量的卡才是对的（炸树救援的形状）。"""
    monkeypatch.delenv("OCTOS_SEAL_DELIVERY", raising=False)
    host = _sealed_host(tmp_path, card_passed=5)
    host._tree_maybe_edited = True
    host.parents = {"cardsha": "head-old"}
    host.spec_results = [_summary(3, 30)]

    host.seal_delivery()

    assert host.restores == ["cardsha"]
    assert host.commits[-1] == "chore: final delivery: best verified state 5/30"
