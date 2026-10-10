"""分批修复（`_accept_batched`）的测试 —— 旧的整段式路径在 test_orchestrator.py 里单独钉。

这里面每一个测试对应判据的一根钉子：

    快照      批前留一手；退回时才能退（没有快照就不退，工作留着）
    净胜      新增绿 ≥1 且**零新增红**才 commit 留下 —— 测试级粒度，不是文件级
    退回      无净胜 → restore 快照，失败集/观察一起退（不能拿退掉的树去说话）
    加宽      弄红了别人 → 把被弄红的文件并进这一批重试（同一批最多 `_batch_attempts` 次）
    park      同一批两次都没净胜 → 这批修不动，换下一批，不反复吞回合
    记账      开工前就红的卷子不进批（判词里写着「不是你的，别修」）
    since     批修会在节点中途提交，porcelain 会瞎 —— `git diff <开工点>` 不会
    异常轮    回合判失败（不是超时）：半成品先退回检查点，再谈重试/测量（#5e）
"""

import types
from pathlib import Path

import pytest

import orchestrator as orch
from acceptance import RunSummary
from acceptance import TestOutcome as _TestOutcome
from contract import Contract
from layers import layerize


# ---------------------------------------------------------------- 假件

def _tree():
    return {
        "id": "REQ-1", "name": "订票系统", "children": [
            {"id": "REQ-1.1", "name": "搜索", "children": [
                {"id": "1.1.1", "name": "按时间排序"},
                {"id": "1.1.2", "name": "按价格筛选"},
            ]},
            {"id": "REQ-1.2", "name": "下单", "children": [
                {"id": "1.2.1", "name": "选座"},
            ]},
        ],
    }


def _summary(*, passed=0, failed=(), file="", error=None):
    """`failed` 是失败用例的 spec 文件名列表；每个失败用例的 file 就是那个名字。

    失败用例的 title 从**文件名**推出来（不是列表下标）—— 真卷子里一条用例的
    身份（file, title）是稳定的，净胜判定就钉在这个身份上；用下标造 title 会
    让同一条红卷子跨轮换身份，判据会把它看成「弄红了一条新的」。
    """
    results = [_TestOutcome(title=f"pass-{i}", ok=True, status="passed", duration_ms=1, file=file)
               for i in range(passed)]
    for name in failed:
        results.append(_TestOutcome(title=f"fails-{name}", ok=False, status="failed",
                                    duration_ms=1, file=name, message="boom"))
    return RunSummary(passed=passed, total=passed + len(failed), results=results, error=error)


class Git:
    def __init__(self, flow):
        self.flow = flow

    def run(self, args, check=True):
        self.flow.git_calls.append(list(args))
        out = ""
        if args[:2] == ["status", "--porcelain"]:
            lines = []
            for entry in self.flow.dirty_files:
                lines.append(entry if entry[:2] in ("??", " M", "M ", "A ", " D", "R ")
                             else f" M {entry}")
            out = "\n".join(lines)
        elif args[:2] == ["diff", "--name-only"]:
            # `diff --name-only <since>`：自开工以来（含批修提交的）改动过的文件
            out = "\n".join(self.flow.diff_names)
        return types.SimpleNamespace(stdout=out, returncode=0)


class BatchFlow:
    """分批路径真正伸手要的那一面：比 LegacyFake 多 `snapshot_state` 与批观察值。"""

    def __init__(self, tmp_path, spec_map=None, dirty_files=()):
        self.output_dir = tmp_path
        self.req_dir = tmp_path / "requirements"
        self.tests_dir = tmp_path / "tests"
        self.spec_map = dict(spec_map or {})
        self.dirty_files = list(dirty_files)
        self.diff_names = []

        self.repair_rounds = 3
        self.min_repair_seconds = 0
        self.node_timeout = 1200
        self.node_budget_cap = 3000
        self.batch_need = 0.0

        self.reply = ""
        self.turns = []
        self.read_only_turns = []
        self.commits = []
        self.restores = []
        self.snapshots = []
        self.snapshot_result = "snap1"
        self.git_calls = []
        self.marks = []

        self._head = "sha0"
        self.runtime = types.SimpleNamespace(git=Git(self))
        self.spec_runner = lambda specs, grader_like: _summary(passed=1)
        self.turn_hook = None

    # -- 编排用到的那一面 --
    def head(self):
        return self._head

    def edit(self, path="frontend/src/App.tsx"):
        """模型这一轮**真的动了文件**：盘上的树不再是刚才量过的那一棵。

        #5 复用（`_measure_state_key`）只认干净树 —— 一次真修复回合留下的就是
        脏树。只把「修好了」写进假 runner、却不在盘上留痕的测试，等于假装那一刀
        什么都没改：复用会在假的干净树上命中。这一声 `edit()` 补的就是这个前提。
        """
        self.dirty_files.append(path)

    def commit(self, message):
        self.commits.append(message)
        self._head = f"sha{len(self.commits)}"
        return True

    def restore_app(self, sha):
        self.restores.append(sha)
        self._head = sha

    def snapshot_state(self, note="snapshot"):
        self.snapshots.append(note)
        return self.snapshot_result

    def mark(self, kind, node_id, message=None):
        self.marks.append((kind, node_id, message))

    def turn(self, prompt, timeout, label, read_only=False):
        self.turns.append((label, prompt))
        if read_only:
            self.read_only_turns.append(label)
        if self.turn_hook:
            self.turn_hook(label, prompt)
        return True, self.reply

    def run_specs(self, specs, workers=None, grader_like=False, fail_fast=False):
        return self.spec_runner(list(specs), grader_like)

    def wound_down(self):
        return False

    def time_up(self):
        return False

    def remaining(self):
        return 100000.0

    def repair_needs(self):
        return 0.0

    def batch_repair_needs(self):
        return self.batch_need

    def batch_repair_timeout(self):
        return 60

    def banked_surplus(self, index, total):
        return 0.0


def _make(flow):
    o = orch.Orchestrator(flow)
    o.layers = layerize(_tree())
    return o


def _stub_prompt(o):
    o._implement_prompt = lambda node_id: f"IMPLEMENT {node_id}"
    return o


# ---------------------------------------------------------------- 开关

def test_the_switch_defaults_on_and_off_is_exact(monkeypatch):
    monkeypatch.delenv("OCTOS_ATOMIC_REPAIR", raising=False)
    assert orch._atomic_repair() is True, "默认走分批路径"
    monkeypatch.setenv("OCTOS_ATOMIC_REPAIR", "0")
    assert orch._atomic_repair() is False, "`=0` 精确回到旧的整段式路径"
    monkeypatch.setenv("OCTOS_ATOMIC_REPAIR", "1")
    assert orch._atomic_repair() is True


# ---------------------------------------------------------------- keep 与验收

def test_a_kept_batch_commits_and_the_node_is_accepted(tmp_path, monkeypatch):
    """一批修绿、零新增红 → commit 留下；own 全绿且无小域 → 当场验收。

    注意提交的形状：先一个**批的提交**（这是验证点），再一个验收提交。
    旧路径只有一个「accepted」提交 —— 失败时盘上什么都没留下，退回都无处可退。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.repair_rounds = 5
    state = {"fixed": False}
    flow.spec_runner = lambda specs, g: (_summary(passed=1, file="a.spec.ts") if state["fixed"]
                                         else _summary(passed=0, failed=["a.spec.ts"]))
    flow.turn_hook = lambda label, prompt: state.update(fixed=True)
    o = _make(flow)

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.passed is True and result.turns == 1
    assert flow.commits == ["N: batch repair a.spec.ts +1", "N: accepted"]
    assert flow.restores == [], "净胜的批不许退回"
    assert flow.snapshots == ["N batch 1"], "批前快照是「退回」的物理前提"
    label, prompt = flow.turns[0]
    assert label == "N batch repair 1/5"
    assert "Fix ONLY the behavior these 1 spec file(s) exercise: a.spec.ts" in prompt, \
        "批的粒度必须进提示词 —— 不然模型交付的还是整段修复"


# ---------------------------------------------------------------- 退回 / park

def test_a_batch_with_no_net_gain_is_rolled_back_and_parked(tmp_path, monkeypatch):
    """零新增绿 → 退回快照；同一批没有「弄红」可加宽，park 一次就换路。

    修不动的红不许反复吞回合 —— 旧路径三轮全喂给同一个修不动的东西，
    这正是「三轮回合耗尽 → 区域连坐」的燃料。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.repair_rounds = 3
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts"])
    o = _make(flow)

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.passed is False
    assert result.exhausted is True, "红全修不动 = 判死（回合没白烧第二次）"
    assert result.turns == 1, "没有净胜也没有加宽理由 → 一次就 park"
    assert flow.restores == ["snap1"], "无净胜必须退回批前快照"
    assert flow.commits == [], "没净胜就没有提交"
    assert "0/1 spec(s) passed" in result.reason


# ---------------------------------------------------------------- 附·两段式测量

