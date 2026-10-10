"""编排主循环的测试。

这一层跑不动真模型、真浏览器、真 git，所以用一个假 Flow。但**三个不变式
是可以在这里精确断言的** —— 它们本来就是关于「什么时候提交、什么时候验收」
的判据，不是关于模型输出质量的。

    ① 实现从大到小   节点开工时，祖先契约一定在它的提示词里
    ② 验收从小到大   全子节点通过之前，父节点不被验收
    ③ 单调性         基线没绿就不提交，且失败信息点名破了谁
"""

import inspect
import json
import re
import sys
import types

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
    """造一个 RunSummary。`failed` 是失败用例的 spec 文件名列表。"""
    results = [_TestOutcome(title=f"pass-{i}", ok=True, status="passed", duration_ms=1, file=file)
               for i in range(passed)]
    for i, name in enumerate(failed):
        results.append(_TestOutcome(title=f"fail-{i}", ok=False, status="failed",
                                   duration_ms=1, file=name, message="boom"))
    return RunSummary(passed=passed, total=passed + len(failed), results=results, error=error)


class Git:
    def __init__(self, flow):
        self.flow = flow

    def run(self, args, check=True):
        self.flow.git_calls.append(list(args))
        out = ""
        if args[:2] == ["status", "--porcelain"]:
            # porcelain 的形状：`XY <path>`，未跟踪是 `?? <path>`，重命名是
            # `XY <old> -> <new>`。`dirty_files` 里以 `?? ` 开头的当成新建。
            lines = []
            for entry in self.flow.dirty_files:
                lines.append(entry if entry[:2] in ("??", " M", "M ", "A ", " D", "R ")
                             else f" M {entry}")
            out = "\n".join(lines)
        return types.SimpleNamespace(stdout=out, returncode=0)


class FakeFlow:
    """只实现编排真正用到的那一面。多一个方法都不给 —— 多了就说明编排伸手太长了。"""

    def __init__(self, tmp_path, spec_map=None, dirty_files=()):
        self.output_dir = tmp_path
        self.req_dir = tmp_path / "requirements"
        self.tests_dir = tmp_path / "tests"
        self.smoke_port = 3100
        self.web_port = 3000
        self.spec_map = dict(spec_map or {})
        self.dirty_files = list(dirty_files)

        self.repair_rounds = 2
        self.min_repair_seconds = 0
        self.node_timeout = 1200
        self.node_budget_cap = 3000
        self.implement_fraction = 0.6

        # 回合的回话。编排平时不读它，只读一件事：它有没有报契约缺陷。
        self.reply = ""
        self.turns = []
        self.read_only_turns = []
        self.turn_timeouts = []
        self.commits = []
        self.restores = []
        self.marks = []
        self.git_calls = []
        self.app_built = 0

        self._head = "sha0"
        self.runtime = types.SimpleNamespace(git=Git(self))
        self.spec_runner = lambda specs, grader_like: _summary(passed=1, file=specs[0] if specs else "")

    # -- 编排用到的那一面 --
    def head(self):
        return self._head

    def commit(self, message):
        self.commits.append(message)
        self._head = f"sha{len(self.commits)}"
        return True

    def restore_app(self, sha):
        self.restores.append(sha)
        self._head = sha

    def mark(self, kind, node_id, message=None):
        self.marks.append((kind, node_id, message))

    def turn(self, prompt, timeout, label, read_only=False):
        self.turns.append((label, prompt))
        self.turn_timeouts.append((label, timeout))
        if read_only:
            self.read_only_turns.append(label)
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

    def banked_surplus(self, index, total):
        return 0.0

    def app_server(self, grader_like):
        self.app_built += 1
        return types.SimpleNamespace(build=lambda: None, start=lambda: None, stop=lambda: None)

    # 提示词槽位（_implement_prompt 会 import main，那个测试里单独替换）
    def has_app(self):
        return True

    def tests_prompt_for(self, node_id, skeleton=False):
        # 真实签名是 (node_id, skeleton=False)：L1 骨架轮传 skeleton=True，
        # 拿到的是一段「只读共享 helper、别急着实现功能」的文本。这里保持同一个
        # 形状，是为了让骨架轮那条路径在测试里也是真的被走到。
        return f"TESTS for {node_id} skeleton={skeleton}\n"

    def ui_contract(self):
        return ""

    def perf_text(self):
        return ""

    def verify_text(self, total):
        return ""


@pytest.fixture(autouse=True)
def _legacy_repair_path(monkeypatch):
    """这个文件钉的是**旧的整体式修复路径**（`_accept_legacy`）的判据。

    分批修复是默认路径（`OCTOS_ATOMIC_REPAIR=1`），它有自己的测试文件
    （`test_atomic_repair.py`）。这里显式关掉 —— 这个开关存在的意义就是
    A/B 对照：旧路径的语义必须一字不动，新路径的判据单独钉。"""
    monkeypatch.setenv("OCTOS_ATOMIC_REPAIR", "0")


@pytest.fixture
def _flow(tmp_path):
    return FakeFlow(tmp_path)


def _make(flow):
    o = orch.Orchestrator(flow)
    o.layers = layerize(_tree())
    return o


def _stub_prompt(o):
    """`_implement_prompt` 要 import main（几十个模块）。测验收判据的用例不需要它。"""
    o._implement_prompt = lambda node_id: f"IMPLEMENT {node_id}"
    return o


# ---------------------------------------------------------------- 不变式 ②

def test_parent_is_not_verified_until_every_child_passes(_flow):
    """**不变式 ②。** 一个子节点通过，父节点不该动。"""
    o = _make(_flow)
    verified = []
    o.accept_aggregate = lambda node_id: verified.append(node_id)

    o.verdict["1.1.1"] = True
    o._aggregate_from("1.1.1")
    assert verified == [], "1.1.2 还没过，REQ-1.1 不该被验收"

    o.verdict["1.1.2"] = True
    o._aggregate_from("1.1.2")
    assert verified == ["REQ-1.1"], "两个孩子都过了，REQ-1.1 应该自动变可验收"


def test_verification_climbs_from_a_leaf_all_the_way_to_the_root(_flow):
    """从小到大是**递归**的：REQ-1.1 过了，REQ-1 也可能因此变得完整。"""
    o = _make(_flow)
    verified = []
    o.accept_aggregate = lambda node_id: (verified.append(node_id), o.verdict.__setitem__(node_id, True))

    for leaf in ("1.1.1", "1.1.2", "1.2.1"):
        o.verdict[leaf] = True
    o._aggregate_from("1.1.2")
    o._aggregate_from("1.2.1")
    assert verified == ["REQ-1.1", "REQ-1.2", "REQ-1"]


