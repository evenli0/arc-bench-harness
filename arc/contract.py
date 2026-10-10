"""架构契约 —— 层级式编排的「形状」。

不变式 ① 说：**正被实现的节点，它的全部祖先契约都已存在。**
这个模块就是产出并持有那些契约的地方。

契约只定**跨节点可见**的东西：数据模型、路由命名、共享文件的分工边界、UI 骨架。
它**不定义任何节点内部怎么实现** —— 那是模型自己的事。

契约不是用来限制模型的，是用来**让模型知道自己的边界在哪**：
A 不需要猜 B 做了什么，因为形状已经定死了。

三个产出点：

    L0  全局蓝图     一次工具回合，读全部需求 + 全部 spec → 全局契约
    逐层 子域展开    每层一次**批量**回合 → 该层每个节点的子域契约
                     （批量而不是逐节点：12306 有 25 个非叶节点，
                       逐个展开要多 25 次调用；按层批量只要 2 次）

契约同时落盘（`.arc/design/`）—— 节点可以在回合里自己读，harness 只把摘要放进提示词。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from layers import Layers, forward_deps

# 放进每个节点提示词的契约上限。契约是「形状」不是「文档」，
# 超过这个长度说明它写了不该写的东西（实现细节）。
CONTRACT_IN_PROMPT_CHARS = 6000


BLUEPRINT_PROMPT = """\
Read the requirement document and the acceptance specs for this project, then write the \
architecture contract every later step will be held to. This is the L0 blueprint: it is \
**not code** and it is **not a plan for one feature** — it is the shape of the whole app.

You have tools. Use them. Read the specs under {tests_dir} (there are {n_specs} of them) — \
they are the exact behaviour every node will be graded on, so the contract has to agree with \
them. Read the requirement document under {req_dir}.

Write exactly one file: `{contract_path}` containing a single JSON object with these keys:

- `stack`     — how the app starts and is built (port {port}, build command, start command)
- `layout`    — directory layout, and **which requirement node owns which directory**
- `data_model`— every table/collection and its fields, with the requirement node that owns it
- `routes`    — every HTTP route: method, path, which node owns it, and the **shape it returns**
- `ui`        — every page/route in the frontend, which node owns it, and its main blocks
- `shared`    — files more than one node will have to touch (these are the dangerous ones)
- `order`     — the top-level requirement node ids, **in the order they should be built**

Rules that matter more than completeness:

1. **Name things exactly as the specs name them.** If a spec asserts on `GET /api/questions` \
returning `{{data: [...]}}`, the contract says `GET /api/questions` returns `{{data: [...]}}`. \
A contract that disagrees with a spec is worse than no contract.
2. **Assign every route and every table to exactly one owning node.** Two owners is the \
bug this whole design exists to prevent.
3. **Be short.** Aim for under 4000 characters. Only the shape — no prose, no justification, \
no implementation notes.
4. `shared` is the most important key. List the files that two or more nodes will edit. \
Those are where regressions come from.
5. `order` is the build plan and it is followed literally: the work is done one requirement \
area at a time, in exactly that order. Put the areas in the order you would build them \
yourself — whatever others depend on first, and whatever can be finished on its own before \
whatever needs it.

Write the file with your Write tool. Do not write any other file. Do not start implementing \
anything — this turn produces the contract and nothing else.

When you are done, say `CONTRACT WRITTEN` and stop.
"""


# 演进回合（app 已经存在）的蓝图提示词。与上面那份的分别只有一处，但那一处是
# 整轮维护的地基：**图是围着老楼画的** —— 契约描述的是「这栋楼改完之后的形状」，
# 而「老楼现在是什么样」由模型自己看过、写在 `{survey_path}` 里。命名沿用事实
# （现存的路由/字段/存储名就是事实），需求以决赛文本为准 —— 两者冲突时按文本
# 改行为、但不改名，除非文本点名要求改名。
BLUEPRINT_PROMPT_EXTEND = """\
Read the survey you just wrote (`{survey_path}`), the requirement document and the acceptance \
specs, then write the architecture contract every later step will be held to. This is the L0 \
blueprint: it is **not code** and it is **not a plan for one feature** — it is the shape of the \
whole app **after this round of work**. The app itself already exists and works; this round \
extends and changes it.