def test_a_zero_gain_round_does_not_pay_for_the_scope_measurement(tmp_path, monkeypatch):
    """批内 0 进账 ⇒ 直接退回：小域那道固定税不叠在任何一次注定被退回的刀上。

    REQ-5-2-1 的形状（每轮 66s × 8 回）：这一刀自己的卷子一条都没修绿，却每轮
    都把「依赖它的那些卷子」再量一遍 —— 量完的结论一定是「退回」。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.repair_rounds = 1
    measured: list[list[str]] = []
    flow.spec_runner = lambda specs, g: (measured.append(sorted(specs)),
                                         _summary(passed=0, failed=["a.spec.ts"]))[1]
    o = _make(flow)
    o._scope_specs = lambda *a, **k: ["tests/dep.spec.ts"]

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert measured.count(["tests/a.spec.ts"]) == 2, "入口一量 + 修复后一量（第一段）"
    assert measured.count(["tests/dep.spec.ts"]) == 1, "小域只在入口量过那一次"
    assert result.passed is False and flow.restores == ["snap1"], "0 进账照退"


def test_a_gaining_round_measures_the_scope_for_collateral_damage(tmp_path, monkeypatch):
    """有进账才跑第二段：连坐要在**这一轮**的小域上量 —— 这也是「修绿了自己、
    却弄红别人」唯一能被挡住的时刻。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.repair_rounds = 1
    state = {"fixed": False}
    seen: list[tuple[list[str], bool]] = []

    def runner(specs, g):
        seen.append((sorted(specs), bool(g)))
        if sorted(specs) == ["tests/dep.spec.ts"]:
            return _summary(passed=1, file="dep.spec.ts")
        return (_summary(passed=1, file="a.spec.ts") if state["fixed"]
                else _summary(passed=0, failed=["a.spec.ts"]))

    flow.spec_runner = runner

    def turn(label, prompt):
        state.update(fixed=True)
        flow.edit()          # 这一刀真的动了文件（#5：脏树才该重量）

    flow.turn_hook = turn
    o = _make(flow)
    o._scope_specs = lambda *a, **k: ["tests/dep.spec.ts"]

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.passed is True
    probes = [g for specs, g in seen if specs == ["tests/dep.spec.ts"]]
    assert len(probes) == 2, "入口一次 + 修复轮后一次（第二次正是第二段）"
    assert probes[-1] is True, "第二段按判分口径量（grader_like）"
    assert flow.commits == ["N: batch repair a.spec.ts +1", "N: accepted"]


def test_an_aggregate_node_still_measures_its_scope_without_own_specs(tmp_path, monkeypatch):
    """聚合点自己没有卷子 ⇒ 「批内 0 进账」无从谈起：第二段就是它的测量，照跑。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = BatchFlow(tmp_path, spec_map={"1.1.1": ["tests/dep.spec.ts"]})
    flow.repair_rounds = 1
    measured: list[list[str]] = []
    flow.spec_runner = lambda specs, g: (measured.append(sorted(specs)),
                                         _summary(passed=0, failed=["dep.spec.ts"]))[1]
    flow.turn_hook = lambda label, prompt: flow.edit()   # 这一刀真的动了文件（#5）
    o = _make(flow)
    o._scope_specs = lambda *a, **k: ["tests/dep.spec.ts"]

    o._accept("REQ-1.1", [], "REQ-1.1", aggregate=True)

    assert measured.count(["tests/dep.spec.ts"]) >= 2, "入口一次 + 修复轮后一次"
    assert [lab for lab, _ in flow.turns] == ["REQ-1.1 batch repair 1/1"], \
        "小域的红进得了批（不然这一批连回合都没有）"


def test_a_breaking_batch_widens_before_it_is_parked(tmp_path, monkeypatch):
    """弄红了别人 → 把被弄红的文件并进这一批重试（同一次尝试的血脉）。

    这里走的正是用户看到的那条杀伤链的反面：第一刀顺手弄红了 c，
    旧路径会把这一刀**提交**进历史，然后一路在坏掉的状态上修；
    新路径退回快照、把 c 并进来一起修 —— 修好了，绿的是 a 和 c，b 留着下一批。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts", "tests/b.spec.ts", "tests/c.spec.ts"]})
    flow.repair_rounds = 5
    world = {"phase": 0}
    phases = {
        0: ["a.spec.ts", "b.spec.ts"],                    # 开工：a、b 红，c 绿
        1: ["a.spec.ts", "b.spec.ts", "c.spec.ts"],       # 第一刀：a 没修好，c 弄红了
        2: ["b.spec.ts"],                                 # 加宽后：a、c 都修好了
        3: [],                                            # 收尾：b 也修好
    }
    flow.spec_runner = lambda specs, g: _summary(
        passed=3 - len(phases[world["phase"]]), failed=phases[world["phase"]])
    flow.turn_hook = lambda label, prompt: world.update(phase=world["phase"] + 1)
    o = _make(flow)

    result = o._accept("N", list(flow.spec_map["N"]), "N", aggregate=False)

    assert result.passed is True
    assert flow.commits == [
        # 加宽后的批：净胜只算 a —— c 是从**退回去的基线**看的，它本来就没红过
        "N: batch repair a.spec.ts, c.spec.ts +1",
        "N: batch repair b.spec.ts +1",
        "N: accepted",
    ]
    assert flow.restores == ["snap1"], "只有第一刀退过一次（净胜的批都不退）"
    assert flow.snapshots == ["N batch 1", "N batch 2", "N batch 3"]
    _, p1 = flow.turns[0]
    assert "a.spec.ts" in p1 and "c.spec.ts" not in p1, "第一刀只面对 a"
    _, p2 = flow.turns[1]
    assert "a.spec.ts" in p2 and "c.spec.ts" in p2, "第二刀把弄红的 c 并进来了"


def test_without_a_snapshot_the_work_stays_and_parking_still_protects(tmp_path, monkeypatch):
    """快照失败（None）→ **不退**：退不了的退回是灾难，工作留着继续。

    「能退就退」的另一半：退不了的时候，失败集就是盘上最新的观察，
    下一批的判定照旧从这里起步；park 依旧把修不动的红挡在回合之外。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts", "tests/b.spec.ts"]})
    flow.repair_rounds = 4
    flow.snapshot_result = None
    world = {"phase": 0}
    phases = {0: ["a.spec.ts", "b.spec.ts"], 1: ["a.spec.ts", "b.spec.ts"], 2: ["a.spec.ts"]}
    flow.spec_runner = lambda specs, g: _summary(
        passed=2 - len(phases[world["phase"]]), failed=phases[world["phase"]])
    flow.turn_hook = lambda label, prompt: world.update(phase=world["phase"] + 1)
    o = _make(flow)

    result = o._accept("N", list(flow.spec_map["N"]), "N", aggregate=False)

    assert result.passed is False and result.exhausted is True
    assert flow.restores == [], "没有快照就绝不调用退回"
    assert flow.snapshots == ["N batch 1", "N batch 2"], "快照试了两次（都失败）"
    assert flow.commits == ["N: batch repair b.spec.ts +1"], "b 的净胜照样留下"


# ---------------------------------------------------------------- 熔断（#3）+ 批账（附）

def test_the_third_always_red_batch_never_gets_a_turn(tmp_path, monkeypatch):
    """连续两轮零进展 → 熔断：第三个批连回合都不开，判词说得清为什么停。

    没有熔断时，三份修不动的红会一个批一个批地烧过去（每批一个回合 + 每轮
    复测）—— 接力实测的 REQ-6-1-1 空转就是这么白量的。零收成与超时**合账**，
    到线即停、记档、不再回队（`OCTOS_REPAIR_STALL_LIMIT`，初步 N=2）。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    monkeypatch.setenv("OCTOS_REPAIR_STALL_LIMIT", "2")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts", "tests/b.spec.ts",
                                               "tests/c.spec.ts"]})
    flow.repair_rounds = 6
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts", "b.spec.ts",
                                                                    "c.spec.ts"])
    o = _make(flow)

    result = o._accept("N", list(flow.spec_map["N"]), "N", aggregate=False)

    assert result.passed is False and result.exhausted is True
    assert result.turns == 2, "两轮零进展就停 —— c 的那个回合没烧"
    assert "no net progress" in result.reason, "判词要说明是零进展到线，不是回合用完"
    tried = "；".join(o.ledger["N"]["tried"])
    assert "reverted" in tried and "nothing kept" in tried, "每一刀的结局都进账本"
    assert o.ledger["N"]["status"] == "parked"
    md = (tmp_path / ".arc" / "ledger.md").read_text(encoding="utf-8")
    assert "no net progress" in md, "账本落盘，人看得见"