def test_a_failed_sibling_blocks_the_parent_forever(_flow):
    """False 和「没做过」一样挡住父节点 —— 不然「全子树都通过」就是句空话。"""
    o = _make(_flow)
    verified = []
    o.accept_aggregate = lambda node_id: verified.append(node_id)

    o.verdict.update({"1.1.1": True, "1.1.2": False, "1.2.1": True})
    o._aggregate_from("1.2.1")
    assert verified == ["REQ-1.2"]
    assert "REQ-1" not in verified


# ---------------------------------------------------------------- 不变式 ③

def test_baseline_break_blocks_the_commit_and_names_what_broke(_flow):
    """**不变式 ③。** 自己的 spec 过了但基线红了 → 不提交，且说清楚破了谁。"""
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"]}
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts"]

    calls = []

    def runner(specs, grader_like):
        calls.append((specs, grader_like))
        if grader_like:
            return _summary(passed=0, failed=["a.spec.ts"])  # 基线红了
        return _summary(passed=1, file="1.1.1.spec.ts")      # 自己的过了

    _flow.spec_runner = runner
    result = o._accept("1.1.1", ["1.1.1.spec.ts"], "1.1.1", aggregate=False)

    assert result.passed is False
    assert result.own_passed is False
    assert result.baseline_ok is False
    assert _flow.commits == [], "基线没绿就不许提交 —— 这是 ③ 变成结构的地方"
    assert "REQ-1.1" in result.reason, "失败信息必须点名破了谁"
    assert "were passing before you started" in result.reason


def test_green_baseline_commits_and_the_node_joins_the_baseline(_flow):
    """通过了就提交，而且它自己的 spec 从此成为底线的一部分。"""
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts"]
    o._baseline_seen = {"a.spec.ts"}
    _flow.spec_runner = lambda specs, grader_like: _summary(passed=1, file=specs[0] if specs else "")

    result = o._accept("1.1.1", ["1.1.1.spec.ts"], "1.1.1", aggregate=False)

    assert result.passed is True
    assert _flow.commits == ["1.1.1: accepted"]
    assert "1.1.1.spec.ts" in o.baseline_specs


def test_an_empty_baseline_does_not_block_the_first_node(_flow):
    """第一个通过的节点没东西可破坏 —— 基线是空的，不该因此卡住。"""
    o = _make(_flow)
    _flow.spec_runner = lambda specs, grader_like: _summary(passed=1, file=specs[0] if specs else "")
    assert o._accept("1.1.1", ["1.1.1.spec.ts"], "1.1.1", aggregate=False).passed is True


# ---------------------------------------------------------------- 基线的范围

def test_scope_is_narrow_until_an_aggregation_point(_flow):
    """**风险表 #3。** 全量基线很贵，所以平时只查「依赖我的那些节点」的 spec。"""
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"], "1.2.1": ["b.spec.ts"]}
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts", "b.spec.ts"]

    # 1.1.1 的依赖者只有 REQ-1.1 / REQ-1 —— b.spec.ts 属于 1.2.1，与它无关
    assert o._scope_specs("1.1.1", aggregate=False) == ["a.spec.ts"]
    # 聚合点是全量
    assert o._scope_specs("1.1.1", aggregate=True) == ["a.spec.ts", "b.spec.ts"]


def test_touching_a_shared_file_widens_the_scope_to_everything(_flow):
    """碰了契约点名的共享文件，就当它能弄坏任何人。"""
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"], "1.2.1": ["b.spec.ts"]}
    _flow.dirty_files = ["backend/db.js"]
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts", "b.spec.ts"]
    o.contract = Contract(global_contract={"shared": ["backend/db.js"]})

    assert o._touched_shared() is True
    assert o._scope_specs("1.1.1", aggregate=False) == ["a.spec.ts", "b.spec.ts"]


def test_an_unrelated_edit_does_not_widen_the_scope(_flow):
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"]}
    _flow.dirty_files = ["frontend/src/app.js"]
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts"]
    o.contract = Contract(global_contract={"shared": ["backend/db.js"]})
    assert o._touched_shared() is False


def test_a_newly_created_shared_file_widens_the_scope_too(_flow):
    """**新建**的共享文件也要能触发全量基线。

    这是原先那条判据的一个洞：它用 `git diff --name-only HEAD`，只列**已跟踪
    文件**的修改。而节点在验收之前是不提交的（提交发生在 `_accept` 里），所以
    「新建一个共享模块」——恰恰是共享文件最常见的来历——在那道检查眼里是隐形的。

    失效的后果不是「少查一次」：这一条是唯一会把范围放大到全量基线的机制，
    它瞎了，动共享文件的节点就只查「依赖我的那几条」，而它弄坏的多半在别的
    分支上（那 31/47 个没人依赖的叶子就是这么漏出去的）。
    """
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"], "1.2.1": ["b.spec.ts"]}
    _flow.dirty_files = ["?? backend/db.js"]          # 未跟踪 = 这次才建的
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts", "b.spec.ts"]
    o.contract = Contract(global_contract={"shared": ["backend/db.js"]})

    assert o._touched_shared() is True
    assert o._scope_specs("1.1.1", aggregate=False) == ["a.spec.ts", "b.spec.ts"]


def test_a_renamed_file_counts_on_both_ends(_flow):
    """重命名时两端都算碰过 —— 改名也是动它。"""
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"], "1.2.1": ["b.spec.ts"]}
    _flow.dirty_files = ["R  backend/old.js -> backend/db.js"]
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts", "b.spec.ts"]
    o.contract = Contract(global_contract={"shared": ["backend/db.js"]})
    assert o._touched_shared() is True


# ---------------------------------------------------------------- 红卷子记账

def test_a_repair_round_does_not_wash_away_the_charge(_flow):
    """**修不动就等一轮 = 没有基线。** 这是记账那段最尖的角。

    「早就红的」必须是相对**这个节点开工那一刻**说的。如果在 `_baseline_break`
    里边判边读最新名单，节点会自己把自己洗白：第 1 轮它弄红了 `a.spec.ts`
    （记账进去了），第 2 轮再跑同一份，它已经是「名单上的」→ 判定成「不是它弄的」
    → 返回 None → **节点通过**。于是修复轮变成了免责轮：什么都不改，等一轮就过。

    所以判据钉在开工快照上。这个用例就是钉子的验收：三个回合每一轮都要挨骂。
    """
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"]}
    _flow.repair_rounds = 2
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts"]

    def runner(specs, grader_like):
        if grader_like:
            return _summary(passed=0, failed=["a.spec.ts"])   # 基线一直是红的
        return _summary(passed=1, file="1.1.1.spec.ts")       # 自己的每次都过

    _flow.spec_runner = runner
    result = o._accept("1.1.1", ["1.1.1.spec.ts"], "1.1.1", aggregate=False)

    assert result.passed is False, "自己没修好，等一轮不该变成通过"
    assert result.exhausted is True
    assert result.turns == 2, "三个回合（1 + 2 次修复）都该挨骂，一次都不许提前放行"
    assert _flow.commits == []
    assert o.baseline_red == {"a.spec.ts"}, "骂完了也要记账，后面的节点才不用替它接"


