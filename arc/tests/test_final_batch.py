"""收尾分批 + 全卷对账（P2）的测试 —— 每一根钉子对应一条判据：

    收尾分批   快照 → 批回合 → 全卷量 → 净胜才 commit；没净胜退回、加宽、park
    无条件提交 旧的收尾边修边提交；新路径**没过秤的刀不上盘**（退回它）
    park 持久  同一个修不动的红不跨 pass 反复吞回合（`_final_parked`）
    L17 尾巴   最后一轮比历史最好差 → 退回最好那棵树（快照失败时的兜底）
    对账门闸   OCTOS_RECONCILE_SECONDS=0 关；首次只记起点；攒够 keep 且到期才量
    对账武装   量全了但没全绿的对账：封箱判据不动，成绩卡武装到这个下界
               （`OCTOS_BEST_MIDRUN=0` 回到旧行为；半截/有 load_errors/零绿不武装）
    对账二分   域外新红 → 在 `_reconcile_sha..HEAD` 里找肇事提交（探针后必退回）
    修前向     肇事提交记进下一批的提示词；绿了自动销账；revert 是 env 对照路
    启动失败   全卷起不来 = 一批「启动修复」，两次后放弃并退回（不叠在坏树上）
    dispatch   `Flow.final_acceptance` 默认走分批；=0 / 无 orchestrator 走旧 body
"""

import time
import types
from pathlib import Path

import pytest

import main as M
import orchestrator as orch
from acceptance import RunSummary, TestOutcome
from test_atomic_repair import BatchFlow, _DEAD_SPEC, _make, _summary, _tree


# ---------------------------------------------------------------- 假件

class SuiteFlow(BatchFlow):
    """收尾路径比批修多伸手要的那一面：全卷观察值 + 成绩卡 + 封箱判据。

    三张底牌（干扰/间歇/并发说明）绑的是 `main.Flow` 的**真方法** —— 复刻一份
    就验不出调用形状了。"""

    def __init__(self, tmp_path, spec_map=None, dirty_files=(), spec_files=()):
        super().__init__(tmp_path, spec_map=spec_map, dirty_files=dirty_files)
        self.tests_dir.mkdir(exist_ok=True)
        for name in spec_files:
            (self.tests_dir / name).write_text("// spec", encoding="utf-8")
        self.mem_limit = None
        self.budget = 100000.0
        self.test_verdict = {}
        self.recorded = []          # record_full_suite 的记账
        self.best_notes = []        # record_best 的注记
        self.best_card = "cardsha"  # record_best 返回的卡 sha（None = 没进卡）
        self.workers_seen = []      # 每次 run_specs 用到的 workers
        self.specs_seen = []        # 每次 run_specs 跑了哪些文件（basename，排好序）
        self.fail_fast_seen = []    # 每次 run_specs 挂没挂判红即停
        self._tree_maybe_edited = True
        self._last_full_measure = None
        self._best_summary = None
        self._best_grouped = None

    def snapshot_state(self, note="snapshot"):
        self.snapshots.append(note)
        if self.snapshot_result is None:
            return None                       # 快照失败：调用方必须看见「退不了」
        return f"snap{len(self.snapshots)}"   # 每次快照一个独立 sha（退回指得清是哪一刀）

    def run_specs(self, specs, workers=None, grader_like=False, fail_fast=False):
        self.workers_seen.append(workers)
        self.specs_seen.append(sorted(Path(s).name for s in specs))
        self.fail_fast_seen.append(fail_fast)
        return self.spec_runner(list(specs), grader_like)

    def record_full_suite(self, summary, grouped):
        self.recorded.append((summary.passed, summary.total))

    def record_best(self, summary, note):
        self.best_notes.append(note)
        return self.best_card

    # -- 真 Flow 的三张底牌（验调用形状，不复刻文案）--
    @staticmethod
    def interference_note(grouped, passed_alone, stores_written=None):
        return M.Flow.interference_note(grouped, passed_alone, stores_written)

    @staticmethod
    def intermittent_note(grouped, passed_a_round):
        return M.Flow.intermittent_note(grouped, passed_a_round)

    @staticmethod
    def worker_parity_note(workers):
        return M.Flow.worker_parity_note(workers)


class ReconcileGit:
    """对账/二分真正伸手要的那几条 git 命令。`checkout` 会把 world 的 HEAD 拨过去
    —— 探针的 spec_runner 就靠这个判断「现在站在哪个提交上」。"""

    def __init__(self, flow):
        self.flow = flow
        self.calls = []
        self.revert_rc = 0

    def run(self, args, check=True):
        self.calls.append(list(args))
        out, rc = "", 0
        if args[:2] == ["rev-list", "--reverse"]:
            out = "\n".join(self.flow.commit_range)
        elif args[:1] == ["checkout"]:
            self.flow.world["head"] = args[1]
        elif args[:1] == ["revert"]:
            rc = self.revert_rc
            if rc == 0 and len(args) > 1 and args[1] == "--no-edit":
                self.flow.world["head"] = "reverted"   # 落刀之后站在一棵新的树上
        elif args[:1] == ["show"]:
            out = self.flow.commit_subjects.get(args[3], "some commit")
        elif args[:2] == ["diff", "--name-only"]:
            out = "\n".join(self.flow.culprit_files)
        return types.SimpleNamespace(stdout=out, returncode=rc)


def _killed():
    s = RunSummary(passed=0, total=0, error="runner killed (OOM)")
    s.killed = True
    return s


# ---------------------------------------------------------------- 开关

def test_the_final_switch_defaults_on_and_off_is_exact(monkeypatch):
    monkeypatch.delenv("OCTOS_ATOMIC_FINAL", raising=False)
    assert orch._atomic_final() is True, "默认走分批收尾"
    monkeypatch.setenv("OCTOS_ATOMIC_FINAL", "0")
    assert orch._atomic_final() is False, "`=0` 精确回到旧的收尾 body"
    monkeypatch.setenv("OCTOS_ATOMIC_FINAL", "1")
    assert orch._atomic_final() is True


# ---------------------------------------------------------------- 收尾分批

def test_a_kept_final_batch_commits_and_the_pass_ends_green(tmp_path, monkeypatch):
    """全卷量到的净胜才 commit；最后全绿 → 一个 pass 的收尾提交。

    和节点批修同一套判据，只换域：这里每一刀都拿**整份卷子**定生死。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"], "N2": ["tests/b.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    world = {"phase": 0}
    phases = {0: ["a.spec.ts", "b.spec.ts"], 1: ["b.spec.ts"], 2: []}
    flow.spec_runner = lambda specs, g: _summary(
        passed=2 - len(phases[world["phase"]]), failed=phases[world["phase"]])
    flow.turn_hook = lambda label, prompt: world.update(phase=world["phase"] + 1)
    o = _make(flow)

    o.final_acceptance_batched()

    assert flow.commits == [
        "fix: final batch repair a.spec.ts +1",
        "fix: final batch repair b.spec.ts +1",
        "chore: full acceptance suite 2/2 pass (full suite)",
    ]
    assert flow.restores == [], "净胜的批不许退回"
    assert flow.snapshots == ["final batch 1", "final batch 2"]
    label, prompt = flow.turns[0]
    assert label == "full-suite batch repair 1"
    assert "Fix ONLY the behavior these 1 spec file(s) exercise: a.spec.ts" in prompt


def test_a_breaking_final_batch_reverts_and_widens(tmp_path, monkeypatch):
    """弄红了别的卷子 → 退回快照、把被弄红的并进这一批重试（同一血脉）。

    旧收尾在这里会把刀**提交**进历史，然后一路在坏树上修 —— 这正是「改一错
    一片」在收尾的原样。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"], "N2": ["tests/b.spec.ts"],
                                         "N3": ["tests/c.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts", "c.spec.ts"))
    world = {"phase": 0}
    phases = {
        0: ["a.spec.ts", "b.spec.ts"],       # 开工：a、b 红，c 绿
        1: ["a.spec.ts", "c.spec.ts"],       # 第一刀：a 没修好，c 弄红了
        2: ["b.spec.ts"],                    # 加宽后：a、c 都修好
        3: [],                               # b 也修好
    }
    flow.spec_runner = lambda specs, g: _summary(
        passed=3 - len(phases[world["phase"]]), failed=phases[world["phase"]])
    flow.turn_hook = lambda label, prompt: world.update(phase=world["phase"] + 1)
    o = _make(flow)

    o.final_acceptance_batched()

    assert flow.commits == [
        # 净胜只算 a —— c 是从**退回去的基线**看的，它本来就没红过
        "fix: final batch repair a.spec.ts, c.spec.ts +1",
        "fix: final batch repair b.spec.ts +1",
        "chore: full acceptance suite 3/3 pass (full suite)",
    ]
    assert flow.restores == ["snap1"], "弄红的那一刀必须退回"
    _, p1 = flow.turns[0]
    assert "a.spec.ts" in p1 and "c.spec.ts" not in p1, "第一刀只面对 a"
    _, p2 = flow.turns[1]
    assert "a.spec.ts" in p2 and "c.spec.ts" in p2, "第二刀把弄红的 c 并进来了"