def test_a_kept_batch_resets_the_no_progress_counter(tmp_path, monkeypatch):
    """净胜 = 进展：熔断计数清零 —— 「慢热」节点只要在往前走就够不着熔断线。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    monkeypatch.setenv("OCTOS_REPAIR_STALL_LIMIT", "2")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts", "tests/b.spec.ts",
                                               "tests/c.spec.ts"]})
    flow.repair_rounds = 6
    world = {"phase": 0}
    phases = {0: ["a.spec.ts", "b.spec.ts", "c.spec.ts"],
              1: ["a.spec.ts", "b.spec.ts", "c.spec.ts"],   # a 那刀零收成
              2: ["a.spec.ts", "c.spec.ts"],                # b 那刀净胜 +1
              3: ["a.spec.ts", "c.spec.ts"]}                # c 那刀零收成（计数已清零）
    flow.spec_runner = lambda specs, g: _summary(
        passed=3 - len(phases[world["phase"]]), failed=phases[world["phase"]])
    flow.turn_hook = lambda label, prompt: world.update(phase=world["phase"] + 1)
    o = _make(flow)

    result = o._accept("N", list(flow.spec_map["N"]), "N", aggregate=False)

    assert result.turns == 3, "b 的净胜把计数清零 —— c 照样有回合"
    assert "no net progress" not in result.reason, "没到线，熔断不响"
    assert flow.commits == ["N: batch repair b.spec.ts +1"]


def test_a_timeout_and_a_zero_gain_round_share_one_counter(tmp_path, monkeypatch):
    """超时与零收成**合账**：一个超时 + 一个零收成 = 两轮没进展，到线就停。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    monkeypatch.setenv("OCTOS_REPAIR_STALL_LIMIT", "2")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts", "tests/b.spec.ts",
                                               "tests/c.spec.ts"]})
    flow.repair_rounds = 6
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts", "b.spec.ts",
                                                                    "c.spec.ts"])
    o = _make(flow)
    turns = []

    def fake_turn(prompt, label, timeout):
        turns.append(label)
        o._last_turn_timed_out = label.endswith("batch repair 1/6")
        return (not o._last_turn_timed_out), ""

    o._turn = fake_turn
    result = o._accept("N", list(flow.spec_map["N"]), "N", aggregate=False)

    assert len(turns) == 2, "超时算一轮零进展，第二个零收成到线 —— c 不再烧"
    assert result.exhausted is True
    assert "no net progress" in result.reason
    tried = "；".join(o.ledger["N"]["tried"])
    assert "timed out" in tried and "reverted" in tried, "两种轮都记进了账本"
    assert o.ledger["N"]["status"] == "parked"


def test_a_stalled_node_is_not_re_entered_but_the_diagnosed_rework_still_gets_in(
        tmp_path, monkeypatch):
    """不再回队：停修记录在案 → 常规入口直接跳过；诊断后的回炉窗口照进。"""
    monkeypatch.setenv("OCTOS_REPAIR_STALL_LIMIT", "2")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"],
                                         "1.1.1": ["tests/x.spec.ts"]})
    flow.repair_rounds = 3
    flow.spec_runner = lambda specs, g: _summary(passed=0,
                                                 failed=[Path(s).name for s in specs])
    o = _make(flow)
    o._stall_streak["N"] = 2

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.exhausted is True and result.turns == 0
    assert flow.turns == [] and flow.snapshots == [], "入口闸挡下 —— 一个回合都不开"
    assert "repair skipped" in result.reason
    assert o.ledger["N"]["status"] == "parked"
    md = (tmp_path / ".arc" / "ledger.md").read_text(encoding="utf-8")
    assert "no net progress" in md

    # 诊断之后的回炉：带着新判词的另一种尝试 —— 照进，且计数清零重新给窗口。
    o._stall_streak["1.1.1"] = 2
    r2 = o._accept_batched("1.1.1", ["tests/x.spec.ts"], "1.1.1 (diagnosed)",
                           aggregate=False, rework=True)
    assert [t for t, _ in flow.turns] == ["1.1.1 (diagnosed) batch repair 1/3"], \
        "回炉真的开了回合（没被入口闸挡下）"
    assert o._stall_streak.get("1.1.1") == 1, "回炉从干净的计数起步（这一轮零收成记 1）"
    assert r2.exhausted is True


# ---------------------------------------------------------------- 小域记账

def test_a_pre_existing_red_is_not_batched_but_a_new_break_is(tmp_path, monkeypatch):
    """开工前就红的卷子不进批；新弄红的进 —— 判词说「别修它」，批里就不该有它。

    聚合点跑的是全量基线，红名单里常常混着「早就是红的」和「这次弄红的」。
    旧路径的判词区分了两者（`_scope_verdict`），批的切法必须同源 ——
    不然模型会为一个不是自己造成、也修不好的红烧掉整批回合。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "3")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/n.spec.ts"]})
    flow.repair_rounds = 3
    probes = {"n": 0}

    def runner(specs, grader_like):
        if not grader_like:
            return _summary(passed=1, file="n.spec.ts")
        probes["n"] += 1
        if probes["n"] == 1:      # 开局：o 是这次弄红的，z 是开工前就红的
            return _summary(passed=0, failed=["o.spec.ts", "z.spec.ts"])
        return _summary(passed=1, failed=["z.spec.ts"], file="o.spec.ts")   # o 修好了

    flow.spec_runner = runner
    flow.turn_hook = lambda label, prompt: flow.edit()   # 修好 o 的那一刀真的动了文件（#5）
    o = _make(flow)
    o.baseline_specs = ["o.spec.ts", "z.spec.ts"]
    o.baseline_red = {"z.spec.ts"}

    result = o._accept("N", ["tests/n.spec.ts"], "N", aggregate=True)

    assert result.passed is True
    assert flow.commits == ["N: batch repair o.spec.ts +1", "N: accepted"]
    assert probes["n"] == 2, "开局一次 before 像 + keep 后一次验收"
    _, prompt = flow.turns[0]
    assert "o.spec.ts" in prompt
    assert "z.spec.ts" not in prompt, "开工前就红的卷子不配进批（也不配出现在提示词里）"
    assert o.baseline_red == {"z.spec.ts"}, "记账照旧：z 还红着，但它没冤枉这个节点"


# ---------------------------------------------------------------- 预算

def test_out_of_budget_stops_without_condemning_the_node(tmp_path):
    """批回合也要过「本钱」这道闸：不够再来一轮就停，但**不判死**。

    判死（exhausted）的后果是 `_abandon_region` 把整个区域连坐 —— 那只留给
    「回合用完了/修不动了」，不留给「预算不够了」。这和旧路径一字不差。
    """
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.repair_rounds = 3
    flow.batch_need = 10 ** 9          # 一个批回合要 10 亿秒
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts"])
    o = _make(flow)

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.passed is False
    assert result.exhausted is False, "没钱不是罪过 —— 不判死"
    assert result.reason.endswith("(no budget left for another repair)")
    assert flow.turns == [] and flow.snapshots == [] and flow.commits == []


# ---------------------------------------------------------------- 切批与判据

def test_failure_keys_are_test_level_not_file_level(tmp_path):
    """净胜判定钉在**用例**上：一份卷子里修好一个、弄坏另一个，两边都要算数。"""
    flow = BatchFlow(tmp_path)
    summary = RunSummary(passed=1, total=3, results=[
        _TestOutcome(title="t1", ok=False, status="failed", duration_ms=1, file="dir/a.spec.ts"),
        _TestOutcome(title="t2", ok=True, status="passed", duration_ms=1, file="dir/a.spec.ts"),
        _TestOutcome(title="t3", ok=False, status="failed", duration_ms=1, file="dir/a.spec.ts"),
    ])
    assert orch.Orchestrator._failure_keys(summary) == {("a.spec.ts", "t1"), ("a.spec.ts", "t3")}


def test_batches_group_by_owner_node_first_capped_and_skip_parked(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "3")
    flow = BatchFlow(tmp_path, spec_map={
        "N": ["tests/n1.spec.ts", "tests/n2.spec.ts"],
        "O": ["tests/o1.spec.ts", "tests/o2.spec.ts", "tests/o3.spec.ts", "tests/o4.spec.ts",
              "tests/o5.spec.ts"],
    })
    o = _make(flow)
    keys = {(f, "t") for f in ["n1.spec.ts", "n2.spec.ts", "o1.spec.ts", "o2.spec.ts", "o3.spec.ts",
                               "o4.spec.ts", "o5.spec.ts", "u.spec.ts"]}

    batches = o._next_batches("N", keys, parked=set())

    assert batches == [
        ["n1.spec.ts", "n2.spec.ts"],                      # 本节点优先，且同属主抱团
        ["u.spec.ts"],                                     # 无主的自己一组
        ["o1.spec.ts", "o2.spec.ts", "o3.spec.ts"],        # 别人名下按 cap=3 切
        ["o4.spec.ts", "o5.spec.ts"],
    ]
    assert o._next_batches("N", keys, parked={"u.spec.ts", "n1.spec.ts"}) == [
        ["n2.spec.ts"], ["o1.spec.ts", "o2.spec.ts", "o3.spec.ts"], ["o4.spec.ts", "o5.spec.ts"],
    ], "parked 的文件不再被切成批"


# ---------------------------------------------------------------- since

def test_batches_reveal_committed_changes_via_since(tmp_path):
    """批修在节点中途提交 —— porcelain 从提交那一刻起就是干净的，`since` 不是。

    共享文件检查（`_touched_shared`）是唯一会把范围放大到全量基线的机制；
    批修提交把它弄瞎的话，动共享文件的节点只查「依赖我的那几条」，
    而它弄坏的多半在别的分支上 —— 静默破坏又会一路冤枉下去。
    """
    flow = BatchFlow(tmp_path, spec_map={"1.1.1": ["tests/n.spec.ts"]}, dirty_files=[])
    o = _make(flow)
    o.contract = Contract(global_contract={"shared": ["frontend/shared/thing.ts"]}, slices={})
    o.baseline_specs = ["n.spec.ts"]

    assert o._touched_shared() is False, "porcelain 在批提交之后确实瞎了 —— 这就是要修的形状"

    flow.diff_names = ["frontend/shared/thing.ts"]
    assert o._touched_shared(since="sha0") is True, "diff <开工点> 看得见已提交的批"
    assert o._scope_specs("1.1.1", aggregate=False, since="sha0") == ["n.spec.ts"], \
        "碰了共享文件 → 范围放大到全量基线"
    assert o._scope_specs("1.1.1", aggregate=False) == [], "不给 since 时不放大（叶子没有下游）"


# ---------------------------------------------------------------- 卷子判错的出口

_GOOD_CLAIM = ("SPEC DEFECT: the requirement document says the grid's accessible name is "
               "`Worksheet grid`, but this spec asserts `Spreadsheet grid` instead")


def test_a_defective_spec_can_still_be_voided_after_two_batch_attempts(tmp_path, monkeypatch):
    """作废的门槛换算成**批尝试数**：前两次老老实实修代码，第三次才许主张卷子错了。

    旧路径的门槛是「第 2 轮之后」；批修把一轮拆小了，门槛必须跟着换单位 ——
    按批数算，不然作废通道会比旧路径早开好几倍。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    req = tmp_path / "requirements"
    req.mkdir()
    (req / "requirements.yaml").write_text("The grid is role=grid named `Worksheet grid`.",
                                           encoding="utf-8")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts", "tests/c.spec.ts"]})
    flow.req_dir = req
    flow.authored_suite = True
    flow.reply = _GOOD_CLAIM            # 每轮回话都主张 —— 真模型也是这样
    flow.repair_rounds = 4
    world = {"phase": 0}
    phases = {0: ["a.spec.ts", "c.spec.ts"], 1: ["a.spec.ts", "c.spec.ts"], 2: ["a.spec.ts"]}
    flow.spec_runner = lambda specs, g: _summary(
        passed=2 - len(phases[world["phase"]]), failed=phases[world["phase"]])
    flow.turn_hook = lambda label, prompt: world.update(phase=world["phase"] + 1)
    o = _make(flow)

    result = o._accept("N", list(flow.spec_map["N"]), "N", aggregate=False)

    assert o.voided_specs == {"a.spec.ts", "c.spec.ts"}, "两次批尝试之后，主张才被受理"
    assert "N" not in flow.spec_map, "作废 = 从节点名下摘掉（不是删文件）"
    assert (req / "requirements.yaml").is_file(), "需求文档不动"
    assert result.passed is True, "摘掉之后按需求文本单独验收"
    assert flow.commits == ["N: batch repair c.spec.ts +1", "N: accepted"]