def test_a_node_is_not_charged_for_a_spec_that_was_already_red(_flow):
    """**别冤枉后来人。** 红卷子记在账上，后来的节点不为它花修复轮。

    原先没有这本账：`_baseline_break` 每次都拿全量基线跟「全过」比，红的就说
    「Your change made N spec file(s) fail」。一个静默的破坏（弄红它的那个节点
    当时根本没查这一片，见 `_scope_specs` 的空范围）会一路冤枉下去：每个后续
    聚合点都挨一次骂、烧掉修复轮、最后 `_abandon_region` 把整个区域判死。
    """
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"]}
    _flow.repair_rounds = 2
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts"]
    o.baseline_red = {"a.spec.ts"}     # 前面某个节点已经看见它红了

    def runner(specs, grader_like):
        if grader_like:
            return _summary(passed=0, failed=["a.spec.ts"])
        return _summary(passed=1, file="1.1.1.spec.ts")

    _flow.spec_runner = runner
    result = o._accept("1.1.1", ["1.1.1.spec.ts"], "1.1.1", aggregate=False)

    assert result.passed is True, "早就红的卷子不该拦下这个节点"
    assert result.turns == 0, "更不该为它烧掉一轮修复"
    assert _flow.commits == ["1.1.1: accepted"]


def test_a_green_run_clears_the_ledger(_flow):
    """后来某个节点把它修好了 → 摘掉。账是**状态**不是**历史**。"""
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"]}
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts"]
    o.baseline_red = {"a.spec.ts"}
    _flow.spec_runner = lambda specs, grader_like: _summary(passed=1, file="a.spec.ts")

    assert o._accept("1.1.1", [], "1.1.1", aggregate=False).passed is True
    assert o.baseline_red == set()


def test_a_foreign_branch_failure_is_not_claimed_as_your_doing(_flow):
    """第一个看见静默破坏的人仍然要接（不接就没人修），但**不许替它编因果**。

    这里被验收的是 `REQ-1.1`（1.1 那一支的聚合点），红卷子 `b.spec.ts` 是
    1.2 那一支的。**这个形状不是编的**：叶子的范围本来就被 `_scope_specs` 收窄成
    「依赖我的那些」，它看不见外分支；只有聚合点会面对全量基线 —— 所以静默破坏
    被发现的现场，几乎总是某个聚合点。而按依赖图它够不着 1.2.1，对着
    「Your change made...」去找一个自己没做过的改动，修复轮就是白烧的。
    """
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"], "1.2.1": ["b.spec.ts"]}
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts", "b.spec.ts"]

    def runner(specs, grader_like):
        if grader_like:                       # 全量基线：自己的绿，别人的红
            return _summary(passed=1, failed=["b.spec.ts"], file="a.spec.ts")
        return _summary(passed=1, file="a.spec.ts")

    _flow.spec_runner = runner
    result = o._accept("REQ-1.1", ["a.spec.ts"], "REQ-1.1", aggregate=True)

    assert result.passed is False, "不绿不验收 —— 这一条没有松"
    assert "1.2.1" in result.reason, "要说清楚卷子是谁的"
    assert "does not put downstream of you" in result.reason
    assert "Your change made" not in result.reason, "别替模型编一个它没做过的因果"
    assert _flow.commits == []


def test_your_own_breakage_is_still_stated_as_yours(_flow):
    """反向：真的弄红了下游的卷子，措辞照旧是「你弄的」，不许含糊过去。"""
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"]}
    o = _make(_flow)
    o.baseline_specs = ["a.spec.ts"]

    def runner(specs, grader_like):
        if grader_like:
            return _summary(passed=0, failed=["a.spec.ts"])
        return _summary(passed=1, file="1.1.1.spec.ts")

    _flow.spec_runner = runner
    result = o._accept("1.1.1", ["1.1.1.spec.ts"], "1.1.1", aggregate=False)

    assert "spec file(s) that were passing before you started are failing now" in result.reason
    assert "does not put downstream of you" not in result.reason


# ---------------------------------------------------------------- 不变式 ①

def test_a_node_cannot_start_without_its_ancestors_contract(_flow, monkeypatch):
    """**不变式 ①。** 祖先的切片必须在它的提示词里 —— 这是「从大到小」的物理形态。"""
    fake_main = types.ModuleType("main")
    # 形状照抄真的 NODE_PROMPT：尾部带 PORT_RULES，所以要 {smoke} 和 {port}
    fake_main.NODE_PROMPT = ("{preamble}\n{node_spec}\n{design}\n{ancestors}\n{tests}{ui}{performance}{verify}"
                             "\nPorts: smoke={smoke}, never bind {port}.\n")
    fake_main.NODE_PREAMBLE_EXTEND = "Implement {node_id} in the existing app (port {port}).\n"
    fake_main.NODE_PREAMBLE_CREATE = "Build {node_id} from {req_dir} (port {port}).\n"
    fake_main.describe_node = lambda node: f"NODE {node.get('id')} {node.get('name')}"
    monkeypatch.setitem(sys.modules, "main", fake_main)

    o = _make(_flow)
    o.contract = Contract(
        global_contract={"stack": "node", "routes": []},
        slices={
            "REQ-1": {"owns": ["everything"], "children": {"REQ-1.1": "search"}},
            "REQ-1.1": {"owns": ["backend/search.js"], "provides": ["GET /api/search"]},
        },
    )
    prompt = o._implement_prompt("1.1.1")

    assert "backend/search.js" in prompt, "父节点的切片必须在"
    assert "GET /api/search" in prompt, "父节点提供的东西必须在 —— 不然孩子要猜"
    assert "everything" in prompt, "根的契约也必须一路传下来"
    assert "Which requirement node" not in prompt


def test_the_prompt_states_who_depends_on_this_node(_flow, monkeypatch):
    """「谁依赖我」是修复视野的向前那一边。"""
    fake_main = types.ModuleType("main")
    fake_main.NODE_PROMPT = "{preamble}\n{design}\nPorts: smoke={smoke}, never bind {port}.\n"
    fake_main.NODE_PREAMBLE_EXTEND = "Implement {node_id} (port {port}).\n"
    fake_main.NODE_PREAMBLE_CREATE = "Build {node_id} (port {port}).\n"
    fake_main.describe_node = lambda node: f"NODE {node.get('id')}"
    monkeypatch.setitem(sys.modules, "main", fake_main)

    o = _make(_flow)
    o.contract = Contract(global_contract={"stack": "node"})
    prompt = o._implement_prompt("1.1.2")
    assert "REQ-1.1" in prompt and "REQ-1" in prompt
    assert "Smoke" not in prompt and "smoke=3100" in prompt, "端口纪律必须照旧生效"


# ---------------------------------------------------------------- 回滚

