"""层级式编排的地基：需求树 → 精度层级。

**这是新架构与旧架构的分界线。**

旧的 `topo_order`（`requirement_order.py`）把需求树压成一串**原子节点**，
按依赖排序，一个一个做 —— DFS 的世界观：需求是任务清单，做完一个划掉一个。

这里不压平。树的结构**就是**编排：深度 = 精度层级，每一层都是一个完整的 app，
只是越来越细。父节点不消失 —— 它同时是「契约的产出者」（实现时先于子节点）
和「验收的聚合点」（验收时后于子节点）。

    REQ-1        订票系统                    深度 0
    ├ REQ-1.1    搜索                        深度 1
    │ ├ 1.1.1    按时间排序                  深度 2
    │ └ 1.1.2    按价格筛选                  深度 2
    └ REQ-1.2    下单                        深度 1
      └ 1.2.1    选座                        深度 2

纯函数，无 I/O、无 LLM、无文件系统。
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _children_of(node: dict) -> list[dict]:
    return [c for c in (node.get("children") or []) if isinstance(c, dict)]


def is_leaf(node: dict) -> bool:
    """无子节点的节点就是叶子：ATOMIC 类型，或没孩子且不是 FOLDER。"""
    node_type = str(node.get("type") or "").upper()
    if node_type == "ATOMIC":
        return True
    if node_type == "FOLDER":
        return False
    return not _children_of(node)


@dataclass
class NodeInfo:
    """一个需求节点在层级里的位置。"""

    id: str
    node: dict
    depth: int
    parent: str | None
    children: list[str] = field(default_factory=list)
    ancestors: list[str] = field(default_factory=list)  # 根 → 父，文档序
    descendants: list[str] = field(default_factory=list)  # 全部后代，文档序

    @property
    def is_leaf(self) -> bool:
        return not self.children

    @property
    def root(self) -> str:
        return self.ancestors[0] if self.ancestors else self.id


@dataclass
class Layers:
    """整棵需求树的分层视图。"""

    tree: dict
    by_id: dict[str, NodeInfo]
    order: list[str]  # 全部节点（含非叶子），文档序
    depths: dict[int, list[str]]  # 深度 → 该深度全部节点，文档序
    max_depth: int

    @property
    def leaves(self) -> list[str]:
        return [i for i in self.order if self.by_id[i].is_leaf]

    @property
    def roots(self) -> list[str]:
        return list(self.depths.get(0, []))

    def chain(self, node_id: str) -> list[str]:
        """根 → 本节点（含自身）。实现时的祖先链。"""
        info = self.by_id.get(node_id)
        return (info.ancestors + [node_id]) if info else [node_id]

def layerize(tree: dict) -> Layers:
    """需求树 → 分层视图。

    深度从 0（根）递增。文档序在所有输出里保持。
    没有 `id` 的节点被跳过，但它的孩子照样会被走下去 —— 容器不建 NodeInfo，
    孩子不能因此丢。
    """
    by_id: dict[str, NodeInfo] = {}
    order: list[str] = []
    depths: dict[int, list[str]] = {}

    def walk(node: dict, depth: int, ancestors: list[str], parent: str | None) -> list[str]:
        node_id = str(node.get("id") or "")
        children = _children_of(node)
        if not node_id:
            # 无 id 的容器：直接下钻，不建 NodeInfo
            out: list[str] = []
            for child in children:
                out.extend(walk(child, depth, ancestors, parent))
            return out

        info = NodeInfo(id=node_id, node=node, depth=depth, parent=parent, ancestors=list(ancestors))
        by_id[node_id] = info
        order.append(node_id)
        depths.setdefault(depth, []).append(node_id)

        descendants: list[str] = []
        for child in children:
            descendants.extend(walk(child, depth + 1, ancestors + [node_id], node_id))
        info.descendants = descendants
        info.children = [d for d in descendants if by_id[d].parent == node_id]
        return [node_id] + descendants

    walk(tree, 0, [], None)
    return Layers(tree=tree, by_id=by_id, order=order, depths=depths,
                  max_depth=max(depths) if depths else 0)


def forward_deps(layers: Layers, node_id: str) -> list[str]:
    """**谁依赖我** —— 我要改东西时的爆炸半径。

    三种依赖，按确定性排序：

      1. **后代** —— 结构上就在我的地盘里。改共享面，他们最先遭殃。
      2. **显式依赖** —— 谁的 `dependencies` 里写了我（含文件夹依赖展开）。
      3. **祖先** —— 我是他们的一部分，我坏了他们就成立不了。
         **由近及远**：父节点是最紧的那条约束，根是最松的。提示词里按这个顺序截断，
         被砍掉的是最松的那些。

    这是旧架构没有的东西。分层把「A 可能弄坏 B，B 可能弄坏 A」这种**无向**关系
    变成了**单向**：上层依赖下层。所以视野就是往上看 + 看脚下。
    """
    info = layers.by_id.get(node_id)
    if not info:
        return []

    out: list[str] = list(info.descendants)

    # 显式依赖：文件夹 id 展开成它的全部节点
    def expand(dep: str) -> list[str]:
        target = layers.by_id.get(str(dep))
        if target is None:
            return []
        return [target.id] + target.descendants

    for other_id in layers.order:
        if other_id == node_id or other_id in out:
            continue
        for dep in layers.by_id[other_id].node.get("dependencies") or []:
            if node_id in expand(dep):
                out.append(other_id)
                break

    # 祖先：我坏了，他们就不成立。由近及远 —— 父节点是最紧的那条约束。
    for anc in reversed(info.ancestors):
        if anc not in out:
            out.append(anc)

    return out


def pending_children(layers: Layers, node_id: str, verdicts: dict) -> list[str]:
    """本节点还没通过的直接子节点。

    不变式 ② 的判据：一个节点只有在**全部子节点都通过**之后才可验收。
    叶子节点没有子节点 → 恒返回空 → 实现完立刻可验收。
    """
    info = layers.by_id.get(node_id)
    if not info:
        return []
    return [c for c in info.children if verdicts.get(c) is not True]


def aggregate_order(layers: Layers, changed: str) -> list[str]:
    """`changed` 刚刚通过之后，**从它往上**有哪些节点可能变得可验收。

    这就是「从小到大验收」的机制本体：每做完一个东西，往上检查它让什么变得完整。
    返回自下而上的顺序（changed 的父、祖父、……），供调用方逐个检查
    `pending_children`。
    """
    info = layers.by_id.get(changed)
    if not info:
        return []
    return list(reversed(info.ancestors))