# ---------------------------------------------------------------- since 的调用点

def test_implement_leaf_hands_the_pre_sha_to_accept(tmp_path):
    """`since` 的起点是**节点开工前的 HEAD**（`implement_leaf` 里的 `pre`）——
    调用点断了，上面那条 `since` 测试就只是单测一个没人用的参数。"""
    flow = BatchFlow(tmp_path, spec_map={"1.1.1": ["tests/1.1.1.spec.ts"]})
    o = _stub_prompt(_make(flow))
    seen = []

    def fake_accept(node_id, specs, label, aggregate, since_sha=None):
        seen.append((node_id, since_sha))
        return orch.NodeResult(node_id=node_id, passed=True)

    o._accept = fake_accept
    flow._head = "pre-sha"
    o.implement_leaf("1.1.1", 1, 4)

    assert seen[0] == ("1.1.1", "pre-sha")


# ---------------------------------------------------------------- #5e 异常终止轮

def test_a_failed_turn_is_rolled_back_before_it_is_measured(tmp_path, monkeypatch):
    """#5e 异常终止轮也回退：回合**判失败**（不是超时）—— 落盘的是半成品，先退再谈。

    实锤那条链（github-2）：炸桥死轮半成品留盘 → 后端种子崩 → 连坐 3 张卷子。
    与超时轮同一条处置：退回检查点、与零收成**合账**、账本记「作废」。区别只在
    重试：超时轮已烧满一整段墙钟，直接 park；失败轮多数是被中途掐断的，尝试
    余额还在就再来一次（`_batch_attempts` 管上限，`_stall_streak` 到线由 #3 收口）。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    monkeypatch.setenv("OCTOS_REPAIR_STALL_LIMIT", "2")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.repair_rounds = 6
    runs = []
    flow.spec_runner = lambda specs, g: (runs.append(list(specs)),
                                         _summary(passed=0, failed=["a.spec.ts"]))[1]
    o = _make(flow)
    turns = []

    def fake_turn(prompt, label, timeout):
        turns.append(label)
        o._last_turn_failed = True
        return False, "bridge_error: 未映射的工具名"

    o._turn = fake_turn
    result = o._accept("N", list(flow.spec_map["N"]), "N", aggregate=False)

    assert len(turns) == 2, "第一次失败后重试一次（尝试余额 2），第二次到线"
    assert flow.restores == ["snap1", "snap1"], "两次都先把半成品退回去"
    assert len(runs) == 1, "失败轮落盘的那棵树一次都没被量过（先退再量）"
    assert flow.commits == [], "没被验证过的东西一个都不留"
    assert result.exhausted is True
    assert o._stall_streak["N"] == 2, "失败轮与零收成合账"
    tried = "；".join(o.ledger["N"]["tried"])
    assert "the turn ended in failure" in tried and "nothing kept" in tried
    assert o.ledger["N"]["status"] == "parked"


def test_without_a_checkpoint_a_failed_turn_stops_the_node(tmp_path, monkeypatch):
    """退不回去就不带脏重试：没有检查点（快照失败）的失败轮 = 停这一节点。

    在脏树上再喂一刀不是修复 —— 与「量不动」那一档同一条理由。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.repair_rounds = 6
    flow.snapshot_result = None
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts"])
    o = _make(flow)
    turns = []

    def fake_turn(prompt, label, timeout):
        turns.append(label)
        o._last_turn_failed = True
        return False, "boom"

    o._turn = fake_turn
    result = o._accept("N", list(flow.spec_map["N"]), "N", aggregate=False)

    assert len(turns) == 1, "没有检查点 → 不重试，这一刀到此为止"
    assert flow.restores == [], "本来就没得退"
    assert result.exhausted is True
    tried = "；".join(o.ledger["N"]["tried"])
    assert "no checkpoint to roll back to" in tried


def test_a_failed_implement_turn_is_rolled_back_to_the_node_checkpoint(tmp_path):
    """实现回合是全流程里**唯一没有快照兜底**的一刀 —— 它没跑完就退回节点检查点。

    与「没做完的原料」分开：回合**正常结束**但节点没过，半成品照旧留着（那是
    后面集成修复的原料）；回合**没跑完**，盘上的是没写完的半成品，不能留。
    """
    flow = BatchFlow(tmp_path, spec_map={"1.1.1": ["tests/1.1.1.spec.ts"]})
    o = _stub_prompt(_make(flow))
    seen = []

    def fake_accept(node_id, specs, label, aggregate, since_sha=None):
        seen.append((node_id, since_sha, list(flow.restores)))
        return orch.NodeResult(node_id=node_id, passed=True)

    def fake_turn(prompt, label, timeout):
        o._last_turn_failed = True
        return False, ""

    o._accept = fake_accept
    o._turn = fake_turn
    flow._head = "pre-sha"
    o.implement_leaf("1.1.1", 1, 4)

    assert flow.snapshots == ["1.1.1 implement"], "开工前拍节点检查点"
    assert flow.restores == ["snap1"], "回合没跑完 → 退回检查点"
    assert seen == [("1.1.1", "pre-sha", ["snap1"])], "退回之后照常走验收（从干净的起点量）"
    assert "rolled back" in "；".join(o.ledger["1.1.1"]["tried"])


def test_the_node_keeps_an_unfinished_but_completed_turn(tmp_path):
    """负样本：回合**正常结束**、节点没过 → 半成品留着（旧的、刻意的行为）。

    #5e 收的是「没跑完的回合」，不是「没做成的尝试」—— 后者是后面集成修复的原料。
    """
    flow = BatchFlow(tmp_path, spec_map={"1.1.1": ["tests/1.1.1.spec.ts"]})
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["1.1.1.spec.ts"])
    o = _stub_prompt(_make(flow))
    o._accept = lambda node_id, specs, label, aggregate, since_sha=None: \
        orch.NodeResult(node_id=node_id, passed=False, baseline_ok=True, reason="red")
    o._turn = lambda prompt, label, timeout: (setattr(o, "_last_turn_failed", False), (True, ""))[1]

    o.implement_leaf("1.1.1", 1, 4)

    assert flow.restores == [], "正常结束的回合不退 —— 半成品是修复的原料"


def test_a_contract_amendment_does_not_steal_the_outer_turn_verdict(tmp_path):
    """契约修订是 `_turn` 的嵌套调用，有自己的结局 —— 不能用来改判外层回合。

    两个标志（超时 / 判失败）是「这一刀要不要退」的判据；修订回合反过来成功
    也不能把它洗白，反过来失败也不能把外层的好回合冤枉成失败。
    """
    flow = BatchFlow(tmp_path)
    o = _make(flow)
    # 场景一：外层回合判失败，修订回合成功 —— 外层仍然要判失败。
    flow.turn = lambda prompt, timeout, label, read_only=False: (label == "contract amend", "x")

    def amend(label):
        # 真实嵌套的形状：修订回合也是 `_turn`（`_amend_contract` 走同一扇门）。
        prev = o._maybe_amend_contract
        o._maybe_amend_contract = lambda _label: None
        try:
            o._turn("AMEND", "contract amend", 60)
        finally:
            o._maybe_amend_contract = prev

    o._maybe_amend_contract = amend

    ok, _ = o._turn("P", "N batch repair 1/1", 60)
    assert ok is False and o._last_turn_failed is True, "修订回合成功，外层仍然判失败"
    assert o._last_turn_timed_out is False

    # 场景二：外层回合好，修订回合失败 —— 外层不能被修订回合的失败冤枉。
    flow.turn = lambda prompt, timeout, label, read_only=False: (label != "contract amend", "x")
    ok, _ = o._turn("P", "N batch repair 1/1", 60)
    assert ok is True and o._last_turn_failed is False, "修订回合失败，外层仍然是好回合"