def test_a_node_that_broke_the_baseline_gets_rolled_back(_flow):
    """③ 的字面意思：破坏 = 立刻回滚。"""
    o = _stub_prompt(_make(_flow))
    _flow._head = "sha-before"
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"]}
    o.baseline_specs = ["a.spec.ts"]
    _flow.spec_runner = lambda specs, grader_like: (
        _summary(passed=0, failed=["a.spec.ts"]) if grader_like
        else _summary(passed=1, file="1.1.1.spec.ts"))
    o._can_repair = lambda guard: False  # 没本钱修了

    o.implement_leaf("1.1.1", 1, 2)

    assert _flow.restores == ["sha-before"]
    assert o.verdict["1.1.1"] is False


def test_a_node_that_simply_did_not_finish_is_not_rolled_back(_flow):
    """没做完 ≠ 破坏了谁。半成品是后面那次集成修复的原料，丢掉等于白付一次回合的钱。"""
    o = _stub_prompt(_make(_flow))
    _flow._head = "sha-before"
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"]}
    _flow.spec_runner = lambda specs, grader_like: _summary(passed=0, failed=["1.1.1.spec.ts"])
    o._can_repair = lambda guard: False

    o.implement_leaf("1.1.1", 1, 2)

    assert _flow.restores == []
    assert o.verdict["1.1.1"] is False
    assert _flow.commits == []


# ---------------------------------------------------------------- L1 骨架

def test_skeleton_verification_builds_and_probes_routes(_flow):
    """L1 的完成标准是**可机械验证**的：build 过 + 声明的路由可达。"""
    o = _make(_flow)
    o.contract = Contract(global_contract={"routes": [{"method": "GET", "path": "/api/x"}]})
    o.contract_path = _flow.output_dir / "contract.json"
    o._probe_one = lambda method, path: (200, "[]")

    assert o.verify_skeleton() is None
    assert _flow.app_built == 1


def test_the_skeleton_turn_carries_the_shared_test_conventions(_flow):
    """L1 的提示词必须带上 `{tests}` 槽位，而且是 skeleton 变体。

    测试装置的约定（base URL、导航、页头）是**跨节点共享面**：架子搭的时候不知道，
    后面每个叶子都要替它还这笔债 —— 每个节点各自猜一次形状，契约就白签了。
    """
    o = _make(_flow)
    o.contract = Contract()
    o.contract.path = None  # 没有契约 → 走「从需求文档自己推出形状」那一支
    o._probe_one = lambda method, path: (200, "[]")

    o.skeleton()

    label, prompt = _flow.turns[-1]
    assert label == "skeleton build"
    assert "TESTS for None skeleton=True" in prompt
    assert "Read the requirement document at" in prompt and str(_flow.req_dir) in prompt


def test_a_missing_route_fails_the_skeleton(_flow):
    o = _make(_flow)
    o.contract = Contract(global_contract={"routes": [{"method": "GET", "path": "/api/x"}]})
    o._probe_one = lambda method, path: (404, "<pre>Cannot GET /api/x</pre>")

    err = o.verify_skeleton()
    assert err is not None
    assert "GET /api/x" in err and "not mounted" in err


def test_a_route_that_answers_404_with_data_is_not_a_missing_route(_flow):
    """`GET /api/q/:id` 取不到记录也是 404 —— 那是路由在，不是路由缺失。"""
    o = _make(_flow)
    o.contract = Contract(global_contract={"routes": [{"method": "GET", "path": "/api/q/:id"}]})
    o._probe_one = lambda method, path: (404, '{"error":"no such question"}')

    assert o.verify_skeleton() is None


def test_a_route_that_returns_500_still_counts_as_reachable(_flow):
    """L1 的标准是「架子在」，不是「功能对」。占位实现返回 500 也算挂上了。"""
    o = _make(_flow)
    o.contract = Contract(global_contract={"routes": [{"method": "GET", "path": "/api/x"}]})
    o._probe_one = lambda method, path: (500, "not implemented")

    assert o.verify_skeleton() is None


def test_an_empty_contract_declares_no_routes_so_nothing_is_probed(_flow):
    o = _make(_flow)
    o.contract = Contract(global_contract={})
    o._probe_one = lambda method, path: pytest.fail("不该探任何路由")
    assert o.verify_skeleton() is None


# ---------------------------------------------------------------- 计划序
#
# 叶子按什么顺序做，**不是 harness 排的**：L0 蓝图给顶层顺序，逐层展开给每一层
# 孩子的顺序，编排只是把那份计划摊平成一条执行线。上层没说的地方退回文档序。

def test_the_plan_decides_the_order_not_the_depth(_flow):
    """顶层顺序来自 L0 的 `order`，层内顺序来自那一层的切片。"""
    o = _make(_flow)
    o.contract = Contract(
        global_contract={"order": ["REQ-1.2", "REQ-1.1"]},
        slices={"REQ-1.1": {"order": ["1.1.2", "1.1.1"]}},
    )
    assert o.plan_order() == ["1.2.1", "1.1.2", "1.1.1"]


def test_without_a_plan_the_document_order_is_used(_flow):
    """没计划 = 树的顺序。文档序本身就是前序，也就是「一个区域连着下一个区域」。"""
    o = _make(_flow)
    assert o.plan_order() == ["1.1.1", "1.1.2", "1.2.1"]


def test_a_plan_naming_something_unknown_does_not_drop_a_node(_flow):
    """计划里写了不存在的 id，那个节点不能被静默丢掉 —— 认不出的补在后面。"""
    o = _make(_flow)
    o.contract = Contract(global_contract={"order": ["REQ-1.2", "REQ-9.9"]})
    assert o.plan_order() == ["1.2.1", "1.1.1", "1.1.2"]


def _ragged_tree():
    """第一个区域比第二个**深一层** —— 这正是深度序会打乱计划的地方。"""
    return {"id": "TL", "name": "总", "children": [
        {"id": "A", "name": "浅区", "children": [
            {"id": "A.1", "name": "a1", "children": [{"id": "A.1.1", "name": "深叶"}]}]},
        {"id": "B", "name": "深区", "children": [
            {"id": "B.1", "name": "b1"}, {"id": "B.2", "name": "b2"}]},
    ]}


def test_one_region_is_finished_before_the_next_one_starts(tmp_path):
    """旧版按深度 BFS：浅的叶子先冒头，一个区域的事被另一个区域横插进来。

    在这棵树上，深度序是 [B.1, B.2, A.1.1]（B 区的叶子浅），计划序是
    [A.1.1, B.1, B.2]。后者才是「A 区做完再开 B 区」。
    """
    flow = FakeFlow(tmp_path)
    o = orch.Orchestrator(flow)
    o.layers = layerize(_ragged_tree())

    by_depth = sorted(o.layers.order, key=lambda n: o.layers.by_id[n].depth)
    assert by_depth == ["TL", "A", "B", "A.1", "B.1", "B.2", "A.1.1"], "深度序的老样子"
    assert o.plan_order() == ["A.1.1", "B.1", "B.2"]


