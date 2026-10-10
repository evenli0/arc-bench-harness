"""时间闸门、探针时机、上报口径 —— 三处「不报错的错」。

这三样有同一个形状：**出问题的时候没有一行异常**。

  · 时间预算算长了 → 跑到一半被平台杀掉，日志停在某个节点上，看起来像卡住
  · 探针跑晚了     → `runner is None`，整个探针空转，看起来像「这个 app 本来就没东西可省」
  · 上报口径不实   → 42 个节点报 42 句一样的「卷子通过」，看起来像全都验过

所以它们的验收标准不是「能跑」，是**边界值**：削到多少、什么时候跑、说的是不是真的。
"""

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402
from layers import layerize  # noqa: E402


# ---------------------------------------------------------------- 平台墙钟

PLATFORM = 79_200          # 22h，平台红线
NODE_TIMEOUT = 2_400       # 单回合闸门
TAIL = 1_800               # 终检 + 彩排 + 上报

# 上限 = 平台 − 在飞的回合 − 收尾。和 `Flow.__init__` 里那行是同一个式子；
# 这里独立算一遍，是为了让「它到底该是多少」在测试里有第二份出处。
CEILING = PLATFORM - NODE_TIMEOUT - TAIL


def _budget(budget, *, explicit=False, nodes=1, per_node=1500, ceiling=CEILING, platform=PLATFORM):
    return M.effective_budget(budget, explicit=explicit, node_count=nodes,
                              seconds_per_node=per_node, max_wall_seconds=ceiling,
                              platform_wall_seconds=platform)


def test_the_two_final_tasks_get_their_fast_pace_caps():
    """**决赛节奏（2026-10-08 首领拍板）：平台不传预算时，按树体量封顶。**

    两个节点数是决赛体量实测：sheet 决赛 28 个节点 → 封 6h；github 决赛
    70 个节点 → 封 10h。原公式（1500×节点数）会给 11.7h / 29.2h —— 决赛要求
    快跑：到点不再开新回合、直接封箱「已验证的最好状态」，不会翻车、只可能少赚。
    40 是两档之间的分界：28 ≤ 40 < 70，怎么取都不影响这两棵真树。
    """
    assert _budget(3600, nodes=28) == 21_600, "sheet 决赛：封 6h"
    assert _budget(3600, nodes=70) == 36_000, "github 决赛：封 10h"
    assert _budget(3600, nodes=42) == 36_000, "更大的树一律按大树档封"


def test_the_fast_pace_cap_says_so_in_the_log(capsys):
    """封顶和削平一样必须**出声**：它把 29.2h 的计划悄悄变成 10h —— 不说的
    后果是事后查不出「为什么这一轮十小时就封箱了」。"""
    _budget(3600, nodes=70)
    text = "".join(capsys.readouterr())
    assert "finals pacing" in text
    assert "105000s" in text and "10.0h" in text


def test_the_ceiling_is_derived_not_guessed():
    """上限是**倒推**出来的：平台红线 − 在飞的回合 − 收尾。

    写死一个 78000 也能跑，但它是对的纯属巧合 —— 谁把 `node_timeout` 从 2400
    调到 3000，78000 就悄悄越线了 600s。而这种越线的后果（收尾阶段被杀：没有
    终检、没有上报）在报告里**看不出来**，所以它必须跟着推导。
    """
    assert CEILING == 75_000
    assert PLATFORM - CEILING == NODE_TIMEOUT + TAIL, "留白必须恰好盖住那两段"


def test_a_turn_already_running_can_finish_before_the_platform_kills_us():
    """**这条是留白的全部意义。**

    预算是「不再开新回合」，不是「打断正在跑的」。最后一个回合可以在
    `remaining()≈0` 时起步，跑满 `node_timeout` 才轮到下一句 `time_up()` ——
    所以留白必须先盖住它，再盖住收尾。
    """
    assert PLATFORM - CEILING >= NODE_TIMEOUT, "一个在飞的回合装不下"
    assert PLATFORM - CEILING >= NODE_TIMEOUT + TAIL, "在飞的回合 + 收尾要一起装下"


def test_a_small_tree_is_untouched():
    """削平只在超限时发生 —— 小树不该被它碰到。"""
    assert _budget(3600, nodes=4) == 6_000
    assert _budget(3600, nodes=1) == 3_600, "单节点：默认 3600 比 1500 大，取 3600"