# ---------------------------------------------------------------- #5a 环境病就地抢修

def _broken(msg="npm run build: Identifier '__dirname' has already been declared"):
    """构建/启动失败的一次观察 —— 环境病，不是卷子红（`startup_error` 置位）。"""
    return RunSummary(passed=0, total=0, error=msg, startup_error=True)


def test_a_broken_build_at_entry_is_repaired_in_place(tmp_path, monkeypatch):
    """#5a 进站先量一次「环境」：构建/启动坏了 → 就地抢修，修好照常走这一节点。

    实锤那条链：一次改坏六连坐 —— 6 个实现轮 + 6 个验收轮都对着同一套坏环境
    白量（≈40 分钟），最后收尾再花两个 pass 修构建。旧行为是每个节点在入口
    量到 error 就整节点判红走人；这里改成**先治环境**（`_startup_repair_prompt`
    的同门手法，量尺换成本节点的卷子）。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.repair_rounds = 5
    calls = {"n": 0}

    def runner(specs, g):
        calls["n"] += 1
        if calls["n"] == 1:
            return _broken()
        if calls["n"] == 2:
            return _summary(passed=0, failed=["a.spec.ts"])
        return _summary(passed=1, file="a.spec.ts")

    flow.spec_runner = runner
    o = _make(flow)

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.passed is True, "抢修好之后这一节点照常走（不是入口判红走人）"
    labels = [label for label, _ in flow.turns]
    assert labels == ["N startup repair 1", "N batch repair 1/5"], "先治环境，再修这一节点"
    prompt = flow.turns[0][1]
    assert "this node's specs (N) fails at startup" in prompt, "提示词要说真话：量尺是这个节点"
    assert "Identifier" in prompt, "错误原文进抢修回合（与收尾同一手）"
    assert flow.snapshots == ["N startup repair", "N batch 1"]
    assert "startup repair 1" in "；".join(o.ledger["N"]["tried"])
    assert result.turns == 2, "抢修回合记在这个节点头上（账要诚实）"


def test_startup_repair_that_does_not_take_gives_up_for_the_whole_run(tmp_path, monkeypatch):
    """治不好 → 记档放弃，且**本跑不再重试**：同一个坏环境反复治 = 新的空转源。

    放弃时把没治好的那几刀退回去、并让这一刀退回本节点检查点（能退就退）——
    坏盘是没被验证过的落盘，不留。判死的是**这棵树**，不是这个节点：后面的
    节点量到同一个坏环境，不再各烧一轮抢修（那正是「六连坐」的钱）。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_ATTEMPTS", "2")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"], "M": ["tests/m.spec.ts"]})
    flow.spec_runner = lambda specs, g: _broken()
    o = _make(flow)

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.passed is False and result.baseline_ok is False, "坏盘要能退（退回节点检查点）"
    assert "startup repair did not take" in result.reason
    assert [label for label, _ in flow.turns] == ["N startup repair 1", "N startup repair 2"]
    assert flow.snapshots == ["N startup repair"], "每次尝试退回同一个入口，不在坏树上叠刀"
    assert flow.restores == ["snap1", "snap1"], "第二次尝试前 + 放弃时各退回一次"
    tried = "；".join(o.ledger["N"]["tried"])
    assert "gave up after 2 attempt(s)" in tried and "nothing kept" in tried
    assert o._startup_given_up is True

    turns_before = len(flow.turns)
    other = o._accept("M", ["tests/m.spec.ts"], "M", aggregate=False)
    assert len(flow.turns) == turns_before, "同一棵坏树不反复治"
    assert other.passed is False and "already given up" in other.reason


def test_a_healthy_measurement_clears_the_given_up_flag(tmp_path, monkeypatch):
    """「本跑已判死」是钉在**这棵树**上的：量到 app 能跑就清零，之后坏了照样治。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_ATTEMPTS", "1")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"], "M": ["tests/m.spec.ts"]})
    verdict = {"broken": True}
    flow.spec_runner = lambda specs, g: (_broken() if verdict["broken"]
                                         else _summary(passed=1, file="m.spec.ts"))
    o = _make(flow)

    o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)
    assert o._startup_given_up is True

    verdict["broken"] = False
    m = o._accept("M", ["tests/m.spec.ts"], "M", aggregate=False)

    assert m.passed is True and o._startup_given_up is False, "树换了一棵，判死不跟着走"


def test_an_ordinary_spec_run_error_is_not_a_broken_build(tmp_path):
    """`error` 非空 ≠ 环境病：判据在 `startup_error` 上（#5a）。

    跑卷子的基建错（报告读不出、收集失败）照旧是「量不动」—— 那不是一次修复
    能治的东西，不许进抢修回合。
    """
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.spec_runner = lambda specs, g: RunSummary(error="could not read the report")
    o = _make(flow)

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert flow.turns == [], "没有抢修回合"
    assert result.reason.startswith("spec run failed:")


def test_an_unhealable_build_sends_the_whole_node_back_to_its_checkpoint(tmp_path, monkeypatch):
    """实锤那条链的正面：**一次改坏六连坐** → 「1 挂 + 自动回退 + 复绿」。

    形状：实现回合把构建改死（假完工）→ 验收入口量到环境坏 → 就地抢修两次
    治不好 → 这一刀**退回节点检查点**（盘上回到开工前的样子 = 复绿的物理前提），
    并且本跑不再对同一棵坏树重复抢修（后面的节点不再各烧一轮）。
    台账记 `parked`（#5d）：停的是**这一跑**，不是这个节点 —— 它压根没量成，
    写成 `red` 等于把环境的锅记在它头上。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_ATTEMPTS", "1")
    flow = BatchFlow(tmp_path, spec_map={"1.1.1": ["tests/1.1.1.spec.ts"]})
    flow.spec_runner = lambda specs, g: _broken()
    o = _stub_prompt(_make(flow))
    o._implement_prompt = lambda node_id: f"IMPLEMENT {node_id}"

    o.implement_leaf("1.1.1", 1, 4)

    assert flow.snapshots == ["1.1.1 implement", "1.1.1 startup repair"]
    assert flow.restores == ["snap1", "sha0"], "抢修没治好 → 这一刀退回节点检查点"
    entry = o.ledger["1.1.1"]
    assert entry["status"] == "parked", "环境判死记 parked（不是这个节点的锅）"
    assert "startup repair did not take" in (entry["why"] or "")
    assert o._startup_given_up is True
    assert o.results[0].env_dead is True and o.results[0].exhausted is False, \
        "两个标志分开：环境判死 ≠ 修复轮用完"
    assert o._diagnosis == {}, "环境病不进诊断队列：跑不起来的树没什么可诊断的"


# ---------------------------------------------------------------- #5d 环境判死与 #3 熔断

def test_a_failed_startup_repair_is_charged_to_the_same_no_progress_account(tmp_path, monkeypatch):
    """#5d 与 #3 熔断联动：抢修烧掉的每一刀都记进**同一本**连续零进展账。

    那本账（`_stall_streak`）是熔断入口闸读的东西（超时 / 净收成为零合账，见
    `_accept_batched` 的入口闸）。环境治不好和白量一轮在账上是同一件事：回合
    花了、盘面没变好 —— 治环境因此不能另开一本谁也不看的私账。

    没烧过回合的节点一分不记：后面的节点撞上「本跑已判死」是**一步没动**地弹
    回来，不欠谁。钱要记准，账才有用。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_ATTEMPTS", "2")
    flow = BatchFlow(tmp_path, spec_map={"1.1.1": ["tests/a.spec.ts"],
                                         "1.1.2": ["tests/b.spec.ts"]})
    flow.spec_runner = lambda specs, g: _broken()
    o = _stub_prompt(_make(flow))
    o._implement_prompt = lambda node_id: f"IMPLEMENT {node_id}"

    o.implement_leaf("1.1.1", 1, 4)
    assert o._stall_streak.get("1.1.1") == 2, "两刀两笔 —— 是回合级，不是文件级"

    o.implement_leaf("1.1.2", 2, 4)
    assert "1.1.2" not in o._stall_streak, "撞上判死的节点一步没动，不记它的账"
    assert o.ledger["1.1.2"]["status"] == "parked", "去向与 1.1.1 一致：停的是这一跑"
    assert [r.env_dead for r in o.results] == [True, True]
    assert [r.exhausted for r in o.results] == [False, False]
    assert o._diagnosis == {}, "环境病不进诊断队列 —— 一个都不进"


def test_a_healed_startup_clears_the_no_progress_account(tmp_path, monkeypatch):
    """治好了 = 有进展：这本账清零（与批修「净胜即清零」同一口径）。

    不清的后果是拿环境的账罚节点：一个慢热节点带着环境病那几笔旧账进修复循环，
    第一轮零收成就到线被熔断 —— 而它其实什么都没做错。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_ATTEMPTS", "2")
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    calls = {"n": 0}

    def runner(specs, g):   # 一刀没治好（记一笔），第二刀治好
        calls["n"] += 1
        if calls["n"] <= 2:
            return _broken()
        return _summary(passed=1, file="a.spec.ts")

    flow.spec_runner = runner
    o = _make(flow)
    seen = []
    flow.turn_hook = lambda label, prompt: seen.append(dict(o._stall_streak))

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert [label for label, _ in flow.turns] == ["N startup repair 1", "N startup repair 2"]
    assert seen == [{}, {"N": 1}], "第一刀没治好当场记账，第二刀前账上有 1 笔"
    assert result.passed is True, "治好了照常走这一节点"
    assert "N" not in o._stall_streak, "治好了 = 有进展：旧账清零"