# ---------------------------------------------------------------- 得分区域
#
# 比赛按**区域**给分：区域里有一个没过，这个区域就没分了。所以一个节点修不动
# 之后，区域里还没做的兄弟不再实现 —— 但**区域之间不传染**：4.2 没了，4.3 照做。

def _always_fails(specs, grader_like):
    return _summary(passed=0, failed=list(specs) or ["x.spec.ts"])


def test_a_dead_leaf_takes_its_whole_region_with_it(_flow, monkeypatch):
    """区域连坐的旧行为 —— **v5 起要显式打开**（`OCTOS_ABANDON_REGION=1`）。

    修复轮用完了还是没过 ⟹ 判死，它所在的区域（含没做的兄弟）一起放弃。
    比赛按区域给分，旧逻辑是「这个区域已经拿不到分了，别再花钱」。v5 把它
    降级成可选：增量维护里，一个要求点修不动不代表整个区域没价值（更可能是
    一条卷子写错了），连坐会让没做错的兄弟白白丢分。
    """
    monkeypatch.setenv("OCTOS_ABANDON_REGION", "1")
    o = _stub_prompt(_make(_flow))
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"]}
    _flow.spec_runner = _always_fails

    o.implement_leaf("1.1.1", 1, 3)

    assert o.verdict["1.1.1"] is False
    # 区域 = 父节点，加上它的全部后代，加上「要它成立才拿得到分」的祖先
    assert set(o.abandoned) == {"1.1.1", "1.1.2", "REQ-1.1", "REQ-1"}
    assert "1.2.1" not in o.abandoned, "**别的区域不受影响** —— 考试第一小问写不出，可以写下一题"


def test_the_rest_of_a_forfeited_region_is_not_built(_flow, monkeypatch):
    """分已经丢了，剩下的兄弟别再花钱；别的区域照做（旧行为，显式打开）。"""
    monkeypatch.setenv("OCTOS_ABANDON_REGION", "1")
    o = _stub_prompt(_make(_flow))
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"], "1.2.1": ["1.2.1.spec.ts"]}
    _flow.spec_runner = lambda specs, grader_like: (
        _summary(passed=1, file=specs[0]) if list(specs) == ["1.2.1.spec.ts"] else _always_fails(specs, grader_like))

    o.implement_leaf("1.1.1", 1, 3)  # 修不动 → REQ-1.1 整区放弃

    built = []
    o.implement_leaf = lambda node_id, index, total: built.append(node_id)
    o.implement_leaves()

    assert built == ["1.2.1"], "1.1.2 不该再建（分已丢），1.2.1 照建"


def test_an_aggregation_that_runs_out_of_rounds_abandons_too(_flow, monkeypatch):
    """聚合验收也会判死 —— 父节点的分要整棵子树都成立才拿得到（旧行为，显式打开）。

    这里 REQ-1.1 挂在根下面，所以它的区域是**根**：根不成立，它底下所有东西
    都不给分，包括跟它无关的 REQ-1.2。
    """
    monkeypatch.setenv("OCTOS_ABANDON_REGION", "1")
    o = _stub_prompt(_make(_flow))
    _flow.spec_map = {"REQ-1.1": ["a.spec.ts"]}
    _flow.spec_runner = _always_fails
    o.verdict.update({"1.1.1": True, "1.1.2": True})

    o.accept_aggregate("REQ-1.1")

    assert o.verdict["REQ-1.1"] is False
    assert set(o.abandoned) == {"REQ-1", "REQ-1.1", "REQ-1.2", "1.1.1", "1.1.2", "1.2.1"}


# ---- v5 的默认：**不连坐**，搁浅的那个进诊断队列 --------------------------

def test_a_stuck_leaf_no_longer_takes_its_region_down(_flow):
    """**v5 默认行为**：一个叶子修不动，区域不受牵连 —— 它进诊断队列。

    「一直大量修、修不动就一直修」的解药不是把区域判死（那等于把没做错的
    兄弟也罚了），而是**把它从修复循环里拿出来**，交给诊断回合查三方：
    是卷子写错了、是实现缺东西、还是当初理解就跑题了。
    """
    o = _stub_prompt(_make(_flow))
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"]}
    _flow.spec_runner = _always_fails

    o.implement_leaf("1.1.1", 1, 3)

    assert o.verdict["1.1.1"] is False
    assert o.abandoned == {}, "一个要求点修不动，不再连坐整个区域"
    assert "1.1.1" in o._diagnosis, "搁浅的红进诊断队列"
    assert o.ledger["1.1.1"]["status"] == "parked", "台账上它是「搁置」，不是「死了」"


def test_the_region_carries_on_after_a_sibling_is_parked(_flow):
    """搁置的兄弟不挡别人的路：另一个叶子照建。"""
    o = _stub_prompt(_make(_flow))
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"], "1.2.1": ["1.2.1.spec.ts"]}
    _flow.spec_runner = lambda specs, grader_like: (
        _summary(passed=1, file=specs[0]) if list(specs) == ["1.2.1.spec.ts"]
        else _always_fails(specs, grader_like))

    o.implement_leaf("1.1.1", 1, 3)  # 修不动 → 搁置（不进 abandoned）

    built = []
    o.implement_leaf = lambda node_id, index, total: built.append(node_id)
    o.implement_leaves()

    assert built == ["1.1.1", "1.1.2", "1.2.1"], \
        "搁置不是放弃：整区都照建（1.1.1 由诊断回合接手，兄弟不再被连坐）"
    assert o.abandoned == {}

    # 诊断队列只在编排的收口处消化（`run()` / 收尾），这里补上那一步：
    # 没有诊断回合可跑时它不该吞掉任何东西，也不该把树弄坏。
    assert o.diagnose_parked("node") is False


def test_an_abandoned_node_is_not_verified_again(_flow):
    """放弃了就别再碰它 —— 每个回合都是钱。"""
    o = _make(_flow)
    o.abandoned["REQ-1.1"] = "1.1.1 failed"
    _flow.spec_runner = lambda specs, grader_like: pytest.fail("放弃了就不该再跑 spec")

    o.accept_aggregate("REQ-1.1")

    assert _flow.turns == [] and _flow.marks == []


def test_a_spec_runner_that_cannot_start_is_not_a_death_sentence(_flow):
    """浏览器起不来不是这个节点的错 —— 拿它废掉一个区域是冤枉。

    `exhausted` 就是为这条存在的：**只有「修复轮用完了还是不过」才判死**。
    """
    o = _stub_prompt(_make(_flow))
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"]}
    _flow.spec_runner = lambda specs, grader_like: _summary(error="browser did not start")

    o.implement_leaf("1.1.1", 1, 3)

    assert o.abandoned == {}
    assert [label for label, _ in _flow.turns] == ["1.1.1 implement"], "环境问题修不了，不该开修复轮"