def test_an_explicit_budget_is_respected_but_still_clamped():
    """命令行给了 `--time-budget` 就不再按节点数抬高 —— **但削平照样生效**。

    两条闸门的性质不一样：抬高是「没明说就按规模算」，明说了就听；削平是平台
    的物理限制，谁说都不能越过。
    """
    assert _budget(3000, explicit=True, nodes=42) == 3_000
    assert _budget(200_000, explicit=True, nodes=42) == CEILING


def test_the_ceiling_can_be_switched_off():
    """`0` = 关掉。本地长跑要得到（跑一整天不被自己削断）。"""
    assert _budget(200_000, nodes=42, ceiling=0) == 200_000
    assert _budget(3_600, explicit=True, nodes=42, ceiling=0) == 3_600


def test_the_clamp_says_so_in_the_log(capsys):
    """削平必须**出声**。它把一个 27h 的计划悄悄变成 20.8h —— 不说的话，
    事后看到「怎么每个节点的份额都变小了」是查不出来的。

    用显式预算触发：平台形态（不显式）如今先被决赛节奏封顶，够不到削平这条线。
    """
    _budget(200_000, explicit=True, nodes=65)
    text = "".join(capsys.readouterr())
    assert "clamping" in text
    assert "200000s" in text and "55.6h" in text


# ---------------------------------------------------------------- 上报口径

def _flow(**attrs):
    """只带被测方法要用的那几个属性的 Flow。多给一个都不给。"""
    flow = object.__new__(M.Flow)
    for key, value in attrs.items():
        setattr(flow, key, value)
    return flow


def test_a_verdict_without_evidence_does_not_claim_specs_passed():
    """**没有卷子跑过，就不能说「卷子通过」。**

    原先这里是一句写死的「acceptance specs pass (node run and full parallel
    suite)」。在官方卷子那条路上它是真的；自产卷子这条路上，出卷失败时的叶子
    和**全部文件夹节点**名下一份卷子都没有，而 `_accept` 里 `own_ok = not specs`
    让它们一条断言都没跑就通过。于是一次跑完，每个节点上报的都是同一句话。

    平台的打分不读这些事件 —— 它骗的是我们自己：事后想复盘「哪些节点是真验过
    的」，读到的是一排没有区别的通过。
    """
    flow = _flow(tests_dir=None)
    passed = flow.spec_basis("REQ-1.1.1", passed=True)
    failed = flow.spec_basis("REQ-1.1.1", passed=False)
    assert "no acceptance suite ran" in passed
    assert "no acceptance suite ran" in failed
    assert "specs pass" not in passed


def test_a_node_with_its_own_specs_still_says_so():
    """有卷子的节点措辞照旧 —— 这句话在那条路上是真的。"""
    flow = _flow(tests_dir=Path("/t"), runner=object(),
                 spec_map={"REQ-1.1.1": ["REQ-1.1.1.spec.ts"]})
    assert flow.spec_basis("REQ-1.1.1", passed=True) == \
        "acceptance specs pass (node run and full parallel suite)"
    assert flow.spec_basis("REQ-1.1.1", passed=False) == "acceptance specs failing"


def test_a_folder_node_is_reported_as_an_aggregation():
    """文件夹节点名下一份卷子都没有，但它跑过全量基线 —— 说清楚是「聚合」。"""
    layers = layerize({"id": "REQ-1", "children": [{"id": "REQ-1.1", "children": [
        {"id": "REQ-1.1.1"}]}]})
    flow = _flow(tests_dir=Path("/t"), runner=object(), spec_map={}, layers=layers)
    text = flow.spec_basis("REQ-1.1", passed=True)
    assert text.startswith("aggregate:")
    assert "owns no spec" in text


def test_a_leaf_with_no_spec_says_nothing_asserted_it():
    """叶子没卷子 = 没有机器检查。不能含糊成「卷子通过」。"""
    layers = layerize({"id": "REQ-1", "children": [{"id": "REQ-1.1", "children": [
        {"id": "REQ-1.1.1"}]}]})
    flow = _flow(tests_dir=Path("/t"), runner=object(), spec_map={}, layers=layers)
    assert "nothing asserted it" in flow.spec_basis("REQ-1.1.1", passed=True)


# ---------------------------------------------------------------- 没卷子时说什么