You have tools. Use them. Read the specs under {tests_dir} (there are {n_specs} of them) — \
they are the exact behaviour every node will be graded on, so the contract has to agree with \
them. Read the requirement document under {req_dir}. Read the app itself when the survey and \
the specs disagree about what is there.

Write exactly one file: `{contract_path}` containing a single JSON object with these keys:

- `stack`     — how the app starts and is built (port {port}, build command, start command)
- `layout`    — directory layout, and **which requirement node owns which directory**
- `data_model`— every table/collection and its fields, with the requirement node that owns it
- `routes`    — every HTTP route: method, path, which node owns it, and the **shape it returns**
- `ui`        — every page/route in the frontend, which node owns it, and its main blocks
- `shared`    — files more than one node will have to touch (these are the dangerous ones)
- `order`     — the top-level requirement node ids, **in the order they should be built**

Every route and every UI entry also carries an `origin`:

- `"carry"` — it exists today and keeps its exact current name and shape. Take the name from \
the app (the survey names them), **not** from your imagination: a rename breaks behaviour the \
requirement text still demands, and the specs that already pass will turn red.
- `"change"` — it exists today and its behaviour now differs: keep the existing name and shape \
wherever the requirement text does not explicitly demand otherwise, and describe the new \
behaviour in the shape you declare.
- `"new"` — nothing like it exists yet. Name it exactly as the spec and requirement text do.

Rules that matter more than completeness:

1. **Name things exactly as the specs name them.** If a spec asserts on `GET /api/questions` \
returning `{{data: [...]}}`, the contract says `GET /api/questions` returns `{{data: [...]}}`. \
A contract that disagrees with a spec is worse than no contract.
2. **Assign every route and every table to exactly one owning node.** Two owners is the \
bug this whole design exists to prevent.
3. **Be short.** Aim for under 4000 characters. Only the shape — no prose, no justification, \
no implementation notes.
4. `shared` is the most important key. List the files that two or more nodes will edit. \
Those are where regressions come from.
5. `order` is the build plan and it is followed literally: the work is done one requirement \
area at a time, in exactly that order. Put the carry-over areas the rest of the app leans on \
first, then the changes, then the new work.

Write the file with your Write tool. Do not write any other file. Do not start implementing \
anything — this turn produces the contract and nothing else.

When you are done, say `CONTRACT WRITTEN` and stop.
"""


EXPAND_PROMPT = """\
The global architecture contract is at `{contract_path}`. Read it first.

Now expand it **one level down**, for these requirement nodes:

{nodes}

For each of them, decide how its children divide up the work, and write exactly one file: \
`{out_path}` — a single JSON object keyed by the node id, each value:

- `owns`      — files/directories that node's subtree is responsible for
- `provides`  — what it exposes to the rest of the app (routes, exported functions, tables)
- `needs`     — what it requires from other nodes (routes it calls, tables it reads)
- `children`  — for each child id: what that child covers, in one line
- `order`     — the child ids, **in the order they should be built**

Rules:

1. **Every child gets a disjoint slice.** If two children would touch the same file, say so \
in `shared` instead of assigning it twice — the point of this file is to make the seams visible.
2. **Stay inside the global contract.** Do not rename a route, add a table, or change a \
return shape that `{contract_path}` already fixed. If the global contract is wrong, say \
`CONTRACT DEFECT: <what>` and stop — do not silently work around it.
3. Read the specs for these nodes under `{tests_dir}` before deciding. The seams have to \
match what the tests actually assert.
4. `order` is the plan and it is followed literally — the children are built in exactly that \
order, one after another. Put whoever provides a route or a table before whoever calls it, and \
anything two children share before both of them.
5. Be short. One line per child.

