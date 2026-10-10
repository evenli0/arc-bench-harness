"""自产验收卷（改造一/二）。

正赛把测试藏起来了：`/workspace/tests` 不存在，bundle 里也没有 public-tests，
于是 `locate_acceptance_tests()` 返回 None，`tests_dir` 是 None。那之后发生的事
没有一行是显式的 —— `tests_prompt_for()` 返回空串、`setup_playwright()` 提前
返回、`own_ok = not specs` 让**每个叶子自动通过**。24 个叶子 24 个实现回合
0 个修复回合。

这一段补的就是它：按需求树自己出一份卷子，出完之后当官方卷子用。

这里断言的都是**判据**，不是模型写出来的内容：
    · 卷子文件名 → 节点 id 的解析（两种 id 写法都要认，且要吃到最深）
    · 出卷依据里有没有祖先契约（改造三在出卷这一侧的同一个道理）
    · 分组：按一级分支切，尾部合并，一个叶子都不许丢
    · 出卷的时间闸门：剩余时间不够就不开新回合
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from acceptance import map_specs_to_nodes, spec_node_id  # noqa: E402
from layers import layerize  # noqa: E402
import main as M  # noqa: E402


# ---------------------------------------------------------------- 假件

def _tree():
    """一棵两分支的树，根上带一条跨节点的硬契约 —— 出卷的人必须看得到它。"""
    return {
        "id": "ROOT", "name": "Workbook", "type": "FOLDER",
        "description": "The grid must be role=grid with the accessible name 'Worksheet grid'.",
        "children": [
            {
                "id": "REQ-1", "name": "Lifecycle", "type": "FOLDER",
                "description": "Every workbook is created, opened and closed.",
                "children": [
                    {"id": "REQ-1-1", "name": "Create", "type": "FOLDER", "description": "Make one.",
                     "children": [
                         {"id": "REQ-1-1-1", "name": "Create workbook", "type": "ATOMIC",
                          "description": "A button named 'New workbook' creates one.",
                          "scenarios": [{"name": "create", "steps": [
                              {"keyword": "When", "content": "the user activates 'New workbook'"}]}]},
                         {"id": "REQ-1-1-2", "name": "Open workbook", "type": "ATOMIC",
                          "description": "Opening lists the workbook by name."},
                     ]},
                    {"id": "REQ-1-2", "name": "Close", "type": "ATOMIC",
                     "description": "Closing returns to the home list."},
                ],
            },
            {
                "id": "REQ-2", "name": "Cells", "type": "FOLDER",
                "description": "Cells are addressed by coordinate.",
                "children": [
                    {"id": "REQ-2-1", "name": "Select", "type": "ATOMIC",
                     "description": "A selected cell is aria-selected='true'."},
                    {"id": "REQ-2-2", "name": "Edit", "type": "ATOMIC",
                     "description": "Typing replaces the cell content."},
                ],
            },
        ],
    }


# ---------------------------------------------------------------- 文件名 → 节点

def test_a_dashed_id_is_read_to_the_deepest_segment():
    """正赛的需求 id 是短横线的（`REQ-1-2-1`）。

    原先的正则只认点分，于是 `REQ-1-2-1.spec.ts` 被解析成 `REQ-1` —— 每个叶子
    的卷子都挂到根上，每个节点拿到的都是别人的卷子。这是自产卷子的前置条件：
    自产的文件名就是 `<NODE_ID>.spec.ts`。
    """
    assert spec_node_id("REQ-1-2-1.spec.ts") == "REQ-1-2-1"
    assert spec_node_id("REQ-1-2-1-4.spec.ts") == "REQ-1-2-1-4"
    assert spec_node_id("REQ-1.spec.ts") == "REQ-1"


def test_a_dotted_id_still_reads_the_way_it_did():
    """老任务的写法（`REQ-1.2.3`）不能被这次改动碰坏。"""
    assert spec_node_id("REQ-1.2.3-user-login.spec.ts") == "REQ-1.2.3"
    assert spec_node_id("REQ-1.1.spec.ts") == "REQ-1.1"


def test_one_spec_per_leaf_lands_on_that_leaf():
    """端到端：自产卷子的文件名 → `spec_map`，必须**一个叶子一份、不串门**。

    串门的后果不是「测少了」，是「测错了」：节点 A 跑节点 B 的卷子，A 的失败
    来自 B 的缺陷，修复回合去改的是 A。
    """
    layers = layerize(_tree())
    specs = [f"{leaf}.spec.ts" for leaf in layers.leaves]
    mapping, aliases = map_specs_to_nodes(specs, layers.order)
    assert aliases == {}
    for leaf in layers.leaves:
        assert mapping[leaf] == [f"{leaf}.spec.ts"], f"{leaf} 挂错了卷子"
    # 文件夹节点不该拥有任何叶子的卷子
    for folder in ("ROOT", "REQ-1", "REQ-1-1", "REQ-2"):
        assert not mapping.get(folder), f"{folder} 是文件夹，不该拥有卷子"


# ---------------------------------------------------------------- 卷子挂到谁名下

def _suite(tmp_path, names):
    """在 suite 目录下摆几个文件（含子目录），返回目录。"""
    suite = tmp_path / M.AUTHOR_SUITE_DIR
    for name in names:
        path = suite / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// spec\n", encoding="utf-8")
    return suite


@pytest.mark.parametrize("scheme", [
    ["REQ-1-1-1", "REQ-1-1-2", "REQ-2-1"],       # 短横线
    ["REQ-1.1.1", "REQ-1.1.2", "REQ-2.1"],       # 点分
    ["ROOT", "1.1", "1.1.1"],                    # 完全不带 REQ-
    ["TASK-A", "TASK-A-1", "TASK-B"],            # 另一种前缀
])
def test_every_id_scheme_maps_its_own_specs(tmp_path, scheme):
    """**这一条是防「全军覆没」的，不是防「挂错人」的。**

    自产卷子的文件名是我们自己指定的，所以归属是已知的 —— 不需要反推。这条路
    必须与 id 长什么形状无关。

    反推那条路的实测结果（`spec_node_id` 的正则要求以 `REQ-` 开头）：
    `ROOT` / `1.1` / `TASK-A` 这三种编号下**一份卷子都认不出来**。而认不出来
    不是归到兜底集合，是被**直接丢掉**；「节点名下的卷子数是 0」在 `_accept`
    里又等于**直接判通过**。所以最坏的形态是：磁盘上躺着 24 份卷子、日志没有
    一行异常、报告写着 24/24 全通过 —— 一个断言都没跑过。
    """
    suite = _suite(tmp_path, [f"{n}.spec.ts" for n in scheme])
    mapping = M.authored_spec_map(suite, scheme, scheme)
    for node_id in scheme:
        assert mapping[node_id] == [f"{node_id}.spec.ts"], f"{node_id} 的卷子挂丢了"
    assert mapping[None] == [], "没有一份卷子该掉进兜底集合"
    covered, missing = M.authoring_coverage(mapping, scheme)
    assert (covered, missing) == (len(scheme), [])


def test_a_spec_named_after_nothing_is_kept_not_dropped(tmp_path):
    """名字对不上任何节点的卷子**不许丢** —— 丢一份就是少一份体检。

    它进兜底集合，`final_acceptance` 的全量体检里仍然会跑。
    """
    suite = _suite(tmp_path, ["REQ-1-1-1.spec.ts", "helpers.spec.ts", "sub/extra.spec.ts"])
    mapping = M.authored_spec_map(suite, ["REQ-1-1-1"], ["REQ-1-1-1"])
    assert mapping["REQ-1-1-1"] == ["REQ-1-1-1.spec.ts"]
    # 路径是平台原生的（Windows 上是 `sub\extra.spec.ts`）—— 与调用方拼给
    # Playwright 的那份是同一个写法，别在这里「顺手统一成正斜杠」。
    assert sorted(Path(p).name for p in mapping[None]) == ["extra.spec.ts", "helpers.spec.ts"]


def test_coverage_counts_the_leaves_that_have_nothing(tmp_path):
    """覆盖率的判据是**磁盘上的文件**，不是模型说了什么。

    出卷回合失败、写了一半、写到别的目录 —— 都要在这里变成「没覆盖」，
    而不是变成一次静默的自动通过。
    """
    suite = _suite(tmp_path, ["REQ-1-1-1.spec.ts"])
    leaves = ["REQ-1-1-1", "REQ-1-1-2", "REQ-2-1"]
    mapping = M.authored_spec_map(suite, leaves, leaves)
    covered, missing = M.authoring_coverage(mapping, leaves)
    assert covered == 1
    assert missing == ["REQ-1-1-2", "REQ-2-1"]


def test_a_suite_that_covered_nothing_is_recognisable_as_such(tmp_path):
    """全军覆没必须**认得出**来 —— 这就是那条 WARNING 的判据。"""
    suite = _suite(tmp_path, ["something-else.spec.ts"])
    leaves = ["REQ-1-1-1", "REQ-1-1-2"]
    mapping = M.authored_spec_map(suite, leaves, leaves)
    covered, missing = M.authoring_coverage(mapping, leaves)
    assert covered == 0 and missing == leaves


def test_authoring_reports_what_it_actually_attached(tmp_path):
    """端到端：出完卷，`author_acceptance_suite` 交回来的表就是 `spec_map`。

    出卷那段**不再调用** `map_specs_to_nodes`（它靠解析文件名反推归属）。
    交回来的表直接当卷子用 —— 少一次反推，少一条会静默失灵的路径。
    """
    tree = _tree()
    layers = layerize(tree)
    flow = _AuthoringFlow(tmp_path)
    mapping = flow.author_acceptance_suite(tree, layers.order)
    for leaf in layers.leaves:
        assert mapping.get(leaf) == [f"{leaf}.spec.ts"], f"{leaf} 的出卷结果里没有它自己的卷子"
    assert mapping.get(None) == []


def test_a_turn_that_writes_nothing_yields_no_map(tmp_path):
    """一个回合都没开（模型空手而归）→ 交回空表，调用方据此知道卷子没出成。"""
    tree = _tree()
    flow = _AuthoringFlow(tmp_path, write=False)
    assert flow.author_acceptance_suite(tree, list(layerize(tree).order)) == {}


# ---------------------------------------------------------------- 出卷依据

def test_the_digest_carries_the_contract_stated_above_the_node():
    """改造三在出卷这一侧的同一个道理。

    跨节点的硬契约写在**它适用的那一层**的描述里（「网格的无障碍名字是
    Worksheet grid」在根上）。出卷的人只看叶子，卷子里就不会有那条断言 ——
    而卷子里没有的东西，实现的模型没有任何理由去满足，24 个叶子会各自发明
    一个名字。所以祖先的描述必须原文出现。
    """
    layers = layerize(_tree())
    digest = M.acceptance_digest(layers, "REQ-2-1")
    root_description = layers.by_id["ROOT"].node["description"]
    assert root_description in digest, "根上的跨节点契约没进叶子 REQ-2-1 的出卷依据"
    assert "aria-selected='true'" in digest, "节点自己的描述不在依据里"
    # 中间层也要在，且顺序是根 → 自己
    assert digest.index("ROOT") < digest.index("REQ-2") < digest.index("## This node")


def test_the_digest_quotes_the_scenarios_too():
    """场景是需求的一部分，出卷的人得看得到。"""
    layers = layerize(_tree())
    digest = M.acceptance_digest(layers, "REQ-1-1-1")
    assert "New workbook" in digest
    assert "activate" in digest and "New workbook" in digest


def test_a_huge_node_is_truncated_rather_than_dropped():
    """一个超大节点要截断，不能把整份依据撑爆（截断标记要在）。"""
    tree = _tree()
    tree["children"][1]["children"][0]["description"] = "x" * 50_000
    layers = layerize(tree)
    digest = M.acceptance_digest(layers, "REQ-2-1", cap=1000)
    assert "truncated" in digest
    assert len(digest) < 6000, "cap 必须真的封顶"


def test_an_unknown_node_yields_no_digest():
    layers = layerize(_tree())
    assert M.acceptance_digest(layers, "REQ-9-9-9") == ""


# ---------------------------------------------------------------- 分组

def test_leaves_are_grouped_by_their_top_level_branch():
    """一组一个出卷回合：一份卷子几十个文件挤在一个回合里，一错就全没了。"""
    layers = layerize(_tree())
    groups = M.authoring_groups(layers, layers.leaves)
    assert [sorted(g) for g in groups] == [
        sorted(["REQ-1-1-1", "REQ-1-1-2", "REQ-1-2"]),
        sorted(["REQ-2-1", "REQ-2-2"]),
    ]


def test_grouping_never_loses_a_leaf():
    layers = layerize(_tree())
    for cap in (1, 2, 3, 8):
        groups = M.authoring_groups(layers, layers.leaves, max_groups=cap)
        merged = [leaf for g in groups for leaf in g]
        assert sorted(merged) == sorted(layers.leaves), f"max_groups={cap} 丢了或重复了叶子"
        assert len(groups) <= max(cap, 1)


def test_the_tail_is_merged_not_dropped():
    """超过上限时，尾部并进最后一组 —— 回合数有上限，覆盖没有。"""
    layers = layerize(_tree())
    groups = M.authoring_groups(layers, layers.leaves, max_groups=1)
    assert len(groups) == 1
    assert sorted(groups[0]) == sorted(layers.leaves)


# ---------------------------------------------------------------- 环境变量

def test_build_octos_env_inherits_the_parent_port(tmp_path, monkeypatch):
    """**这是出卷那段代码的一个陷阱，钉在这里免得被「简化」掉。**

    平台上父进程带着 `PORT=<判分口>` 出生。`build_octos_env` 从
    `os.environ.copy()` 起步，所以它返回的 dict 里 `PORT` 是判分口。出卷那段
    为了刷新 `config.json` 里的 hooks 又调了一次它并 `env.update(...)` 回同一个
    dict（驱动拿的是引用）—— 于是 main.py 早先钉好的冒烟口被改回判分口，
    **两边的值都是合法端口号，所以没有任何症状**。回写一次冒烟口是必须的。
    """
    monkeypatch.setenv("PORT", "3000")
    env = M.build_octos_env(tmp_path / "cfg")
    assert env["PORT"] == "3000"
    env["PORT"] = "3100"                       # main.py 里钉的冒烟口
    env.update(M.build_octos_env(tmp_path / "cfg"))   # 出卷那段的刷新
    assert env["PORT"] == "3000", "这就是那个陷阱；调用点必须自己把冒烟口钉回去"


# ---------------------------------------------------------------- 时间闸门

class _FakeFlow:
    """只带 `_authoring_timeout` 需要的三个东西。"""

    def __init__(self, budget, elapsed, node_timeout=2400):
        self.budget = budget
        self._elapsed = elapsed
        self.node_timeout = node_timeout

    def remaining(self):
        return self.budget - self._elapsed


class _AuthoringFlow:
    """跑得动 `author_acceptance_suite` 的最小 Flow。

    `turn` 是这个假件的全部戏肉：它从提示词里认出这一组该写哪些节点，然后
    真的把 `<ID>.spec.ts` 写到 `suite_dir` 下 —— 因为被测的方法**只认磁盘**
    （模型说写完了不算，文件在才算），所以假模型也必须真的落盘。
    """

    def __init__(self, output_dir, budget=10_000, elapsed=0, write=True, node_timeout=2400):
        self.output_dir = Path(output_dir)
        self.budget = budget
        self._elapsed = elapsed
        self.node_timeout = node_timeout
        self._write = write
        self.labels: list[str] = []
        self.prompts: list[str] = []

    def remaining(self):
        return self.budget - self._elapsed

    def turn(self, prompt, timeout, label, expect_verification=True):
        self.labels.append(label)
        self.prompts.append(prompt)
        assert expect_verification is False, "出卷回合不跑构建，别让它因为「没验证就宣称完成」被记一笔"
        if self._write:
            suite = self.output_dir / M.AUTHOR_SUITE_DIR
            for line in prompt.splitlines():
                head = line.strip().split()[0] if line.strip() else ""
                if head.startswith("REQ-"):
                    (suite / f"{head}.spec.ts").write_text("// spec\n", encoding="utf-8")
        return True, "written"

    _authoring_timeout = M.Flow._authoring_timeout
    author_acceptance_suite = M.Flow.author_acceptance_suite
    audit_authored_suite = M.Flow.audit_authored_suite


def test_authoring_writes_a_digest_and_a_spec_for_every_leaf(tmp_path):
    """端到端：每个叶子一份出卷依据、一份卷子，一个都不少。

    依据落盘而不是塞进提示词，是因为一棵 42-65 节点的树渲染出来是
    200-440KB；挤进一个回合，回合失败就什么都没有。
    """
    tree = _tree()
    layers = layerize(tree)
    flow = _AuthoringFlow(tmp_path)
    flow.author_acceptance_suite(tree, layers.order)

    suite = tmp_path / M.AUTHOR_SUITE_DIR
    for leaf in layers.leaves:
        assert (suite / f"{leaf}.spec.ts").is_file(), f"{leaf} 没有卷子"
        digest = suite / "_requirements" / f"{leaf}.md"
        assert digest.is_file(), f"{leaf} 没有出卷依据"
        # 依据是可读的：根上的契约在叶子那一份里
        assert layers.by_id["ROOT"].node["description"] in digest.read_text(encoding="utf-8")
    # 分了几组就开了几个回合，且提示词里点名了本组的节点。出卷回合之后还各跟
    # 一个找茬回合（B 档）—— 标签前缀区分，两类各数各的。
    groups = M.authoring_groups(layers, layers.leaves)
    author_labels = [label for label in flow.labels if label.startswith("author specs")]
    audit_labels = [label for label in flow.labels if label.startswith("audit specs")]
    assert len(author_labels) == len(groups)
    assert len(audit_labels) == len(groups), "一组出完卷就该跟一个找茬回合"
    first = flow.prompts[0]
    for leaf in M.authoring_groups(layers, layers.leaves)[0]:
        assert f"  {leaf}  " in first, f"第一组点名了 {leaf}，提示词里没有"
        assert f"_requirements/{leaf}.md" in first, f"{leaf} 的依据文件没在提示词里"
    # 其他分支的节点不该出现在这一组的提示词里 —— 一组一个回合，别把整棵树塞进去
    assert "REQ-2-1" not in first


def test_a_turn_that_writes_nothing_is_not_counted_as_a_suite(tmp_path):
    """模型空手而归时，harness 不许自己把空目录当成一份卷子。"""
    tree = _tree()
    flow = _AuthoringFlow(tmp_path, write=False)
    flow.author_acceptance_suite(tree, list(layerize(tree).order))
    suite = tmp_path / M.AUTHOR_SUITE_DIR
    assert list(suite.glob("*.spec.ts")) == []


def test_authoring_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOS_ARC_AUTHOR_TESTS", "0")
    flow = _AuthoringFlow(tmp_path)
    flow.author_acceptance_suite(_tree(), list(layerize(_tree()).order))
    assert flow.labels == []


def _leaves(tree):
    return [i for i in layerize(tree).leaves if i in set(layerize(tree).order)]


def test_a_suite_on_disk_is_adopted_when_authoring_returns_nothing(tmp_path):
    """接力模式：出卷交回空表，但盘上躺着上一轮的卷子 —— 必须挂回节点。

    `OCTOS_ARC_AUTHOR_TESTS=0`（从卷子阶段往后跑）走的正是这里。挂空表的
    后果不是「验收弱一点」：`authoring_coverage` 0/N，每个节点在
    `own_ok = not specs` 下**自动通过**，而一个断言都没跑。
    """
    tree = _tree()
    suite = tmp_path / M.AUTHOR_SUITE_DIR
    suite.mkdir(parents=True)
    (suite / "REQ-1-1-1.spec.ts").write_text("// spec", encoding="utf-8")
    (suite / "REQ-2-1.spec.ts").write_text("// spec", encoding="utf-8")
    adopted = M.adopt_suite_on_disk({}, suite, list(layerize(tree).order), _leaves(tree))
    assert adopted["REQ-1-1-1"] == ["REQ-1-1-1.spec.ts"]
    assert adopted["REQ-2-1"] == ["REQ-2-1.spec.ts"]
    covered, missing = M.authoring_coverage(adopted, _leaves(tree))
    assert covered == 2
    assert "REQ-2-2" in missing


def test_adoption_never_overrides_what_authoring_returned(tmp_path):
    """出卷自己有产出时，盘上的盘点不许插嘴 —— 交回的就是出卷的那张表。"""
    tree = _tree()
    authored = {"REQ-1-1-1": ["REQ-1-1-1.spec.ts"]}
    suite = tmp_path / M.AUTHOR_SUITE_DIR
    suite.mkdir(parents=True)
    (suite / "REQ-2-1.spec.ts").write_text("// spec", encoding="utf-8")
    adopted = M.adopt_suite_on_disk(authored, suite, list(layerize(tree).order), _leaves(tree))
    assert adopted is authored


def test_disk_files_that_match_no_node_are_not_a_suite(tmp_path):
    """盘上有卷子、却一份都挂不上 —— 交回**空表**，不是「全是空列表的真话表」。

    调用方的 `if mapping:` 判断的就是「出卷成没成」，全空表的真值与空表相反；
    读错的话上面那条「每个节点自动通过」的 WARNING 永远不会触发。
    """
    tree = _tree()
    suite = tmp_path / M.AUTHOR_SUITE_DIR
    suite.mkdir(parents=True)
    (suite / "SOMETHING-ELSE.spec.ts").write_text("// spec", encoding="utf-8")
    assert M.adopt_suite_on_disk({}, suite, list(layerize(tree).order), _leaves(tree)) == {}


def test_authoring_stops_once_half_the_budget_is_gone():
    """剩余时间跌破一半就不再出卷：卷子不完整仍然能用（没卷子的节点退回
    今天的行为），没时间施工才是什么都没有。"""
    assert M.Flow._authoring_timeout(_FakeFlow(10_000, 5_001)) == 0.0
    assert M.Flow._authoring_timeout(_FakeFlow(10_000, 6_000)) == 0.0


def test_authoring_is_capped_by_the_single_turn_gate():
    """出卷回合也是回合：上限就是单回合闸门（超时不重放）。

    剩下的时间再多也不能给一个比闸门更长的回合 —— 那个回合会在闸门处被砍断，
    而超时不重放。
    """
    assert M.Flow._authoring_timeout(_FakeFlow(1_000_000, 0)) == 2400.0
    assert M.Flow._authoring_timeout(_FakeFlow(100_000, 40_000)) == 2400.0


def test_a_spent_budget_stops_authoring_before_any_turn(tmp_path):
    """时间不够就不开新回合 —— 一个回合都不开。

    依据文件仍然会落盘（那是本地几毫秒的事，而且下一轮可能还来得及用），
    但一个模型回合都不发，也就一个 spec 都不会产出。
    """
    flow = _AuthoringFlow(tmp_path, budget=10_000, elapsed=9_000)
    flow.author_acceptance_suite(_tree(), list(layerize(_tree()).order))
    assert flow.labels == []
    assert list((tmp_path / M.AUTHOR_SUITE_DIR).glob("*.spec.ts")) == []


def test_a_nearly_spent_budget_still_gets_a_usable_turn():
    """预算将尽但还没到一半时，按剩余时间的比例给，且有 240s 的下限。

    低于 240s 的回合写不完一组 spec，开它只是浪费一次调用。
    """
    assert M.Flow._authoring_timeout(_FakeFlow(1_000, 0)) == 240.0      # 200 → 抬到 240
    assert M.Flow._authoring_timeout(_FakeFlow(10_000, 4_900)) == 1020.0  # 5100 × 0.2，比例生效
    assert M.Flow._authoring_timeout(_FakeFlow(6_000, 0)) == 1200.0     # 6000 × 0.2，比例生效