def test_a_node_without_specs_is_not_told_every_spec_is_its_checklist(tmp_path):
    """**「没有挂到你名下的卷子」不等于「全部卷子都是你的评分标准」。**

    原先那条分支是好意（总比什么都不给强），在自产卷子这条路上却是一句不成立的
    话：后台对这个节点的断言是 0 条（`_accept` 里 `own_ok = not specs`），却告诉
    它「这批卷子就是评判你的依据」。模型于是去满足一堆跟它无关的断言，
    花掉的是这个节点自己的回合预算。

    正确的话是：这里没有对你的机器检查，照需求原文做；别的卷子在那儿，
    **只当约定看，别当清单用**。
    """
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "REQ-9-9-9.spec.ts").write_text("// someone else's\n", encoding="utf-8")
    flow = _flow(tests_dir=tests, spec_map={}, web_port=3000, smoke_port=3100)
    text = flow.tests_prompt_for("REQ-1-1-1")

    assert "no acceptance spec for this node" in text
    assert "REQ-9-9-9.spec.ts" not in text, "别人的卷子不该出现在它的提示词里"
    assert "never as a checklist" in text


def test_no_tests_at_all_yields_no_text(tmp_path):
    """整个 run 都没有卷子 → 空串，一个字都不提。"""
    assert _flow(tests_dir=None, spec_map={}).tests_prompt_for("REQ-1-1-1") == ""


# ---------------------------------------------------------------- 探针时机

class _ProbeFlow:
    """跑得动 `probe_existing_app` 的最小 Flow。"""

    def __init__(self, runner=True, evolution=True, already=(), probe_count=0,
                 output_dir=None):
        self.evolution = evolution
        self.runner = object() if runner else None
        self._probed = False
        self.probe_count = probe_count
        self._already = set(already)
        self.discarded = 0
        self.nodes_to_implement = None
        self.calls = []
        self.plans = 0
        # 默认给一个**真 app 大小**的工作区：v5 起，拆楼的许可要看盘上有没有
        # 可维护的东西（`discard_allowed` → `scaffold_like`），不再只看探针结果。
        self.output_dir = Path(output_dir) if output_dir else _workspace(real=True)

    def already_passing_nodes(self, node_ids):
        self.calls.append(list(node_ids))
        return {n for n in node_ids if n in self._already}

    def discard_template(self):
        self.discarded += 1

    def write_construction_plan(self, node_ids):
        self.plans += 1

    probe_existing_app = M.Flow.probe_existing_app


def _workspace(real: bool, tmp: Path | None = None):
    """造一个工作区：`real=True` 是一个有体量的 app，`False` 是空壳模板。"""
    import tempfile
    root = Path(tmp) if tmp is not None else Path(tempfile.mkdtemp())
    for name in ("frontend", "backend"):
        d = root / name / "src"
        d.mkdir(parents=True, exist_ok=True)
        # 空壳模板 = 几个几百字节的占位文件；真 app = 几十 KB 的源码
        # （默认阈值 64 KiB，两侧都留出余量，免得测试钉在边界上）。
        body = "export const placeholder = 1;\n" * (3 if not real else 1500)
        (d / "app.ts").write_text(body, encoding="utf-8")
    return root


def test_the_probe_needs_a_runner():
    """没有 runner（没有卷子）时探针不跑 —— 而且**不算跑过**。"""
    flow = _ProbeFlow(runner=False)
    assert flow.probe_existing_app(["A", "B"], set()) == set()
    assert flow._probed is False, "没跑成不算跑过，下一次调用还得再试"


def test_the_probe_only_ever_runs_once():
    """两个调用点：官方卷子那条路在构造驱动之前，自产卷子那条路在出卷之后。

    两条路都要有机会跑，但**只有一个会真的跑**（另一个时候条件不成立）。
    """
    flow = _ProbeFlow(already=["A"])
    first = flow.probe_existing_app(["A", "B"], set())
    assert first == {"A"}
    assert flow.calls == [["A", "B"]]

    second = flow.probe_existing_app(["A", "B"], first)
    assert second == {"A"}
    assert flow.calls == [["A", "B"]], "第二次不该再探一遍"


def test_a_useless_template_is_set_aside(tmp_path, monkeypatch):
    """一个**空壳模板**（什么都没有的毛坯）不能当地基用。

    不设它的话，每个节点都会拿到 `NODE_PREAMBLE_EXTEND`（「在现有基础上扩展，
    不要重写」）—— 而它其实什么都不满足。注释里记的实测代价是 fresh build 的
    40-80 倍。

    v5 起，这个判断加了第二道：**盘上得真的是毛坯**（`scaffold_like`）。
    """
    monkeypatch.delenv("ARCBENCH_KEEP_TEMPLATE", raising=False)
    flow = _ProbeFlow(already=[], probe_count=40,      # 探过 40 个节点，一个都没天然满足
                      output_dir=_workspace(real=False, tmp=tmp_path))
    flow.probe_existing_app(["A", "B"], set())
    assert flow.discarded == 1
    assert flow.evolution is False


