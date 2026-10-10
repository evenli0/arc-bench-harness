"""分层器的测试。

这一层是纯函数，所以能精确断言 —— 编排里最容易搞错的两件事
（「祖先是谁」和「谁的验收可以开始了」）都在这里定死。
"""

from layers import (
    aggregate_order,
    forward_deps,
    is_leaf,
    layerize,
    pending_children,
)


def _tree():
    """    REQ-1        订票系统        深度 0
           ├ REQ-1.1    搜索            深度 1
           │ ├ 1.1.1    按时间排序        深度 2
           │ └ 1.1.2    按价格筛选        深度 2
           └ REQ-1.2    下单            深度 1
             └ 1.2.1    选座            深度 2
    """
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


def test_layerize_records_depth_and_ancestor_chain():
    layers = layerize(_tree())
    assert layers.by_id["REQ-1"].depth == 0
    assert layers.by_id["REQ-1.1"].depth == 1
    assert layers.by_id["1.1.1"].depth == 2
    assert layers.by_id["1.1.1"].ancestors == ["REQ-1", "REQ-1.1"]
    assert layers.chain("1.1.1") == ["REQ-1", "REQ-1.1", "1.1.1"]
    assert layers.max_depth == 2


def test_layerize_keeps_document_order_at_every_depth():
    layers = layerize(_tree())
    assert layers.depths[1] == ["REQ-1.1", "REQ-1.2"]
    assert layers.depths[2] == ["1.1.1", "1.1.2", "1.2.1"]
    assert layers.order == ["REQ-1", "REQ-1.1", "1.1.1", "1.1.2", "REQ-1.2", "1.2.1"]


def test_leaves_are_the_nodes_the_implementation_pass_visits():
    layers = layerize(_tree())
    assert layers.leaves == ["1.1.1", "1.1.2", "1.2.1"]
    assert layers.roots == ["REQ-1"]


def test_atomic_node_with_children_is_still_a_leaf():
    """ATOMIC 是判据里更强的那一条 —— 有孩子也拦不住它。"""
    assert is_leaf({"id": "X", "type": "ATOMIC", "children": [{"id": "Y"}]}) is True
    assert is_leaf({"id": "X", "type": "FOLDER"}) is False
    assert is_leaf({"id": "X"}) is True


def test_node_without_id_is_walked_through_not_dropped():
    """没有 id 的容器不建 NodeInfo，但它的孩子不能被丢掉。"""
    layers = layerize({"id": "R", "children": [
        {"name": "no id here", "children": [{"id": "A"}]},
    ]})
    assert "A" in layers.by_id
    assert layers.by_id["A"].depth == 1
    assert layers.by_id["A"].ancestors == ["R"]


# ---------------------------------------------------------------- 不变式 ②

def test_node_becomes_verifiable_only_when_every_child_passed():
    """**不变式 ② 的判据。** 这就是「父节点的验收等子节点全部完成」。"""
    layers = layerize(_tree())
    verdict = {}

    assert pending_children(layers, "REQ-1.1", verdict) == ["1.1.1", "1.1.2"]

    verdict["1.1.1"] = True
    assert pending_children(layers, "REQ-1.1", verdict) == ["1.1.2"]

    verdict["1.1.2"] = True
    assert pending_children(layers, "REQ-1.1", verdict) == []


def test_a_failed_child_keeps_the_parent_unverifiable():
    """没过 ≠ 通过。False 和「没做过」一样挡住父节点。"""
    layers = layerize(_tree())
    verdict = {"1.1.1": True, "1.1.2": False}
    assert pending_children(layers, "REQ-1.1", verdict) == ["1.1.2"]


def test_leaf_has_no_children_so_it_is_verifiable_immediately():
    """叶子实现完立刻可验收 —— 不然「从小到大」就没有起点了。"""
    layers = layerize(_tree())
    assert pending_children(layers, "1.1.1", {}) == []


def test_aggregate_order_walks_upward_from_the_changed_node():
    """「每做完一个东西，往上检查它让什么变得完整了」。"""
    layers = layerize(_tree())
    assert aggregate_order(layers, "1.1.1") == ["REQ-1.1", "REQ-1"]


# ---------------------------------------------------------------- 视野

def test_forward_deps_lists_descendants_dependents_and_ancestors():
    """**视野 = 往上看 + 看脚下。** 三种依赖都要在，因为它们是不同的爆炸半径。"""
    t = _tree()
    t["children"][0]["children"][0]["dependencies"] = ["1.2.1"]
    layers = layerize(t)
    deps = forward_deps(layers, "1.2.1")
    # 祖先：我坏了他们就不成立。**由近及远** —— 父节点是最紧的那条。
    assert deps.index("REQ-1.2") < deps.index("REQ-1")
    # 显式依赖：1.1.1 的 dependencies 里写了我
    assert "1.1.1" in deps


def test_ancestors_come_out_nearest_first():
    """提示词按这个顺序截断，所以父节点必须在祖父前面 —— 被砍掉的要是最松的。"""
    layers = layerize(_tree())
    assert forward_deps(layers, "1.1.1") == ["REQ-1.1", "REQ-1"]


def test_dependency_naming_a_folder_expands_to_its_whole_subtree():
    t = _tree()
    t["children"][1]["children"][0]["dependencies"] = ["REQ-1.1"]
    layers = layerize(t)
    deps = forward_deps(layers, "1.1.1")
    assert "1.2.1" in deps