Write the file with your Write tool, then say `EXPANDED` and stop.
"""


@dataclass
class Contract:
    """全局契约 + 每个节点的子域切片。"""

    global_contract: dict = field(default_factory=dict)
    # node_id -> 该节点在父契约里的那一份（owns/provides/needs）
    slices: dict[str, dict] = field(default_factory=dict)
    path: Path | None = None

    def prompt_text(self, node_id: str, ancestors: list[str]) -> str:
        """给某个节点看的契约摘要 —— 全局 + 祖先链上的每一份切片。

        这是不变式 ① 的落地：节点开工时，**祖先契约一定在这里**。
        """
        parts: list[str] = []
        if self.global_contract:
            parts.append("Global contract:\n" + json.dumps(self.global_contract, ensure_ascii=False, indent=1))
        for anc in ancestors:
            piece = self.slices.get(anc)
            if piece:
                parts.append(f"{anc} (ancestor):\n" + json.dumps(piece, ensure_ascii=False, indent=1))
        mine = self.slices.get(node_id)
        if mine:
            parts.append(f"{node_id} (yours):\n" + json.dumps(mine, ensure_ascii=False, indent=1))
        text = "\n\n".join(parts)
        return text or "(no contract yet)"

    def root_order(self) -> list | None:
        """顶层的实现顺序 —— L0 蓝图给的那份计划。"""
        order = self.global_contract.get("order")
        return order if isinstance(order, list) else None

    def order_for(self, node_id: str) -> list | None:
        """某个节点的孩子该按什么顺序做 —— 它的子域切片里写着。"""
        piece = self.slices.get(node_id)
        if isinstance(piece, dict):
            order = piece.get("order")
            if isinstance(order, list):
                return order
        return None


def apply_order(declared: list | None, candidates: list[str]) -> list[str]:
    """把一份计划的顺序套在一组候选上。

    计划里认得的按计划排，没提到的按文档序补在后面。计划里出现不存在的 id 只是
    被忽略 —— 那是「计划没覆盖到这里」，不是错误。所以**没有计划时退化的就是树
    本来的顺序**，而不是某种排序启发式。
    """
    known = set(candidates)
    out: list[str] = []
    for item in declared or []:
        name = str(item)
        if name in known and name not in out:
            out.append(name)
    out.extend(c for c in candidates if c not in out)
    return out


def ordered_children(contract: Contract, layers: Layers, node_id: str) -> list[str]:
    """这个节点的孩子，按**计划**排好。

    顺序不是 harness 排的 —— 它是上层（L0 蓝图、逐层展开）在做计划时给出的，
    编排只是照着执行。
    """
    info = layers.by_id.get(node_id)
    if info is None:
        return []
    kids = list(info.children)
    declared = contract.order_for(node_id)
    return kids if declared is None else apply_order(declared, kids)


# ---------------------------------------------------------------- 解析

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_object(text: str) -> dict | None:
    """从回合文本里取一个 JSON 对象。

    先试裸解析，再从 ``` 围栏里取，最后退到「第一个 { 到最后一个 }」——
    模型经常在 JSON 前后加一句人话。
    """
    for candidate in (text, *(_FENCE.findall(text or ""))):
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except (ValueError, TypeError):
            return None
    return None


def read_contract_file(path: Path) -> dict | None:
    """读契约文件。模型可能没写、写坏、或写在别处 —— 全部返回 None，由调用方决定怎么办。"""
    try:
        return parse_json_object(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


# ---------------------------------------------------------------- 校验

def contract_defects(contract: dict, layers: Layers) -> list[str]:
    """L0 的完成标准：**契约自洽**。这是纯结构检查，不用 LLM。

    检查三件事 —— 每一条都对应一种会让后面坍塌的错误：

      1. 每个路由有且只有一个 owner，且那是个真实的需求节点
      2. 每个数据表有 owner
      3. `shared` 里的文件确实被两个以上的节点碰

    返回缺陷列表。空列表 = 契约通过。
    """
    defects: list[str] = []
    known = set(layers.order)

    routes = contract.get("routes") or []
    if not isinstance(routes, list):
        defects.append("routes is not a list")
        routes = []
    seen_route: dict[str, str] = {}
    for route in routes:
        if not isinstance(route, dict):
            defects.append(f"route entry is not an object: {route!r}")
            continue
        method = str(route.get("method") or "").upper()
        path = str(route.get("path") or "")
        owner = str(route.get("owner") or "")
        key = f"{method} {path}"
        if not path:
            defects.append("route with no path")
            continue
        if not owner:
            defects.append(f"{key}: no owner")
        elif owner not in known:
            defects.append(f"{key}: owner {owner!r} is not a requirement node")
        elif key in seen_route and seen_route[key] != owner:
            defects.append(f"{key}: owned by both {seen_route[key]} and {owner}")
        else:
            seen_route[key] = owner

    model = contract.get("data_model") or {}
    if isinstance(model, dict):
        for table, spec in model.items():
            owner = str((spec or {}).get("owner") or "") if isinstance(spec, dict) else ""
            if not owner:
                defects.append(f"table {table!r}: no owner")
            elif owner not in known:
                defects.append(f"table {table!r}: owner {owner!r} is not a requirement node")
    else:
        defects.append("data_model is not an object")

    shared = contract.get("shared") or []
    if not isinstance(shared, list):
        defects.append("shared is not a list")

    # `order` 是计划：一个不存在的 id 会让那一层退回文档序，而"计划被静默无视"
    # 比"计划写错了"更难发现 —— 所以它出声。
    order = contract.get("order")
    if order is not None:
        if not isinstance(order, list):
            defects.append("order is not a list")
        else:
            for item in order:
                if str(item) not in known:
                    defects.append(f"order names {item!r}, which is not a requirement node")

    return defects


def summarize(contract: dict) -> str:
    """给日志看的一行摘要。"""
    routes = contract.get("routes") or []
    tables = contract.get("data_model") or {}
    shared = contract.get("shared") or []
    return (f"{len(routes)} routes, "
            f"{len(tables) if isinstance(tables, dict) else '?'} tables, "
            f"{len(shared) if isinstance(shared, list) else '?'} shared files")


# ---------------------------------------------------------------- 提示词片段

def node_contract_block(contract: Contract, layers: Layers, node_id: str) -> str:
    """一个节点开工时看到的契约。**这是不变式 ① 的物理形态。**"""
    info = layers.by_id.get(node_id)
    ancestors = info.ancestors if info else []
    text = contract.prompt_text(node_id, ancestors)
    if len(text) > CONTRACT_IN_PROMPT_CHARS:
        text = text[:CONTRACT_IN_PROMPT_CHARS] + "\n...(truncated; read the full file if you need it)"
    return ("Architecture contract (you MUST conform to this — it is what makes your code "
            "compose with everyone else's):\n" + text + "\n")


def forward_deps_block(layers: Layers, node_id: str, depth_limit: int = 8) -> str:
    """**谁依赖我** —— 修复视野的「向前」那一边。

    分层把「A 可能弄坏 B、B 可能弄坏 A」这种无向关系变成了单向：上层依赖下层。
    所以视野就是「往上看 + 看脚下」，而这两样都是**已知的**，不用猜。
    """
    deps = forward_deps(layers, node_id)
    if not deps:
        return ("Nothing depends on you — you are a leaf with no dependents. "
                "You may change anything you own.\n")
    shown = deps[:depth_limit]
    more = f" (and {len(deps) - len(shown)} more)" if len(deps) > len(shown) else ""
    return (f"**Depends on you**{more}: {', '.join(shown)}\n"
            f"Changing a shared file or a route shape breaks these. If you must change one, "
            f"their specs are in the baseline — check them before you commit to the change.\n")