def test_a_template_that_satisfies_something_is_kept(tmp_path, monkeypatch):
    """只要有一个节点天然满足，模板就是有用的 —— 别丢。"""
    monkeypatch.delenv("ARCBENCH_KEEP_TEMPLATE", raising=False)
    flow = _ProbeFlow(already=["A"], probe_count=40, output_dir=tmp_path)
    flow.probe_existing_app(["A", "B"], set())
    assert flow.discarded == 0
    assert flow.evolution is True


def test_a_real_app_is_kept_even_when_nothing_passes(tmp_path, monkeypatch):
    """**v5 的起跑纪律**：一个都不满足也不拆 —— 只要盘上是有体量的真东西。

    正赛的教训：探针只知道「今天没有一条卷子全过」，它不知道这楼里有多少能用。
    一个大部分能用的继承 app 被整个搬走、从零重建，然后超时。所以「拆」只剩
    两种许可：真毛坯，或人按了强制开关。
    """
    monkeypatch.delenv("ARCBENCH_KEEP_TEMPLATE", raising=False)
    flow = _ProbeFlow(already=[], probe_count=40,
                      output_dir=_workspace(real=True, tmp=tmp_path))
    flow.probe_existing_app(["A", "B"], set())
    assert flow.discarded == 0, "有体量的 app 不管探针怎么说都留着"
    assert flow.evolution is True
    assert flow.plans == 1, "起跑必须留下施工清单"


def test_keep_template_forces_the_app_to_stay(tmp_path, monkeypatch):
    """`ARCBENCH_KEEP_TEMPLATE=1` 是总闸：连毛坯都不拆（人在现场判断）。"""
    monkeypatch.setenv("ARCBENCH_KEEP_TEMPLATE", "1")
    flow = _ProbeFlow(already=[], probe_count=40,
                      output_dir=_workspace(real=False, tmp=tmp_path))
    flow.probe_existing_app(["A", "B"], set())
    assert flow.discarded == 0
    assert flow.evolution is True


def test_nodes_already_clear_are_not_reprobed():
    """上次探针已经判定「天然满足」的节点，不会再送进 `already_passing_nodes`。"""
    flow = _ProbeFlow(already=["A", "B"])
    flow.probe_existing_app(["A", "B", "C"], {"A"})
    assert flow.calls == [["B", "C"]]


# ---------------------------------------------------------------- Playwright 缺席

def test_specs_that_cannot_be_run_are_not_a_death_sentence(capsys):
    """**名下有卷子、却一份都跑不起来 —— 这是正赛上最坏的一种半配置状态。**

    `setup_playwright()` 的三层兜底（机器自带的、全盘搜、自己装一个）可能全失败，
    那时它留下 `tests_dir` 有、`runner` 没有。而 `_accept` 不问 runner 在不在：
    它拿到 `spec_map` 里挂着的卷子就去跑 → `run_specs` 里 `self.runner.run(...)`
    当场 AttributeError → 被 `_run_specs` 的宽 except 吃掉 → 节点收到的是
    「specs could not run」，而这句话的处理是**不修复、不回滚、当场判负**。
    每个叶子都要跑自己那份卷子，于是二十几个叶子同一个死法，`_abandon_region`
    再把整个区域连坐：**一次 Playwright 找不到 = 全树零分。**

    降级之后退回「没有验收机制」那条路（空 `spec_map`）：`own_ok = not specs`
    直接放行，app 照样做出来交上去 —— 判分机跑隐藏测试用的是**它自己的**
    Playwright，「我们本地验不了」和「这个 app 不行」是两件事。
    """
    flow = _flow(tests_dir=Path("/t"), runner=None,
                 spec_map={"REQ-1.1.1": ["REQ-1.1.1.spec.ts"], None: ["orphan.spec.ts"]})
    assert flow.degrade_to_no_acceptance() is True
    assert flow.spec_map == {"REQ-1.1.1": [], None: []}, "节点名下要一份卷子都不剩"
    assert "NOTHING in this run can be verified" in "".join(capsys.readouterr())


def test_a_run_that_can_run_specs_is_left_alone():
    """跑得动就什么都不动 —— 别把正常路径也一起降级了。"""
    flow = _flow(tests_dir=Path("/t"), runner=object(), spec_map={"REQ-1.1.1": ["x.spec.ts"]})
    assert flow.degrade_to_no_acceptance() is False
    assert flow.spec_map == {"REQ-1.1.1": ["x.spec.ts"]}