# ---------------------------------------------------------------- 契约缺陷
#
# 模型说契约错了，**可以听，但不能盲听**：事实是 spec 和需求文档（那才是被判分的
# 东西），主张先过取证，文件站哪边就按哪边办。

def test_a_defect_claim_nothing_supports_does_not_touch_the_contract(_flow):
    o = _make(_flow)
    o.contract.path = o.contract_path
    o.contract.global_contract = {"routes": [{"method": "GET", "path": "/api/a"}]}
    amended = []
    o._amend_contract = lambda claim, evidence: amended.append(claim)

    _flow.reply = "CONTRACT DEFECT: `/api/nope` should return `items`, not `data`."
    o._turn("x", "1.1.1 repair 1/2", 60)

    assert amended == [], "文件里找不到这个对象，一句主张不足以改契约"
    assert o.contract.global_contract == {"routes": [{"method": "GET", "path": "/api/a"}]}


def test_a_claim_the_specs_back_up_amends_the_contract(_flow):
    o = _make(_flow)
    o.contract.path = o.contract_path
    specs = _flow.tests_dir
    specs.mkdir(parents=True, exist_ok=True)
    (specs / "questions.spec.ts").write_text(
        "const body = await res.json();\n"
        "expect(body.data).toEqual([]);   // GET /api/questions\n",
        encoding="utf-8")
    got = {}
    o._amend_contract = lambda claim, evidence: got.update(claim=claim, evidence=evidence)

    _flow.reply = ("CONTRACT DEFECT: the contract says `GET /api/questions` returns `items`, "
                   "but the specs read `data`.")
    o._turn("x", "1.1.1 repair 1/2", 60)

    assert got, "spec 里确实写着 data，这个主张该被采纳"
    assert any("questions.spec.ts" in line for line in got["evidence"])


def test_the_amendment_budget_is_bounded(_flow):
    """一句反复出现的主张不能把整个 run 拖进「改契约 → 又不服 → 再改」的环里。"""
    o = _make(_flow)
    o.contract.path = o.contract_path
    specs = _flow.tests_dir
    specs.mkdir(parents=True, exist_ok=True)
    (specs / "a.spec.ts").write_text("expect(body.data).toEqual([]);\n", encoding="utf-8")
    o.amendments = ["a", "b", "c"]
    o._amend_contract = lambda claim, evidence: pytest.fail("预算用完了不该再改")

    _flow.reply = "CONTRACT DEFECT: `data` is the wrong key."
    o._turn("x", "1.1.1 repair 1/2", 60)

    assert len(o.amendments) == 3


def test_the_amendment_replaces_the_contract_from_the_file(_flow):
    """改契约的产物是**文件**，不是模型的回话 —— 回话只用来记日志。

    （回话不进决策：编排读的是 `read_contract_file`。）
    """
    o = _make(_flow)
    o.contract.global_contract = {"routes": [{"method": "GET", "path": "/api/a", "returns": "items"}]}
    amended = {"routes": [{"method": "GET", "path": "/api/a", "owner": "1.1.1", "returns": "data"}]}

    def turn(prompt, timeout, label, read_only=False):
        _flow.turns.append((label, prompt))
        o.contract_path.parent.mkdir(parents=True, exist_ok=True)
        o.contract_path.write_text(json.dumps(amended), encoding="utf-8")
        return True, "CONTRACT AMENDED"

    _flow.turn = turn
    o._amend_contract("the return shape is wrong", ["a.spec.ts:3: expect(body.data)"])

    assert o.contract.global_contract == amended
    assert _flow.turns[-1][0] == "contract amend"


# ---------------------------------------------------------------- 卷子判错的出口

_GOOD_CLAIM = ("SPEC DEFECT: the requirement document says the grid's accessible name is "
               "`Worksheet grid`, but this spec asserts `Spreadsheet grid` instead")


def _spec_flow(_flow, tmp_path, claim=_GOOD_CLAIM, requirement="The grid is role=grid named "
                                                                "`Worksheet grid`."):
    """一个自产卷子的场景：需求文档里写着 `Worksheet grid`，卷子却要 `Spreadsheet grid`。

    代码是对的、卷子是错的 —— 这正是那条出口要处理的形状。
    """
    req = tmp_path / "requirements"
    req.mkdir(exist_ok=True)
    (req / "requirements.yaml").write_text(requirement, encoding="utf-8")
    _flow.req_dir = req
    _flow.authored_suite = True
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"]}
    o = _make(_flow)
    o.baseline_specs = []
    # 主张是从**最近一轮**的回话里读的（`_turn` 每次都覆盖 `last_turn_text`），
    # 所以假模型要每轮都说一遍 —— 真模型在真跑的时候也是这样：第 3 轮还在说
    # 「卷子判错了」，那就是我们要接住的那句话。回话保持空的话，第 1 轮之后
    # 就没有主张了，出口根本不会被走到。
    _flow.reply = claim
    o.last_turn_text = claim
    return o


def test_a_defective_spec_can_be_voided_with_the_requirement_as_evidence(_flow, tmp_path):
    """**要解决的问题是「永远过不去」。**

    自产卷子也是模型写的，它可能断言一个需求文档里根本没有的东西。这时候代码
    是对的、卷子是错的，而 `_accept` 只会一轮一轮修代码，修不动就 `exhausted`，
    再往后 `_abandon_region` 把**整个区域**连坐判死 —— 惩罚落在一堆没做错的
    节点上，日志里看不出区别。
    """
    o = _spec_flow(_flow, tmp_path)
    voided = o._maybe_void_spec("1.1.1", round_index=2)

    assert voided == "1.1.1.spec.ts"
    assert "1.1.1" not in _flow.spec_map, "作废 = 从这个节点名下摘掉"
    assert o.voided_specs == {"1.1.1.spec.ts"}, "摘掉要记账"
    assert (tmp_path / "requirements" / "requirements.yaml").is_file(), "需求文档不动"


def test_an_official_spec_can_never_be_voided(_flow, tmp_path):
    """**官方卷子没有这条出口，一行都不许走。**

    官方卷子是判分机在跑的另一份拷贝。本地把它作废，只是把自己弄瞎：隐藏测试
    照跑、照判错，而我们这边少了一条本来会失败的验收 —— 分数掉下去，日志好看。
    这是最坏的一种改动，所以闸门放在最前面。
    """
    o = _spec_flow(_flow, tmp_path)
    _flow.authored_suite = False
    assert o._maybe_void_spec("1.1.1", round_index=5) is None
    assert _flow.spec_map["1.1.1"] == ["1.1.1.spec.ts"]


def test_the_code_gets_a_fair_try_before_the_spec_is_blamed(_flow, tmp_path):
    """前两轮先老老实实修代码。

    「模型没做出来」和「卷子写错了」在前两轮里是分不清的，而前者远更常见 ——
    一上来就允许作废，等于给了模型一条「过不去就把卷子删了」的捷径。
    """
    o = _spec_flow(_flow, tmp_path)
    assert o._maybe_void_spec("1.1.1", round_index=0) is None
    assert o._maybe_void_spec("1.1.1", round_index=1) is None
    assert _flow.spec_map["1.1.1"] == ["1.1.1.spec.ts"]
    assert o._maybe_void_spec("1.1.1", round_index=2) == "1.1.1.spec.ts"