# ---------------------------------------------------------------- #5f 出厂态验收

class FactoryGit:
    """#5f 的假 git：答得出出厂提交是谁、哪个文件与出厂不同、照厂里那份写回来。"""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.dirty: set[str] = set()      # 与出厂不同的文件（盘上那份 = 「随包态」）
        self.worktree: dict[str, str] = {}

    def run(self, args, check=True):
        self.calls.append(list(args))
        if args[:2] == ["rev-list", "--max-parents=0"]:
            return types.SimpleNamespace(stdout="root1\n", returncode=0)
        if args[:2] == ["diff", "--quiet"]:
            paths = args[args.index("--") + 1:]
            return types.SimpleNamespace(
                stdout="", returncode=1 if any(p in self.dirty for p in paths) else 0)
        if args and args[0] == "checkout":
            _, sha, _, path = args[:4]
            if sha == "root1":
                self.worktree[path] = "factory-content"   # 照厂里那份写回来
                self.dirty.discard(path)
            return types.SimpleNamespace(stdout="", returncode=0)
        return types.SimpleNamespace(stdout="", returncode=0)


def _factory_flow(tmp_path, **kw):
    flow = BatchFlow(tmp_path, **kw)
    flow.runtime.git = FactoryGit()
    return flow, flow.runtime.git


def _stores(summary, *files):
    """给一次观察挂上盘上证据：app 在运行时写过这些文件（`stores_written`）。"""
    summary.stores_written = list(files)
    return summary


def test_a_runtime_file_is_observed_and_reset_to_factory_before_the_next_round(tmp_path):
    """#5f 每轮验收前复位到出厂态：被 app 写脏的运行时文件，按厂里那份写回来。

    观测集只认**盘上证据**（`stores_written` = spec run 之后 `frontend/backend` 的
    diff）—— 一个文件都没观测到时一个都不动：不按文件名猜（`data.json` 是这一族
    题目的样子，不是判据）。
    """
    flow, git = _factory_flow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.spec_runner = lambda specs, g: _stores(_summary(passed=1, file="a.spec.ts"),
                                                "backend/data.json")
    o = _make(flow)

    o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert o._runtime_files == {"backend/data.json"}, "测量把运行时文件带上来了"
    assert [c for c in git.calls if c and c[0] == "checkout"] == [], "还没观测到/还没脏，就不退"

    git.dirty.add("backend/data.json")          # 盘上那份被写脏了（随包态 ≠ 出厂态）
    o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert ["checkout", "root1", "--", "backend/data.json"] in git.calls, \
        "下一轮验收之前照厂里那份写回来"
    assert git.dirty == set(), "复位之后与出厂一致"


def test_a_startup_failure_caused_by_our_own_runtime_state_is_not_healed(tmp_path, monkeypatch):
    """#5f 双态：随包态起不来、出厂态起得来 ⇒ 坏的是我们自己的运行残渣。

    触发形状（每股残渣唯一还能活到这里的形状）：**这一轮的测量自己**在盘上留下
    了运行时残渣（`restore_worktree` 没盖住的那份），紧接着的启动就是坏的 ——
    轮首的复位在这之前已经跑过（那时盘上还等于出厂态），拦不住它。先复位、再
    复量一次：出厂态起得来 ⇒ 接着走，0 修复回合（治环境那几刀省下来），账本记
    清为什么 —— 这既不是「环境病了」，也不是「新代码在出厂数据上起不来」。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_ATTEMPTS", "2")
    flow, git = _factory_flow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    o = _make(flow)
    o._runtime_files.add("backend/data.json")    # 上一轮观测到的；盘上此刻与出厂一致
    calls = {"n": 0}

    def runner(specs, g):
        calls["n"] += 1
        if calls["n"] == 1:      # 随包态起不来；残渣退掉之后（出厂态）起得来
            git.dirty.add("backend/data.json")   # 这一轮自己留下的残渣（随包态 ≠ 出厂态）
            return _broken("TypeError: data.collections is undefined")
        return _summary(passed=1, file="a.spec.ts")

    flow.spec_runner = runner

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert result.passed is True, "复位之后照常走这一节点"
    assert flow.turns == [], "一个修复回合都没烧 —— 环境没病，是状态脏了"
    assert o._startup_given_up is False, "这不是判死"
    assert calls["n"] == 2, "入口一量 + 复位后复量 —— 没有第三刀"
    assert ["checkout", "root1", "--", "backend/data.json"] in git.calls, "退的是残渣那一个文件"
    assert "no repair needed" in "；".join(o.ledger["N"]["tried"])


def test_a_startup_failure_that_survives_the_reset_still_gets_healed(tmp_path, monkeypatch):
    """出厂态也起不来 ⇒ 真的坏：照旧走 #5a 就地抢修 —— 复位不吞真故障。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_ATTEMPTS", "1")
    flow, git = _factory_flow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    o = _make(flow)
    o._runtime_files.add("backend/data.json")
    git.dirty.add("backend/data.json")
    flow.spec_runner = lambda specs, g: _broken()

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert [label for label, _ in flow.turns] == ["N startup repair 1"], "复位没治好，照样抢修"
    assert result.env_dead is True


def test_a_broken_build_with_nothing_to_reset_is_not_measured_twice(tmp_path, monkeypatch):
    """复位没改任何东西 ⇒ 不复量：常见的「代码写坏了构建」不多付一次测量的钱。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_ATTEMPTS", "1")
    flow, git = _factory_flow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    o = _make(flow)
    o._runtime_files.add("backend/data.json")   # 观测到了，但与出厂一致（dirty 是空的）
    calls = {"n": 0}

    def runner(specs, g):
        calls["n"] += 1
        return _broken()

    flow.spec_runner = runner

    o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert calls["n"] == 2, "入口一量 + 抢修那一量 —— 没有第三刀"
    assert [c for c in git.calls if c and c[0] == "checkout"] == []


# ---------------------------------------------------------------- #5b 卷子坏 → 自动改道申诉

# 「任何实现都过不了」的那条写法：expect() 拿到的是 async 助手返回的 Promise，
# Playwright 对 locator 断言直接拒绝（`main.spec_structural_defects` 的唯一判据）。
_DEAD_SPEC = ("import { test, expect } from '@playwright/test';\n"
              "async function cellText(page, ref) { return page.getByRole('gridcell'); }\n"
              "test('x', async ({ page }) => {\n"
              "  await expect(cellText(page, 'A1')).toHaveText('42');\n"
              "});\n")

_LIVE_SPEC = ("import { test, expect } from '@playwright/test';\n"
              "test('x', async ({ page }) => {\n"
              "  await expect(page.getByRole('grid')).toBeVisible();\n"
              "});\n")


def _put_spec(tmp_path, name, body):
    """卷子写到盘上 —— 扫描器是**读盘**判的（`_mechanical_defects` → `flow.tests_dir`）。"""
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    (tests / name).write_text(body, encoding="utf-8")


def test_a_mechanically_dead_spec_is_voided_at_the_door_without_a_claim(tmp_path):
    """#5b 卷子坏 → **自动改道申诉**：机器验过的死卷子当场作废，实现一个回合都不烧。

    「死」是扫描器读盘验的（`spec_structural_defects`），不是模型的主张 —— 所以
    没有申辩回合、也没有「先给代码两轮机会」：那条人情是给**主张**的
    （`_maybe_void_spec`），这里给的是事实。模型全程一句话没说，作废照样发生。
    """
    _put_spec(tmp_path, "a.spec.ts", _DEAD_SPEC)
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.authored_suite = True
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts"])
    o = _make(flow)

    result = o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert flow.turns == [], "死卷子不进实现修复队列（一个回合都不烧）"
    assert o.voided_specs == {"a.spec.ts"}
    assert "N" not in flow.spec_map, "作废 = 从节点名下摘掉"
    assert (tmp_path / "tests" / "a.spec.ts").is_file(), "摘的是账，不是文件"
    assert result.passed is True, "摘掉之后这一节点照常验收"
    assert "voided as defective" in result.reason
    tried = "；".join(o.ledger["N"]["tried"])
    assert "mechanically dead" in tried, "账本要写清为什么摘（机器验的，不是模型的嘴）"


def test_a_healthy_spec_is_not_swept_and_still_gets_repaired(tmp_path, monkeypatch):
    """负样本：卷子活得好好的 → 一个字段都不动，照常进修复队列。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    _put_spec(tmp_path, "a.spec.ts", _LIVE_SPEC)
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.authored_suite = True
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts"])
    o = _make(flow)

    o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert o.voided_specs == set(), "活卷子不作废"
    assert flow.spec_map["N"] == ["tests/a.spec.ts"]
    assert [label for label, _ in flow.turns] == ["N batch repair 1/3"], "照旧进修复队列"