def test_having_no_suite_at_all_is_not_a_degradation():
    """没有卷子的那条正常路径（正赛上出卷之前）不该被认成故障。

    那条路上 `runner` 本来就是 None —— 判据要两个条件都成立，少一个就什么都不做。
    """
    assert _flow(tests_dir=None, runner=None, spec_map={None: []}).degrade_to_no_acceptance() is False


def test_a_folder_node_does_not_claim_a_baseline_that_never_ran():
    """**降级之后最会说谎的是文件夹节点。**

    叶子那条分支说「nothing asserted it」还算贴边；文件夹节点那条会说
    「aggregate: the full baseline ran and stayed green」—— 而 Playwright 都没
    找到，一次全量基线也没跑过。所以「跑不起来」这一条必须排在所有分支前面。
    """
    layers = layerize({"id": "REQ-1", "children": [{"id": "REQ-1.1", "children": [
        {"id": "REQ-1.1.1"}]}]})
    flow = _flow(tests_dir=Path("/t"), runner=None, spec_map={}, layers=layers)
    for node_id in ("REQ-1.1", "REQ-1.1.1"):
        assert "Playwright was unavailable" in flow.spec_basis(node_id, passed=True)
        assert "Playwright was unavailable" in flow.spec_basis(node_id, passed=False)
        assert "baseline ran" not in flow.spec_basis(node_id, passed=True)


# ---------------------------------------------------------------- 彩排的时长

class _RehearsalFlow:
    """跑得动 `rehearsal()` 的最小 Flow。构建永远失败，除非说了不然。"""

    def __init__(self, remaining, build_ok=False):
        self._remaining = float(remaining)
        self._build_ok = build_ok
        self.smoke_port = 3100
        self.web_port = 3000
        self.node_timeout = 2400          # 和 `Flow.__init__` 的默认值同源
        self.min_repair_seconds = 450
        self.builds = 0
        self.turns = []
        self.commits = []

    def remaining(self):
        return self._remaining

    def commit(self, message):
        self.commits.append(message)
        return True

    def app_server(self, grader_like):
        self.builds += 1
        err = None if self._build_ok else "boom: the app did not start"
        return types.SimpleNamespace(build=lambda: err, start=lambda: None, stop=lambda: None)

    def turn(self, prompt, timeout, label):
        self.turns.append((label, timeout))
        self._remaining -= timeout       # 最坏情况：修复回合把它的预算花满
        return True, ""

    rehearsal = M.Flow.rehearsal


def test_the_rehearsal_does_not_start_a_repair_it_cannot_afford():
    """**预算是 `remaining()`，超支吃掉的是平台的留白。**

    原来唯一的闸门 `remaining() < -600` 是在构建失败**之后**才问的，而且允许先
    超支 600s、再去开一个上限 `node_timeout`(2400s) 的修复回合。预算是 75000s
    （平台 22h 减掉 `node_timeout + tail`，见 `test_the_ceiling_is_derived_not_guessed`），
    那段留白要给**已经在飞的回合**和 `postflight()`。彩排多吃的每一秒都在挤它们，
    而吃穿的代价不是「少跑一会儿」：`postflight()` 在 `finally` 里，进程被平台在
    22h 一刀杀掉就没机会跑，模型留在后台的服务器活到判分，四个 Chromium 在同一个
    内存笼子里 OOM，**整套隐藏测试被杀**。
    """
    flow = _RehearsalFlow(remaining=100.0)
    assert flow.rehearsal() is False
    assert flow.turns == [], "只剩 100s 还去开修复回合"
    assert flow.builds == 1, "但构建/启动本身要跑 —— app 起不起得来是必须知道的"


def test_a_repair_turn_is_capped_by_what_is_left():
    """够跑但不够跑满：回合时长按剩余时间削，不按 `node_timeout` 走。"""
    flow = _RehearsalFlow(remaining=1000.0)
    assert flow.rehearsal() is False
    label, timeout = flow.turns[0]
    assert timeout == 800, f"应该削到 1000-200（留一次构建的余量），实际 {timeout}"
    assert timeout < flow.node_timeout


def test_a_healthy_budget_keeps_the_full_timeout():
    """预算充足时和原来一样 —— 别把正常路径也削了。"""
    flow = _RehearsalFlow(remaining=20000.0)
    flow.rehearsal()
    assert flow.turns[0][1] == flow.node_timeout


def test_a_clean_start_needs_no_repair_at_all():
    """起得来就直接收工，一分钱修复预算都不该花。"""
    flow = _RehearsalFlow(remaining=100.0, build_ok=True)
    assert flow.rehearsal() is True
    assert flow.turns == []