def test_a_stuck_final_batch_is_parked_and_stays_parked_across_passes(tmp_path, monkeypatch):
    """修不动的红 park 一次；`final_acceptance_passes` 跑第二趟时不再吞回合。

    pass 之间会重跑收尾 —— park 只活在一次 pass 里的话，同一份红每趟都重新
    烧两轮，而「多跑几趟」本来是账上最充裕的资源。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"], "N2": ["tests/b.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    world = {"phase": 0}
    phases = {0: ["a.spec.ts", "b.spec.ts"], 1: ["a.spec.ts", "b.spec.ts"], 2: ["a.spec.ts"],
              3: ["a.spec.ts"]}
    flow.spec_runner = lambda specs, g: _summary(
        passed=2 - len(phases[world["phase"]]), failed=phases[world["phase"]])
    flow.turn_hook = lambda label, prompt: world.update(phase=world["phase"] + 1)
    o = _make(flow)

    o.final_acceptance_batched()
    first_round_turns = len(flow.turns)

    o.final_acceptance_batched()

    assert o._final_parked == {"a.spec.ts"}, "修不动的红进了 park 名单"
    assert len(flow.turns) == first_round_turns, "第二趟不再给同一份红烧回合"
    assert flow.commits == ["fix: final batch repair b.spec.ts +1"]


def test_a_snapshotless_worse_round_is_recovered_by_the_l17_tail(tmp_path, monkeypatch):
    """快照失败时的兜底：最后一轮比最好差 → 退回**最好那一轮**的树。

    每一刀有快照兜底的场合，尾巴无事可做；它管的是快照失败/被掐停留下的盘面
    —— 交付物必须是量到过的最好版本，不是盘上恰好剩下的版本。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"], "N2": ["tests/b.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    flow.snapshot_result = None          # 快照全失败 —— 退不了，工作留在盘上
    world = {"phase": 0}
    phases = {0: ["a.spec.ts", "b.spec.ts"], 1: ["b.spec.ts"],
              2: ["a.spec.ts", "b.spec.ts"], 3: ["a.spec.ts", "b.spec.ts"]}
    flow.spec_runner = lambda specs, g: _summary(
        passed=2 - len(phases[world["phase"]]), failed=phases[world["phase"]])
    flow.turn_hook = lambda label, prompt: world.update(phase=world["phase"] + 1)
    o = _make(flow)

    o.final_acceptance_batched()

    assert flow.restores == ["cardsha"], "退回的是成绩卡钉住的最好那棵树"
    assert flow.commits[-1] == "chore: keep best full-suite state 1/2"
    assert flow.best_notes and flow.best_notes[-1] == "final acceptance"
    # 第三刀是同一血脉（batch {b}）的第二次尝试 —— 提示词要换手法，不许复读。
    # v5 起尾巴上还挂着一个诊断回合（搁置的红交给它查三方），所以按标签找那一刀。
    repairs = [p for label, p in flow.turns if label.startswith("full-suite batch repair")]
    assert repairs and "did not turn any of them green" in repairs[-1]
    assert flow.turns[-1][0].startswith("diagnose "), "搁置的红最后走诊断，而不是被丢掉"


def test_a_timed_out_final_batch_is_rolled_back_and_shows_up_in_the_ledger(tmp_path, monkeypatch):
    """收尾的批被墙钟烧完 → 退回、park 进诊断，且**这一刀的结局进账本**。

    附·超时记账在收尾域的样子：被掐断 = 没验证过 = 明确作废（nothing kept），
    不许在账本里冒充「试过了」。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]}, spec_files=("a.spec.ts",))
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts"])
    o = _make(flow)
    labels = []

    def fake_turn(prompt, label, timeout):
        labels.append(label)
        o._last_turn_timed_out = label.startswith("full-suite batch repair")
        o.last_turn_text = ""
        return (not o._last_turn_timed_out), ""

    o._turn = fake_turn
    o.final_acceptance_batched()

    assert [lab for lab in labels if lab.startswith("full-suite batch repair")] == \
        ["full-suite batch repair 1"], "这一批一个回合就止损"
    assert flow.restores == ["snap1"], "被掐断的那一刀必须退回（没验证的东西不上盘）"
    tried = "；".join(o.ledger["N1"]["tried"])
    assert "timed out" in tried and "nothing kept" in tried, "作废的结局写进账本"
    md = (tmp_path / ".arc" / "ledger.md").read_text(encoding="utf-8")
    assert "timed out" in md and "nothing kept" in md, "账本落盘，人看得见"


def test_a_failed_final_batch_turn_is_rolled_back_and_not_measured(tmp_path, monkeypatch):
    """#5e 收尾域：回合**判失败**（不是超时）—— 半成品先退回，且一次都不被量。

    现行为是「失败了也往下量，量不动才退」；github-2 那条路的代价就是坏树留在
    盘上被后面的测量当真。这里钉住两件事：失败轮一落地就退回检查点、尝试余额
    用完之后这批 park（`runs` 计数证明脏树没被量过）。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]}, spec_files=("a.spec.ts",))
    state = {"dirty": False}
    runs = []

    def spec_runner(specs, g):
        runs.append(state["dirty"])
        return _summary(passed=0, failed=["a.spec.ts"])

    flow.spec_runner = spec_runner
    real_restore = flow.restore_app

    def restore(sha):
        state["dirty"] = False     # 退回 = 脏树没了
        real_restore(sha)

    flow.restore_app = restore
    o = _make(flow)
    labels = []

    def fake_turn(prompt, label, timeout, read_only=False):
        labels.append(label)
        failed = label.startswith("full-suite batch repair")
        if failed:
            state["dirty"] = True   # 判失败的回合落盘 = 一树半成品
        o._last_turn_failed = failed
        o.last_turn_text = ""
        return (not failed), ""

    o._turn = fake_turn
    o.final_acceptance_batched()

    assert [lab for lab in labels if lab.startswith("full-suite batch repair")] == \
        ["full-suite batch repair 1", "full-suite batch repair 2"], "失败后重试一次（余额 2）"
    assert flow.restores == ["snap1", "snap2"], "每一刀都退回自己的入口"
    assert runs and not any(runs), "没有一次测量跑在失败轮留下的脏树上"
    tried = "；".join(o.ledger["N1"]["tried"])
    assert "the turn ended in failure" in tried and "nothing kept" in tried


# ---------------------------------------------------------------- 启动失败

def test_a_startup_failure_is_one_batch_and_recovers(tmp_path, monkeypatch):
    """全卷起不来 = 一个「启动修复」批；修回来了就接着走，不回退。"""
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    world = {"phase": 0}
    flow.spec_runner = lambda specs, g: (
        _summary(error="npm start failed: Error: listen EADDRINUSE :::3000")
        if world["phase"] == 0 else _summary(passed=2))
    flow.turn_hook = lambda label, prompt: world.update(phase=world["phase"] + 1)
    o = _make(flow)

    o.final_acceptance_batched()

    assert [label for label, _ in flow.turns] == ["full-suite startup repair 1"]
    _, prompt = flow.turns[0]
    assert "application startup exactly as the grader runs it" in prompt
    assert "listen EADDRINUSE" in prompt, "启动错误原文必须进提示词（诊断信息就是它）"
    assert flow.restores == [], "修回来了就不退回"
    assert flow.test_verdict["N1"] is False, "起不来的那一轮：每个节点都判红"
    assert flow.commits == ["chore: full acceptance suite 2/2 pass (full suite)"]


def test_a_startup_failure_that_does_not_heal_gives_up_after_two(tmp_path, monkeypatch):
    """两次修不回启动 → 放弃这一趟，并把盘退回进这一趟之前（不叠在坏树上）。"""
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    flow.spec_runner = lambda specs, g: _summary(error="npm start failed: boom")
    o = _make(flow)

    o.final_acceptance_batched()

    assert [label for label, _ in flow.turns] == ["full-suite startup repair 1",
                                                  "full-suite startup repair 2"]
    assert flow.snapshots == ["final startup 1", "final startup 2"]
    assert flow.restores == ["snap1", "snap2"], "每一刀都退回去（第二刀在放弃前退）"
    assert flow.commits == []