def test_a_claim_the_requirement_document_does_not_back_is_rejected(_flow, tmp_path):
    """主张要有据 —— 编造的对象哪条通道都过不了。

    拿卷子自己证明卷子错是循环论证，所以 `_requirement_evidence` 不查 spec
    （和 `_fact_evidence` 的分别就在这）。反向通道也不行：`TotallyInventedThing`
    没有锚到卷子的任何一行。两条通道都空就驳回：卷子站着，继续修代码。
    """
    o = _spec_flow(_flow, tmp_path,
                   claim="SPEC DEFECT: the spec asserts `TotallyInventedThing` but that is "
                         "not what the requirement wants at all, I am quite sure of it")
    assert o._maybe_void_spec("1.1.1", round_index=3) is None
    assert _flow.spec_map["1.1.1"] == ["1.1.1.spec.ts"]


def _line_flow(_flow, tmp_path, claim, spec_body):
    """带真卷子文件的场景 —— 反向取证要读盘，卷子必须真的在盘上。"""
    o = _spec_flow(_flow, tmp_path, claim=claim)
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "1.1.1.spec.ts").write_text(spec_body, encoding="utf-8")
    return o


def test_a_claim_naming_a_spec_line_can_void(_flow, tmp_path):
    """「需求**没**说它」型主张的第二条证据通道：卷子行指认。

    实测的驳回形状：模型引用 `REQ-4-3-3.spec.ts:40-41`，而这串东西在需求
    文档里永远搜不到（需求文档不会有自产卷子的文件名）——只留正向取证，
    这类主张永远死锁。但行号指认本身是可查证的：读盘验证那行真的是断言
    行，就是证据。
    """
    spec_body = ("import { test, expect } from '@playwright/test';\n"
                 "test('x', async ({ page }) => {\n"
                 "  await expect(page.getByText('NeverRequiredThing')).toBeVisible();\n"
                 "});\n")
    claim = ("SPEC DEFECT: `1.1.1.spec.ts:2-3` asserts that `NeverRequiredThing` must be "
             "visible, but the requirement document does not ask for anything of the kind — "
             "the code is correct and this assertion can never pass")
    o = _line_flow(_flow, tmp_path, claim, spec_body)
    assert o._maybe_void_spec("1.1.1", round_index=2) == "1.1.1.spec.ts"
    assert o.voided_specs == {"1.1.1.spec.ts"}, "摘掉要记账"


def test_a_line_claim_pointing_past_the_end_of_the_file_is_rejected(_flow, tmp_path):
    """行号是空口报的（文件只有 2 行，引用第 999 行）—— 读盘一验就穿帮。"""
    spec_body = ("import { test, expect } from '@playwright/test';\n"
                 "test('x', async () => {});\n")
    claim = ("SPEC DEFECT: `1.1.1.spec.ts:999` asserts something the requirement document "
             "does not say, and this whole assertion genuinely can never pass as written")
    o = _line_flow(_flow, tmp_path, claim, spec_body)
    assert o._maybe_void_spec("1.1.1", round_index=2) is None


def test_a_line_claim_naming_another_nodes_spec_is_rejected(_flow, tmp_path):
    """**只摘自己名下的。** 引别人的卷子行号，看都不看。"""
    spec_body = "await expect(page.getByText('X')).toBeVisible();\n"
    claim = ("SPEC DEFECT: `9.9.9.spec.ts:1` asserts something the requirement document does "
             "not say, and the assertion genuinely can never pass as it stands today")
    o = _line_flow(_flow, tmp_path, claim, spec_body)
    assert o._maybe_void_spec("1.1.1", round_index=2) is None
    assert _flow.spec_map["1.1.1"] == ["1.1.1.spec.ts"]


def test_a_line_claim_pointing_at_a_non_assertion_line_is_rejected(_flow, tmp_path):
    """指认处必须是断言行 —— 点名一行 import/赋值不算数。"""
    spec_body = ("import { test, expect } from '@playwright/test';\n"
                 "const someHelper = 1;\n"
                 "test('x', async () => {});\n")
    claim = ("SPEC DEFECT: `1.1.1.spec.ts:2` is wrong, the requirement document does not say "
             "anything like what this line does, so the code is fine and this can never pass")
    o = _line_flow(_flow, tmp_path, claim, spec_body)
    assert o._maybe_void_spec("1.1.1", round_index=2) is None


def test_a_mechanical_spec_defect_voids_without_requirement_backing(_flow, tmp_path):
    """第三条取证通道：**机器自己验过的**缺陷 —— 不要求需求原文背书（#4 申诉疏通）。

    实测的驳回形状（接力 REQ-6-1-1 / REQ-6-2-1）：判决正确且行号级（`:27` async
    辅助函数缺 await，正是审计实锤的死卷），却因为「需求文档/契约都没提它」被驳回。
    这类缺陷的证据按定义不在需求文档里 —— 它在**卷子自己的代码**里：`expect(...)`
    直接拿到 async 助手的 Promise，Playwright 对**每一个**实现都红。扫描器读盘即可
    证实，所以主张撞上它时不需要需求背书：需求写没写都改变不了这条断言对每个
    实现都失败的事实。
    """
    spec_body = ("import { test, expect } from '@playwright/test';\n"
                 "async function cellText(page, ref) { return page.getByRole('gridcell'); }\n"
                 "test('x', async ({ page }) => {\n"
                 "  await expect(cellText(page, 'A1')).toHaveText('42');\n"
                 "});\n")
    claim = ("SPEC DEFECT: the locator helper this spec builds is declared async, so the "
             "value handed to expect is a promise and the assertion fails for every possible "
             "implementation of the requirement")
    o = _line_flow(_flow, tmp_path, claim, spec_body)
    assert o._maybe_void_spec("1.1.1", round_index=2) == "1.1.1.spec.ts"
    assert o.voided_specs == {"1.1.1.spec.ts"}, "机器验过的缺陷：摘掉、记账"


def test_an_awaited_async_helper_is_not_a_mechanical_defect(_flow, tmp_path):
    """负样本：**难但正确的卷子不许被误作废。**

    `expect(await helper(...))` 是合法写法（`.resolves` 同理）—— 卷子只是难，
    不是错。三条通道都拿不出证据，它就站着，代码继续修。
    """
    spec_body = ("import { test, expect } from '@playwright/test';\n"
                 "async function cellText(page, ref) { return page.getByRole('gridcell'); }\n"
                 "test('x', async ({ page }) => {\n"
                 "  await expect(await cellText(page, 'A1')).toHaveText('42');\n"
                 "});\n")
    claim = ("SPEC DEFECT: the locator helper this spec builds is declared async, so the "
             "value handed to expect is a promise and the assertion fails for every possible "
             "implementation of the requirement")
    o = _line_flow(_flow, tmp_path, claim, spec_body)
    assert o._maybe_void_spec("1.1.1", round_index=2) is None
    assert _flow.spec_map["1.1.1"] == ["1.1.1.spec.ts"], "卷子站着"