def test_only_the_dead_file_is_voided_and_the_rest_still_gets_repaired(tmp_path, monkeypatch):
    """点名作废：节点名下死一份、活一份 → 摘死的，活的照旧修（不是整节点口径）。

    `_void_specs` 是整节点口径（模型主张「我的卷子写错了」时摘的是全部）；
    这条通道是机器点名的，一份就是一份。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    _put_spec(tmp_path, "dead.spec.ts", _DEAD_SPEC)
    _put_spec(tmp_path, "live.spec.ts", _LIVE_SPEC)
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/dead.spec.ts", "tests/live.spec.ts"]})
    flow.authored_suite = True
    # 假件如实回答**被问到的那几份**（真 `run_specs` 只跑点名的卷子）：作废的那份
    # 从此不在任何请求里，它的失败也就不会再出现在测量里。
    flow.spec_runner = lambda specs, g: _summary(
        passed=0, failed=[Path(s).name for s in specs])
    o = _make(flow)

    o._accept("N", list(flow.spec_map["N"]), "N", aggregate=False)

    assert o.voided_specs == {"dead.spec.ts"}, "只摘机器验过的那份"
    assert flow.spec_map["N"] == ["tests/live.spec.ts"], "活的留在名下"
    assert len(flow.turns) == 1
    _, prompt = flow.turns[0]
    assert "these 1 spec file(s) exercise: live.spec.ts" in prompt, "批的范围只有活的这一份"
    assert "was voided as mechanically dead" in prompt, "作废这回事得告诉模型（免得它回头去找）"


def test_the_void_budget_bounds_the_sweep(tmp_path):
    """预算见底 → 死卷子照旧立着：这是一条能删验收的通道，闸门不能没有。"""
    _put_spec(tmp_path, "a.spec.ts", _DEAD_SPEC)
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})
    flow.authored_suite = True
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts"])
    o = _make(flow)
    spent = {f"spent{i}.spec.ts" for i in range(orch._MAX_SPEC_VOIDS)}
    o.voided_specs = set(spent)

    o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert o.voided_specs == spent, "预算用光，谁都不许再摘"
    assert flow.spec_map["N"] == ["tests/a.spec.ts"], "卷子照旧立在名下"
    assert flow.turns == [], "但也不进修复队列 —— 那条断言谁都救不了（#5b）"


def test_an_official_suite_is_not_swept_but_keeps_its_repair_chance(tmp_path, monkeypatch):
    """官方卷子：**不作废**，也**照旧进修复队列** —— 两张牌都得留在桌上。

    不作废是因为判分机跑的是另一份拷贝，本地摘它只是把自己弄瞎；不进队列的
    代价则是永久弃掉同一份卷子里**别的**可修用例（净胜判定是用例级的，一条死
    断言挡不住别的用例被修绿）—— 与省下的几个回合比，代价不对称，所以宁可让它
    进队列、由 park/熔断收口。（实测口径：全排练 406 份官方卷子 0 命中。）
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    _put_spec(tmp_path, "a.spec.ts", _DEAD_SPEC)
    flow = BatchFlow(tmp_path, spec_map={"N": ["tests/a.spec.ts"]})   # 官方档：没有 authored_suite
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts"])
    o = _make(flow)

    o._accept("N", ["tests/a.spec.ts"], "N", aggregate=False)

    assert o.voided_specs == set(), "官方卷子不作废"
    assert flow.spec_map["N"] == ["tests/a.spec.ts"], "照旧立在名下"
    assert [label for label, _ in flow.turns] == ["N batch repair 1/3"], "照旧进修复队列"


# ---------------------------------------------------------------- #8 系统性失败哨兵

def _wide_tree():
    """6 张叶子的树 —— 哨兵要「连续几个」才到线，`_tree()` 的 3 张不够用。"""
    return {
        "id": "REQ-1", "name": "宽树", "children": [
            {"id": "REQ-1.1", "name": "大区", "children": [
                {"id": "1.1.1", "name": "a"},
                {"id": "1.1.2", "name": "b"},
                {"id": "1.1.3", "name": "c"},
                {"id": "1.1.4", "name": "d"},
                {"id": "1.1.5", "name": "e"},
                {"id": "1.1.6", "name": "f"},
            ]},
        ],
    }


def _wide_run(tmp_path, tallies):
    """宽树上跑一遍叶子环：`tallies[i]` = 第 i 张叶子的收尾台账（None = 量不动）。

    叶子按 DFS 序派发（1.1.1 … 1.1.6）。`implement_leaf` 被换成记账 + 返回
    预置结论 —— 哨兵是环上的事，种在环上量它，不掺验收的细节。
    """
    flow = BatchFlow(tmp_path)
    o = orch.Orchestrator(flow)
    o.layers = layerize(_wide_tree())
    calls = []

    def fake(node_id, index, total):
        calls.append(node_id)
        slot = tallies[len(calls) - 1]
        result = orch.NodeResult(node_id=node_id,
                                 passed=bool(slot and slot.get("_passed")))
        if slot and slot.get("_tally") is not None:
            result.own_final = slot["_tally"]
        return result

    o.implement_leaf = fake
    o.implement_leaves()
    return o, calls


def _red(name="a.spec.ts"):
    return {"_tally": {name: (0, 1)}}


def test_a_red_leaf_carries_its_tally_to_the_leaf_loop(tmp_path, monkeypatch):
    """收尾的逐卷台账真的从**测量**里来 —— 一张 0 通过的卷子 → (0, 1)。

    这是哨兵判据的地基：`own_final` 不是猜的，是收尾那次 own 量的账。
    """
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "1")
    monkeypatch.setenv("OCTOS_REPAIR_STALL_LIMIT", "1")   # 一次零收成就 park，快
    flow = BatchFlow(tmp_path, spec_map={"1.1.1": ["tests/a.spec.ts"]})
    flow.spec_runner = lambda specs, g: _summary(passed=0, failed=["a.spec.ts"])
    o = _stub_prompt(_make(flow))

    result = o.implement_leaf("1.1.1", 1, 6)

    assert result.passed is False
    assert result.own_final == {"a.spec.ts": (0, 1)}, "全判不通过的卷子记 (0, 1)"


def test_four_all_red_leaves_in_a_row_stop_the_leaf_line(tmp_path):
    """连红到线：第 4 张全红之后**不再派发**第 5、6 张（github-2 的 29 连败形状）。

    停线不动整跑：叶子环返回，剩下的红交给收尾/对账（各有 #3/#7 止损）。
    """
    o, calls = _wide_run(tmp_path, [_red()] * 6)

    assert calls == ["1.1.1", "1.1.2", "1.1.3", "1.1.4"], "到线即停，后来的叶子不派发"
    assert o._systemic_streak == 4
    assert any("systemic stop" in t for t in o.ledger["1.1.4"]["tried"]), "账本上留痕"


def test_a_spec_with_one_green_test_resets_the_streak(tmp_path):
    """有通过用例的卷子把连击清零 —— 应用不是死的，这正是哨兵要区分的。"""
    o, calls = _wide_run(tmp_path, [
        _red(), _red(), _red(),
        {"_tally": {"b.spec.ts": (1, 2)}},   # 一条绿：应用活着
        _red(), _red(),
    ])

    assert calls == ["1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5", "1.1.6"]
    assert o._systemic_streak == 2, "清零之后重新数"


def test_a_passing_leaf_resets_the_streak(tmp_path):
    """稳稳通过（验收收下）的叶子同样是「应用活着」的证据。"""
    o, calls = _wide_run(tmp_path, [
        _red(), _red(), {"_tally": None, "_passed": True}, _red(), _red(), _red(),
    ])

    assert calls == ["1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5", "1.1.6"]
    assert o._systemic_streak == 3


def test_unmeasured_leaves_neither_count_nor_reset(tmp_path):
    """「量不动」既不计数（它没红）也不清零（不知道不等于没事）—— 链不断也不涨。

    序列 red red 没量成 red red red：若 None 会清零则永远到不了线（6 张全派发）；
    若 None 也计数则第 4 张就该停（只有 3 张红）。事实是第 5 张停 —— 两个性质
    一起钉住。
    """
    o, calls = _wide_run(tmp_path, [
        _red(), _red(), {"_tally": None}, _red(), _red(), _red(),
    ])

    assert calls == ["1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5"], "第 5 张到线"
    assert o._systemic_streak == 4


def test_the_sentinel_threshold_is_configurable_and_can_be_switched_off(tmp_path, monkeypatch):
    """阈值可调（演练校准），`<=0` 关得掉 —— 判死一条线的手，不能没有闸门。"""
    monkeypatch.setenv("OCTOS_SYSTEMIC_STREAK", "2")
    o, calls = _wide_run(tmp_path, [_red()] * 6)
    assert calls == ["1.1.1", "1.1.2"], "N=2 就在第二张停"

    monkeypatch.setenv("OCTOS_SYSTEMIC_STREAK", "0")
    o, calls = _wide_run(tmp_path, [_red()] * 6)
    assert calls == ["1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5", "1.1.6"], "关掉照跑"
    assert o._systemic_streak == 0

    monkeypatch.delenv("OCTOS_SYSTEMIC_STREAK", raising=False)
    assert o._systemic_limit() == 4, "默认 4（计划给的 3~5 取中）"