def test_the_full_suite_halves_workers_when_the_runner_is_oom_killed(tmp_path, monkeypatch):
    """显式提速（`OCTOS_ARC_FINAL_WORKERS`）后 OOM 被杀 → 并发减半重试到 1。"""
    monkeypatch.setenv("OCTOS_ARC_FINAL_WORKERS", "4")
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))

    def runner(specs, g):
        return _killed() if flow.workers_seen[-1] > 1 else _summary(passed=2)

    flow.spec_runner = runner
    o = _make(flow)

    o.final_acceptance_batched()

    assert flow.workers_seen == [4, 2, 1], "4 → 2 → 1，活下来的是 1"
    assert flow.commits == ["chore: full acceptance suite 2/2 pass (full suite)"]


def test_the_default_full_suite_is_already_at_the_floor_when_killed(tmp_path, monkeypatch):
    """默认口径（判分同款 workers=1）被杀 → 没有「减半」可做：不空转重试，
    保留逐节点结论、不拿被杀的那次进账（旧路径同一手）。"""
    monkeypatch.delenv("OCTOS_ARC_FINAL_WORKERS", raising=False)
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    flow.spec_runner = lambda specs, g: _killed()
    o = _make(flow)

    o.final_acceptance_batched()

    assert flow.workers_seen == [1], "已经在判分口径的地板上，重试同一个数没有意义"
    assert flow.recorded == [] and flow.commits == [], "被杀的测量不进账、不上刀"


# ---------------------------------------------------------------- #5b 全卷作废

def test_a_dead_spec_found_at_final_is_voided_and_the_suite_is_remeasured(tmp_path):
    """收尾量到**机器验过的死卷子** → 当场作废 + **重量一遍**（#5b 的兜底那半）。

    节点入口已经作废过一次，这里兜的是「属主节点没走到」的那些。作废改变的是
    **全卷本身**：不重量的话，这一轮的 grouped、成绩卡、切批都还带着一份对任何
    实现都不可能的卷子 —— 拿修复回合去够它是白烧，它还会把 best 的分压低。
    """
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"], "N2": ["tests/b.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    flow.authored_suite = True
    (flow.tests_dir / "a.spec.ts").write_text(_DEAD_SPEC, encoding="utf-8")
    calls = {"n": 0}

    def runner(specs, g):
        calls["n"] += 1
        return (_summary(passed=1, failed=["a.spec.ts"]) if calls["n"] == 1
                else _summary(passed=2))

    flow.spec_runner = runner
    o = _make(flow)

    o.final_acceptance_batched()

    assert o.voided_specs == {"a.spec.ts"}
    assert "N1" not in flow.spec_map, "作废 = 从属主名下摘掉"
    assert calls["n"] == 2, "作废之后重量了一遍（拿的是摘掉之后的卷面）"
    assert flow.turns == [], "死卷子一个修复回合都没烧"
    assert "mechanically dead" in "；".join(o.ledger["N1"]["tried"])
    assert flow.commits == ["chore: full acceptance suite 2/2 pass (full suite)"]


# ---------------------------------------------------------------- 对账门闸

def test_reconcile_gates_interval_keeps_and_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("OCTOS_RECONCILE_SECONDS", raising=False)
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    flow.spec_runner = lambda specs, g: _summary(passed=2)
    o = _make(flow)

    o._maybe_reconcile("N1")
    assert o._reconcile_sha == flow.head() and o._reconcile_at > 0
    assert flow.workers_seen == [], "第一次 keep 只记起点，不花测量的钱"

    o._reconcile_keeps = 5
    o._reconcile_at = time.time() - 10 ** 6
    o._maybe_reconcile("N1")
    assert flow.workers_seen == [], "keep 没攒够，不量"

    o._reconcile_keeps = 6
    o._maybe_reconcile("N1")
    assert len(flow.workers_seen) == 1, "到期且攒够 → 量一次"
    assert o._reconcile_keeps == 0, "量过之后重新计数"

    monkeypatch.setenv("OCTOS_RECONCILE_SECONDS", "0")
    fresh = _make(SuiteFlow(tmp_path, spec_files=("a.spec.ts", "b.spec.ts")))
    fresh._maybe_reconcile("N1")
    assert fresh._reconcile_sha is None, "=0 关掉：连起点都不记"

    monkeypatch.delenv("OCTOS_RECONCILE_SECONDS", raising=False)
    poor = _make(SuiteFlow(tmp_path, spec_files=("a.spec.ts", "b.spec.ts")))
    poor._reconcile_sha, poor._reconcile_keeps = poor.flow.head(), 6
    poor._reconcile_at = time.time() - 10 ** 6
    poor.flow.remaining = lambda: 1.0    # 预算只剩 1 秒
    poor._maybe_reconcile("N1")
    assert poor.flow.workers_seen == [], "尾巴上的钱留给收尾，不花在对账"


def test_accepted_counts_keeps_and_rings_the_reconcile_bell(tmp_path):
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts",))
    o = _make(flow)
    rung = []
    o._maybe_reconcile = lambda node_id: rung.append(node_id)

    result = o._accepted(orch.NodeResult(node_id="N1"), [])

    assert result.passed is True
    assert o._reconcile_keeps == 1
    assert rung == ["N1"], "验收就是一次 keep —— 门铃要响"


# ---------------------------------------------------------------- #1/#2 提速批

def _cut(*, passed=0, failed=(), never=(), file=""):
    """判红即停留下的报告形状（冒烟8 实测，Playwright 1.63）：没跑到的用例
    `ok=False, status="unknown"` —— 报告里有条目、没有结果。`never` 是这些
    用例所属的 spec 文件名。"""
    results = [TestOutcome(title=f"pass-{i}", ok=True, status="passed", duration_ms=1, file=file)
               for i in range(passed)]
    results += [TestOutcome(title=f"fails-{name}", ok=False, status="failed",
                            duration_ms=1, file=name, message="boom") for name in failed]
    results += [TestOutcome(title=f"never-{name}", ok=False, status="unknown",
                            duration_ms=0, file=name) for name in never]
    return RunSummary(passed=passed, total=len(results), results=results)


def test_the_green_ledger_only_counts_what_came_back(tmp_path):
    """绿名单只记**报回来的用例**，而且「没跑到的」不是证据：

      · 请求跑了、一份结果都没有的卷子不算量过 —— 隔离循环中途起不来，
        下一轮它还得按「从没量过」全量看见；
      · 判红即停截断出的 `status="unknown"` 当绿看，红透的卷子会被记成绿过；
        当红看，截断点后面一整片会被冤枉成「域外新红」。
    """
    flow = SuiteFlow(tmp_path, spec_files=("a.spec.ts",))
    o = _make(flow)

    o._note_spec_observations(_summary(passed=1, file="a.spec.ts"))
    o._note_spec_observations(_summary(passed=0, failed=["b.spec.ts"]))
    o._note_spec_observations(_cut(passed=1, file="c.spec.ts",
                                   failed=["c.spec.ts"], never=["c.spec.ts"]))
    o._note_spec_observations(_cut(never=["d.spec.ts"]))          # 整份一条都没跑到

    assert o.ever_green_specs == {"a.spec.ts", "c.spec.ts"}, \
        "绿过一次就永远在绿名单里：c 真跑到过一条绿，它是回归的候选"
    assert o.observed_specs == {"a.spec.ts", "b.spec.ts", "c.spec.ts"}, \
        "红过也是量过（只是不绿）；整份没跑到的不算"
    assert "d.spec.ts" not in o.observed_specs and "d.spec.ts" not in o.ever_green_specs, \
        "一条真结果都没有 = 从没量过，下一轮必须全量看见"


def test_a_truncated_file_is_never_cleared_from_the_red_ledger(tmp_path):
    """截断的卷子不许从红名单上被摘掉：它这轮红着，而且**没看全**——

    判红即停把它在第一条红处截断，前缀里那些绿说明不了「修好了」：没跑到的
    用例里可能正红着。看全了的绿才准摘。
    """
    flow = SuiteFlow(tmp_path, spec_files=("a.spec.ts",))
    o = _make(flow)
    o.baseline_red = {"a.spec.ts", "c.spec.ts"}

    failing, prior = o._absorb_scope(
        ["a.spec.ts", "c.spec.ts"],
        _cut(passed=1, file="a.spec.ts", failed=["c.spec.ts"], never=["c.spec.ts"]))

    assert failing == {"c.spec.ts"}, "红的就是红的（真失败那条）"
    assert prior == {"a.spec.ts", "c.spec.ts"}
    assert "c.spec.ts" in o.baseline_red, "没看全 → 摘不掉"
    assert "a.spec.ts" not in o.baseline_red, "a 看全了、这轮绿 → 该摘就摘"