def test_an_empty_claim_is_not_a_claim(_flow, tmp_path):
    """`SPEC DEFECT: 我觉得不对` 这种长度的主张，取证无从谈起 —— 不算主张。"""
    o = _spec_flow(_flow, tmp_path, claim="SPEC DEFECT: it is wrong I think")
    assert o._maybe_void_spec("1.1.1", round_index=3) is None


def test_the_void_budget_is_bounded(_flow, tmp_path):
    """这是一条能删验收的通道。不管住它，它就成了「过不去就把卷子删了」。"""
    o = _spec_flow(_flow, tmp_path)
    _flow.spec_map = {"1.1.1": ["a.spec.ts"], "1.1.2": ["b.spec.ts"], "1.1.3": ["c.spec.ts"]}
    assert o._maybe_void_spec("1.1.1", 2) == "a.spec.ts"
    assert o._maybe_void_spec("1.1.2", 2) == "b.spec.ts"
    assert o._maybe_void_spec("1.1.3", 2) is None, "预算是 2，第三份不许再作废"
    assert "1.1.3" in _flow.spec_map


def test_voiding_only_ever_touches_your_own_spec(_flow, tmp_path):
    """**只摘自己名下的。** 别人的卷子不是这个节点能碰的。"""
    o = _spec_flow(_flow, tmp_path)
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"], "1.1.2": ["1.1.2.spec.ts"]}
    o._maybe_void_spec("1.1.1", 2)
    assert _flow.spec_map["1.1.2"] == ["1.1.2.spec.ts"]


def test_a_voided_spec_leaves_the_baseline_scope(_flow, tmp_path):
    """作废的卷子从基线里划掉 —— 否则它永远红着，每个聚合点都要为它挨一次骂。"""
    o = _spec_flow(_flow, tmp_path)
    o.baseline_specs = ["1.1.1.spec.ts", "a.spec.ts"]
    o.voided_specs = {"1.1.1.spec.ts"}
    assert o._scope_specs("REQ-1", aggregate=True) == ["a.spec.ts"]


def test_a_voided_spec_does_not_come_back_at_the_final_check(_flow, tmp_path):
    """**终检也要放过它** —— 否则那条路等于没走。

    节点那关放行了，终检又拿同一份错卷子把整个 run 判红 —— 「永远过不去」
    只是从节点推迟到了终检，而终检**没有修复轮**。
    """
    o = _spec_flow(_flow, tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir(exist_ok=True)
    for name in ("1.1.1.spec.ts", "a.spec.ts"):
        (tests / name).write_text("// spec\n", encoding="utf-8")
    _flow.tests_dir = tests
    o.voided_specs = {"1.1.1.spec.ts"}
    assert o._all_specs() == ["a.spec.ts"]
    assert (tests / "1.1.1.spec.ts").is_file(), "排除 ≠ 删掉：文件还在，复盘看得到"


def test_voiding_does_not_cost_a_round(_flow, tmp_path):
    """**作废一卷不能消耗一轮。** 这条钉的是一个真的踩过的坑。

    `round_index` 的允许上界（`_SPEC_DEFECT_FROM_ROUND`，2）恰好落在
    `repair_rounds` 上时，作废那一轮就是这个节点的**最后一轮**。要是作废写成
    `continue`（看起来等价，其实多走一轮），循环当场结束，节点连基线都没查就
    判死 —— 作废等于白作，而且日志里那句「specs voided」看起来一切正常。
    """
    _flow.repair_rounds = 2          # 三个回合：0、1、2 —— 作废只可能发生在最后一个
    o = _spec_flow(_flow, tmp_path)

    def runner(specs, grader_like):
        return _summary(passed=0, failed=["1.1.1.spec.ts"])

    _flow.spec_runner = runner
    result = o._accept("1.1.1", ["1.1.1.spec.ts"], "1.1.1", aggregate=False)

    assert o.voided_specs == {"1.1.1.spec.ts"}, "作废该发生"
    assert result.passed is True, "作废之后要接着查基线，不能就地判死"
    assert result.exhausted is False
    assert _flow.commits == ["1.1.1: accepted"]


def test_the_escape_hatch_is_offered_exactly_when_it_is_open(_flow, tmp_path):
    """提示词里的话和代码里的闸门**必须同源**。

    不说的后果是这条路一行都不会被走到（模型不知道它存在）；对着官方卷子说的
    后果更糟：模型以为过不去可以主张卷子错了，而那条主张会被直接驳回 ——
    白烧一轮修复。
    """
    o = _spec_flow(_flow, tmp_path)
    assert "SPEC DEFECT" in o._spec_defect_note()
    _flow.authored_suite = False
    assert o._spec_defect_note() == "", "官方卷子：一个字都不提"
    _flow.authored_suite = True
    o.voided_specs = {"a.spec.ts", "b.spec.ts"}
    assert o._spec_defect_note() == "", "预算用完了：出口关了，话也得收回去"


def test_the_claim_extractor_ignores_ordinary_prose():
    assert orch._defect_claim("I implemented the thing and it works.") is None
    claim = orch._defect_claim("CONTRACT DEFECT: `GET /api/x` returns `items`, not `data`.")
    assert claim.startswith("`GET /api/x`")


def test_the_tokens_are_the_disputable_objects():
    tokens = orch._defect_tokens(
        "CONTRACT DEFECT: the contract says `GET /api/questions` returns `items`")
    assert "GET /api/questions" in tokens
    assert "items" in tokens


# ---------------------------------------------------------------- 事件口径

def test_every_event_kind_the_orchestrator_emits_actually_exists():
    """`flow.mark(kind)` 底下是 `getattr(self.events, f"mark_{kind}")`。

    所以一个不存在的 kind **不是「少记一条日志」，是当场 AttributeError**：
    旧版这里发的是 `verification_started/done/failed`，那三个方法在 EventClient
    上根本不存在 —— 第一个节点通过、走到第一次聚合验收时，整个 run 就 abort 了。
    对着源码扫，新增一个 kind 而没加对应方法，这里立刻红。
    """
    from arcbench_agent_runtime.events import EventClient

    kinds = set(re.findall(r'\.mark\(\s*"([a-z_]+)"', inspect.getsource(orch)))
    assert kinds, "一个 mark 调用都没扫到 —— 这条测试自己失效了"
    missing = sorted(k for k in kinds if not hasattr(EventClient, f"mark_{k}"))
    assert not missing, f"EventClient 上没有这些方法：{missing}"