def test_dry_run_never_trips_the_sentinel(tmp_path, monkeypatch):
    """DRYRUN 的 0 通过不是测量，是摆设 —— 拿它停线只会让冒烟走不完整条线。"""
    monkeypatch.setenv("OCTOS_ARC_DRYRUN", "1")
    o, calls = _wide_run(tmp_path, [_red()] * 6)

    assert calls == ["1.1.1", "1.1.2", "1.1.3", "1.1.4", "1.1.5", "1.1.6"]
    assert o._systemic_streak == 0


# ---------------------------------------------------------------- #9 接盘归一 + 冷启动预检

def _handover_flow(tmp_path, specs=(("big.spec.ts", 4000), ("small.spec.ts", 40))):
    """接盘站的假流程：**自产卷** + 有 runner + 盘上真有卷子（预检要从盘上挑量尺）。

    `specs` 是 (文件名, 字节数) —— 大小是真的写在盘上的，量尺的挑选（最小那份）
    因此量的是真判据，不是桩。
    """
    flow = BatchFlow(tmp_path)
    tests_dir = Path(flow.tests_dir)
    tests_dir.mkdir(parents=True, exist_ok=True)
    for name, size in specs:
        (tests_dir / name).write_text("x" * size, encoding="utf-8")
    flow.authored_suite = True
    flow.runner = object()          # 非 None = 有 Playwright（这条路上只看在不在）
    flow.has_app = lambda: True     # 接盘 = 有楼可接
    return flow


def test_a_clean_handover_is_measured_once_with_the_smallest_spec(tmp_path):
    """阴性（正常接盘）：量一次最小卷子、零回合、不判死、不复位。"""
    flow = _handover_flow(tmp_path)
    calls = []

    def runner(specs, g):
        calls.append(list(specs))
        return _summary(passed=1, file="small.spec.ts")

    flow.spec_runner = runner
    o = _make(flow)

    o.preflight()

    assert calls == [["small.spec.ts"]], "量尺 = 盘上最小的一份；健康接盘只量这一次"
    assert flow.turns == [], "健康接盘一个模型回合都不烧"
    assert o._startup_given_up is False
    assert "_preflight" not in o.ledger, "常态不打字：没病就没这笔账"


def test_a_dirty_handover_is_normalized_and_rechecked_from_factory(tmp_path):
    """阳性（github-2 形态，可归一）：随包态与出厂态不同 → 复位 → **从出厂态**
    再量一次才算冷启动成功（判分环境看到的就是出厂口径）。"""
    flow = _handover_flow(tmp_path)
    calls = []

    def runner(specs, g):
        calls.append(list(specs))
        return _summary(passed=1, file="small.spec.ts")

    flow.spec_runner = runner
    o = _make(flow)
    resets = []
    o._reset_runtime_state = lambda why: (resets.append(why), True)[1]

    o.preflight()

    assert calls == [["small.spec.ts"]] * 2, "归一动过东西：出厂态那次复量不能省"
    assert resets == ["the handover cold-start check"], "复位要说清为什么（why 进日志）"
    assert flow.turns == [], "残渣之病不用模型掏一个回合"
    assert o._startup_given_up is False
    assert o.ledger["_preflight"]["status"] == "green"


def test_a_handover_that_only_starts_on_factory_state_is_cured_by_the_reset(tmp_path):
    """阳性（github-2 那一幕的正面）：随包态**起不来**、出厂态起得来 —— 0 回合。

    序列：第一量坏 → 复位（观测集已由第一量填上，这正是「开盘量一下」的价值）
    → 第二量好。
    """
    flow = _handover_flow(tmp_path)
    calls = []

    def runner(specs, g):
        calls.append(list(specs))
        return _broken() if len(calls) == 1 else _summary(passed=1, file="small.spec.ts")

    flow.spec_runner = runner
    o = _make(flow)
    o._reset_runtime_state = lambda why: True

    o.preflight()

    assert len(calls) == 2
    assert flow.turns == [], "复位能治的起动病不花一个模型回合（与 #5f 双态同一手）"
    assert o._startup_given_up is False
    assert o.ledger["_preflight"]["status"] == "green"


def test_a_broken_handover_is_repaired_at_open(tmp_path):
    """随包态、出厂态都起不来（或复位不了）→ 开盘就抢修：量尺写对、回合记账。"""
    flow = _handover_flow(tmp_path)
    calls = []

    def runner(specs, g):
        calls.append(list(specs))
        if len(calls) == 1:
            return _broken("npm start: listen EADDRINUSE")
        return _summary(passed=1, file="small.spec.ts")

    flow.spec_runner = runner
    o = _make(flow)
    o._reset_runtime_state = lambda why: False

    o.preflight()

    assert [label for label, _ in flow.turns] == ["preflight startup repair 1"]
    prompt = flow.turns[0][1]
    assert "the handover cold-start check (small.spec.ts) fails at startup" in prompt, \
        "提示词要说真话：量尺是这一站，不是某个节点"
    assert "EADDRINUSE" in prompt, "错误原文进抢修回合（与 #5a 同一手）"
    assert flow.snapshots == ["preflight startup repair"]
    assert calls[1] == ["small.spec.ts"], "抢修的复测量的是同一把尺"
    assert o._startup_given_up is False


def test_a_hopeless_handover_is_given_up_before_the_blueprint(tmp_path, monkeypatch):
    """治不好 → **当场**记本跑判死：后面的节点不再各烧一轮（比入口判死更早一格）。"""
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_ATTEMPTS", "2")
    flow = _handover_flow(tmp_path)
    flow.spec_runner = lambda specs, g: _broken()
    o = _make(flow)
    o._reset_runtime_state = lambda why: False

    o.preflight()

    assert [label for label, _ in flow.turns] == ["preflight startup repair 1",
                                                  "preflight startup repair 2"]
    assert flow.restores == ["snap1", "snap1"], "每次尝试退回同一个入口，放弃时也退"
    assert o._startup_given_up is True, "判死当场记上"
    assert "gave up after 2 attempt(s)" in "；".join(o.ledger["_preflight"]["tried"])


def test_the_preflight_skips_official_suites_and_runs_without_a_ruler(tmp_path):
    """三扇静默门：官方卷（跨项约定）／没有 runner／没有 app 或没有卷子。"""
    # 官方卷：路径不下水
    flow = _handover_flow(tmp_path)
    flow.authored_suite = False
    calls = []
    flow.spec_runner = lambda specs, g: (calls.append(list(specs)), _summary(passed=1))[1]
    o = _make(flow)
    o.preflight()
    assert calls == [] and o._preflighted is False, "官方卷路径一字不动"

    # 没有 runner：没有量尺
    f2 = _handover_flow(tmp_path / "b")
    f2.runner = None
    calls2 = []
    f2.spec_runner = lambda specs, g: (calls2.append(list(specs)), _summary(passed=1))[1]
    orch.Orchestrator(f2).preflight()
    assert calls2 == [], "没有 Playwright 就没有量尺"

    # 毛坯（还没有 app）：没有「接盘」这回事
    f3 = _handover_flow(tmp_path / "c")
    f3.has_app = lambda: False
    calls3 = []
    f3.spec_runner = lambda specs, g: (calls3.append(list(specs)), _summary(passed=1))[1]
    orch.Orchestrator(f3).preflight()
    assert calls3 == [], "楼还没有，冷启动预检没有对象"

    # 盘上一份卷子都没有
    f4 = _handover_flow(tmp_path / "d", specs=())
    calls4 = []
    f4.spec_runner = lambda specs, g: (calls4.append(list(specs)), _summary(passed=1))[1]
    orch.Orchestrator(f4).preflight()
    assert calls4 == [], "没有卷子就没有量尺"


def test_the_open_check_happens_once_across_both_call_sites(tmp_path):
    """预检挂在两处（接盘站 + `run()` 开场），同一实例只量一次 —— 重复没有新信息。"""
    flow = _handover_flow(tmp_path)
    calls = []
    flow.spec_runner = lambda specs, g: (calls.append(list(specs)),
                                         _summary(passed=1, file="small.spec.ts"))[1]
    o = _make(flow)

    o.preflight()
    o.preflight()

    assert len(calls) == 1, "开盘只量一次：重复预检只多付一次构建的钱"


def test_the_preflight_never_sinks_the_run(tmp_path):
    """辅助动作绝不变成新的故障点：盘炸了也只是日志里一句话。"""
    flow = _handover_flow(tmp_path)
    o = _make(flow)

    def boom():
        raise RuntimeError("disk on fire")

    o._all_specs = boom
    o.preflight()               # 抛不出来 = 通过

    assert o._preflighted is True, "先钉住：异常不重来（重来只是再炸一次）"


def test_run_preflights_before_it_surveys(tmp_path):
    """`run()` 的接线：预检排在最前 —— 勘察/蓝图/骨架看的是治过之后的现场。"""
    order = []
    flow = _handover_flow(tmp_path)
    o = orch.Orchestrator(flow)
    o.preflight = lambda: order.append("preflight")
    o.survey = lambda: order.append("survey")
    o.blueprint = lambda: order.append("blueprint")
    o.skeleton = lambda: order.append("skeleton")
    o.expand_all = lambda: order.append("expand")
    o.implement_leaves = lambda: order.append("leaves")
    o.diagnose_parked = lambda kind: order.append("diagnose")
    o.finalize = lambda: order.append("finalize")
    o._ledger_write = lambda: order.append("ledger")

    o.run(_tree())

    assert order == ["preflight", "survey", "blueprint", "skeleton", "expand", "leaves",
                     "diagnose", "finalize", "ledger"]