def test_a_never_run_cut_is_not_charged_as_newly_red(tmp_path):
    """整批模式下判红即停连后面的文件一起停：那些卷子整份都是「没跑到的用例」
    —— 它们是**没看见**，不是「新红」。当红看，域外新红会从 1 份炸成一片，
    二分和修前向被引向一堆无辜的卷子。"""
    flow = SuiteFlow(tmp_path, spec_files=("a.spec.ts",))
    o = _make(flow)
    o.baseline_red = {"a.spec.ts"}

    failing, prior = o._absorb_scope(
        ["a.spec.ts", "z.spec.ts"], _cut(passed=0, failed=["a.spec.ts"], never=["a.spec.ts", "z.spec.ts"]))

    assert failing == {"a.spec.ts"}, "z 根本没跑到，不许算红"
    assert prior == {"a.spec.ts"}
    assert o.baseline_red == {"a.spec.ts"}, "a 红照旧；z 既不加也不摘（没看见就不猜）"


def test_pre_passed_nodes_seed_the_green_ledger(tmp_path):
    """evolution 预通过的节点：探测那一步**就是**一次测量（现成 app 满足它）
    —— 卷子开局就在绿名单里，不然体检会把它们当成「从没量过」多量一遍。"""
    flow = SuiteFlow(tmp_path, spec_map={"P1": ["tests/seed.spec.ts"]})
    o = orch.Orchestrator(flow, pre_passed={"P1"})

    assert o.ever_green_specs == {"seed.spec.ts"}
    assert o.observed_specs == {"seed.spec.ts"}


def test_the_reconcile_sweeps_green_and_unseen_only(tmp_path):
    """体检范围 = 曾经绿过 ∪ 从没量过：量过却从没绿过的卷子不陪跑。

    顺序做题的形状：后面的卷子本来就是红的，它们永远不会变成体检要抓的
    东西（回归只发生在绿过的东西身上）—— 每趟全卷都为它们重复付钱，就是
    「体检比干活还贵」的那一半。少了一份 → 不是完整测量 → 不进账。
    """
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts", "c.spec.ts"))
    flow.spec_runner = lambda specs, g: _summary(passed=2)
    o = _make(flow)
    o.ever_green_specs = {"a.spec.ts"}              # 绿过：回归的候选，必须点名
    o.observed_specs = {"a.spec.ts", "b.spec.ts"}   # b 量过、从没绿过 → 不陪跑
    o._reconcile_sha = flow.head()

    o._run_reconcile("N1")

    assert flow.specs_seen == [["a.spec.ts", "c.spec.ts"]], \
        "绿过 ∪ 从没量过；量过却从没绿过的 b 不陪跑"
    assert flow.fail_fast_seen == [True], "体检这一趟挂判红即停"
    assert flow.recorded == [] and flow.best_notes == [], "少了一份 → 半截测量不进账"
    assert flow._tree_maybe_edited is True, "封箱判据不许被半截测量改写"


def test_a_full_green_reconcile_still_books(tmp_path):
    """一份没漏、全绿（判红即停没咬到东西）→ 照旧进账：封箱判据与成绩卡口径不变。"""
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    flow.spec_runner = lambda specs, g: _summary(passed=2)
    o = _make(flow)
    o._reconcile_sha = flow.head()

    o._run_reconcile("N1")

    assert flow.specs_seen == [["a.spec.ts", "b.spec.ts"]], "两本账都空 → 全部按从没量过点名"
    assert flow.recorded == [(2, 2)], "完整测量照旧进成绩卡"
    assert flow.best_notes[-1].startswith("reconcile after N1")
    assert flow._last_full_measure == {"passed": 2, "total": 2}
    assert flow._tree_maybe_edited is False, "盘上这棵树确实量过了"


def test_a_red_reconcile_is_not_booked_even_when_the_red_is_pre_existing(tmp_path):
    """红着的卷子（早就在账上的那种）同样不进全卷账：截断就是截断，跟红是谁的无关。

    旧行为是「红也进账」（全量测量本来就是红的也是事实）；判红即停之后
    summary 的 total 是半截的 —— 拿去记账，封箱判据会以为这棵树量过了、
    成绩卡会报一个假的 total。2026-10-08 起这趟有第二个用途（只动卡）：
    **每一份都量到了**的话，成绩卡武装到这个下界 —— 进程死在收尾之前时，
    盘上已经有卡可依。封箱判据照旧一个字不碰。"""
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    flow.spec_runner = lambda specs, g: _summary(passed=1, failed=["b.spec.ts"])
    o = _make(flow)
    o.baseline_red = {"b.spec.ts"}          # 早就在账上的红
    o._reconcile_sha = flow.head()

    o._run_reconcile("N1")

    assert flow.recorded == [], "半截测量不进全卷成绩卡"
    assert flow.best_notes == ["reconcile after N1 (mid-run lower bound)"], \
        "两本账都量到了 → 最好成绩卡武装到这个下界"
    assert flow._last_full_measure is None and flow._tree_maybe_edited is True, \
        "封箱判据不许被半截测量改写"
    assert o.baseline_red == {"b.spec.ts"}, "红名单照旧记账（不因不 book 而变）"
    assert o._forward_fix_files == set(), "不是新红 → 没有修前向的账"


def test_the_midrun_card_is_not_armed_by_a_partial_broken_or_empty_sweep(tmp_path, monkeypatch):
    """武装的三个护栏：开关=0 / load_errors / 一条绿都没量到 —— 都不进卡，
    也都不碰封箱判据。"""
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()

    # 开关 =0：精确回到旧行为（跑动中途不碰卡）
    monkeypatch.setenv("OCTOS_BEST_MIDRUN", "0")
    flow = SuiteFlow(tmp_path / "a", spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    flow.spec_runner = lambda specs, g: _summary(passed=1, failed=["b.spec.ts"])
    o = _make(flow)
    o._reconcile_sha = flow.head()
    o._run_reconcile("N1")
    assert flow.best_notes == [], "=0 精确回到旧行为"
    assert flow._last_full_measure is None and flow._tree_maybe_edited is True
    monkeypatch.delenv("OCTOS_BEST_MIDRUN")

    # load_errors：看起来全绿也不算量全（文件没收上来 → 下界都谈不上）
    flow2 = SuiteFlow(tmp_path / "b", spec_map={"N1": ["tests/a.spec.ts"]},
                      spec_files=("a.spec.ts", "b.spec.ts"))

    def _with_load_errors(specs, grader_like):
        summary = _summary(passed=2)
        summary.load_errors = ["a.spec.ts: the report could not be read"]
        return summary

    flow2.spec_runner = _with_load_errors
    o2 = _make(flow2)
    o2._reconcile_sha = flow2.head()
    o2._run_reconcile("N1")
    assert flow2.best_notes == [], "有文件没收上来 → 不武装"
    assert flow2.recorded == [] and flow2._tree_maybe_edited is True

    # 零绿：空卡不提供任何可退回的版本
    flow3 = SuiteFlow(tmp_path / "c", spec_map={"N1": ["tests/a.spec.ts"]},
                      spec_files=("a.spec.ts",))
    flow3.spec_runner = lambda specs, g: _summary(failed=["a.spec.ts"])
    o3 = _make(flow3)
    o3._reconcile_sha = flow3.head()
    o3._run_reconcile("N1")
    assert flow3.best_notes == [], "一条绿都没量到：空卡不武装"
    assert flow3._tree_maybe_edited is True


# ---------------------------------------------------------------- #5 同树同卷集复用

def _per_spec(specs, *, failed=()):
    """按卷子分装的测量结果（`file` = basename）：复用存档是按这个粒度记的。"""
    failed = set(failed)
    results = [TestOutcome(title=f"{Path(s).name}-1", ok=Path(s).name not in failed,
                           status="failed" if Path(s).name in failed else "passed",
                           duration_ms=1, file=Path(s).name,
                           message="boom" if Path(s).name in failed else "")
               for s in specs]
    return RunSummary(passed=sum(1 for r in results if r.ok), total=len(results), results=results)


def _probe_flow(tmp_path, files=("a.spec.ts", "b.spec.ts"), **kw):
    flow = SuiteFlow(tmp_path, spec_map={"N": [f"tests/{files[0]}"]}, spec_files=files, **kw)
    flow.spec_runner = lambda specs, g: _per_spec(specs)
    return flow, _make(flow)


def test_the_same_clean_tree_and_the_same_specs_are_measured_once(tmp_path):
    """**背靠背重复**：代码一个字没变、卷子集没变 —— 第二遍不跑，直接顶账。

    日志实锤的那一对（4m49s）：对账刚在 sha X 上量完整卷，紧接着聚合验收又来
    量同一批卷子。树是干净的（验收即 commit），两次测量的起点逐字节相同。
    """
    flow, o = _probe_flow(tmp_path)

    first = o._probe_scope(["tests/a.spec.ts", "tests/b.spec.ts"])
    second = o._probe_scope(["tests/a.spec.ts", "tests/b.spec.ts"])

    assert flow.specs_seen == [["a.spec.ts", "b.spec.ts"]], "同一棵树上只量一次"
    assert second is not None and (second[0].passed, second[0].total) == (2, 2)
    assert [r.file for r in second[0].results] == ["a.spec.ts", "b.spec.ts"], \
        "顶账的像原样带回来（顺序按请求）"
    assert second[1] == set(), "记账照走：这轮谁红谁绿的判据在当下重算"
    assert first[2] == second[2] == set(), "跑前的红名单 = 当下那本账"


def test_a_dirty_tree_is_never_served_from_the_memo(tmp_path):
    """树一脏，「量的是哪份内容」就没有名字 —— 那份测量谁也别认领（多量一次）。"""
    flow, o = _probe_flow(tmp_path, dirty_files=["frontend/src/App.tsx"])

    o._probe_scope(["tests/a.spec.ts"])
    o._probe_scope(["tests/a.spec.ts"])

    assert flow.specs_seen == [["a.spec.ts"], ["a.spec.ts"]], "脏树不复用"
    assert o._measure_memo == {}, "脏树上的测量也不进档"


def test_a_new_commit_expires_the_archived_measurement(tmp_path):
    """钥匙是 HEAD：中间落了任何一刀（哪怕只改一个字节），旧的像就作废。"""
    flow, o = _probe_flow(tmp_path, files=("a.spec.ts",))

    o._probe_scope(["tests/a.spec.ts"])
    flow.commit("N: accepted")           # 树变了（sha0 → sha1）
    o._probe_scope(["tests/a.spec.ts"])

    assert flow.specs_seen == [["a.spec.ts"], ["a.spec.ts"]], "换棵树就得重量"


def test_a_subset_is_served_but_a_missing_file_forces_a_fresh_run(tmp_path):
    """**并集覆盖**才算命中：请求里每一份都得在档里，少一份就整个不复用 ——

    半份旧 + 半份新拼出来的像，下游会当成一次完整测量记账（记账守卫、
    成绩卡看的就是「一份没漏」）。"""
    flow, o = _probe_flow(tmp_path)

    o._probe_scope(["tests/a.spec.ts", "tests/b.spec.ts"])
    o._probe_scope(["tests/a.spec.ts"])                       # 子集：在档里 → 顶账
    assert len(flow.specs_seen) == 1

    o._probe_scope(["tests/a.spec.ts", "c.spec.ts"])          # c 没量过 → 整个重量
    assert flow.specs_seen[-1] == ["a.spec.ts", "c.spec.ts"]


def test_a_truncated_measurement_is_never_archived(tmp_path):
    """判红即停截断出来的半截测量不进档 —— 存进去，下一次就拿它当完整测量用了。"""
    flow, o = _probe_flow(tmp_path, files=("a.spec.ts",))
    flow.spec_runner = lambda specs, g: _cut(passed=1, file="a.spec.ts",
                                             failed=["a.spec.ts"], never=["a.spec.ts"])

    o._probe_scope(["tests/a.spec.ts"])
    o._probe_scope(["tests/a.spec.ts"])

    assert o._measure_memo == {}, "半截测量一个字节都不许进档"
    assert len(flow.specs_seen) == 2, "下一趟还得真量"


def test_a_run_that_could_not_measure_is_never_archived(tmp_path):
    """基建错（起不来 / 被杀）不是测量 —— 不进档，免得被当成「量过了」。"""
    flow, o = _probe_flow(tmp_path, files=("a.spec.ts",))
    flow.spec_runner = lambda specs, g: _summary(passed=0, error="the app did not start")

    o._probe_scope(["tests/a.spec.ts"])
    o._probe_scope(["tests/a.spec.ts"])

    assert o._measure_memo == {}
    assert len(flow.specs_seen) == 2


def test_the_reuse_switch_turns_it_off_exactly(tmp_path, monkeypatch):
    """`OCTOS_REUSE_MEASUREMENTS=0`：回到「每次都真跑」（A/B 校准与急停用）。"""
    monkeypatch.setenv("OCTOS_REUSE_MEASUREMENTS", "0")
    flow, o = _probe_flow(tmp_path, files=("a.spec.ts",))

    o._probe_scope(["tests/a.spec.ts"])
    o._probe_scope(["tests/a.spec.ts"])

    assert len(flow.specs_seen) == 2 and o._measure_memo == {}


def test_the_memo_keeps_only_the_most_recent_states(tmp_path):
    """存档限量（`_MEMO_STATES`）：真正的重复永远是背靠背的那几对。"""
    flow, o = _probe_flow(tmp_path, files=("a.spec.ts",))

    for i in range(orch._MEMO_STATES + 2):
        o._run_specs(["tests/a.spec.ts"], grader_like=True, allow_reuse=True)
        flow.commit(f"c{i}")

    assert len(o._measure_memo) == orch._MEMO_STATES, "只留最近几个状态"
    assert ("sha0", True) not in o._measure_memo, "最老的先出局"


def test_the_reconcile_archives_the_tree_for_the_very_next_probe(tmp_path):
    """**生产形状的那一对**：对账（体检）在 sha X 上量完全卷 → 聚合验收的小域
    立刻又问同一批卷子 —— 第二趟直接顶账，一分钱不花。"""
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "b.spec.ts"))
    flow.spec_runner = lambda specs, g: _per_spec(specs)
    o = _make(flow)
    o._reconcile_sha = flow.head()

    o._run_reconcile("N1")
    got = o._probe_scope(["tests/a.spec.ts", "tests/b.spec.ts"])

    assert flow.specs_seen == [["a.spec.ts", "b.spec.ts"]], "小域那一趟是纯重复"
    assert got is not None and got[0].total == 2
    assert flow.recorded == [(2, 2)], "对账那趟照常进账（它才是真跑的那一趟）"


def test_the_deliberate_re_measurements_never_reuse(tmp_path):
    """**故意的复测一律不复用**：二遍跑检测、幽灵复查、二分探针、收尾全卷量。

    它们的价值就在「再量一次」—— 复用会把结论变成同义反复（二遍跑永远看不出
    残渣、幽灵复查永远看不见出厂态的真实样子）。这条钉子钉的是调用形状。"""
    flow = SuiteFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts",))
    flow.authored_suite = True
    flow.spec_runner = lambda specs, g: _per_spec(specs)
    o = _make(flow)
    seen: list[bool] = []
    real = o._run_specs

    def spy(specs, grader_like, **kw):
        seen.append(bool(kw.get("allow_reuse", False)))
        return real(specs, grader_like, **kw)

    o._run_specs = spy
    o._second_pass_check("N", ["tests/a.spec.ts"], False)      # #7 二遍跑
    o._nonrestoring_specs = {"a.spec.ts"}
    o._ghost_reds(["a.spec.ts"], grader_like=True)             # #7 幽灵复查
    o._run_specs(["tests/a.spec.ts"], grader_like=True)        # 默认：fresh

    assert seen == [False, False, False], \
        f"故意的复测一个都不许复用（实际 allow_reuse={seen}）"


# ---------------------------------------------------------------- 对账二分

def _reconcile_flow(tmp_path, monkeypatch):
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "z.spec.ts"))
    flow.world = {"head": "sha0"}
    flow.commit_range = ["c1", "c2", "c3"]
    flow.commit_subjects = {"c2": "N9: accepted"}
    flow.culprit_files = ["frontend/x.ts"]
    flow.git = ReconcileGit(flow)
    flow.runtime = types.SimpleNamespace(git=flow.git)

    def runner(specs, g):
        if len(specs) == 1:      # 二分探针：站在哪个提交上决定 z 红不红
            if flow.world["head"] == "c2":
                return _summary(passed=0, failed=["z.spec.ts"])
            return _summary(passed=1, file="z.spec.ts")
        return _summary(passed=1, failed=["z.spec.ts"])   # 全卷：z 在域外新红

    flow.spec_runner = runner
    o = _make(flow)
    o._reconcile_sha = "sha0"
    # z 在更早的测量里见过绿灯 —— 「域外新红」的前提（2026-10-08 提速批后的
    # 记账口径）：只有「曾绿 → 红」才算回归；从没绿过的红是「还没做到」，
    # 不进域外新红、不记修前向账。
    o.ever_green_specs.add("z.spec.ts")
    o.ever_green_tests.add(("z.spec.ts", "fails-z.spec.ts"))
    return flow, o


def test_reconcile_bisects_the_culprit_commit_and_restores_the_tree(tmp_path, monkeypatch):
    """域外新红 → 二分到肇事提交 → 记进修前向；探针之后**树回到进来时的样子**。

    记账口径随 2026-10-08 提速批改了：这趟挂了判红即停，红 summary 是**截断**
    的，所以不进全卷账（见 `_run_reconcile` 的记账守卫）—— 判据（谁红了、
    二指到哪一刀、修前向账）一字不变。同日稍后：每一份都量到了的对账会把
    最好成绩卡武装到这个下界 —— 只动卡，封箱判据照旧不碰。
    """
    monkeypatch.delenv("OCTOS_RECONCILE_REVERT", raising=False)
    flow, o = _reconcile_flow(tmp_path, monkeypatch)

    o._run_reconcile("N1")

    assert flow.fail_fast_seen[0] is True, "对账主跑挂判红即停"
    assert flow.fail_fast_seen[1:] and not any(flow.fail_fast_seen[1:]), \
        "二分探针一根都不许挂：少看一条红，探针的判据就残了"
    assert flow.recorded == [], "红的 summary 是截断的（第一条红之后没跑完）→ 全卷账保持原样"
    assert flow.best_notes == ["reconcile after N1 (mid-run lower bound)"], \
        "但最好成绩卡武装到这个下界（每一份都量到了）"
    assert flow._tree_maybe_edited is True, "封箱判据照旧一个字不碰"
    assert o._forward_fix_files == {"z.spec.ts"}
    assert "c2" in o._forward_fix_note and "fix forward" in o._forward_fix_note.lower()
    assert flow.restores == ["snap1"], "对账是旁观者：探针之后无条件退回"
    assert o._reconcile_sha == "sha0" or o._reconcile_sha == flow.head()


def test_reconcile_bisects_the_window_since_the_last_measurement(tmp_path, monkeypatch):
    """二分窗口的起点 = **上次量过的 HEAD**，不是「测量完刚拨到的这次 HEAD」。

    `_reconcile_sha` 在测量收工后立刻被拨到这次的 HEAD（给下一轮当起点），而
    二分在这之后才走 —— 窗口照字面读成「这次 HEAD..HEAD」就是恒空，肇事提交
    永远指认不出来（github 决赛实锤：全程没有一行 "trace back to"）。这里让
    两次测量隔着一次提交：上次量在 sha0，这次量在 sha_now。"""
    monkeypatch.delenv("OCTOS_RECONCILE_REVERT", raising=False)
    flow, o = _reconcile_flow(tmp_path, monkeypatch)
    flow._head = "sha_now"          # 期间攒了新提交：这次测量站在 sha_now

    o._run_reconcile("N1")

    windows = [c[2] for c in flow.git.calls if c[:2] == ["rev-list", "--reverse"]]
    assert windows == ["sha0..HEAD"], \
        f"窗口起点丢了（{windows}）—— 空窗口 = 永远指认不出肇事提交"
    assert o._reconcile_sha == "sha_now", "对账收工后，窗口起点才拨到这次量过的 HEAD"
    assert o._forward_fix_files == {"z.spec.ts"} and "c2" in o._forward_fix_note, \
        "窗口对了，肇事提交就指认得出（c2），修前向说明带着它"


def test_reconcile_leaves_never_green_reds_to_their_owner_nodes(tmp_path, monkeypatch, capsys):
    """从没绿过的红不是「域外新红」—— 不记账、不二分、不进修前向。

    github 决赛实锤（2026-10-08）：第一次对账把整棵**还没实现**的树记成域外
    新红（52 份里 36 份），修前向账此后把 36 份红卷并进每一次验收小域 —— 每轮
    为此重扫 25 分钟（占全程 45%），且从没修成过任何一份。收口到「曾绿 → 红」
    之后：它们是「还没做到」，归属主节点管。"""
    monkeypatch.delenv("OCTOS_RECONCILE_REVERT", raising=False)
    flow, o = _reconcile_flow(tmp_path, monkeypatch)
    o.ever_green_specs.clear()          # 抹掉「z 曾绿过」的前提：它从没绿过
    o.ever_green_tests.clear()

    o._run_reconcile("N1")

    assert o._forward_fix_files == set() and o._forward_fix_keys == set()
    assert o._forward_fix_note == ""
    assert len(flow.fail_fast_seen) == 1, "不二分：没有回归要指认，探针一个都不跑"
    assert "not yet implemented" in capsys.readouterr().out, \
        "日志要说得出口径：这些红归属主节点，不是被谁弄红的"


def test_reconcile_counts_only_tests_that_were_green_before(tmp_path, monkeypatch):
    """精确到用例：卷子「见过绿灯」不算数，红着的**那条**得曾绿过才算回归。

    半绿的卷子（实测 9/10、1/3 这类）里红着的那几条从来不是回归 —— 别的用例
    绿过改变不了这一点。判据口径与 `_failure_keys` 一致：(basename, title)。"""
    monkeypatch.delenv("OCTOS_RECONCILE_REVERT", raising=False)
    flow, o = _reconcile_flow(tmp_path, monkeypatch)
    o.ever_green_tests.clear()          # 文件级绿名单不动：别的用例绿过

    o._run_reconcile("N1")

    assert o._forward_fix_files == set() and o._forward_fix_note == ""
    assert len(flow.fail_fast_seen) == 1, "红的用例从没绿过 → 不是回归 → 不探针"


def test_a_green_forward_fix_is_cleared_from_the_ledger(tmp_path, monkeypatch):
    """修前向的红绿了 → 销账（说明也不许挂在后续无关的批上）。"""
    flow, o = _reconcile_flow(tmp_path, monkeypatch)
    o._run_reconcile("N1")
    assert o._forward_fix_files and o._forward_fix_note

    o._absorb_scope(["z.spec.ts"], _summary(passed=1, file="z.spec.ts"))

    assert o._forward_fix_files == set() and o._forward_fix_keys == set()
    assert o._forward_fix_note == ""


def test_voiding_a_spec_sweeps_it_out_of_the_fix_forward_book(tmp_path, monkeypatch):
    """作废要**一次销干净**：修前向账本、红名单、说明 —— 否则白烧轮次（#4c）。

    彩排实测：REQ-7-2-1 作废后又被量 3 轮、约 45s。账本里的名字会在
    `_extend_scope_with_forward` 里被并进之后**每一个**小域，作废的卷子于是
    又回到测量里。「它从此不存在」= 所有提到它的账一起销。
    """
    monkeypatch.delenv("OCTOS_RECONCILE_REVERT", raising=False)
    flow, o = _reconcile_flow(tmp_path, monkeypatch)
    o._run_reconcile("N1")
    assert o._forward_fix_files == {"z.spec.ts"}
    o.baseline_red = {"z.spec.ts", "a.spec.ts"}

    o._void_specs("N9", ["z.spec.ts"])

    assert o.voided_specs == {"z.spec.ts"}
    assert "z.spec.ts" not in o.baseline_red, "红名单里也划掉"
    assert o._forward_fix_files == set(), "修前向账本销账"
    assert o._forward_fix_keys == set()
    assert o._forward_fix_note == "", "说明也撤 —— 它只许说还欠着的红"


def test_a_voided_spec_never_rides_back_in_through_the_forward_book(tmp_path):
    """兜底：万一账本被别的写入口塞回来了，也不许把作废的卷子并进测量。"""
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "z.spec.ts"))
    o = _make(flow)
    o.voided_specs.add("z.spec.ts")
    o._forward_fix_files = {"z.spec.ts"}          # 模拟漏网的写入口

    assert o._extend_scope_with_forward(["a.spec.ts"]) == ["a.spec.ts"]


def test_the_forward_note_rides_into_the_batch_prompt(tmp_path, monkeypatch):
    """修前向的说明只在批里真的碰到那些卷子时出现 —— 别的批看到的是噪音。"""
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts", "tests/z.spec.ts"]},
                     spec_files=("a.spec.ts", "z.spec.ts"))
    o = _make(flow)
    o._forward_fix_files = {"z.spec.ts"}
    o._forward_fix_note = "\nMARKER-FIX-FORWARD: restore the behaviour z exercises.\n"

    with_z = o._batch_repair_prompt("N1", ["z.spec.ts"], _summary(passed=0, failed=["z.spec.ts"]),
                                    None, aggregate=False)
    without_z = o._batch_repair_prompt("N1", ["a.spec.ts"], _summary(passed=0, failed=["a.spec.ts"]),
                                       None, aggregate=False)

    assert "MARKER-FIX-FORWARD" in with_z
    assert "MARKER-FIX-FORWARD" not in without_z


# ---------------------------------------------------------------- #6 主脊换位

def _with_requirement(flow):
    """给假 flow 挂上真 Flow 那一面（`repair_requirements`）的最小复刻。"""
    flow.repair_requirements = lambda node_id=None: (
        "Original requirements (read-only; preserve details even when acceptance does not "
        "assert them):\nID: N1\nName: the form\nDescription: the form must submit on Enter.\n")
    return flow


def test_the_repair_prompt_puts_the_requirement_text_before_the_failures(tmp_path):
    """#6 主脊换位：任务陈述是「行为符合需求原文」，卷子红只是**证据**。

    形状上的钉法：需求原文出现在失败清单**之前**（先立目标、再看证据），并且
    明说「证据，不是目标」；小步纪律（一小步、写完即交、harness 马上量、
    keep/revert 按这一小步判）同时在场 —— 它是 #6b/#6c 落进提示词的那一半。
    """
    flow = _with_requirement(SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                                       spec_files=("a.spec.ts",)))
    o = _make(flow)

    text = o._batch_repair_prompt("N1", ["a.spec.ts"],
                                  _summary(passed=0, failed=["a.spec.ts"]), None, aggregate=False)

    assert "the form must submit on Enter." in text
    assert "evidence about your code, not the goal" in text
    assert "one small step" in text and "rolled back whole" in text
    assert text.index("the form must submit on Enter.") < text.index("fails-a.spec.ts"), \
        "需求原文要在失败清单之前 —— 先立目标，再看证据"


def test_a_repair_prompt_without_requirement_text_still_keeps_the_small_step_note(tmp_path):
    """取不到需求原文（老 flow / 需求树里没有它）就不许编一段 —— 但小步纪律与它
    无关，照常拼进提示词。"""
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]}, spec_files=("a.spec.ts",))
    o = _make(flow)          # 没有 repair_requirements：这条路整体静默退出

    text = o._batch_repair_prompt("N1", ["a.spec.ts"],
                                  _summary(passed=0, failed=["a.spec.ts"]), None, aggregate=False)

    assert "this is the goal" not in text, "取不到原文一个字都不许编"
    assert "one small step" in text


def test_the_aggregate_repair_turn_also_carries_the_requirement_text(tmp_path):
    """聚合点（收尾整合）走的是另一份模板 —— 主脊换位必须两条路都到。"""
    flow = _with_requirement(SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                                       spec_files=("a.spec.ts",)))
    o = _make(flow)

    text = o._batch_repair_prompt("N1", ["a.spec.ts"],
                                  _summary(passed=0, failed=["a.spec.ts"]), None, aggregate=True)

    assert "integration failure" in text, "聚合模板本体还在"
    assert "the form must submit on Enter." in text
    assert "evidence about your code, not the goal" in text
    assert "one small step" in text


def test_reconcile_revert_is_the_env_switch_and_falls_back_on_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOS_RECONCILE_REVERT", "1")
    flow, o = _reconcile_flow(tmp_path, monkeypatch)

    flow.git.revert_rc = 0                      # revert 干净落刀；探针确认红没了
    o._run_reconcile("N1")
    assert ["revert", "--no-edit", "c2"] in flow.git.calls
    assert o._forward_fix_files == set(), "revert 成功就没有修前向的账"

    flow2, o2 = _reconcile_flow(tmp_path, monkeypatch)
    flow2.git.revert_rc = 1                     # revert 冲突 → abort + 修前向兜底
    o2._run_reconcile("N1")
    assert ["revert", "--abort"] in flow2.git.calls
    assert o2._forward_fix_files == {"z.spec.ts"}, "回退不成 → 修前向兜底"


# ---------------------------------------------------------------- #7 二遍跑检测

def _second_pass_flow(tmp_path, *, authored=True, first_green=True, second_green=True):
    """一份卷子的节点：第 1 遍（出厂态）绿、第 2 遍（立刻复跑、不重置）红
    —— 就是《出卷审计》§四的「用完不还原」形状。"""
    flow = SuiteFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]}, spec_files=("a.spec.ts",))
    flow.authored_suite = authored
    runs = []

    def runner(specs, g):
        runs.append((list(specs), g))
        green = first_green if len(runs) == 1 else second_green
        return _summary(passed=1, file="a.spec.ts") if green else _summary(passed=0, failed=["a.spec.ts"])

    flow.spec_runner = runner
    return flow, runs


def test_the_second_pass_marks_a_spec_that_does_not_restore_what_it_changes(tmp_path):
    """变绿那一刻原样复跑一遍：第二遍红 → 点名进 `_nonrestoring_specs` + 台账。

    两个关键形状：复跑**中间不重置**（复位了就测不出残渣 —— 那正是「同库二遍跑」
    这条判据本身）；检测**不改判** —— 验收照走（判分机面对的也是新鲜库里跑第一遍）。
    """
    flow, runs = _second_pass_flow(tmp_path, second_green=False)
    o = _make(flow)
    resets = []
    o._reset_runtime_state = lambda why: (resets.append(why), False)[1]

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.passed is True, "检测不改判：验收照走"
    assert [r[0] for r in runs] == [["tests/a.spec.ts"]] * 2, "第二遍就是同一份卷子原样复跑"
    assert resets == ["measuring N"], "两遍之间不许复位 —— 「不重置跑第二遍」才是这条测量"
    assert o._nonrestoring_specs == {"a.spec.ts"}
    assert "does not restore what it changes" in " ".join(o.ledger["N"]["tried"])


def test_a_re_runnable_spec_is_not_marked(tmp_path):
    """第二遍也全绿 → 名单上不留人（阳性对照：别把正常卷子也标上）。"""
    flow, runs = _second_pass_flow(tmp_path)
    o = _make(flow)

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.passed is True and len(runs) == 2, "检测本身照付一次复跑的钱"
    assert o._nonrestoring_specs == set()


def test_the_second_pass_needs_authored_specs(tmp_path):
    """官方卷不受影响（跨项约定）：一次复跑都不花，一个名字都不点。"""
    flow, runs = _second_pass_flow(tmp_path, authored=False)
    o = _make(flow)

    o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert len(runs) == 1, "官方卷是判分机在跑的另一份拷贝，复跑它改变不了判分"
    assert o._nonrestoring_specs == set() and o._secondpass_done == set()


def test_the_second_pass_fires_once_per_node_and_never_on_aggregates(tmp_path):
    """「叶子首次变绿」：查过的节点不重复查；聚合点的验收不是这条检测的对象。"""
    flow, runs = _second_pass_flow(tmp_path, second_green=False)
    o = _make(flow)
    o._second_pass_check("N", ["tests/a.spec.ts"], False)
    o._second_pass_check("N", ["tests/a.spec.ts"], False)
    assert len(runs) == 1, "查过一次就记档，不重复付钱"

    flow2, runs2 = _second_pass_flow(tmp_path, second_green=False)
    o2 = _make(flow2)
    o2._second_pass_check("P", ["tests/a.spec.ts"], True)
    assert runs2 == [], "聚合点（aggregate=True）不查"


def _ghost_sealing_flow(tmp_path, *, recheck_green=True):
    """收尾一趟里的两只鬼：a 是真红（要修），g 是「用完不还原」的幽灵红。

    形状：量#1（出厂态：a 红、g 绿）→ 修 a 的那一刀 → 量#2（不重置：a 绿、g 红）
    —— g 的红就出现在「刚修完」的复量里，正是要摘出去的那一种。"""
    flow = SuiteFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"], "G": ["tests/g.spec.ts"]},
                     spec_files=("a.spec.ts", "g.spec.ts"))
    flow.authored_suite = True
    state = {"repaired": False, "rechecks": []}

    def runner(specs, g):
        names = {Path(s).name for s in specs}
        if names == {"g.spec.ts"}:                 # 幽灵复查：出厂态下 g 是绿的
            state["rechecks"].append(g)
            return (_summary(passed=1, file="g.spec.ts") if recheck_green
                    else _summary(passed=0, failed=["g.spec.ts"]))
        if state["repaired"]:
            return _summary(passed=1, failed=["g.spec.ts"])   # 量#2：a 修绿了，g 红了
        return _summary(passed=1, failed=["a.spec.ts"])       # 量#1：出厂态，g 绿

    flow.spec_runner = runner
    flow.turn_hook = lambda label, prompt: state.update(repaired=True)
    return flow, state


def test_the_final_pass_does_not_charge_a_confirmed_ghost_to_repair(tmp_path, monkeypatch):
    """收尾不带重置复量 —— 幽灵红会冒充「刚修的那批弄红的」；**不摘它，好刀被冤枉退回**。

    形状：点名过的卷子先按出厂态复查（绿 = 残渣），再参与净胜判定 —— 修好 a 的
    那一刀留下，g 的幽灵红不进任何修复提示词、不吞一个回合。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow, state = _ghost_sealing_flow(tmp_path)
    o = _make(flow)
    o._nonrestoring_specs = {"g.spec.ts"}     # 检测器已在它变绿那刻点过名

    o.final_acceptance_batched()

    assert len(state["rechecks"]) == 1, "点名过的才复查（a 没被点名，一次都不跑）"
    assert any(c.startswith("fix: final batch repair a.spec.ts") for c in flow.commits), \
        "g 的幽灵红不参与净胜判定 —— 修好 a 的那一刀不许被它冤枉退回"
    assert flow.restores == [], "没有一刀该被退回"
    assert all("g.spec.ts" not in p for _, p in flow.turns), "幽灵红不进任何修复提示词"


def test_without_the_marking_the_ghost_reverts_a_good_repair(tmp_path, monkeypatch):
    """对照（不点名时）：同一只鬼、同样的形状 —— 好刀被当成「弄红了 g」而退回。

    这条钉的不是新行为，是**理由**：没有 #7 的名单，收尾每一次不带重置的复量
    都会被同一批残渣红冤枉一次（正赛收尾烧掉的几个小时里有它一份）。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    monkeypatch.setenv("OCTOS_DIAGNOSE", "0")
    flow, state = _ghost_sealing_flow(tmp_path)
    o = _make(flow)                    # 不点名 —— g 不在 `_nonrestoring_specs` 里

    o.final_acceptance_batched()

    assert not any(c.startswith("fix: final batch repair") for c in flow.commits), \
        "幽灵红把好刀算成「弄红了」→ 留下不了"
    assert flow.restores, "被冤枉的那一刀退回了"


def test_a_real_red_in_a_marked_spec_is_still_charged(tmp_path, monkeypatch):
    """复查后仍旧红 → 留在红集里照常给修复 —— 不放过真红。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    monkeypatch.setenv("OCTOS_DIAGNOSE", "0")
    flow, state = _ghost_sealing_flow(tmp_path, recheck_green=False)
    o = _make(flow)
    o._nonrestoring_specs = {"g.spec.ts"}

    o.final_acceptance_batched()

    assert state["rechecks"], "复查跑过了"
    assert not any(c.startswith("fix: final batch repair") for c in flow.commits), \
        "出厂态也红的卷子是真红：修不绿它，那一刀不许留下"
    assert flow.restores, "没净胜的一刀要退回"
    assert any("g.spec.ts" in p for _, p in flow.turns), "真红照进提示词（加宽后的那一批）"


def test_reconcile_does_not_bisect_a_confirmed_ghost(tmp_path):
    """对账也不带重置 —— 「用完不还原」的卷子会冒充「域外新红」。出厂态复查确认
    是残渣 → 不进账本、不烧二分探针（更要紧的：不冤枉一个肇事提交）。"""
    flow = SuiteFlow(tmp_path, spec_map={"N1": ["tests/a.spec.ts"]},
                     spec_files=("a.spec.ts", "z.spec.ts"))
    flow.authored_suite = True
    flow.world = {"head": "sha0"}
    flow.commit_range = ["c1", "c2"]
    flow.git = ReconcileGit(flow)
    flow.runtime = types.SimpleNamespace(git=flow.git)

    def runner(specs, g):
        names = {Path(s).name for s in specs}
        if names == {"z.spec.ts"}:      # 幽灵复查：出厂态下 z 是绿的
            return _summary(passed=1, file="z.spec.ts")
        return _summary(passed=1, failed=["z.spec.ts"])   # 全卷：z 在域外「新红」

    flow.spec_runner = runner
    o = _make(flow)
    o._nonrestoring_specs = {"z.spec.ts"}

    o._run_reconcile("N1")

    assert o._forward_fix_files == set() and o._forward_fix_note == "", "残渣不进修前向"
    assert not [c for c in flow.git.calls if c[:1] == ["checkout"]], "不烧二分探针"
    assert "z.spec.ts" in o.baseline_red, "观察照记账 —— 只是不记到实现头上"


# ---------------------------------------------------------------- 切批

def test_final_batches_group_by_owner_capped_without_owner_last(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "3")
    flow = SuiteFlow(tmp_path, spec_map={
        "N": ["tests/n1.spec.ts", "tests/n2.spec.ts"],
        "O": ["tests/o1.spec.ts", "tests/o2.spec.ts", "tests/o3.spec.ts", "tests/o4.spec.ts",
              "tests/o5.spec.ts"],
    }, spec_files=("n1.spec.ts", "n2.spec.ts", "o1.spec.ts", "o2.spec.ts", "o3.spec.ts",
                   "o4.spec.ts", "o5.spec.ts", "u.spec.ts"))
    o = _make(flow)

    batches = o._final_batches(["n1.spec.ts", "n2.spec.ts", "o1.spec.ts", "o2.spec.ts",
                                "o3.spec.ts", "o4.spec.ts", "o5.spec.ts", "u.spec.ts"])

    assert batches == [
        ["n1.spec.ts", "n2.spec.ts"],                  # 同属主抱团
        ["o1.spec.ts", "o2.spec.ts", "o3.spec.ts"],    # 按 cap=3 切
        ["o4.spec.ts", "o5.spec.ts"],
        ["u.spec.ts"],                                 # 无主的排最后
    ]


# ---------------------------------------------------------------- dispatch

class DispatchHost:
    """`Flow.final_acceptance` 的真方法 + 一个可替换的 orchestrator。"""

    def __init__(self, tmp_path, orchestrator):
        self.output_dir = tmp_path
        self.tests_dir = tmp_path / "tests"
        self.tests_dir.mkdir(exist_ok=True)
        (self.tests_dir / "a.spec.ts").write_text("// spec", encoding="utf-8")
        self.runner = object()
        self.test_verdict = {}
        self.spec_map = {}
        self.orchestrator = orchestrator
        self.final_acceptance = types.MethodType(M.Flow.final_acceptance, self)


def _stub_orchestrator(calls, layers=object()):
    return types.SimpleNamespace(layers=layers,
                                 final_acceptance_batched=lambda: calls.append("batched"))


def test_final_acceptance_dispatches_to_the_batched_pass(tmp_path, monkeypatch):
    monkeypatch.delenv("OCTOS_ATOMIC_FINAL", raising=False)
    calls = []
    host = DispatchHost(tmp_path, _stub_orchestrator(calls))

    host.final_acceptance()

    assert calls == ["batched"], "默认（开着）整个交给分批收尾"


def test_final_acceptance_falls_back_to_the_old_body(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("OCTOS_ATOMIC_FINAL", "0")
    DispatchHost(tmp_path, _stub_orchestrator(calls)).final_acceptance()
    assert calls == [], "=0 逐字走旧 body"

    monkeypatch.delenv("OCTOS_ATOMIC_FINAL", raising=False)
    DispatchHost(tmp_path, None).final_acceptance()
    assert calls == [], "没有 orchestrator（旧假件）也落回旧 body"

    DispatchHost(tmp_path, _stub_orchestrator(calls, layers=None)).final_acceptance()
    assert calls == [], "orchestrator 没有 layers（没跑起来）同样落回旧 body"
