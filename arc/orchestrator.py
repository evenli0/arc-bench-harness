"""层级式编排的主循环 —— 实现从大到小，验收从小到大。

这是旧 `node_cycle` / `topo_order` 那套 **DFS 编排**的替代品。区别不在参数，
在世界观：

    DFS（旧）    需求是任务清单，做完一个划掉一个。中间态**残缺**。
    层级（新）   需求是精度层级，每层都是完整 app。中间态**完整**。

判别式：**跑到一半，app 是完整的吗？**

在 DFS 结构里，「全局」这个概念只在最后才存在 —— 所以每个节点的验收
**结构上不可能**是全局的。这不是检测写得不好，是它被放在那个位置。
所以这里不是改判据，是换结构。

整个编排就是三个不变式：

    ① 实现从大到小   正被实现的节点，全部祖先契约都已存在
    ② 验收从小到大   节点 spec 通过 ⟹ 整棵子树都通过。父节点的验收等子节点全完成
    ③ 单调性         通过的只能增加。破坏 = 不提交 + 告诉它破坏了谁

执行的形状：

    L0 蓝图    读全部需求 + 全部 spec → 架构契约（契约自洽）
    L1 骨架    按契约搭空壳（build 通过 + 路由可达）
    逐层展开   每层一次**批量**回合 → 该层每个节点的子域契约
    叶子实现   按深度 BFS，工具模式，一次调用
    聚合验收   子节点全过 → 父节点变可验收 → 递归向上 → 根

    L0 ──▶ L1 ──▶ 展开 ──▶ 实现 ──▶ 聚合
                             │         ▲
                             └─────────┘
                       往下走实现，往上走验收

**这个模块不替模型做决定。** harness 只做两件事：给足信息、验全局。
不排序源码、不算上下文配额、不打包引用 —— 那些是把智能从模型手里拿走，
旧架构那 15 个模式分支就是这么长出来的。唯一的硬约束是契约。

用法（`Flow.run()` 里）：

    Orchestrator(self).run(tree)
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from acceptance import (
    RunSummary,
    failure_summaries,
    final_workers,
    nodes_for_failures,
    startup_error_digest,
)
from contract import (
    BLUEPRINT_PROMPT,
    BLUEPRINT_PROMPT_EXTEND,
    EXPAND_PROMPT,
    Contract,
    contract_defects,
    forward_deps_block,
    node_contract_block,
    apply_order,
    ordered_children,
    parse_json_object,
    read_contract_file,
    summarize,
)
from layers import Layers, aggregate_order, forward_deps, layerize, pending_children


def log(msg: str) -> None:
    """和 `main.log` 同形。这里自己写一份是为了**避免 main ↔ orchestrator 循环导入**
    —— main 要 import 本模块才能换入口，本模块再 import main 就成环了。"""
    import sys

    print(msg, flush=True)
    print(msg, file=sys.stderr, flush=True)


def _atomic_repair() -> bool:
    """分批修复开关。默认开（`OCTOS_ATOMIC_REPAIR=1`）。

    `OCTOS_ATOMIC_REPAIR=0` 精确回到旧的**整段式**修复路径（`_accept_legacy`）——
    两套路径验收判据逐字相同，差别只在修复的粒度，所以这个开关是安全的
    A/B 对照旋钮，不是两套逻辑。"""
    return os.environ.get("OCTOS_ATOMIC_REPAIR", "1") != "0"


def _atomic_final() -> bool:
    """收尾分批开关。默认开（`OCTOS_ATOMIC_FINAL=1`）。

    `OCTOS_ATOMIC_FINAL=0` 时 `Flow.final_acceptance` 走它自己的旧 body（原来的
    一把修全部红节点）—— 逐字保留，没有 orchestrator 的旧测试照样走那条。
    两套收尾的差别只在修复粒度与提交时机：新路径「没过秤的刀不上盘」，
    旧路径边修边无条件提交。"""
    return os.environ.get("OCTOS_ATOMIC_FINAL", "1") != "0"


def _abandon_region_enabled() -> bool:
    """**v5 默认关**：一个要求点修不动，不再连坐它的整个区域。

    正赛的杀伤链是这个：一个节点三轮回合耗尽 → `_abandon_region` 把它整个区域
    （兄弟节点一起）判死 → 那些没做错的需求点也拿不到分。增量维护里更糟：
    继承的旧代码本来就有一部分是坏的，连坐等于拿旧账罚新工。

    旧行为一个都不删，`OCTOS_ABANDON_REGION=1` 原样回来（连同它的测试）。
    """
    return os.environ.get("OCTOS_ABANDON_REGION", "0") == "1"


def _diagnose_enabled() -> bool:
    """诊断队列开关，默认开（`OCTOS_DIAGNOSE=0` 关掉）。"""
    return os.environ.get("OCTOS_DIAGNOSE", "1") != "0"


def _diagnosis_cap() -> int:
    """一次编排里最多做几次诊断回合。**有上限**才敢让它主动花回合 ——
    诊断是投资（找出真正的病根），不是无底洞。"""
    try:
        return max(0, int(os.environ.get("OCTOS_DIAGNOSE_MAX", "8")))
    except ValueError:
        return 8


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " […]"


# ---------------------------------------------------------------- 提示词

SKELETON_TURN = """\
The architecture contract is written at `{contract_path}`. Read it — it is the shape of the \
whole app and every later step is held to it.

Your job now is the **L1 skeleton**: make the app's frame exist so that every requirement \
that comes later has somewhere to land. This is **not** a feature. Do not implement any \
requirement's behaviour.

Build, from the contract:

- the directory layout it names (`frontend/`, `backend/`, and whatever else it lists)
- **every backend route it declares**, as a placeholder handler that returns the declared \
shape with empty data (an empty list, an empty object) — correct path, correct method, \
correct response shape, no real logic
- **every frontend page it declares**, as a page that exists and renders its main blocks
- the data model as storage that exists (an empty JSON file or in-memory structure is fine)

{tests}
The completion standard is mechanical and it is the only thing that counts:

    1. `npm run build` in `frontend/` succeeds
    2. the backend starts on port {smoke_port} and **every route in the contract answers**

A route that is missing, or whose path/method differs from the contract, fails the standard. \
A route that returns empty data passes it.

Write the code with your tools. When you are done, say `SKELETON BUILT` and stop.
"""

NODE_REPAIR_TURN = """\
Your work on `{node_id}` is not accepted yet.
{requirement}
{failures}
{baseline_note}
{contract}
{forward}
The contract above is the boundary you must stay inside. Fix the code with your tools.

If the failure is that a **route or a shape does not match the contract**, fix your code to \
match the contract — do not change the contract, and do not change another node's file to \
make yours pass.

If you believe the contract itself is wrong — it names something the specs contradict — say \
`CONTRACT DEFECT: <what>` and stop. Do not silently work around it.
"""

CONTRACT_AMEND_TURN = """\
A node reported a defect in the architecture contract at `{contract_path}`:

    {claim}

Before acting on that claim the harness checked it against the specs and the requirement \
document — the files the work is graded on. This is what those files actually say:

{evidence}

**The files are the ground truth, not the claim.** Change the contract file `{contract_path}` \
to agree with them: change only the part the claim is about, and leave every other route, \
table, owner and file exactly as it is. Keep it one JSON object with the same keys.

If what the files say does not actually contradict the contract, change nothing at all.

Say `CONTRACT AMENDED` and stop.
"""

AGGREGATE_REPAIR_TURN = """\
`{node_id}` is now the last thing standing between the app and its acceptance: every one of \
its children passes, but its own specs do not. That means the seam between them, or \
something that belongs at this level, is missing.
{requirement}
{failures}
{contract}
{forward}
This is an integration failure, not a fresh feature. The children's code is already in place \
and passing — read it before you change it, and prefer adding the missing piece over \
rewriting what works.

Fix it with your tools.
"""

# #6b/#6c 小步修复纪律（与 `_requirement_block` 的「主脊换位」一起拼进每一次
# 修复回合）。两句话对应两条机器事实，不是空话：
#   · 「一小步、写完即交」—— 回合之后 harness 立刻测量（批修循环），keep/revert
#     按这一小步判；模型在自己的回合里跑长自测（1165s 超时那一夜）既慢又不可信
#     （Windows 上 shell 本就不可靠），还把墙钟烧在 harness 马上要重做的事上。
#   · 「结果回喂」—— 下一回合的失败清单就是上一次测量刚量出来的（own/小域都
#     是测得最新的一份），所以「交出去」不是交进黑洞。
SMALL_STEP_NOTE = """
Work in **one small step** — one file, or the single coherent change the behaviour directly \
needs — then stop and hand the turn back. The harness builds the app, starts it, and runs \
these specs the moment you stop; its measurement, not your own testing, decides whether the \
step is kept: a step that turns a failing spec green without breaking any other is kept, one \
that breaks anything is rolled back whole, and the next turn's failure list tells you what \
that measurement found. Do not run builds or test suites yourself — small verified steps beat \
a large unverified rewrite, and that is how this turn will be judged either way.
"""

# ---------------------------------------------------------------- v5 增量维护
#
# 三个新提示词 —— 对应「起跑先勘察现场 / 图纸围着老楼画 / 搁浅的题做诊断」。

SURVEY_TURN = """\
The app already exists in this workspace and this round is **maintenance, not a rebuild**. \
Before any design work, survey what is actually there and write the facts down.

Read, with your tools:

- the requirement document under `{req_dir}` — the text the app is now held to;
- the pre-build health list at `{plan_path}` — every requirement node with the result of \
running its spec against the current app (green = already satisfied, partial, red = not met);
- the app itself — `frontend/` and `backend/`: the routes it serves, the pages it has, the \
storage files it keeps, and the names it already uses (routes, fields, storage keys, \
accessible names).

Write exactly one file, `{survey_path}` — a short factual survey. Nothing invented: every \
line has to be something you saw in the files.

1. **Carry over** — requirement areas the health list shows green: name the file(s) and \
route(s) that implement each one. These must keep working exactly as they do now.
2. **Change** — requirement areas the app already covers but whose text now demands a \
different behaviour: what exists today, and what the new text asks for.
3. **New** — requirement areas with nothing in the app yet.
4. **Facts to build on** — the real route table, storage locations, page structure and shared \
conventions (ports, sign-in, data files) the new work must extend rather than reinvent.

Do not change any code in this turn. Say `SURVEY WRITTEN` and stop.
"""

SKELETON_TURN_EXTEND = """\
The architecture contract is written at `{contract_path}`. Read it — it is the shape of the \
app *after* this round. The app itself already exists and works; this round **extends** it.

Your job now is the **frame check plus completion**, not a new skeleton:

- the survey at `{survey_path}` lists what this app already is (its routes, pages, storage, \
accessible names) — read it before touching anything, and treat every item in it as something \
that already works;
- verify the frame the contract describes is in place: `npm run build` in `frontend/` \
succeeds, the backend starts on port {smoke_port}, and every route the contract declares \
answers (empty data is fine — that is what a placeholder returns);
- where the contract says a route or page is **new** and it does not exist yet, add it as a \
placeholder that returns the declared shape, written in the style of the existing code;
- where something already exists, leave it alone.

**Do not restructure.** Do not move, rename or rewrite anything that already exists — not a \
file, not a route, not a page, not a storage file, not an accessible name. Everything the app \
already does is a promise to its users; a rename breaks it silently. Carry-over behaviour \
changes happen in the node turns, against the requirement text, and only there.

{tests}
The completion standard is mechanical and it is the only thing that counts:

    1. `npm run build` in `frontend/` succeeds
    2. the backend starts on port {smoke_port} and **every route in the contract answers**

A route that is missing, or whose path/method differs from the contract, fails the standard. \
A route that returns empty data passes it.

Write the code with your tools. When you are done, say `SKELETON BUILT` and stop.
"""

DIAGNOSE_TURN = """\
`{label}` is a requirement this run has already failed to repair, twice or more. Before \
anything else happens to it, diagnose it: the code, the spec and the requirement text are \
three different things, and they can disagree.

This turn is **read-only** — you cannot edit files or run commands; read, judge, and name \
the change and where it goes. A repair turn will make it, and the harness measures it.

Read, with your tools:

- the requirement text — `{req_dir}`;
{requirement}
- the spec file(s) that fail: {specs}
- the implementation they exercise — read the code before judging it;
- the last failure summary below, and the ledger of what was already tried.

{failures}
{ledger}
Before you decide what is wrong, check the neighbours. Every spec in this suite drives one \
running server and one persisted store, so this one can fail because another spec changed the \
object it depends on — the data, the name, the state — and never put it back. Read the specs \
that touch the same workbook, sheet, cell, account or name; when one of them explains the \
failure, name it (and the exact line that leaves the shared state changed, \
`<spec file>:<line>`) instead of blaming the app for it.

Decide which of the three is wrong, then do the one thing that follows:

- The **spec** asserts something the requirement text does not say, or contradicts it: say \
`DIAGNOSIS: SPEC DEFECT: <what the spec asserts that the text does not>` and stop. A harness \
check will read the requirement document and void the spec only if the document backs you, \
so name your evidence: the exact requirement line it contradicts, or the spec line \
(`<spec file>:<line>`) where the unsupported assertion sits. An assertion that no \
implementation could ever pass — the spec's own code guaranteeing the failure, like a \
promise handed straight to `expect()` — is the strongest form: the harness can verify \
that by reading the spec, so say plainly why it fails for every implementation.
- The **code** is missing the behaviour the text requires: say \
`DIAGNOSIS: IMPLEMENTATION: <what is missing, in one sentence>` and name where the change \
goes (file path, and the line if you have it) — a repair turn makes the change.
- The **work was off track** — the requirement was misread or built in the wrong shape: say \
`DIAGNOSIS: OFF TRACK: <what was misread and where the text actually says it>` and stop.

Never weaken, delete or rewrite an assertion to make it pass — that is the one thing this \
turn must never do. End with exactly one `DIAGNOSIS:` line.
"""


# ---------------------------------------------------------------- 结果

@dataclass
class NodeResult:
    """一个节点的验收结论。"""

    node_id: str
    passed: bool = False
    own_passed: bool = False
    baseline_ok: bool = True
    turns: int = 0
    reason: str = ""
    # 修复轮真的用完了还是环境出问题（spec 跑不起来）。**只有前者才判死** ——
    # 浏览器起不来不是这个节点的错，拿它去废掉一个区域是冤枉。
    exhausted: bool = False
    # #5d 环境判死：构建/启动坏了，且本跑已经不再治（`_startup_given_up`）。
    # 与 `exhausted` 分开的理由是**去向不同**：这两个都记 `parked`（停的是这一跑，
    # 不是这个节点 —— 它压根没量成），但只有 `exhausted` 进诊断队列：诊断的判词
    # 建立在「应用能跑」上，对一棵跑不起来的树说什么都是瞎猜，治环境该烧的预算
    # 已经在入口烧过了（`_heal_app_startup`）。
    env_dead: bool = False
    # #8 哨兵的观察值：收尾时「自己的卷子」的**逐卷台账**
    # （basename -> (通过的用例数, 用例总数)）。只有走到「有过一次可信测量」的
    # 收尾才有值；`None` = 没量成 / 环境判死 / 压根没卷子 —— 哨兵对它既不计数
    # 也不清零（不知道的事不猜）。判据见 `_systemic_watch`。
    own_final: dict[str, tuple[int, int]] | None = None


# ---------------------------------------------------------------- 编排

class Orchestrator:
    """层级式编排。持有 `Flow`，但不改它的行为 —— 只用它的回合、spec 运行和 git。"""

    def __init__(self, flow, pre_passed: set[str] | None = None) -> None:
        self.flow = flow
        self.layers: Layers | None = None
        self.contract = Contract()
        # 已经通过、不需要实现的节点。目前只有一个来源：evolution 模式下
        # harness 探测出「现有 app 已经满足它的 spec」的那些节点。
        self.pre_passed: set[str] = set(pre_passed or ())
        self.verdict: dict[str, bool] = {}  # node_id -> 通过了
        self.results: list[NodeResult] = []
        # 全局基线：**已经通过的全部 spec**。不变的只有「只增不减」这一条。
        # 它是 ③ 的底线，也是每一次修复提示词里那份清单。
        self.baseline_specs: list[str] = []
        self._baseline_seen: set[str] = set()
        # 判死之后**一起放弃的整个得分区域**：node_id -> 为什么。
        # 比赛按区域给分：区域里有一个没过，这个区域就没分了 —— 所以区域里
        # 还没做的兄弟不再实现（做了也不得分），预算让给别的区域。
        self.abandoned: dict[str, str] = {}
        # **哪些基线卷子在最近一次观察时是红的。** 按文件名记（Playwright 报回来的
        # 是 work_dir 下的路径，稳定的只有 basename）。
        #
        # 没有这本账的时候，红的卷子会一直留在 `baseline_specs` 里、永远没人摘掉，
        # 于是**每一个**走到聚合验收的节点都会被同一句话冤枉一次：「你的改动让 N 份
        # 本来通过的卷子变红了」——而它多半什么都没弄坏。被冤枉的节点会花光全部修复
        # 轮去修一个不是自己造成的失败，修不好就连坐：整个区域被判死、放弃。
        #
        # 于是一处没抓到的回归会变成一串「冤枉 → 连坐」。这本账把链条切断：红过一次
        # 的卷子，之后不再算在任何人的头上。
        self.baseline_red: set[str] = set()
        # 被作废的自产卷子（按 basename 记，和 `baseline_red` 同一个口径）。
        # 它们还在盘上、还在 `spec_map` 之外的地方，但**不算验收、不算基线**。
        self.voided_specs: set[str] = set()
        # -- #1 绿过点名体检：两本「这份卷子见过什么」的账（2026-10-08 提速批）----
        # 全量体检（对账）只为**曾经绿过**和**从没量过**的卷子付钱：顺序做题时
        # 还没轮到的卷子本来就是红的，而它们永远不会变成体检要抓的东西（体检
        # 抓的是「我们的刀弄红了域外的绿」和「从没量过的卷子」）—— 每次全卷都
        # 为它们重复付一遍钱，就是大树上「体检比干活还贵」的那一半。
        # 口径与 `baseline_red`/`voided_specs` 一致：**basename**（Playwright
        # 报回来的是工作目录下的路径，稳定的只有文件名）。两本账都只增不减。
        self.ever_green_specs: set[str] = set()
        self.observed_specs: set[str] = set()
        # 用例级的同一本账（`_run_reconcile` 用它把「域外新红」收口到回归）：
        # 一份卷子「见过绿灯」不等于它的每一条用例都见过 —— 只实现了一半的
        # 需求（实测 9/10、1/3 这类部分绿的卷子）红着的那几条从来不是回归。
        # 口径与 `_failure_keys` 一致：(basename, title)。由 `_note_spec_
        # observations` 在每次测量后填（探针那次测量会把满分的卷子整份填进来）。
        self.ever_green_tests: set[tuple[str, str]] = set()
        # evolution 预通过的节点：探测那一步就是一次测量（现成 app 满足它），
        # 卷子从开局起就在绿名单里 —— 不然它们的卷子会被当成「从没量过」多量
        # 一遍，绿名单记账要等第一次对账才补上。
        for pre_id in self.pre_passed:
            for spec in (getattr(flow, "spec_map", None) or {}).get(pre_id) or []:
                self.ever_green_specs.add(Path(spec).name)
                self.observed_specs.add(Path(spec).name)
        self.design_dir: Path = Path(flow.output_dir) / ".arc" / "design"
        self.contract_path: Path = self.design_dir / "contract.json"
        # 契约缺陷：主张 → 取证 → 改（或不改）。上限是防一句反复出现的主张
        # 把整个 run 拖进「改契约 → 又不服 → 再改」的环里。
        self.last_turn_text: str = ""
        self.amendments: list[str] = []
        self.max_amendments: int = 3
        self._amending: bool = False
        # -- P2：全卷对账（节点阶段隔一段时间查一次域外的账） ------------------
        # 上次对账的墙钟与那时的 HEAD。第一次 keep 只记起点、不测量 —— 见
        # `_maybe_reconcile`。
        self._reconcile_at: float = 0.0
        self._reconcile_sha: str | None = None
        # 上次对账之后攒了几次 keep（每次 keep 是一个新的提交，才有可查的刀）。
        self._reconcile_keeps: int = 0
        # 修前向：对账查到「域外新红 + 肇事提交」之后，把这份红并进接下来的
        # 修复批（见 `_record_forward_fix`）。green 之后自动摘掉。
        self._forward_fix_keys: set[tuple[str, str]] = set()
        self._forward_fix_files: set[str] = set()
        self._forward_fix_note: str = ""
        # -- 实现→卷子 静态接口对账的缓存（2026-10-08，只提示）------------------
        # 键 = (HEAD, 这批卷子的文件名)：同一棵树上同一批卷子只扫一遍源码。
        # 一提一提交 HEAD 就变 —— 缓存自然失效，不需要额外的失效逻辑。
        self._impl_iface_cache: dict = {}
        # -- #7 二遍跑检测：卷子「用完不还原」的证据 ---------------------------
        # `_nonrestoring_specs`：变绿那一刻又原样复跑一遍、第二遍红了的自产卷
        # （basename 口径，与 `baseline_red`/`voided_specs` 一致）。收尾/对账在
        # 把红记到实现头上之前，先按这份名单**从出厂态复查**（见 `_ghost_reds`）。
        # `_secondpass_done`：查过的节点不重复查 —— 「首次变绿」只发生一次。
        self._nonrestoring_specs: set[str] = set()
        self._secondpass_done: set[str] = set()

        # -- #8 系统性失败哨兵：叶子环的连续全红计数 -------------------------
        # 每张收尾后仍然 0 用例通过的卷子 +1；任何一张有通过用例的卷子清零；
        # 「量不动」既不计数也不清零。到线（`_systemic_limit`）就停叶子线。
        self._systemic_streak: int = 0
        # 收尾分批里被 park 的文件。**跨 pass 持久**：`final_acceptance_passes`
        # 会跑好几趟，同一份修不动的红不该在每一趟里重新吞一轮回合。
        self._final_parked: set[str] = set()
        # -- v5 增量维护：台账 / 诊断队列 / 超时止损 --------------------------
        # 常驻台账：谁绿谁红、为什么、试过哪几招、下一个修谁。每回合读写，
        # 落盘 `.arc/ledger.md` —— 「模型心里有数」靠的就是这本账。
        self.ledger: dict[str, dict] = {}
        # 搁浅的红进诊断队列：机器主动对比「需求原文 vs 卷子断言 vs 实现」，
        # 出结论（卷子错 → 作废 / 实现缺 → 带着结论回炉 / 理解跑题 → 重读需求）。
        # 只在已经判死、常规修复不会再碰的题上花回合。
        self._diagnosis: dict[str, dict] = {}
        self._diagnosis_done: set[str] = set()
        self._diagnosis_turns = 0
        self._requeued: set[str] = set()
        self._off_track: dict[str, str] = {}
        # 连续超时：一轮修复被墙钟烧完还没交活，不是「修过了」—— 记下来，
        # 别按常规再试一次同样的超时（见 `_turn` 与两处批循环）。
        self._last_turn_timed_out = False
        # #5e 异常终止轮：回合判**失败**（桥梁/接口异常、被判死），不只是超时。
        # 这种轮次盘上留的是写了一半的树，与超时轮同属「没被验证过的落盘」——
        # github-2 实锤：死轮半成品留盘 → 后端种子崩 → 连坐 3 张卷子。调用方
        # 靠这个标志把失败轮也退回检查点（见 `_accept_batched` / `implement_leaf`）。
        self._last_turn_failed = False
        # #5a 环境病「本跑已判死」：一次没治回来的坏构建，后面的节点别再各烧一轮
        # （同一个坏环境反复治 = 新的空转源）。任何一次量到 app 能跑就清零 ——
        # 判死的是**这棵坏树**，不是这次 run。见 `_heal_app_startup`。
        self._startup_given_up = False
        # #9 接盘预检只发生一次：它挂在两处（main 的接盘站 + `run()` 开场），
        # 谁先到谁执行（见 `preflight`）。开盘的「量一次」重复一遍没有任何新信息，
        # 白付一次构建 + 一卷墙钟。
        self._preflighted = False
        # #3 熔断（超时与零收成**合账**）：同一节点连续多少轮修复零进展。
        # 到 `_stall_limit()` 就停修、记档、不再回队 —— 修不动的东西不再
        # 一轮轮复测白量（接力实测：REQ-6-1-1 空转一轮 101s、REQ-8-1-1 被量
        # 20 次）。任何一次净胜都清零。在它名下的批次照旧 park 进诊断队列。
        self._stall_streak: dict[str, int] = {}
        # #5f 出厂态验收：运行时状态文件（盘上证据：`stores_written`）与它们的
        # 出厂内容参照（仓库根提交 = run 开始那棵树）。每轮验收前复位，见
        # `_reset_runtime_state`。
        self._runtime_files: set[str] = set()
        self._factory_sha: str | None = None
        # #5 同树同卷集复用（2026-10-08 提速批）：**干净树 → 那棵树上量过的结果**。
        # 键 (HEAD sha, grader_like)，值 {"files": {卷子 basename: [TestOutcome…]},
        # "stores": [运行时写过的文件]}。只有 `allow_reuse=True` 的测量才进/出这本
        # 账（见 `_run_specs`）—— 故意的复测一律绕开它。
        self._measure_memo: dict[tuple[str, bool], dict] = {}

    # -- 入口 ------------------------------------------------------------

    def run(self, tree: dict) -> dict[str, bool]:
        """需求树进，通过/未通过的结论出。"""
        self.layers = layerize(tree)
        n_leaf = len(self.layers.leaves)
        log(f"[layer] {len(self.layers.order)} nodes, {n_leaf} leaves, "
            f"{len(self.layers.order) - n_leaf} inner, max depth {self.layers.max_depth}")
        try:
            self.design_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log(f"[layer] could not create {self.design_dir}: {exc}")

        # 整个新架构只有工具模式一条路：模型必须自己翻文件、自己改。
        # 驱动没有工具能力时它会一条路都走不动，而失败的样子是「模型只回了一句话」
        # —— 看上去像模型不行，其实是它手里什么都没有。这一句要喊得足够响。
        checker = getattr(self.flow, "tool_tier_available", None)
        if callable(checker) and not checker():
            log("[layer] !! the driver has NO tool tier: this architecture is tool-only "
                "and cannot build anything without it. Expect zero.")

        if self._stop():
            return self.verdict

        # #9 接盘归一 + 冷启动预检：任何一层楼开工之前，先量一次「判分环境看到的
        # 那份东西能不能跑」，并把运行时状态归一到出厂内容（见 `preflight`）。
        self.preflight()

        flow = self.flow
        # v5 增量维护：楼是旧的，图纸要围着老楼画 —— 先勘察现场，再出蓝图。
        if flow.has_app():
            self.survey()

        # 实现从大到小：契约先于实现，骨架先于功能。
        self.blueprint()
        self.skeleton()
        self.expand_all()

        # 叶子实现 + 每做完一个就往上聚合（验收从小到大）。
        self.implement_leaves()

        # v5：搁浅的红进诊断队列（需求原文 vs 卷子断言 vs 实现），走完再往上聚合
        # —— 卡住的题只赔它自己，不连坐。
        self.diagnose_parked("node")

        # 收尾：没走到的聚合点（比如某个子节点没通过）与最终全量基线。
        self.finalize()
        self._ledger_write()

        passed = sum(1 for ok in self.verdict.values() if ok)
        log(f"[layer] done: {passed}/{len(self.layers.order)} nodes passed")
        if self.abandoned:
            log(f"[layer] {len(self.abandoned)} node(s) forfeited: "
                f"{sorted(self.abandoned)} (a region is scored as a whole — one failure "
                f"inside it loses the region, so nothing else in it was built)")
        return self.verdict

    def _stop(self) -> bool:
        """预算烧完 / 墙钟到点 —— 编排的每一层循环都要问这一句。"""
        flow = self.flow
        try:
            if flow.wound_down():
                log("[layer] cost guard tripped; stopping the layered run")
                return True
            if flow.time_up():
                log(f"[layer] time budget exhausted ({flow.remaining():.0f}s left); stopping")
                return True
        except Exception as exc:  # noqa: BLE001
            log(f"[layer] budget check failed ({exc}); continuing")
        return False

    # -- #9 接盘归一 + 冷启动预检 -------------------------------------------

    def preflight(self) -> None:
        """#9 接盘预检：开盘先量一次 —— 判分环境看到的那份东西，能不能跑。

        背景（github-2 实锤）：接盘时没有任何一关检查「应用能不能跑」。上一轮
        留下的运行时数据（`data.json` 这一族，或任何被应用自己写脏的状态文件）
        配上一轮的新代码，可以做到启动即崩 —— 整晚 29 张卷连红、零告警，烧的是
        叶子环，病根在开盘那一刻就埋下了。

        这一趟做三件事，只对**自产/认回的卷子**生效（`authored_suite` ——
        跨项约定：官方卷路径一字不动）：

        ① 量尺 = 盘上**最小的一份卷子**（按文件大小挑：预检只问「起得来吗」，
           越小越便宜）。这一量顺带把运行时状态观测集填上（#5f 的观测唯一入口
           是测量 —— 开盘这一次也是测量）。
        ② 把观测到的运行时文件**归一到出厂内容**（`_reset_runtime_state`）。
           随包态 ≠ 出厂态 = github-2 那一幕：判分环境预置的是出厂数据，拿我们
           跑了一半的状态称量，称出来的是「新代码 × 我们的残渣」。归一动过东西
           时，从出厂态**再量一次**才算「冷启动成功」—— 证明的是判分口径起得来，
           不是我们的残渣起得来。
        ③ 起不来（随包态、或归一后的出厂态）→ 走 #5a 的同一门抢修（量尺换成这份
           最小卷子，提示词如实说量尺）；治不好**当场**记 `_startup_given_up` +
           一句重话，后面的节点不会再各烧一轮（与入口判死同一套口径，只是把
           发现它的时刻提到了蓝图之前）。

        纪律同快照：整个预检包在 try/except 里、所有动作幂等 —— **辅助动作
        绝不变成新的故障点**。没有卷子 / 没有 runner / 不是自产卷 / 还没有 app：
        整段静默跳过。
        """
        if self._preflighted:
            return                     # 开盘只量一次（接盘站与 run 开场共用它）
        flow = self.flow
        if not getattr(flow, "authored_suite", False):
            return                     # 跨项约定：官方卷路径不受这一条影响
        if getattr(flow, "runner", None) is None:
            return                     # 没有 Playwright / 没有卷子：没有量尺
        has_app = getattr(flow, "has_app", None)
        if callable(has_app) and not has_app():
            return                     # 毛坯（app 还没有）：没有「接盘」这回事
        self._preflighted = True       # 先钉住：异常/预算死也不重来（见上面的注释）
        try:
            if self._stop():
                return
            specs = self._all_specs()
            if not specs:
                return
            self._preflight_checks(specs)
        except Exception as exc:  # noqa: BLE001
            log(f"[preflight] the handover check raised ({exc}); continuing without it")

    def _preflight_checks(self, specs: list[str]) -> None:
        """预检的正身（异常兜底在 `preflight`；这里只写正常路径）。"""
        flow = self.flow
        ruler = self._pick_ruler(specs)
        log(f"[preflight] handover cold-start check: measuring {ruler} against the workspace "
            f"as handed over ({len(specs)} spec file(s) on disk)")
        summary = self._run_specs([ruler], grader_like=False)
        if summary is None:
            log("[preflight] the check could not measure anything; leaving the workspace as it is")
            return
        normalized = self._reset_runtime_state("the handover cold-start check")
        if normalized:
            # 归一动过东西：随包态与出厂态确实不同（github-2 那一幕）。口径切换
            # 的意义只有从出厂态**再量一次**之后才成立 —— 判分环境看到的就是它。
            retry = self._run_specs([ruler], grader_like=False)
            if retry is not None:
                summary = retry
            else:
                log("[preflight] the re-measure after the factory reset did not come back; "
                    "judging on the measurement taken before it")
        if not self._app_broken(summary):
            if normalized:
                log("[preflight] cold start from factory state: OK — the handed-over runtime "
                    "state was reset to what the grader would see, and the app comes up on it")
                self._ledger_note("_preflight", status="green", tried=[
                    "handover: the runtime state differed from factory content; reset to the "
                    "factory copy, cold start from factory state OK (no repair needed)"])
                self._ledger_write()
            else:
                log("[preflight] cold start: OK — the app builds and starts, and its runtime "
                    "state already matches the factory content")
            return
        # 起不来 = 环境病。先把诊断信息喊进日志（谁在什么状态下量到的），
        # 抢修走 #5a 同一门 —— 量尺不变，提示词如实说这一站。
        log(f"[preflight] **the app does not build/start at handover**: "
            f"{startup_error_digest(str(summary.error or ''), 200)}")
        result = NodeResult(node_id="_preflight")
        deadline_guard = time.time() + max(300.0, flow.remaining() * 0.5)
        healed = self._heal_app_startup(result, "preflight", summary, [ruler], deadline_guard,
                                        where=f"the handover cold-start check ({ruler})")
        if healed is None:
            self._startup_given_up = True
            log("[preflight] startup repair did not take: the run continues with the app in this "
                "state — every node that has to measure will be parked without burning repair "
                "turns (the same judgment the first acceptance would have made, made now, "
                "before the blueprint)")
        else:
            self._ledger_note("_preflight", status="green", tried=[
                "handover: the app did not build/start; startup repair brought it back "
                "(the run proceeds)"])
            self._ledger_write()
            log("[preflight] the app builds/starts again after startup repair; the run proceeds")

    def _pick_ruler(self, specs: list[str]) -> str:
        """预检的量尺：盘上最小的一份卷子（按字节大小）。

        挑小的理由单一：预检只问「起得来吗」，用例多少与这个问题无关；量尺越小，
        构建之外的墙钟越省。读不到大小的排最后（照样能当量尺，只是不优先）。
        空 `specs` 由调用方挡掉。
        """
        root = Path(str(getattr(self.flow, "tests_dir", "") or ""))

        def weight(rel: str) -> tuple[float, str]:
            try:
                return (float((root / rel).stat().st_size), rel)
            except OSError:
                return (float("inf"), rel)

        return min(specs, key=weight)

    # -- v5 勘察 ---------------------------------------------------------

    def survey(self) -> None:
        """起跑前的勘察现场：读旧楼、写事实，一行代码都不改。

        它的产出（`.arc/survey.md`）是蓝图的事实底料：哪些行为已经在了（沿用）、
        哪些要改、哪些没有，以及这个 app 已经在用的真实名字（路由、存储、页面、
        无障碍名）。没有这一趟，蓝图就是凭空画的 —— 而它画的每一条都会被后面
        每个节点当成边界遵守。
        """
        if self._stop():
            return
        flow = self.flow
        plan_path = Path(flow.output_dir) / ".arc" / "construction-plan.md"
        survey_path = self.design_dir.parent / "survey.md"
        log("[v5] survey: reading the existing app before any design work")
        prompt = SURVEY_TURN.format(req_dir=flow.req_dir or "(none)",
                                    plan_path=plan_path, survey_path=survey_path)
        self._turn(prompt, "survey existing app", timeout=self._long_turn())
        log(f"[v5] survey: {survey_path if survey_path.exists() else '(not written)'}")

    # -- L0 蓝图 ---------------------------------------------------------

    def blueprint(self) -> None:
        """读全部需求 + 全部 spec → 架构契约。完成标准：契约自洽（纯结构检查）。"""
        if self._stop():
            return
        flow = self.flow
        log("[L0] blueprint: writing the global architecture contract")
        flow.mark("design_started", self.layers.roots[0] if self.layers.roots else "root")
        # v5：楼是旧的 → 用「沿用/改造/新增」版蓝图提示词，先读勘察报告。
        template = BLUEPRINT_PROMPT_EXTEND if flow.has_app() else BLUEPRINT_PROMPT
        prompt = template.format(
            tests_dir=flow.tests_dir or "(none)",
            n_specs=len(self.baseline_specs or self._all_specs()),
            req_dir=flow.req_dir,
            contract_path=self.contract_path,
            port=flow.web_port,
            survey_path=self.design_dir.parent / "survey.md",
        )
        defects: list[str] = []
        for attempt in (1, 2):
            self._turn(prompt, "L0 blueprint", timeout=self._long_turn())
            self.contract.global_contract = read_contract_file(self.contract_path) or {}
            if not self.contract.global_contract:
                defects = [f"no readable JSON object at {self.contract_path}"]
            else:
                defects = contract_defects(self.contract.global_contract, self.layers)
            if not defects:
                self.contract.path = self.contract_path
                log(f"[L0] contract accepted: {summarize(self.contract.global_contract)}")
                return
            log(f"[L0] contract defects (attempt {attempt}): {defects[:6]}")
            if attempt == 1:
                prompt = (f"The contract you wrote at `{self.contract_path}` does not hold up. "
                          f"Structural problems:\n- " + "\n- ".join(defects[:12]) +
                          "\n\nRewrite the file with those fixed. Every route and every table needs "
                          "exactly one owner, and that owner has to be a real requirement node id from "
                          "the requirement document. Say `CONTRACT WRITTEN` and stop.")
        # 两次都没自洽：带着现有的契约往下走。
        # **不做硬失败** —— 结构缺陷会在 L1 的 build 和路由探针那里暴露，
        # 而节点被允许回一句 `CONTRACT DEFECT: ...` 而不是硬适配（见 NODE_REPAIR_TURN）。
        self.contract.path = self.contract_path if self.contract.global_contract else None
        log(f"[L0] proceeding with an unverified contract ({len(defects)} defects); "
            f"L1 build and the route probe are the next filter")

    # -- L1 骨架 ---------------------------------------------------------

    def skeleton(self) -> None:
        """按契约搭空壳。完成标准：**build 通过 + 声明的路由可达**（可机械验证）。"""
        if self._stop():
            return
        flow = self.flow
        log("[L1] skeleton: building the frame every later node lands in")
        base = read_contract_file(self.contract_path) if self.contract.path else {}
        # 测试装置的约定（base URL、导航、页头）属于**跨节点共享面**，所以 L1 就要知道 ——
        # 不然架子搭出来的形状和所有 spec 都对不上，后面每个节点都要替它还债。
        tests = flow.tests_prompt_for(None, skeleton=True)
        slots = dict(smoke_port=flow.smoke_port, tests=tests,
                     survey_path=self.design_dir.parent / "survey.md")
        # v5：楼是旧的 → 「体检＋补齐」版：框架已在，动作是核实与补缺，不是重建。
        template = SKELETON_TURN_EXTEND if flow.has_app() else SKELETON_TURN
        prompt = template.format(contract_path=self.contract_path, **slots)
        if not base:
            # 契约没落盘也要搭骨架 —— 从需求文档直接搭，比没有架子强。
            prompt = ("No readable contract exists yet. Read the requirement document at "
                      f"`{flow.req_dir}` and the specs under `{flow.tests_dir}`, derive the app's "
                      "shape yourself, and build the L1 skeleton as described below.\n\n") + \
                     template.format(contract_path="(none — derive it from the requirements)",
                                     **slots)
        err = "not attempted"
        for attempt in (1, 2):
            self._turn(prompt, "skeleton build", timeout=self._long_turn())
            err = self.verify_skeleton()
            if err is None:
                log("[L1] skeleton accepted: build passes and every declared route answers")
                flow.mark("implementation_started", self.layers.roots[0] if self.layers.roots else "root")
                return
            log(f"[L1] skeleton rejected (attempt {attempt}): {err[:400]}")
            if attempt == 1 and not self._stop():
                prompt = (f"The skeleton you built does not meet the standard.\n\n{err}\n\n"
                          "Fix it with your tools. The standard is mechanical: `npm run build` in "
                          "`frontend/` succeeds, the backend starts, and every route in the contract "
                          "answers. A route returning empty data passes; a route that 404s or is "
                          "missing fails. Say `SKELETON BUILT` and stop.")
        log(f"[L1] skeleton still failing after 2 attempts; node turns will build on what is there")

    def verify_skeleton(self) -> str | None:
        """L1 的完成标准，机械检查。None = 通过。"""
        flow = self.flow
        server = flow.app_server(grader_like=False)
        try:
            err = server.build()
            if err:
                return f"`npm run build` failed:\n{err}"
            err = server.start()
            if err:
                return f"the backend did not start:\n{err}"
            return self.probe_routes()
        except Exception as exc:  # noqa: BLE001
            return f"could not verify the skeleton: {exc}"
        finally:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass

    def probe_routes(self) -> str | None:
        """契约里声明的每一条后端路由都要答得上话。None = 全通。"""
        contract = self.contract.global_contract or read_contract_file(self.contract_path) or {}
        routes = contract.get("routes") or []
        if not isinstance(routes, list) or not routes:
            return None  # 没有声明就没有可验的；不拿这个拦路
        missing: list[str] = []
        for route in routes[:60]:
            if not isinstance(route, dict):
                continue
            method = str(route.get("method") or "GET").upper()
            path = str(route.get("path") or "")
            if not path:
                continue
            probe = _probe_path(path)
            status, body = self._probe_one(method, probe)
            if status is None:
                missing.append(f"{method} {path} — no answer ({body[:120]})")
            elif status == 404 and _DEFAULT_404.search(body or ""):
                missing.append(f"{method} {path} — 404, the route is not mounted")
        if not missing:
            return None
        return ("The contract declares these routes and they do not answer:\n- "
                + "\n- ".join(missing[:20]) +
                f"\n\n({len(missing)} of {len(routes)} declared routes are unreachable.)")

    def _probe_one(self, method: str, path: str) -> tuple[int | None, str]:
        url = f"http://127.0.0.1:{self.flow.smoke_port}{path}"
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return resp.status, resp.read(600).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read(600).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            return exc.code, body
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    # -- 逐层批量展开 -----------------------------------------------------

    def expand_all(self) -> None:
        """每一层一次批量回合，把契约展开到下一级。

        **批量而不是逐节点**：12306 有 143 个节点、26 个非叶 —— 逐个展开要多 26 次调用；
        按层批量只要 `max_depth` 次。树的实测形状是浅而宽（最大深度 3），所以这是 2–3 次。
        """
        if self.layers is None:
            return
        for depth in range(self.layers.max_depth):
            parents = [n for n in self.layers.depths.get(depth, [])
                       if self.layers.by_id[n].children]
            if not parents:
                continue
            if self._stop():
                return
            self.expand_layer(depth, parents)

    def expand_layer(self, depth: int, parents: list[str]) -> None:
        out_path = self.design_dir / f"contract-d{depth}.json"
        lines: list[str] = []
        for pid in parents:
            info = self.layers.by_id[pid]
            kids = ", ".join(f"{c} ({self._title(c)})" for c in info.children)
            lines.append(f"- `{pid}` — {self._title(pid)}\n  children: {kids}")
        log(f"[expand] depth {depth}: {len(parents)} node(s) → {out_path.name}")
        prompt = EXPAND_PROMPT.format(
            contract_path=self.contract_path if self.contract.path else "(not written)",
            nodes="\n".join(lines),
            out_path=out_path,
            tests_dir=self.flow.tests_dir or "(none)",
        )
        self._turn(prompt, f"contract expand d{depth}", timeout=self._long_turn())
        data = read_contract_file(out_path)
        if not data:
            log(f"[expand] depth {depth}: no readable slice at {out_path.name}; "
                f"the children will work from the global contract alone")
            return
        added = 0
        for pid in parents:
            piece = data.get(pid)
            if isinstance(piece, dict):
                self.contract.slices[pid] = piece
                added += 1
        log(f"[expand] depth {depth}: {added}/{len(parents)} slices recorded")

    # -- 叶子实现 ---------------------------------------------------------

    def plan_order(self) -> list[str]:
        """叶子按**计划**的顺序，一个区域连着下一个区域。

        顺序不是这里排的。L0 蓝图给出顶层顺序，逐层展开给出每一层孩子的顺序，
        这个函数只是把那份计划摊平成一条执行线 —— 上层没说的地方退回文档序
        （文档序本身就是前序，也就是"区域连着区域"）。

        旧版按深度 BFS：它会把同一区域的兄弟拆散（4.1.1 → 4.2.1 → 4.3.1 → 4.1.2），
        于是一个区域的事从来不在一个时间窗里做完。
        """
        out: list[str] = []
        seen: set[str] = set()

        def kids_of(node_id: str) -> list[str]:
            """顶层用 L0 蓝图给的顺序 —— 它说的是**顶层区域**（根的孩子们）。

            只有多个根的时候它才是在排根本身，所以两种形状都试一遍，认不出的那份
            由 apply_order 原样补在后面，退化成文档序。
            """
            info = self.layers.by_id.get(node_id)
            if info is None:
                return []
            declared = self.contract.root_order()
            if declared is not None and info.parent is None:
                return apply_order(declared, list(info.children))
            return ordered_children(self.contract, self.layers, node_id)

        def walk(node_id: str) -> None:
            info = self.layers.by_id.get(node_id)
            if info is None or node_id in seen:
                return
            seen.add(node_id)
            if info.is_leaf:
                out.append(node_id)
                return
            for child in kids_of(node_id):
                walk(child)

        declared = self.contract.root_order()
        roots = list(self.layers.roots)
        if declared is not None:
            roots = apply_order(declared, roots)
        for root in roots:
            walk(root)
        for node_id in self.layers.order:  # 计划没覆盖到的（无 id 容器等）
            if node_id not in seen:
                walk(node_id)
        return out

    def implement_leaves(self) -> None:
        """按**计划**实现叶子：一个区域连着下一个区域。

        非叶节点**默认不实现**：它们产出契约（不变式 ①），由聚合验收判分（不变式 ②）。
        只有当一个非叶节点的 spec 在子节点全过后仍然失败，它才被修复 —— 那时候
        所有子节点的代码都在，那是一次真正的集成修复，不是凭空造功能。
        """
        if self.layers is None:
            return
        # 已经验证通过的节点直接记进 verdict、不进实现队列 —— 不变式 ② 的聚合
        # 会被它们触发，所以这一步必须在实现之前。
        for node_id in self.pre_passed:
            if node_id in self.layers.by_id:
                self.verdict[node_id] = True
        if self.pre_passed:
            log(f"[layer] {len(self.pre_passed)} node(s) already pass against the existing app: "
                f"{sorted(self.pre_passed)}")
            for node_id in self.pre_passed:
                if node_id in self.layers.by_id and self.layers.by_id[node_id].is_leaf:
                    self._aggregate_from(node_id)

        leaves = [n for n in self.plan_order() if n not in self.pre_passed]
        total = len(leaves)
        for index, node_id in enumerate(leaves, start=1):
            if self._stop():
                log(f"[layer] stopping with {total - index + 1} leaves untouched")
                return
            if node_id in self.abandoned:
                log(f"[abandon] {index}/{total} {node_id} not built: {self.abandoned[node_id]}")
                continue
            result = self.implement_leaf(node_id, index, total)
            # #8 系统性失败哨兵：一张一张地看 —— 叶子环连续全红就喊停，别把
            # 整晚烧在「连跑 29 张全红、零告警」那条路上（github-2 实锤）。
            if self._systemic_watch(node_id, result, index, total):
                return

    def implement_leaf(self, node_id: str, index: int, total: int) -> NodeResult:
        """实现 + 验收一个叶子。返回值 = 验收结论（#8 哨兵要读它收尾时的
        逐卷台账 `own_final`）。"""
        flow = self.flow
        info = self.layers.by_id[node_id]
        specs = list(flow.spec_map.get(node_id) or [])
        log(f"[leaf] {index}/{total} {node_id} (depth {info.depth}, {len(specs)} spec(s))")
        flow.mark("implementation_started", node_id)
        pre = flow.head()
        # #5c 节点检查点：这一刀之前整棵树的盘面。实现回合是全流程里**唯一没有
        # 快照兜底**的回合 —— 它判失败/被掐时落盘的是写了一半的树，会污染之后
        # 每一次测量（github-2 实锤：死轮半成品留盘 → 后端种子崩 → 连坐 3 张卷子）。
        snap = self._snapshot(f"{node_id} implement")

        prompt = self._implement_prompt(node_id)
        self._turn(prompt, f"{node_id} implement", timeout=self._implement_timeout(index, total))
        if self._last_turn_failed:
            # #5e 异常终止轮也回退：回合没跑完（判失败 / 被墙钟烧完）—— 这一刀不是
            # 「没做完的原料」（那种照旧留着，见下面 `_accept` 之后的注释），而是
            # 没写完的半成品：退回节点检查点，这个节点回到「没开工」的样子，照常
            # 走验收 / 修复（`_accept` 会量出它的起点，修复批就地把它补起来）。
            if snap:
                flow.restore_app(snap)
                log(f"[leaf] {node_id}: the implement turn did not finish; rolled back to the "
                    f"node checkpoint")
            else:
                log(f"[leaf] {node_id}: the implement turn did not finish and there is no "
                    f"checkpoint to roll back to; the tree is left as it is")
            self._ledger_note(node_id, tried=[
                "implement turn did not finish"
                + (", rolled back — nothing kept" if snap else " (no checkpoint to roll back to)")])
            self._ledger_write()

        result = self._accept(node_id, specs, f"{node_id}", aggregate=False, since_sha=pre)
        result.turns += 1
        self.verdict[node_id] = result.passed
        self.results.append(result)
        if result.passed:
            flow.mark("implementation_done", node_id, "specs pass, baseline intact")
            self._ledger_note(node_id, status="green", why="specs pass, baseline intact")
            self._ledger_write()
            self._aggregate_from(node_id)
            return result
        flow.mark("implementation_failed", node_id, result.reason)
        log(f"[leaf] {node_id} NOT accepted: {result.reason}")
        # 回滚只在**它破坏了已经通过的东西**时发生（③ 的字面意思：「破坏 = 立刻回滚」）。
        # 一个节点没做完自己的 spec 并没有破坏谁，它的半成品是后面那次集成修复的原料 ——
        # 丢掉等于白付一次回合的钱。所以：**没做完就留着工作区，只保证 HEAD 干净**。
        if not result.baseline_ok and pre:
            flow.restore_app(pre)
            log(f"[leaf] rolled back {node_id}: it broke the baseline")
        # 修复轮用完了还是没过。**v5：不连坐** —— 这个节点进诊断队列，别处的兄弟
        # 照做（整区放弃的旧行为在 `OCTOS_ABANDON_REGION=1` 下原样保留）。
        if result.exhausted:
            if _abandon_region_enabled():
                self._abandon_region(node_id, result.reason or "repairs exhausted")
            else:
                self._queue_diagnosis(node_id, node_id,
                                      [Path(s).name for s in specs],
                                      result.reason or "repairs exhausted", "node")
                log(f"[leaf] {node_id} parked for diagnosis; the rest of the app carries on")
        # `env_dead` 也记 `parked`（#5d）：停的是这一跑，不是这个节点 —— 它压根没
        # 量成，写成 `red` 等于把环境的锅记在它头上。与 `exhausted` 的区别只在
        # 去向：诊断队列见上面那条（环境病不进）。
        self._ledger_note(node_id,
                          status=("parked" if (result.exhausted or result.env_dead) else "red"),
                          why=result.reason,
                          tried=[f"implement + {result.turns} repair turn(s)"])
        self._ledger_write()
        return result

    @staticmethod
    def _spec_tally(summary) -> dict[str, tuple[int, int]]:
        """一次测量 → 逐卷台账：basename -> (通过的用例数, 用例总数)。

        收尾的 `own_final` 就是这么来的。`summary.results` 里通过与失败的用例
        都在（`_failure_keys` 只挑失败的那半边）—— 所以「一份卷子全判不通过」
        （total>0 且 passed==0）从这份台账里数得出来。
        """
        tally: dict[str, list[int]] = {}
        for r in getattr(summary, "results", None) or []:
            name = Path(r.file or "").name
            if not name:
                continue
            slot = tally.setdefault(name, [0, 0])
            slot[1] += 1
            if r.ok:
                slot[0] += 1
        return {name: (passed, total) for name, (passed, total) in tally.items()}

    def _systemic_watch(self, node_id: str, result: NodeResult | None,
                        index: int, total: int) -> bool:
        """#8 系统性失败哨兵（叶子环）：连续 N 张卷全判不通过 → 告警 + 停线。

        背景（github-2 实锤）：**29 连败、零告警零早停，烧掉整晚** —— 环境或
        集成系统性坏掉时，叶子环一张接一张地「实现 → 全红 → 修复 → 全红」，
        每一张都在烧真金白银的回合，而没有任何一处说「停」。

        判据钉在**收尾的逐卷台账**（`result.own_final`）上：

          · 一张卷子 total>0 且 0 用例通过 ⇒ 计数 +1（「全判不通过」）；
          · 任何一张有通过用例 ⇒ 清零（它证明应用不是死的 —— 这正是哨兵要区分的）；
          · 整个节点通过 ⇒ 清零；
          · 量不动 / 环境判死 / 没卷子（`own_final is None`）⇒ **既不计数也不清零**
            —— 「不知道」不等于「没事」，也不等于「全红」：前者会把病树的账洗掉，
            后者会拿基建毛病当系统性失败喊停。链不因此断，也不因此涨。

        到线（连击 ≥ `_systemic_limit`，按**卷子数**算）⇒ 告警 + **停叶子线**：
        不再派发剩余叶子。止损到这里为止，不顺手停整跑 —— 剩下的红交给既有的
        收尾/对账（它们按归属修复、有 #3/#7 两道止损兜着），已经 parked 的节点
        照旧走诊断。**不做「剩余叶子批量转诊断」**：诊断是逐节点的判词机制，
        对没开过工的节点发判决没有判据。

        DRYRUN 不参与：不写盘的跑里每张卷子按定义都是 0 通过，那个读数不是
        「测量」，是摆设 —— 拿它停线只会让冒烟不再走完整条线（真跑里这条闸照常）。
        """
        if result is None or self._systemic_limit() <= 0:
            return False
        if os.environ.get("OCTOS_ARC_DRYRUN") == "1":
            return False
        if result.passed:
            self._systemic_note_reset(node_id)
            return False
        tally = result.own_final or {}
        red = sorted(f for f, (passed, total) in tally.items() if total > 0 and passed == 0)
        green = sorted(f for f, (passed, _total) in tally.items() if passed > 0)
        if green:
            self._systemic_note_reset(node_id, green)
            return False
        if not red:
            return False   # 量不动 / 没卷子：既不计数也不清零
        self._systemic_streak += len(red)
        if self._systemic_streak < self._systemic_limit():
            return False
        left = total - index
        log(f"[systemic] **{self._systemic_streak} spec file(s) in a row all-red** "
            f"(latest {node_id}: {', '.join(red)}) — the app is measurable but nothing "
            f"passes; stopping the leaf line with {left} leaf/leaves untouched")
        self._ledger_note(node_id, tried=[
            f"systemic stop: the last {self._systemic_streak} spec file(s) (ending with "
            f"{', '.join(red)}) all measured 0-passing; the remaining {left} leaf/leaves "
            f"are left to the sealing repairs"])
        self._ledger_write()
        return True

    def _systemic_note_reset(self, node_id: str, green: list[str] | None = None) -> None:
        """哨兵计数清零时说一声（只在**曾经数到过**的时候 —— 常态不打字）。"""
        if not self._systemic_streak:
            return
        why = (f" — {', '.join(green)} passes at least one test, the app is alive"
               if green else " — the node passed")
        log(f"[systemic] the all-red streak resets (was {self._systemic_streak} spec "
            f"file(s)); {node_id}{why}")
        self._systemic_streak = 0

    def _implement_prompt(self, node_id: str) -> str:
        """一个节点开工时拿到什么。**这就是不变式 ① 的落地。**"""
        flow = self.flow
        info = self.layers.by_id[node_id]
        # 延迟导入：main 要 import 本模块才能换入口，模块顶层再 import main 就成环了。
        # `NODE_PROMPT` 的模板尾部已经带了 `PORT_RULES`，端口纪律在这里照旧生效。
        from main import NODE_PREAMBLE_CREATE, NODE_PREAMBLE_EXTEND, NODE_PROMPT, describe_node

        preamble = (NODE_PREAMBLE_EXTEND if flow.has_app() else NODE_PREAMBLE_CREATE).format(
            node_id=node_id, req_dir=flow.req_dir, port=flow.web_port)
        design = (node_contract_block(self.contract, self.layers, node_id)
                  + "\n" + forward_deps_block(self.layers, node_id))
        slots = dict(
            preamble=preamble,
            node_spec=describe_node(info.node),
            design=design,
            ancestors=self._ancestors_text(node_id),
            tests=flow.tests_prompt_for(node_id),
            ui=flow.ui_contract(),
            performance=flow.perf_text(),
            verify=flow.verify_text(len(self.layers.order)),
            # `NODE_PROMPT` 尾部拼着 `PORT_RULES`，那两个占位符是从这里来的：
            # 节点自己起的冒烟服务器只能绑 smoke 口，**碰 web 口会被 runner 杀掉**。
            smoke=flow.smoke_port,
            port=flow.web_port,
        )
        return NODE_PROMPT.format(**slots)

    def _ancestors_text(self, node_id: str) -> str:
        """祖先链 —— 我在哪。需求路径，不是代码路径。

        **每一层都带全文描述，不做任何筛选**（2026-10-01 改）。原先只带标题，
        理由是不想把提示词撑大；但**跨节点的硬性契约恰恰写在父章节的描述里**
        ——「工作表网格必须用 ARIA grid 角色、无障碍名字是 "Worksheet grid"、
        格子以坐标命名（A1）、选中的格子 aria-selected="true"」这一整段就在
        根节点的描述里，而叶子拿到的是自己那一页。等于图纸上的硬规矩没下发给
        施工的人。

        代价在正赛里是直接掉分：判分按 DOM 定位器找人，牌子没挂这一条就是 0，
        而挂不挂牌子和这一页自己的实现说明毫无关系。

        不做筛选是刻意的：抽取「哪句重要」是一个主观判断，而它判断错了不会有
        任何测试来发现；搬运原文没有这个问题——任务内容全部来自平台给的
        需求文件，同一份代码在两个任务上跑出两套不同的内容，一个字的题目都
        没有写死在这里。
        """
        chain = self.layers.chain(node_id)[:-1]
        if not chain:
            return ""
        parts = []
        for anc in chain:
            info = self.layers.by_id.get(anc)
            if info is None:
                continue
            title = self._title(anc)
            block = [f"{anc} ({title})"]
            description = str(info.node.get("description") or "").strip()
            # `_title` 在没有 name/title 时会退回描述的第一行；那种情况下
            # 再贴一遍全文就是重复。
            if description and not description.startswith(title):
                block.append(description)
            piece = self.contract.slices.get(anc)
            if piece:
                block.append("contract slice: " + json.dumps(piece, ensure_ascii=False)[:1200])
            parts.append("\n".join(block))
        if not parts:
            return ""
        return ("Where this node sits (the requirement path from the root down to it). "
                "Every layer is quoted in full, and the descriptions above are requirements "
                "in their own right: cross-cutting contracts (accessible names, ARIA roles, "
                "persistence and state) are stated at the level they apply to, so they bind "
                "this node too even though it does not own them. Read them before deciding "
                "what this node must expose. The layers above are the contracts you must not "
                "contradict:\n"
                + "\n\n".join(parts) + "\n")

    # -- 聚合验收 ---------------------------------------------------------

    def _abandon_region(self, node_id: str, reason: str) -> None:
        """一个节点判死 ⟹ **整个得分区域一起放弃**。

        比赛按区域给分：区域里有一个没过，这个区域就没分了。所以区域里还没做的
        兄弟**不再实现** —— 做了也不得分，那是把预算扔进一个已经丢掉的筐。
        区域之间**不传染**：4.2 没了，4.3 照样做（像考试，第一小问写不出可以写下一题）。

        进放弃集的三种，动机各不相同：

          * 区域本身和它的全部后代 —— 不划算了（分已经丢了）
          * 祖先 —— 它们的分要这个区域成立才拿得到，别再花回合去修它们
          * 显式依赖它的节点 —— 不是不划算，是**真做不成**：它要用的东西没了
        """
        info = self.layers.by_id.get(node_id)
        if info is None:
            return
        region = info.parent or node_id
        lost = {region} | set(self.layers.by_id[region].descendants)
        lost |= set(forward_deps(self.layers, node_id))
        fresh = sorted(n for n in lost if n not in self.abandoned)
        if not fresh:
            return
        for other in lost:
            self.abandoned.setdefault(other, f"{node_id} failed: {reason[:140]}")
        log(f"[abandon] {node_id} is out of repair rounds; the region `{region}` is forfeited "
            f"as a whole — {len(fresh)} node(s) will not be built or repaired: {fresh}")

    def _aggregate_from(self, node_id: str) -> None:
        """**从小到大验收的机制本体。**

        一个节点通过之后，往上检查它让什么变得完整了：某个祖先的直接子节点全过了，
        那个祖先就自动变为可验收。不需要时钟，也不需要「所有同层都完成」。
        """
        for anc in aggregate_order(self.layers, node_id):
            if self._stop():
                return
            pending = pending_children(self.layers, anc, self.verdict)
            if pending:
                # 还没到它 —— 而且更上面的祖先更不可能到，直接收工。
                return
            if self.verdict.get(anc) is True:
                continue  # 已经验过了
            self.accept_aggregate(anc)

    def accept_aggregate(self, node_id: str) -> None:
        """子节点全过之后，验收这个父节点。**这时候跑的是全量基线。**"""
        flow = self.flow
        if node_id in self.abandoned:
            log(f"[aggregate] {node_id}: skipped — {self.abandoned[node_id]}")
            return
        specs = list(flow.spec_map.get(node_id) or [])
        log(f"[aggregate] {node_id}: all children pass — verifying at this level")
        result = self._accept(node_id, specs, node_id, aggregate=True)
        result.turns += 1
        self.verdict[node_id] = result.passed
        self.results.append(result)
        if result.passed:
            log(f"[aggregate] {node_id} accepted")
            # 事件口径只有 design / implement / test 三组（见 arcbench_agent_runtime.events），
            # 聚合验收跑的是这个节点自己的 spec，所以它报的就是 test。**旧版这里发的是
            # `verification_*`：那三个方法在 EventClient 上不存在，`flow.mark()` 里一句
            # `getattr(self.events, f"mark_{kind}")` 会当场 AttributeError —— 也就是
            # 第一个节点通过、走到第一次聚合验收时，整个 run 就 abort 了。**
            flow.mark("test_passed", node_id, "specs pass, baseline intact")
            self._ledger_note(node_id, status="green", why="aggregate accepted")
            self._ledger_write()
            self._aggregate_from(node_id)
        else:
            log(f"[aggregate] {node_id} failed: {result.reason}")
            flow.mark("test_failed", node_id, result.reason)
            if result.exhausted:
                if _abandon_region_enabled():
                    self._abandon_region(node_id, result.reason or "repairs exhausted")
                else:
                    self._queue_diagnosis(node_id, node_id,
                                          [Path(s).name for s in specs],
                                          result.reason or "repairs exhausted", "node")
                    log(f"[aggregate] {node_id} parked for diagnosis; the rest carries on")
            self._ledger_note(node_id,
                              status=("parked" if (result.exhausted or result.env_dead) else "red"),
                              why=result.reason,
                              tried=[f"aggregate + {result.turns} turn(s)"])
            self._ledger_write()

    def finalize(self) -> None:
        """收尾：把还没走到根的那条链补完，再跑一次全量基线。"""
        if self._stop():
            return
        if self.layers is None:
            return
        # 深度优先地往上补聚合：从最深的非叶开始，让连锁反应自然发生。
        for depth in range(self.layers.max_depth - 1, -1, -1):
            for node_id in self.layers.depths.get(depth, []):
                if self._stop():
                    return
                if self.verdict.get(node_id) is True:
                    continue
                if pending_children(self.layers, node_id, self.verdict):
                    continue
                self.accept_aggregate(node_id)
        self.final_baseline()

    def final_baseline(self) -> None:
        """最后一道：全量 spec 跑一遍，确认基线没有被落在后面。"""
        flow = self.flow
        if self._stop():
            return
        all_specs = self._all_specs()
        if not all_specs:
            return
        log(f"[layer] final baseline: {len(all_specs)} spec file(s)")
        summary = self._run_specs(all_specs, grader_like=True)
        if summary is None:
            return
        if summary.error:
            log(f"[layer] final baseline could not run: {summary.error}")
            return
        log(f"[layer] final baseline: {summary.passed}/{summary.total} passed")
        self.baseline_specs = list(all_specs)

    # -- P2：收尾分批（全卷域） --------------------------------------------

    def final_acceptance_batched(self, _recheck: bool = True) -> None:
        """收尾全卷体检的分批版 —— `Flow.final_acceptance` 在 `OCTOS_ATOMIC_FINAL=1`
        （默认）时整个交给这里。

        和节点阶段（`_accept_batched`）是**同一套判据**，只换域：那里量「自己的
        卷子 + 小域」，这里量**整份卷子**。收尾这趟存在的理由就是跨节点干扰 ——
        小域量分看不见它，所以这儿的每一刀都用全卷定生死：

            快照 → 一个批回合（≤ `_batch_cap()` 份卷子，提示词只给批内失败）
                 → 全卷量一次 → 有新增绿且零新增红 → commit；否则退回，加宽或 park

        旧路径（`final_acceptance` 自己的 body）一把修全部红节点、量完才发现弄坏了
        别的、**而且无条件提交** —— 那一刀就留在 git 历史里了（正赛里「改一错一片」
        在收尾的原样）。这里每次留下都必须过秤，没过秤的一律退回；全卷的每一次
        测量都记进成绩卡（`record_best`）和封箱判据（`_last_full_measure`），
        所以即便这一趟什么都修不动，交付锚点也不会比开跑前更差（见尾部 L17 回收）。
        """
        flow = self.flow
        all_specs = self._all_specs()
        unverified = ([n for n, v in flow.test_verdict.items() if v is not True]
                      or [n for n in flow.spec_map if n and flow.spec_map[n]
                          and n not in flow.test_verdict])
        if len(all_specs) < 2 and not unverified:
            return
        workers = final_workers(getattr(flow, "mem_limit", None))
        deadline_guard = time.time() + max(300.0, flow.remaining() * 0.5)
        # 本次 pass 之前，逐节点判过「好」的行为 —— 干扰说明的底料。
        passed_alone = {node for node, verdict in flow.test_verdict.items() if verdict is True}
        passed_a_round: set = set()   # 本 pass 里见到过绿的行为（间歇说明的底料）
        owner_of = {Path(path).name: node for node, paths in flow.spec_map.items()
                    for path in (paths or [])}
        attempts: dict[frozenset, int] = {}
        parked = self._final_parked   # 跨 pass 保留：给过两次机会的红不反复吞回合
        ghost_ok: set[str] = set()    # #7 本趟复查过、确认是残渣（幽灵红）的卷子
        best: dict | None = None
        summary = None
        grouped: dict = {}
        fail_keys: set[tuple[str, str]] = set()
        last_passed = -1
        spent = 0
        startup_attempts = 0
        startup_snap: str | None = None
        fresh = False   # 手上的 summary/fail_keys 就是盘上这棵树的最新观察
        # #5f 出厂态验收：收尾每一趟也从出厂态起跑 —— 交付锚点（成绩卡 / 封箱
        # 判据）称的就是这一趟，称的必须是判分机将看到的那份状态。
        self._reset_runtime_state("the full-suite acceptance pass")
        while True:
            # ---- 量一次全卷（OOM 被杀 → 减半重试，与旧路径同一手）--------
            if not fresh:
                summary, workers = self._measure_full_suite(all_specs, workers)
                if summary is None:
                    break   # 量不起来：不碰盘，交付交给成绩卡
                if summary.error and getattr(summary, "killed", False):
                    log(f"[acceptance] full suite could not run ({str(summary.error)[:120]}); "
                        f"keeping per-node verdicts")
                    break
                if summary.error:
                    # 起不来 —— 每个节点都判红，拿「启动本身」当一批来修。
                    log(f"[acceptance] full suite (grader-like start) failed: "
                        f"{str(summary.error)[:300]}")
                    for node_id in flow.spec_map:
                        if node_id:
                            flow.test_verdict[node_id] = False
                    booked = RunSummary(passed=0, total=len(all_specs))
                    grouped = {None: []}
                    self._book_final_measure(booked, grouped)
                    last_passed = 0
                    if best is None:
                        best = {"passed": 0, "sha": flow.head(), "summary": booked, "grouped": grouped}
                    if startup_snap is not None:
                        # 上一刀没把启动修回来 —— 退回去，别在坏树上继续叠。
                        flow.restore_app(startup_snap)
                        startup_snap = None
                    if (startup_attempts >= self._batch_attempts()
                            or not self._can_repair(deadline_guard, need=self._batch_needs())):
                        log("[acceptance] startup repair did not take; giving up on this pass")
                        break
                    snap = self._snapshot(f"final startup {startup_attempts + 1}")
                    self._turn(self._startup_repair_prompt(summary, retry=startup_attempts > 0),
                               f"full-suite startup repair {startup_attempts + 1}",
                               timeout=self._batch_timeout())
                    spent += 1
                    startup_attempts += 1
                    startup_snap = snap
                    continue
                grouped = nodes_for_failures(summary.results, flow.spec_map)
                passed_a_round |= {owner_of.get(Path(r.file or "").name)
                                   for r in summary.results if r.ok} - {None}
                log(f"[acceptance] full suite: {summary.passed}/{summary.total}; failing nodes "
                    f"{sorted(k for k in grouped if k) or ('all' if None in grouped else [])}")
                card = self._book_final_measure(summary, grouped)
                fail_keys = self._failure_keys(summary)
                last_passed = summary.passed
                if best is None or summary.passed > best["passed"]:
                    best = {"passed": summary.passed, "sha": card or flow.head(),
                            "summary": summary, "grouped": grouped}
                # #5b 卷子坏 → 自动改道申诉：全卷里若有**机器验过的死卷子**，当场作废
                # 并重新量一遍 —— 作废改变的是「全卷」本身，不重量的话这一轮的
                # grouped/成绩卡/切批都还带着一份对任何实现都不可能的卷子（拿修复
                # 回合去够它 = 白烧；它还会把 best 的分压低）。罕见路径：节点入口
                # 已经作废过一次，这里兜的是「属主节点没走到」的那些。
                if self._auto_void_dead_specs(
                        sorted(self._mechanically_dead_files({k[0] for k in fail_keys if k[0]}))):
                    continue
            fresh = False
            # ---- 全绿收工 ---------------------------------------------------
            if not grouped:
                flow.commit(f"chore: full acceptance suite {summary.passed}/{summary.total} "
                            f"pass (full suite)")
                return
            # ---- 切批 -------------------------------------------------------
            # #7 停损：这一趟收尾从出厂态起跑、但不重置地复量了好几轮 —— 一份
            # 「用完不还原」的卷子在第二趟起就会带着残渣红，而它不是实现的红。
            # 点过名的卷子先按出厂态复查：确认是残渣就从「要修的红」里摘出去
            # （拿修复回合去够它 = 白烧，正赛收尾烧掉的那几小时里有它一份），
            # 复查后仍旧红的照旧留在红集里给修复 —— 不放过真红。
            ghosts = self._ghost_reds({k[0] for k in fail_keys if k[0]} - parked,
                                      grader_like=True, confirmed=ghost_ok)
            if ghosts:
                ghost_ok |= ghosts
                fail_keys = {k for k in fail_keys if k[0] not in ghosts}
            chargeable = sorted({k[0] for k in fail_keys if k[0]} - parked)
            if not chargeable:
                log(f"[acceptance] no batch left to try: {len(fail_keys)} failing test(s), "
                    f"{len(parked)} spec file(s) parked"
                    + (f", {len(ghost_ok)} spec file(s) confirmed as residue (not charged)"
                       if ghost_ok else ""))
                break
            kept_any = False
            for batch in self._final_batches(chargeable):
                if set(batch) & parked:
                    continue   # 加宽可能把别的批里的文件带进 park，跳过
                if self._stop() or not self._can_repair(deadline_guard, need=self._batch_needs()):
                    break      # 预算/墙钟到点：收摊，剩下的交给 L17 尾巴
                original = frozenset(batch)   # 同一批的「血脉」：加宽重试算同一次
                current = list(batch)
                while True:
                    if attempts.get(original, 0) >= self._batch_attempts():
                        parked.update(current)
                        log(f"[acceptance] parking {', '.join(sorted(current))}: "
                            f"{attempts.get(original, 0)} attempt(s), no net gain")
                        self._queue_diagnosis(f"files:{','.join(sorted(current))}",
                                              owner_of.get(current[0]), list(current),
                                              f"{attempts.get(original, 0)} attempt(s), no net "
                                              f"gain", "final")
                        # 附·超时记账：这批的下场进账本 —— 试过几刀、没净胜、作废
                        self._ledger_note(owner_of.get(current[0]), tried=[
                            f"full-suite batch {', '.join(sorted(current))}: parked after "
                            f"{attempts.get(original, 0)} attempt(s), no net gain — nothing kept"])
                        self._ledger_write()
                        break
                    snap = self._snapshot(f"final batch {spent + 1}")
                    before_keys = set(fail_keys)
                    before_summary, before_grouped = summary, grouped
                    attempt_no = attempts.get(original, 0) + 1
                    self._turn(self._final_batch_prompt(current, summary, attempt_no,
                                                        passed_alone, passed_a_round, workers),
                               f"full-suite batch repair {spent + 1}", timeout=self._batch_timeout())
                    spent += 1
                    attempts[original] = attempt_no
                    if self._last_turn_timed_out:
                        # v5 超时止损：收尾也一样 —— 被墙钟烧完的回合不算「试过了」，
                        # 退回这一刀，这批进诊断队列，剩下的红继续按批走。
                        if snap:
                            flow.restore_app(snap)
                            summary, grouped, fail_keys = before_summary, before_grouped, before_keys
                        parked.update(current)
                        self._queue_diagnosis(f"files:{','.join(sorted(current))}",
                                              owner_of.get(current[0]), list(current),
                                              "full-suite repair turn timed out", "final")
                        # 附·超时记账：被掐断的批次一律进账本，明确记「作废」
                        self._ledger_note(owner_of.get(current[0]), tried=[
                            f"full-suite batch {', '.join(sorted(current))}: repair turn timed "
                            f"out (wall-clock), rolled back — nothing kept"])
                        self._ledger_write()
                        log(f"[acceptance] batch timed out ({', '.join(sorted(current))}); "
                            f"parked for diagnosis")
                        break
                    if self._last_turn_failed:
                        # #5e 异常终止轮也回退：收尾同样 —— 判失败的回合落盘的是半成品，
                        # 与超时轮同一条处置（退回检查点 + 记「作废」）。区别同一处：
                        # 没有检查点（退回不了）就停这批，别在脏树上叠刀；有检查点
                        # 且尝试余额还在就再来一次（循环头按余额收口）。
                        if snap:
                            flow.restore_app(snap)
                            summary, grouped, fail_keys = before_summary, before_grouped, before_keys
                        tried_note = (f"full-suite batch {', '.join(sorted(current))}: the turn "
                                      f"ended in failure"
                                      + (", rolled back" if snap else
                                         " (no checkpoint to roll back to)")
                                      + " — nothing kept")
                        self._ledger_note(owner_of.get(current[0]), tried=[tried_note])
                        self._ledger_write()
                        if snap and attempt_no < self._batch_attempts():
                            log(f"[acceptance] batch failed ({', '.join(sorted(current))}); "
                                f"rolled back, retrying")
                            continue
                        parked.update(current)
                        self._queue_diagnosis(f"files:{','.join(sorted(current))}",
                                              owner_of.get(current[0]), list(current),
                                              "full-suite repair turn failed", "final")
                        log(f"[acceptance] batch failed ({', '.join(sorted(current))}); "
                            + ("rolled back, parked" if snap else "no checkpoint, parked"))
                        break
                    summary2, workers = self._measure_full_suite(all_specs, workers)
                    if summary2 is None or summary2.error:
                        # 量不动：这一刀看不见，退回它并 park 这批 —— 量不动
                        # 说明树可疑，继续在它上面叠刀不是修复。
                        log(f"[acceptance] the suite could not run after this batch "
                            f"({str(getattr(summary2, 'error', '') or 'no results')[:120]}); "
                            f"rolling it back")
                        if snap:
                            flow.restore_app(snap)
                            summary, grouped, fail_keys = before_summary, before_grouped, before_keys
                        parked.update(current)
                        self._queue_diagnosis(f"files:{','.join(sorted(current))}",
                                              owner_of.get(current[0]), list(current),
                                              "the suite could not run after this batch", "final")
                        # 附·超时记账：量不动 = 没被验证 —— 进账本，明确记「作废」
                        self._ledger_note(owner_of.get(current[0]), tried=[
                            f"full-suite batch {', '.join(sorted(current))}: unverifiable (the "
                            f"suite could not run), rolled back — nothing kept"])
                        self._ledger_write()
                        break
                    grouped2 = nodes_for_failures(summary2.results, flow.spec_map)
                    passed_a_round |= {owner_of.get(Path(r.file or "").name)
                                       for r in summary2.results if r.ok} - {None}
                    log(f"[acceptance] full suite: {summary2.passed}/{summary2.total} after "
                        f"batch {', '.join(sorted(current))}")
                    card = self._book_final_measure(summary2, grouped2)
                    last_passed = summary2.passed
                    if best is None or summary2.passed > best["passed"]:
                        best = {"passed": summary2.passed, "sha": card or flow.head(),
                                "summary": summary2, "grouped": grouped2}
                    after_keys = self._failure_keys(summary2)
                    # #7 停损：这一趟复量不带重置 —— 「用完不还原」的卷子会在这一趟
                    # 才红，而它上一趟（出厂态）是绿的。不摘出去的话，它会被算成
                    # 「刚修的这批弄红了它」→ 净胜判不掉 → **一刀好修复被冤枉退回**。
                    # 同样先按出厂态复查，确认是残渣才摘；真红照旧进 `broke`。
                    ghosts = self._ghost_reds({k[0] for k in after_keys} - parked,
                                              grader_like=True, confirmed=ghost_ok)
                    if ghosts:
                        ghost_ok |= ghosts
                        after_keys = {k for k in after_keys if k[0] not in ghosts}
                    fixed = before_keys - after_keys
                    broke = after_keys - before_keys
                    if not broke and fixed:
                        flow.commit(f"fix: final batch repair {', '.join(sorted(current))} "
                                    f"+{len(fixed)}")
                        log(f"[acceptance] final batch kept ({', '.join(sorted(current))}): "
                            f"+{len(fixed)} test(s) green, nothing newly broken")
                        self._ledger_note(owner_of.get(current[0]), tried=[
                            f"full-suite batch {', '.join(sorted(current))}: +{len(fixed)} "
                            f"test(s) green (kept)"])
                        self._ledger_write()
                        summary, grouped, fail_keys = summary2, grouped2, after_keys
                        kept_any = True
                        break
                    log(f"[acceptance] final batch reverted ({', '.join(sorted(current))}): "
                        f"+{len(fixed)} green, {len(broke)} newly broken")
                    # 附·超时记账：这一批的观察进账本（退回 = 一刀作废）
                    self._ledger_note(owner_of.get(current[0]), tried=[
                        f"full-suite batch {', '.join(sorted(current))}: reverted "
                        f"(+{len(fixed)} green, {len(broke)} newly broken) — nothing kept"])
                    self._ledger_write()
                    if snap:
                        flow.restore_app(snap)
                        summary, grouped, fail_keys = before_summary, before_grouped, before_keys
                    else:
                        # 退不了：盘上留着这一刀，它量出来的就是最新观察。
                        summary, grouped, fail_keys = summary2, grouped2, after_keys
                    if broke and attempts[original] < self._batch_attempts():
                        widened = sorted({f for f in current} | {k[0] for k in broke if k[0]})
                        log(f"[acceptance] widening this batch with the newly broken file(s): "
                            f"{widened}")
                        current = widened
                        continue
                    parked.update(current)
                    self._queue_diagnosis(f"files:{','.join(sorted(current))}",
                                          owner_of.get(current[0]), list(current),
                                          f"batch reverted: +{len(fixed)} green, "
                                          f"{len(broke)} newly broken", "final")
                    break
                if kept_any:
                    break
            if kept_any:
                fresh = True
                continue   # 失败集变了 —— 重新量/切批
            break
        # ---- L17 尾巴：交付的是**量到过的最好一轮**，不是最后一轮 -------------
        # 每一刀都有快照兜底，这里管的是快照失败 / 被掐停留下的盘面。
        if best is not None and best.get("sha") and last_passed < best["passed"]:
            log(f"[acceptance] full suite: last round {last_passed} < best {best['passed']}; "
                f"restoring the best state")
            flow.restore_app(best["sha"])
            self._book_final_measure(best["summary"], best["grouped"])
            flow.commit(f"chore: keep best full-suite state {best['passed']}/"
                        f"{best['summary'].total}")

        # ---- v5 诊断：修不动的红交给诊断回合，再给全卷一趟复量的机会 -------------
        # 诊断按**需求原文 vs 卷子断言 vs 实现**三方对比出一种处置：卷子错（作废）、
        # 实现难（就地回炉）、理解跑题（记档、重排队一次）。它改过树，所以到这里
        # 必须重跑一趟全卷 —— 净胜判据和 L17 回收都在那一趟里照常生效，
        # 诊断不是绕过秤的后门，只是给秤多送一批可判的活。
        if self.diagnose_parked("final") and _recheck and not self._stop():
            log("[acceptance] diagnosis changed the tree; re-running the full acceptance pass")
            self.final_acceptance_batched(_recheck=False)
        self._ledger_write()

    def _measure_full_suite(self, specs: list[str], workers: int) -> tuple:
        """全卷量一次（OOM 被杀就减半重试），返回 (summary, 实际用的 workers)。

        与旧收尾同一手：跑一次要多少内存事先不知道，被杀一次就把并发减半再试，
        而不是放弃修复。summary 为 None = 连跑都跑不起来（异常）。"""
        flow = self.flow
        while True:
            try:
                summary = self._run_specs(specs, grader_like=True, workers=workers)
            except Exception as exc:  # noqa: BLE001
                log(f"[acceptance] full suite raised: {exc}")
                return None, workers
            if not (getattr(summary, "error", None) and getattr(summary, "killed", False)
                    and workers > 1):
                return summary, workers
            workers = max(1, workers // 2)
            log(f"[acceptance] full suite runner was killed; retrying with {workers} worker(s)")

    def _book_final_measure(self, summary, grouped: dict, note: str = "final acceptance") -> str | None:
        """一次全卷观察的记账 —— 收尾和对账都走这里，口径只有一份：

          · 成绩卡（`record_full_suite`）与最好成绩卡（`record_best`，严格更好才登记）；
          · 封箱判据「盘上这棵树已经量过了」（`_tree_maybe_edited=False` +
            `_last_full_measure`）—— 封箱时不必再把最后的编辑当成没量过。
        返回最好成绩卡的 sha；这次测量没进卡时返回 None（调用方退回 `head()`）。
        """
        flow = self.flow
        try:
            flow.record_full_suite(summary, grouped)
        except Exception as exc:  # noqa: BLE001
            log(f"[acceptance] record_full_suite failed: {exc}")
        flow._tree_maybe_edited = False
        flow._last_full_measure = {"passed": summary.passed, "total": summary.total}
        card = None
        try:
            card = flow.record_best(summary, note)
        except Exception as exc:  # noqa: BLE001
            log(f"[acceptance] record_best failed: {exc}")
        if card:
            flow._best_summary, flow._best_grouped = summary, grouped
        return card

    def _arm_best_from_sweep(self, summary, grouped: dict, full_coverage: bool, node_id: str) -> None:
        """量全了却没量到全绿的对账：记账不动，但把**成绩卡**往前推一格。

        守卫（见 `_run_reconcile`）只让「全绿的全卷测量」记账，这条一个字不改：
        全卷成绩卡（`record_full_suite`）和封箱判据（`_tree_maybe_edited` /
        `_last_full_measure`）在这里都不碰 —— 半截的像拿去记账，封箱会把没量过
        的树当成量过了。

        变的是最好成绩卡：只要这一趟真的把每一份卷子都量到了（`full_coverage`
        且没有 load_errors），`summary.passed` 就是**这棵树的下界** —— 判红即停
        会把红卷子里第一条红之后的用例截掉，所以它只会低、不会高；低报的卡拿
        去做封箱比较，不会把差树当好树。换来的是保底机制的全部价值
        （`refs/arc/best` + `.arc/best_state.json`，见 `Flow.record_best`）从收尾
        时刻提前到**第一次全卷对账**：进程在收尾之前结束（超支、被叫停、被杀）
        时，盘上已经有卡可依。

        `OCTOS_BEST_MIDRUN=0` 精确回到旧行为（跑动中途不碰卡）。一条绿都没量到
        （`passed=0`）也不武装 —— 空卡不提供任何可退回的版本。
        """
        untouched = ("not a complete measurement (swept files / fail-fast cut / load errors): "
                     "the score card and the seal criterion stay untouched")
        passed = int(getattr(summary, "passed", 0) or 0)
        if (os.environ.get("OCTOS_BEST_MIDRUN", "1") == "0" or not full_coverage
                or getattr(summary, "load_errors", None) or passed <= 0):
            log(f"[reconcile] {untouched}")
            return
        flow = self.flow
        arm = getattr(flow, "record_best", None)
        if not callable(arm):
            log(f"[reconcile] {untouched}")
            return
        try:
            card = arm(summary, f"reconcile after {node_id} (mid-run lower bound)")
        except Exception as exc:  # noqa: BLE001
            log(f"[reconcile] arming the score card mid-run failed: {exc}")
            return
        if card:
            # 封箱如果退回这张卡，卡上那棵树的判词要能补记回来（`seal_delivery`
            # 的退回分支就认这一对）—— 与 `_book_final_measure` 的尾巴同文。
            flow._best_summary, flow._best_grouped = summary, grouped
            log(f"[reconcile] score card armed mid-run: {passed}/{summary.total} at the tree "
                "measured just now (a lower bound — fail-fast cuts red files inside); "
                "the seal criterion stays untouched")
        else:
            log(f"[reconcile] the mid-run measurement {passed}/{summary.total} did not beat "
                "the score card; the seal criterion stays untouched")

    def _snapshot(self, note: str) -> str | None:
        """给这一刀拍快照。失败（None）就不退 —— 「能退就退」，
        辅助动作绝不变成新的故障点（见 `Flow.snapshot_state`）。"""
        snap_fn = getattr(self.flow, "snapshot_state", None)
        if not callable(snap_fn):
            return None
        try:
            return snap_fn(note)
        except Exception as exc:  # noqa: BLE001
            log(f"[acceptance] snapshot failed: {exc}")
            return None

    def _final_batches(self, files: list[str]) -> list[list[str]]:
        """收尾切批：按属主分组（同属主优先），每组 ≤ `_batch_cap()` 份。

        和 `_next_batches` 同一套分法，差别只有一处：收尾没有「本节点」——
        所有卷子一视同仁，属主只用来把同一个人名下的卷子凑一批（共享同一套
        API/组件，一起修省回合，改动面也天然收在一个域里）。"""
        owner_of: dict[str, str | None] = {}
        for owner, paths in self.flow.spec_map.items():
            for path in (paths or []):
                owner_of[Path(path).name] = owner
        groups: dict[str | None, list[str]] = {}
        for name in files:
            groups.setdefault(owner_of.get(name), []).append(name)
        cap = self._batch_cap()
        batches: list[list[str]] = []
        for key in sorted(groups, key=lambda k: (k is None, str(k))):
            chunk = sorted(groups[key])
            batches.extend(chunk[i:i + cap] for i in range(0, len(chunk), cap))
        return batches

    def _startup_repair_prompt(self, summary, retry: bool,
                               where: str = "the full acceptance suite") -> str:
        """启动失败专用的一批：范围是「让 app 按判分方式起来」，别的不碰。

        `where` 是「哪一把量尺量到它起不来」—— 收尾是全卷，节点就地抢修
        （`_heal_app_startup`）是这个节点的卷子。提示词必须说真话：范围说错了，
        模型会去修一个没人量过的全卷。
        """
        head = (f"Repair scope — {where} fails at startup: the app does not come "
                "up exactly as the grader starts it (only PORT set). Fix the startup and nothing "
                "else — do not touch the specs and do not add features.\n"
                "- Feature: application startup exactly as the grader runs it (only PORT set)\n"
                "  Failed at: npm start\n"
                f"  Observation: {startup_error_digest(str(summary.error or ''), 700)}\n"
                "  Steps: npm run build -> npm start\n")
        if retry:
            head += ("The previous startup repair did not make the app start. Change the cause the "
                     "error above points at rather than repeating the last edit.\n")
        return self._repair_prompt("all nodes", head, aggregate=True)

    def _app_broken(self, summary) -> bool:
        """「环境病」判据：构建或启动失败（不是卷子红）。

        判据落在 `RunSummary.startup_error` 上（`Flow.run_specs` /
        `run_specs_isolated` 在 `server.build()/start()` 失败时置位）—— 一个
        error 非空只能说「量不动」，而「应用起不来」和「跑卷子时基建出错」
        的处置完全不同（#5a 就地抢修 / #5b 三分类），不能只看 error。
        """
        return bool(getattr(summary, "error", None)) and bool(
            getattr(summary, "startup_error", False))

    def _heal_app_startup(self, result, label: str, summary, specs: list[str],
                          deadline_guard: float, where: str | None = None):
        """#5a 就地抢修：app 构建/启动坏了 —— 先治环境，再谈节点。

        「坏构建」不是「这个节点红」：它是环境病，一个回合能治；不治的话每个
        节点都对着同一套坏环境白量一遍（实测形状：一次改坏六连坐 —— 6 个实现
        轮 + 6 个验收轮 ≈ 40 分钟，收尾还要再花两个 pass 修构建）。这就是
        「构建一坏立即停线」的机制化。

        手法与收尾的启动抢修**同一套**（`_startup_repair_prompt`；每次尝试前
        退回上一次尝试的入口，不在坏树上叠刀；治不好把没治好的那几刀退回去），
        差别只有量尺：这里用**本节点的卷子**量 —— 治的是启动，量尺越小越省。
        `where` 是提示词里那句「哪把量尺量到它起不来」的如实说法：默认是
        「this node's specs (id)」，#9 接盘预检传自己那一站的名字（说错量尺，
        模型会去修一个没人量过的东西 —— 见 `_startup_repair_prompt`）。

        返回治好之后能跑的那次观察；治不好返回 None，并记上一笔（调用方按原样
        放弃这一节点，且本跑不再重试：`_startup_given_up`）。
        """
        flow = self.flow
        node_id = result.node_id
        attempts = 0
        snap: str | None = None
        while summary is not None and self._app_broken(summary):
            if attempts >= self._batch_attempts() or not self._can_repair(
                    deadline_guard, need=self._batch_needs()):
                if snap:
                    # 治不好：把没治好的那几刀退回去（与收尾同一手）。
                    flow.restore_app(snap)
                self._ledger_note(node_id, tried=[
                    f"startup repair gave up after {attempts} attempt(s) — nothing kept "
                    f"({startup_error_digest(str(summary.error or ''), 60)})"])
                self._ledger_write()
                return None
            if snap:
                # 上一刀没治好 —— 退回同一个入口再试，别在坏树上叠刀。
                flow.restore_app(snap)
            else:
                snap = self._snapshot(f"{label} startup repair")
            broken = startup_error_digest(str(summary.error or ""), 80)
            self._turn(self._startup_repair_prompt(summary, retry=attempts > 0,
                                                   where=where or f"this node's specs ({node_id})"),
                       f"{label} startup repair {attempts + 1}", timeout=self._batch_timeout())
            attempts += 1
            result.turns += 1
            self._ledger_note(node_id, tried=[
                f"app would not build/start; startup repair {attempts} ({broken})"])
            self._ledger_write()
            log(f"[{label}] app does not build/start; startup repair {attempts} for {node_id}")
            summary = self._run_specs(specs, grader_like=False)
            if summary is not None and self._app_broken(summary):
                # #5d 与 #3 熔断联动：抢修烧掉的每一刀都记进**同一本**连续零进展
                # 账（与超时、净收成为零合账，见 `_accept_batched`）—— 熔断入口闸
                # 读的就是这本账，环境病因此不会变成一本谁也不看的私账。
                self._stall_streak[node_id] = self._stall_streak.get(node_id, 0) + 1
        if attempts and summary is not None and not self._app_broken(summary):
            # 治好了 = 有进展：清零（与批修「净胜即清零」同一口径）。只在**真治好了**
            # 时清 —— 量不动（summary 为 None）不算进展，那本账照旧留着。
            log(f"[{label}] app builds/starts again after {attempts} startup repair(s); the "
                f"no-progress streak for {node_id} is cleared")
            self._stall_streak.pop(node_id, None)
        return summary

    def _final_batch_prompt(self, files: list[str], summary, attempt_no: int,
                            passed_alone: set, passed_a_round: set, workers: int) -> str:
        """收尾一批的提示词 = 批修提示词 + 全卷特有的三张底牌 + 换手法纠正。

        底牌和旧收尾逐字同源（`interference_note`/`intermittent_note`/
        `worker_parity_note`），只按批内文件过滤 —— 「只在公司里失败」「已经
        绿过一次」这类话，对不在这批里的卷子说是噪音。"""
        prompt = self._batch_repair_prompt("all nodes", files, summary, None, aggregate=True)
        # v5 台账：收尾这一批是「谁的卷子」，就把那几位的要求点台账贴上来 ——
        # 「试过哪几招」是模型最缺、又最贵（重试一次才知道）的那部分记忆。
        seen: list[str] = []
        owner_of = {Path(p).name: node for node, paths in self.flow.spec_map.items()
                    for p in (paths or [])}
        for name in files:
            owner = owner_of.get(name)
            if owner and owner not in seen:
                seen.append(owner)
                prompt += self._ledger_block(owner)
        grouped = nodes_for_failures([r for r in summary.results
                                      if not r.ok and Path(r.file or "").name in set(files)],
                                     self.flow.spec_map)
        notes = self.flow.interference_note(grouped, passed_alone,
                                            getattr(summary, "stores_written", None))
        notes += self.flow.intermittent_note(grouped, passed_a_round)
        notes += self.flow.worker_parity_note(workers)
        if notes:
            prompt += notes
        if attempt_no > 1:
            prompt += ("\n\nThe previous attempt on these same spec files did not turn any of them "
                       "green. Recheck the assumptions behind the repair: inspect expected and "
                       "received values, the preceding actions and the actual application state. "
                       "Change the cause supported by this evidence; do not manufacture the "
                       "expected output or bypass the underlying operation.\n")
        return prompt

    # -- 一次验收 ---------------------------------------------------------

    def _accept(self, node_id: str, specs: list[str], label: str, aggregate: bool,
                since_sha: str | None = None) -> NodeResult:
        """验收一个节点。分批路径是默认（`_accept_batched`），旧循环是回退路径。

        `OCTOS_ATOMIC_REPAIR=0` 回到旧行为 —— 两套路径的差别只在**修复的粒度**：
        旧循环量的是「这一刀把整个节点弄成什么样」，新循环量的是「这一批 1~3 份
        卷子」。验收判据（own 过 + 基线没新增红）两边逐字相同。

        `since_sha` 是这个节点开工前的 HEAD（`implement_leaf` 里的 `pre`）。
        分批路径用它判断「碰没碰共享文件」—— 批修会在节点中途提交，porcelain
        在那之后是干净的，只有对比开工点才看得出这个节点动过什么。
        """
        if _atomic_repair():
            return self._accept_batched(node_id, specs, label, aggregate, since_sha=since_sha)
        return self._accept_legacy(node_id, specs, label, aggregate)

    def _accept_legacy(self, node_id: str, specs: list[str], label: str, aggregate: bool) -> NodeResult:
        """跑 specs → 不过就修 → 通过前还要过基线。**不变式 ③ 在这里变成结构。**

        关键在于：**基线没绿就不提交。** HEAD 永远等于「基线全绿」的状态，
        所以「通过的只能增加」不是一条纪律，是 git 的事实。

        修复是「往前修」而不是「先回滚再修」：工作区里还有这个节点的全部改动，
        把「你弄红了谁的 spec」直接喂给它，比丢掉重来便宜得多。回滚留给修不动的时候。

        （旧的整段修复循环。保留是回退开关的落点，也是既有测试钉住的语义。）
        """
        flow = self.flow
        result = NodeResult(node_id=node_id)
        rounds = max(0, int(getattr(flow, "repair_rounds", 3)))
        deadline_guard = time.time() + max(300.0, flow.remaining() * 0.5)

        # **「早就红的」是相对这个节点开工前而言的，不是相对上一轮而言的。**
        #
        # 红名单在 `_baseline_break` 里是边判边更新的，如果判断直接读那份最新的名单，
        # 节点的修复循环就会自己把自己洗白：第 1 轮它弄红了 `a.spec.ts`（记账进去了），
        # 第 2 轮再跑同一份 `a.spec.ts` 时它已经是「名单上的」，于是这一轮判定为
        # 「不是它弄的」→ 返回 None → **节点通过**。修不动就等一轮，等于没有基线。
        #
        # 所以这里取一次开工时的快照，整个节点的修复循环都用它做判断；记账照旧实时写
        # （后面的节点仍然受益），但**它自己的这几轮，责任是钉死的**。
        red_before = set(self.baseline_red)
        summary = None   # 最近一次自己的测量（#8 哨兵的收尾台账从这里来）

        # 没有 spec 挂到这个节点上 ≠ 它失败了。它只是没有自己要证明的东西 ——
        # 但基线照样不能被它弄红，所以直接进基线检查那一步。
        own_ok = not specs

        for round_index in range(rounds + 1):
            if not own_ok:
                summary = self._run_specs(specs, grader_like=False)
                if summary is None:
                    result.reason = "specs could not run"
                    return result
                if summary.error:
                    result.reason = f"spec run failed: {summary.error[:300]}"
                    return result
                own_ok = summary.all_passed
            if not own_ok:
                # **修不动的时候，先问问「是不是卷子写错了」。**
                # 上面每一轮都是「改代码去满足卷子」，而自产卷子也是模型写的，
                # 它可能断言了一个需求文档里根本没有的东西。那时候代码是对的、
                # 卷子是错的，而这个循环只会一直修代码，修到 `exhausted` ——
                # 再往后 `_abandon_region` 把整个区域连坐判死，惩罚落在一堆
                # 没做错的节点上。出口只在「自产卷子 + 修过两轮 + 有据 + 没超预算」
                # 四条都满足时打开。
                #
                # ★ 作废**不消耗轮次**：`own_ok` 就地变真、往下走同一个回合的基线
                # 检查。写成 `continue` 看起来一样，实际差一轮 —— 而 `round_index`
                # 的允许上界（`_SPEC_DEFECT_FROM_ROUND`）恰好落在 `rounds` 上时，
                # 那个 `continue` 直接跳出循环，节点照样判死，作废等于白作。
                if self._maybe_void_spec(node_id, round_index) is not None:
                    specs = list(self.flow.spec_map.get(node_id) or [])
                    own_ok = True
                    result.reason = "its own spec was voided as defective"
            if own_ok:
                # 自己的 spec 过了才检查基线 —— 不过本来就要修，基线结果不影响决定。
                # 这一条就是风险表里的 #3：**基线只在 own specs 全过时才跑**。
                broke = self._baseline_break(node_id, aggregate, red_before=red_before)
                if broke is None:
                    # 通过 ⇒ 基线是好的。某一轮挨过骂、后来又真修好了，`baseline_ok`
                    # 还留着上一轮的 False —— 今天没人读它（只在 `passed` 为假时才读），
                    # 但那是「靠调用点记得先看 passed」维系的，不是结构。这里钉死。
                    self._second_pass_check(node_id, specs, aggregate)   # #7
                    result.own_passed = result.passed = result.baseline_ok = True
                    self._commit(node_id)
                    self._extend_baseline(specs)
                    return result
                result.baseline_ok = False
                result.reason = broke
                log(f"[{label}] specs pass but the baseline broke: {broke[:200]}")
                if round_index >= rounds:
                    result.exhausted = True  # 轮数用完了还是绿的不了 —— 判死
                    break
                if not self._can_repair(deadline_guard):
                    break
                self._turn(self._repair_prompt(node_id, broke, aggregate),
                           f"{label} repair {round_index + 1}/{rounds}", timeout=self._repair_timeout())
                result.turns += 1
                continue

            # 自己的 spec 没过。
            result.reason = (f"{summary.passed}/{summary.total} spec(s) passed"
                             if summary.total else "no specs mapped to this node")
            if round_index >= rounds:
                result.exhausted = True  # 轮数用完了还是没过 —— 判死
                break
            if not self._can_repair(deadline_guard):
                result.reason += " (no budget left for another repair)"
                break
            failures = failure_summaries(summary) or "(no failure detail available)"
            self._turn(self._repair_prompt(node_id, failures, aggregate),
                       f"{label} repair {round_index + 1}/{rounds}", timeout=self._repair_timeout())
            result.turns += 1
        # #8 哨兵的观察值：最后那次自己的测量（上面每个「没量成」的出口都
        # 提前 return 了，走到这里的 `summary` 是可信的；没卷子时为 None）。
        if summary is not None and not summary.error:
            result.own_final = self._spec_tally(summary)
        return result

    # -- 分批修复 ---------------------------------------------------------

    def _accept_batched(self, node_id: str, specs: list[str], label: str, aggregate: bool,
                        since_sha: str | None = None, rework: bool = False) -> NodeResult:
        """分批修复：**一个提交 = 一个验证点**。

        旧循环（`_accept_legacy`）的毛病不在判据，在粒度：它让模型一次改「整个节点
        的失败」，改完才量，量完才发现弄坏了别的 —— 而那一刀已经提交进 git 历史，
        下一步是在坏掉的状态上继续修。正赛里「改一错一片、三轮回合耗尽、区域连坐」
        的那条杀伤链就是这么来的。

        这里把修复切成 **1~3 份卷子一批**：

            快照 → 一个批回合 → own 量 + 小域量 → 测试级净胜判定
            有新增绿 且 零新增红 → commit（留下）；否则 restore 快照（退回）

        同属主优先的批（同一个人名下的卷子多半共享同一套 API/组件，一起修省回合，
        改动面也天然收在一个域里）。同一批两次（第二次按弄红的文件加宽）都没有
        净胜 → 这批文件 park，换下一批 —— 修不动的红不再反复吞回合。

        验收判据与旧路径**逐字相同**：own specs 全过 + 小域没有「本次弄红的卷子」。
        每次 keep 之后都当场问一次能不能验收，能就直接收；不能就继续切下一批。
        """
        flow = self.flow
        result = NodeResult(node_id=node_id)
        # #3 熔断入口闸：停修过的节点不再回队 —— 除非这趟是诊断之后的「回炉」
        # （那是带着新判词的另一种尝试，见 `_diagnose_item`；进来之后照旧计数）。
        streak = self._stall_streak.get(node_id, 0)
        if streak >= self._stall_limit() and not rework:
            result.reason = (f"repair skipped: {streak} repair round(s) in a row made no net "
                             f"progress")
            result.exhausted = True
            self._ledger_note(node_id, status="parked", why=result.reason)
            self._ledger_write()
            log(f"[{label}] {node_id}: skipping repair — {streak} no-progress round(s) on record")
            return result
        if rework:
            # 回炉 = 带着诊断判词的另一种尝试：给它一个干净的计数窗口
            # （回炉本身每个要求点只给一次，烧不穿）。
            self._stall_streak.pop(node_id, None)
        rounds = max(0, int(getattr(flow, "repair_rounds", 3)))
        # 节点级的回合上限：批修把「一轮」拆小了，但一个节点能烧的回合总数
        # 不该因此变多（`repair_rounds` 仍是外沿，这个上限只收不放）。
        budget = min(rounds, int(os.environ.get("OCTOS_REPAIR_NODE_ATTEMPTS", "6")))
        deadline_guard = time.time() + max(300.0, flow.remaining() * 0.5)
        red_before = set(self.baseline_red)
        own_specs = list(specs)
        own_names = {Path(s).name for s in own_specs}
        own_fail: set[tuple[str, str]] = set()
        scope_fail: set[tuple[str, str]] = set()
        scope_state: tuple | None = None   # (summary, 红文件集, 跑前红名单) 最近一次小域观察
        own_summary = None
        parked: set[str] = set()
        attempts: dict[frozenset, int] = {}
        spent = 0
        budget_stopped = False
        all_parked = False
        stalled = 0   # 熔断触发时的连击数（收尾判词要把它写进 reason，见本函数末尾）

        # -- 自己的一轮：错误语义与旧路径逐字一致 --------------------------
        # #5f 出厂态验收：这一轮**从出厂态起跑**（复位只动运行时状态文件，见
        # `_reset_runtime_state`）。轮内的修复/复测不再复位 —— 迭代要在同一份
        # 状态上看得见自己的改动；下一轮的验收回出厂态。
        self._reset_runtime_state(f"measuring {node_id}")
        if own_specs:
            # #5：节点自己的首测是**验证性**测量，允许复用 —— 但这一处大多数时候
            # 命不中：模型刚改完的东西还没提交，树是脏的（钥匙是 None）。真正会命中的
            # 是「自己没动过手」的那一类（聚合点、退回检查点之后的复验）。
            own_summary = self._run_specs(own_specs, grader_like=False, allow_reuse=True)
            if own_summary is None:
                result.reason = "specs could not run"
                return result
            if self._app_broken(own_summary) and self._reset_runtime_state(
                    f"checking {label} at startup"):
                # #5f 双态（随包态 / 出厂态）：一轮里被某一刀写脏的运行时状态，可能
                # 正是起不来的原因（复位前那一份 = 随包态）。先复位、再复量一次：
                # 出厂态起得来 ⇒ 坏的是我们自己的运行残渣，不是环境也不是这个节点
                # （0 修复回合，接着走）；出厂态也起不来 ⇒ 真的坏，照旧治环境（#5a）。
                # 没改动时上面那条已经返回 False —— 常见的「代码写坏了构建」不会
                # 因此多付一次测量。
                retry = self._run_specs(own_specs, grader_like=False)
                if retry is not None and not self._app_broken(retry):
                    log(f"[{label}] the app starts on factory state — the run's own runtime "
                        f"state was what broke startup; reset, nothing to repair")
                    self._ledger_note(node_id, tried=[
                        "app would not start; runtime state reset to factory — starts again "
                        "(no repair needed)"])
                    self._ledger_write()
                    own_summary = retry
            if self._app_broken(own_summary):
                # #5a 环境坏：构建/启动失败不是「这个节点红」—— 判据在
                # `RunSummary.startup_error` 上（见 `_app_broken`）。先就地抢修，
                # 治不好才照旧放弃；不治的话每个节点都对着同一套坏环境白量一遍。
                if self._startup_given_up:
                    result.reason = ("the app does not build/start, and startup repair was "
                                     "already given up on in this run: "
                                     f"{str(own_summary.error)[:200]}")
                    result.baseline_ok = False   # 让调用方退回本节点检查点（能退就退）
                    result.env_dead = True       # 台账记 parked（#5d）
                    return result
                healed = self._heal_app_startup(result, label, own_summary, own_specs,
                                                deadline_guard)
                if healed is None:
                    # 治不好：本跑不再治（同一个坏环境反复治 = 新的空转源），
                    # 并让**这一刀**退回检查点 —— 坏盘是没被验证过的落盘，
                    # 不该留在盘上装成工作成果（#5 的总主题）。
                    self._startup_given_up = True
                    result.reason = ("the app does not build/start and startup repair did not "
                                     f"take: {str(own_summary.error)[:200]}")
                    result.baseline_ok = False
                    result.env_dead = True
                    return result
                own_summary = healed
            else:
                # 量得动（哪怕是卷子红）⇒ 这棵树的构建/启动是好的：判死清零。
                self._startup_given_up = False
            if own_summary is None:
                result.reason = "specs could not run"
                return result
            if own_summary.error:
                result.reason = f"spec run failed: {own_summary.error[:300]}"
                return result
            if not own_summary.all_passed:
                own_fail = self._failure_keys(own_summary)
                result.reason = (f"{own_summary.passed}/{own_summary.total} spec(s) passed"
                                 if own_summary.total else "no specs mapped to this node")
                # #5b 验收失败三分类（环境坏 → 抢修；卷子坏 → 申诉；行为缺 → 才进修复）。
                # 这一档是**卷子坏**：机器验过的死卷子当场作废（= 自动改道申诉），
                # 绝不进实现修复队列 —— 拿实现去修一条语言上对**任何**实现都红的断言
                # 是白烧回合。（#1 扫描器实测口径：全排练 16 套 408 份真卷只命中 2 份
                # 已知死卷、0 误报；官方卷子不在作废范围，见 `_auto_void_dead_specs`。）
                dead = self._mechanically_dead_files({k[0] for k in own_fail if k[0]})
                voided = self._auto_void_dead_specs(sorted(dead), only_owner=node_id) if dead else []
                if voided:
                    gone = set(voided)
                    own_specs = [s for s in own_specs if Path(s).name not in gone]
                    own_names = {Path(s).name for s in own_specs}
                    own_fail = {k for k in own_fail if k[0] not in gone}
                    result.reason = f"its own spec was voided as defective: {', '.join(voided)}"

        # -- 小域的 before 像 ------------------------------------------------
        # 批级净胜判定需要「批前」的失败集：自己的就是上面那份，小域的在这里量。
        # 旧路径在 own 不过时不跑基线（跑了也不影响决定）；这里跑是因为**它影响
        # 批的切法** —— 弄红的小域卷子和自己的卷子放进同一批修，正是「改一错
        # 一片」的解药。范围通常很小（forward_deps ∩ 活基线）。
        #
        # 对账记账的修前向卷子也并进小域（`_extend_scope_with_forward`）：它们
        # 在别人的域里，不并进来就没有任何一批会碰到它们，修前向的说明也就没有
        # 着陆点 —— 并进来之后一切走既有机制（修好算净胜、弄坏算弄坏）。
        scope = self._extend_scope_with_forward(
            self._scope_specs(node_id, aggregate, since=since_sha))
        if scope:
            scope_state = self._probe_scope(scope)
            if scope_state is not None:
                scope_fail = self._failure_keys(scope_state[0])

        # -- 一开始就能验收吗 -------------------------------------------------
        if not own_fail:
            verdict = self._accept_verdict(node_id, scope_state, red_before)
            if verdict is None:
                self._second_pass_check(node_id, own_specs, aggregate)   # #7
                return self._accepted(result, own_specs)
            result.baseline_ok = False
            result.reason = verdict

        # -- 批修循环 ---------------------------------------------------------
        while spent < budget:
            if own_fail and self._maybe_void_spec(node_id, spent) is not None:
                # 修不动的时候先问「是不是卷子写错了」—— 和旧路径同一道出口，
                # 门槛换算成本节点已花掉的尝试数（「先给代码公平的机会」不变）。
                # 作废不消耗回合，就地往验收走。
                own_specs = list(flow.spec_map.get(node_id) or [])
                own_names = {Path(s).name for s in own_specs}
                own_fail = set()
                result.reason = "its own spec was voided as defective"
                verdict = self._accept_verdict(node_id, scope_state, red_before)
                if verdict is None:
                    self._second_pass_check(node_id, own_specs, aggregate)   # #7
                    return self._accepted(result, own_specs)
                result.baseline_ok = False
                result.reason = verdict
            # 只修**记账在本节点头上**的红：自己的失败 + 小域里「开工前还绿着」
            # 的那些（= `_scope_verdict` 会算 newly_red 的那批文件）。开工前就红
            # 的不进批 —— 判词里写着「不是你的，别修」，批里就不该出现它们，
            # 否则模型会为一个修不好的东西烧掉整批回合。accidentally 修好它们
            # 仍然算净胜（`fixed` 用的是全量失败集 `scope_fail`）。
            # 修前向的红是例外：它们本来就「开工前就红」（被前面的某刀弄红的），
            # 但对账已经确认了肇事者并把它派给了接下来的修复 —— 必须进批。
            charged = own_fail | {k for k in scope_fail
                                  if k[0] not in red_before or k[0] in self._forward_fix_files}
            # #5b：机器验过的死卷子不进实现修复队列 —— 但**只在能改道申诉时**
            # （`authored_suite`：入口作废 / 属主作废 / 收尾作废，三条路总有一条
            # 会把它摘下来）。自己的已在入口作废，这里管小域里**别人**的。
            # 官方卷子不在队列是因为**它摘不掉**：判分机跑的是另一份拷贝，本地
            # 作废只是把自己弄瞎；而不进队列的代价是永久弃掉同一份卷子里**别的**
            # 可修用例（净胜判定是**用例级**的，一条死断言挡不住别的用例被修绿），
            # 所以那一档宁可让它进队列、由 park/熔断收口（实测：全排练 406 份
            # 官方卷子 0 命中，这条路基本不会走到）。
            if getattr(flow, "authored_suite", False):
                charged = {k for k in charged
                           if k[0] not in self._mechanically_dead_files({k[0] for k in charged if k[0]})}
            if not charged:
                break
            batches = self._next_batches(node_id, charged, parked)
            if not batches:
                all_parked = True   # 剩下的红全修不动了
                break
            kept_any = False
            stop_here = False
            for batch in batches:
                if set(batch) & parked:
                    # 这一批的切法早于某个加宽动作 —— 里面的文件已经被 park 了
                    # （加宽会把**别的批里的**文件并进来一起 park），别再试一遍。
                    continue
                if spent >= budget:
                    stop_here = True
                    break
                if not self._can_repair(deadline_guard, need=self._batch_needs()):
                    budget_stopped = True
                    stop_here = True
                    break
                original = frozenset(batch)   # 同一批的「血脉」：加宽重试算同一次
                current = list(batch)
                while True:
                    if attempts.get(original, 0) >= self._batch_attempts():
                        parked.update(current)
                        log(f"[{label}] parking {', '.join(sorted(current))}: "
                            f"{attempts.get(original, 0)} attempt(s), no net gain")
                        break
                    # 快照这一批的入口。快照失败（None）就不退 —— 「能退就退」，
                    # 辅助动作绝不变成新的故障点（见 `Flow.snapshot_state`）。
                    snap = None
                    snap_fn = getattr(flow, "snapshot_state", None)
                    if callable(snap_fn):
                        try:
                            snap = snap_fn(f"{label} batch {spent + 1}")
                        except Exception as exc:  # noqa: BLE001
                            log(f"[{label}] snapshot failed: {exc}")
                    before_own, before_scope = set(own_fail), set(scope_fail)
                    # 提示词和验收用的「盘上什么样」也要能一起退 —— 批被退回后
                    # 如果 own_summary/scope_state 还描述着被退掉的那棵树，下一批
                    # 的提示词就是在对着不存在的失败说话，验收判词也会拿它冤枉人。
                    before_summary, before_state = own_summary, scope_state
                    self._turn(
                        self._batch_repair_prompt(node_id, current, own_summary, scope_state, aggregate),
                        f"{label} batch repair {spent + 1}/{budget}", timeout=self._batch_timeout())
                    spent += 1
                    result.turns += 1
                    attempts[original] = attempts.get(original, 0) + 1
                    if self._last_turn_timed_out:
                        # v5 超时止损：这一轮被墙钟烧完还没交活，不是「修过了」——
                        # 不按常规加宽重试，退回这一刀、搁置 + 进诊断队列。
                        streak = self._stall_streak.get(node_id, 0) + 1   # 超时与零收成合账
                        self._stall_streak[node_id] = streak
                        if snap:
                            flow.restore_app(snap)
                        parked.update(current)
                        self._queue_diagnosis(node_id, node_id, list(current),
                                              "repair turn timed out", "node")
                        cooled = streak >= self._stall_limit()
                        # 附·超时记账：被掐断的批次一律进账本，明确记「作废」
                        # （这一刀没被验证过）；到线即停修记档（#3 熔断）。
                        tried_note = (f"batch {', '.join(sorted(current))}: repair turn timed "
                                      f"out (wall-clock), rolled back — nothing kept")
                        if cooled:
                            self._ledger_note(
                                node_id, status="parked",
                                why=(f"repair stopped for this run: {streak} repair round(s) in "
                                     f"a row with no net progress (timeouts / zero gain)"),
                                tried=[tried_note])
                        else:
                            self._ledger_note(node_id, tried=[tried_note])
                        self._ledger_write()
                        log(f"[{label}] batch timed out ({', '.join(sorted(current))}); "
                            f"parked for diagnosis (no-progress streak {streak})")
                        if cooled:
                            all_parked = True
                            stalled = streak
                            # 剩下的红全部 park：不然这个 for 还会把下一个批喂给模型
                            # ——「no further repair turns」必须是真的。
                            parked.update(k[0] for k in charged if k[0])
                            log(f"[{label}] {node_id}: {streak} no-progress round(s) in a row "
                                f"— no further repair turns for it in this run")
                        break

                    if self._last_turn_failed:
                        # #5e 异常终止轮也回退：回合**判失败**（桥梁/接口异常、被判死），
                        # 落盘的是写了一半的树 —— 现行为是照常往下量，量不动才退；
                        # github-2 实锤那条路的代价：死轮半成品留盘 → 后端种子崩 →
                        # 连坐 3 张卷子。与超时轮同一条处置：退回检查点、与零收成
                        # **合账**、明确记「作废」。区别只在下一步：超时轮直接 park
                        # （它已经烧满一整段墙钟），失败轮多数是被中途掐断的，同一批
                        # 还有尝试余额就再来一次（`_batch_attempts` 管重试上限，
                        # `_stall_streak` 管连续零进展，到线仍由 #3 熔断收口）。
                        # 没有检查点（快照失败）时不带脏重试 —— 退回不了就停这一节点。
                        if snap:
                            flow.restore_app(snap)
                            own_fail, scope_fail = before_own, before_scope
                            own_summary, scope_state = before_summary, before_state
                        streak = self._stall_streak.get(node_id, 0) + 1
                        self._stall_streak[node_id] = streak
                        tried_note = (f"batch {', '.join(sorted(current))}: the turn ended in "
                                      f"failure" + (", rolled back" if snap else
                                                    " (no checkpoint to roll back to)")
                                      + " — nothing kept")
                        cooled = streak >= self._stall_limit()
                        if cooled or not snap:
                            parked.update(current)
                            parked.update(k[0] for k in charged if k[0])
                            all_parked = True
                            stalled = streak
                            self._queue_diagnosis(node_id, node_id, list(current),
                                                  "repair turn failed", "node")
                            self._ledger_note(
                                node_id, status="parked",
                                why=(f"repair stopped for this run: {streak} repair round(s) in "
                                     f"a row with no net progress (failures / timeouts / zero "
                                     f"gain)"),
                                tried=[tried_note])
                            self._ledger_write()
                            log(f"[{label}] batch failed ({', '.join(sorted(current))}); "
                                f"parked — no further repair turns for it in this run")
                            break
                        self._ledger_note(node_id, tried=[tried_note])
                        self._ledger_write()
                        log(f"[{label}] batch failed ({', '.join(sorted(current))}); rolled "
                            f"back (no-progress streak {streak})")
                        continue   # 循环头按尝试余额决定：还有就再来一次，用完了 park

                    # 量：own 永远量；小域只在**这轮可能改变了它**时量 ——
                    # own 动了 / 批里有小域文件 / own 已经绿了（验收要用它）。
                    if own_specs:
                        own_summary = self._run_specs(own_specs, grader_like=False)
                        if own_summary is None or own_summary.error:
                            result.reason = ("spec run failed: " + str(
                                getattr(own_summary, "error", "") or "specs could not run")[:300])
                            # 量不动 = 这一刀没被验证（附·超时记账的"作废"半边）：
                            # 退回它（有快照时），记一笔 —— 没验证的东西不留在盘上，
                            # 也不会在账本里冒充"试过且有用"。这一轮同样算零收成。
                            if snap:
                                flow.restore_app(snap)
                            parked.update(current)
                            streak = self._stall_streak.get(node_id, 0) + 1
                            self._stall_streak[node_id] = streak
                            cooled = streak >= self._stall_limit()
                            # 「量不动」有两种来源，账本要分开记：这一刀把 app 弄到
                            # 构建/启动都起不来（环境病，退回即止血），还是跑卷子的
                            # 基建自己出错（与这一刀未必有关）。
                            why_stuck = ("the app would not build/start" if self._app_broken(own_summary)
                                         else "the spec run failed")
                            tried_note = (f"batch {', '.join(sorted(current))}: unverifiable "
                                          f"({why_stuck}), rolled back — nothing kept")
                            if cooled:
                                self._ledger_note(
                                    node_id, status="parked",
                                    why=(f"repair stopped for this run: {streak} repair round(s) "
                                         f"in a row with no net progress (timeouts / zero gain)"),
                                    tried=[tried_note])
                            else:
                                self._ledger_note(node_id, tried=[tried_note])
                            self._ledger_write()
                            if cooled:
                                result.exhausted = True
                                result.reason += (f" (repair stopped: {streak} no-progress "
                                                  f"round(s) in a row)")
                            return result
                        own_fail = self._failure_keys(own_summary)
                    else:
                        own_fail = set()
                    # 附·两段式测量（第一段 = 批内卷子，就是上面这次 own 量）：
                    # 批内 0 进账 ⇒ 这一刀没修绿任何一条被点名的失败，判定必然是
                    # 「退回」—— 那就**别跑第二段**：小域/全卷那道固定税（REQ-5-2-1
                    # 每轮 66s × 8 回的形状）不再叠在任何一次注定被退回的批上。
                    # 有进账才跑第二段查连坐（own 全绿那条验收窗口也在内：此时
                    # `scope_state` 必须是这一轮刚量的）。批里带小域文件时照跑 ——
                    # 那些文件的进账不在 `own_fail` 里看得见。退化的边界（批里全是
                    # 自己的名字、而自己那本账已经空了）也照退：那一刀本来就没有
                    # 可判的进账。
                    own_gained = bool(before_own - own_fail)
                    batch_has_scope = any(f not in own_names for f in current)
                    if scope and (own_gained or batch_has_scope):
                        got = self._probe_scope(scope)
                        if got is not None:
                            scope_state = got
                            scope_fail = self._failure_keys(got[0])

                    # **测试级净胜判定**：新增绿 ≥1 且零新增红才留下。
                    # 键是 (卷子 basename, 用例 title) —— 一份卷子里修好一个用例、
                    # 弄坏另一个，两边都算数，不会被文件的粒度抹平。
                    fixed = (before_own - own_fail) | (before_scope - scope_fail)
                    broke = (own_fail - before_own) | (scope_fail - before_scope)
                    if not broke and fixed:
                        # 有净胜 = 有进展：连续零进展计数清零（#3 熔断的另一半 ——
                        # 「慢热」节点只要真的在往前走，就永远够不着熔断线）。
                        self._stall_streak.pop(node_id, None)
                        flow.commit(f"{node_id}: batch repair {', '.join(sorted(current))} "
                                    f"+{len(fixed)}")
                        log(f"[{label}] batch kept ({', '.join(sorted(current))}): "
                            f"+{len(fixed)} test(s) green, nothing newly broken")
                        self._ledger_note(node_id, tried=[
                            f"batch {', '.join(sorted(current))}: +{len(fixed)} test(s) green "
                            f"(kept)"])
                        self._ledger_write()
                        kept_any = True
                        self._reconcile_keeps += 1   # 又一个可查的提交（见 `_accepted`）
                        if not own_fail:
                            # 验收窗口：用刚量的这轮小域判，不额外再跑一次。
                            verdict = self._accept_verdict(node_id, scope_state, red_before)
                            if verdict is None:
                                self._second_pass_check(node_id, own_specs, aggregate)   # #7
                                return self._accepted(result, own_specs)
                            result.baseline_ok = False
                            result.reason = verdict
                        break
                    # 无净胜：退回这一批的入口状态。
                    log(f"[{label}] batch reverted ({', '.join(sorted(current))}): "
                        f"+{len(fixed)} green, {len(broke)} newly broken")
                    # #3 熔断（零收成这一半）：没弄红也没修绿的轮，与超时合账；
                    # 到线就停修 —— 加宽重试是「再来一刀」，熔断说「够了」。
                    streak = self._stall_streak.get(node_id, 0) + 1
                    self._stall_streak[node_id] = streak
                    cooled = streak >= self._stall_limit()
                    if cooled:
                        if snap:
                            flow.restore_app(snap)
                            own_fail, scope_fail = before_own, before_scope
                            own_summary, scope_state = before_summary, before_state
                        parked.update(current)
                        all_parked = True
                        stalled = streak
                        # 剩下的红全部 park：不然这个 for 还会把下一个批喂给模型
                        # ——「no further repair turns」必须是真的。
                        parked.update(k[0] for k in charged if k[0])
                        self._queue_diagnosis(node_id, node_id, list(current),
                                              f"{streak} repair round(s) with no net progress",
                                              "node")
                        # 附·超时记账：这一批的结局进账本 —— 明确记「作废」（退回了）
                        self._ledger_note(
                            node_id, status="parked",
                            why=(f"repair stopped for this run: {streak} repair round(s) in a "
                                 f"row with no net progress (timeouts / zero gain)"),
                            tried=[f"batch {', '.join(sorted(current))}: reverted "
                                   f"(+{len(fixed)} green, {len(broke)} newly broken) — "
                                   f"nothing kept"])
                        self._ledger_write()
                        log(f"[{label}] {node_id}: {streak} no-progress round(s) in a row "
                            f"— no further repair turns for it in this run")
                        break
                    self._ledger_note(node_id, tried=[
                        f"batch {', '.join(sorted(current))}: reverted (+{len(fixed)} green, "
                        f"{len(broke)} newly broken) — nothing kept"])
                    self._ledger_write()
                    if snap:
                        flow.restore_app(snap)
                        # 树退回去了，观察跟着退 —— 下一批的 before 像必须描述
                        # 盘上真实的树（失败集、own 结果、小域观察一起）。
                        own_fail, scope_fail = before_own, before_scope
                        own_summary, scope_state = before_summary, before_state
                    # 退不了（没有快照）：盘上留着这一刀，失败集就是最新观察。
                    if broke and attempts[original] < self._batch_attempts():
                        widened = sorted({f for f in current} | {k[0] for k in broke if k[0]})
                        log(f"[{label}] widening this batch with the newly broken file(s): {widened}")
                        current = widened
                        continue
                    parked.update(current)
                    break
                if kept_any:
                    break
            if kept_any:
                continue   # 失败集变了 —— 重新切批
            if stop_here or spent >= budget:
                break
            if spent == 0:
                break      # 预算为 0：一次都没花，别再转
            # 没 keep 也没超预算：本轮试过的批全 park 了。下一轮 `_next_batches`
            # 会把剩余的 park 文件排除 —— 要么切出没试过的批，要么返回空
            # （`all_parked`）。两条路都终止。

        # -- 收尾 -------------------------------------------------------------
        if own_fail:
            result.reason = (f"{own_summary.passed}/{own_summary.total} spec(s) passed"
                             if own_summary is not None and own_summary.total
                             else result.reason or "no specs mapped to this node")
        if spent >= budget or all_parked:
            result.exhausted = True   # 回合用完了 / 剩下的红全修不动 —— 判死
            if stalled:
                # 熔断的判词要说得清为什么停 —— 不是"回合用完了"，是"零进展到线"。
                result.reason = (result.reason + f" (repair stopped: {stalled} round(s) in a "
                                                 f"row with no net progress)").strip()
        if budget_stopped:
            result.reason = (result.reason + " (no budget left for another repair)").strip()
        # #8 哨兵的观察值：只有走到这里才有**可信的**测量（前面每个提前返回的
        # 出口都是「没量成 / 环境判死」，那里 `own_final` 留在 None —— 哨兵对
        # 「不知道」既不计数也不清零）。
        if own_summary is not None and not own_summary.error:
            result.own_final = self._spec_tally(own_summary)
        return result

    @staticmethod
    def _failure_keys(summary) -> set[tuple[str, str]]:
        """失败用例的**测试级身份**：卷子 basename + 用例 title。

        净胜判定就钉在这上面 —— 一份卷子里的用例也有各自的命，修好一个和弄坏
        另一个在文件粒度上会互相抵消，测试粒度不会。

        「没跑到的用例」不算失败（见 `_never_ran`）：判红即停的截断会让它们
        以 `ok=False` 的样子出现，把它们算进键集，二分探针的 `is_red` 判据
        会被一条从没跑过的红蒙过。
        """
        return {(Path(r.file or "").name, r.title or "")
                for r in summary.results if not r.ok and not _never_ran(r)}

    def _batch_cap(self) -> int:
        """一批修几份卷子。**v5 默认 1**（一次只动一张卷子）。

        旧默认是 3（同属主凑一批，省回合）；实测的代价是「好几件事一起修」——
        模型一次面对三个失败，改一处弄红另一处，净胜判不掉、三轮回合烧光。
        「最小的改动来，一点一点推进」在机制上的落点就是这里：
        `OCTOS_REPAIR_BATCH_MAX=3` 可以回到旧行为。
        """
        try:
            return max(1, int(os.environ.get("OCTOS_REPAIR_BATCH_MAX", "1")))
        except ValueError:
            return 1

    def _batch_attempts(self) -> int:
        try:
            return max(1, int(os.environ.get("OCTOS_REPAIR_BATCH_ATTEMPTS", "2")))
        except ValueError:
            return 2

    def _stall_limit(self) -> int:
        """同一节点连续零进展（超时 / 净收成为零合账）多少轮就停修（#3 熔断）。

        初步 N=2（与原超时连击同阈值）；`OCTOS_REPAIR_STALL_LIMIT` 可用演练
        数据校准 —— 调大对「慢热」节点更宽容，代价是白量的回合更多。
        """
        try:
            return max(1, int(os.environ.get("OCTOS_REPAIR_STALL_LIMIT", "2")))
        except ValueError:
            return 2

    def _systemic_limit(self) -> int:
        """#8 哨兵阈值：叶子环**连续**多少张全红就停线。默认 4（计划给的 3~5
        取中），`OCTOS_SYSTEMIC_STREAK` 可调；**`<=0` = 关掉哨兵**（与 #3 熔断
        不同，这里不 clamp 到 1 —— 关得掉是标定期的需要）。
        """
        try:
            return int(os.environ.get("OCTOS_SYSTEMIC_STREAK", "4"))
        except ValueError:
            return 4

    def _batch_needs(self) -> float:
        """再来一个批回合大约要多少秒 —— 批修自己的观察值。"""
        flow = self.flow
        try:
            fn = getattr(flow, "batch_repair_needs", None)
            if callable(fn):
                return float(fn())
            return float(flow.repair_needs())
        except Exception:  # noqa: BLE001
            return float(getattr(flow, "min_repair_seconds", 300))

    def _batch_timeout(self) -> int:
        flow = self.flow
        try:
            fn = getattr(flow, "batch_repair_timeout", None)
            if callable(fn):
                return max(60, int(fn()))
            return max(60, int(min(flow.node_timeout, min(1200, flow.repair_needs() * 1.5))))
        except Exception:  # noqa: BLE001
            return 600

    def _next_batches(self, node_id: str, fail_keys: set[tuple[str, str]],
                      parked: set[str]) -> list[list[str]]:
        """把当前红名单切成批：按属主分组、本节点优先、每组 ≤ `_batch_cap()` 份。

        同属主优先的理由和用户分卷一样：同一个人名下的卷子多半共享同一套
        API/组件，一起修一次比分开修省回合，改动面也收在一个域里。
        """
        owner_of: dict[str, str | None] = {}
        for owner, files in self.flow.spec_map.items():
            for path in (files or []):
                owner_of[Path(path).name] = owner
        files = sorted({k[0] for k in fail_keys if k[0]} - set(parked))
        if not files:
            return []
        groups: dict[str | None, list[str]] = {}
        for f in files:
            groups.setdefault(owner_of.get(f), []).append(f)
        cap = self._batch_cap()
        batches: list[list[str]] = []
        for key in sorted(groups, key=lambda k: (k != node_id, str(k))):
            chunk = sorted(groups[key])
            batches.extend(chunk[i:i + cap] for i in range(0, len(chunk), cap))
        return batches

    def _batch_repair_prompt(self, node_id: str, files: list[str], own_summary,
                             scope_state: tuple | None, aggregate: bool) -> str:
        """一批修复的提示词：**范围先钉死，失败详情只给批内文件的**。

        不钉范围的后果有实测：一个节点级修复回合被要求「修这个节点的失败」，
        模型把整个功能域重写了一遍，修好的没几个、弄红的一大片。批的粒度若只
        存在于 harness 的记账里、不进提示词，模型交付的还是一个整段修复。
        """
        head = (f"Repair scope — this is one small batch of the repair for `{node_id}`. Fix ONLY "
                f"the behavior these {len(files)} spec file(s) exercise: {', '.join(files)}.\n"
                f"Do not restructure code outside that behavior, do not touch any other spec's "
                f"area, and do not edit the specs themselves. This batch is kept only if it turns "
                f"at least one failing test green **without breaking any other** — a change that "
                f"breaks something else is rolled back whole.\n")
        parts: list[str] = []
        for summary in (own_summary, scope_state[0] if scope_state else None):
            if summary is None:
                continue
            piece = failure_summaries(summary, files=set(files))
            if piece and piece not in parts:
                parts.append(piece)
        failures = head + ("\n" + "\n".join(parts) if parts else "")
        if set(files) & self._forward_fix_files:
            # 这一批碰到了对账记账的修前向红 —— 把肇事提交的原话和「这是修前向
            # 不是回滚」一起交给它。节点阶段和收尾都从这里注入，只有一份文案。
            failures += self._forward_fix_note
        return self._repair_prompt(node_id, failures, aggregate, spec_files=files)

    # -- 全卷对账与修前向（P2） -------------------------------------------

    def _maybe_reconcile(self, node_id: str) -> None:
        """节点阶段隔一段时间对一次全卷账。**只旁观，不动盘。**

        分批修复的小域是「本节点 + 它的下游」，一个把红卷子留在**别人的**域里的
        改动，在这个视野里是隐形的 —— 而它要到收尾那趟才冒出来，那时已经没人
        记得是哪一刀弄的，只能对着既成事实修。这条通道专门堵它：趁验收 keep
        的间隙跑一遍全卷，域外新红就进 git 历史二分找肇事提交，然后**修前向**
        —— 把那份红并进下一次修复，而不是回滚已经正确的部分。

        门闸（三条都满足才跑）：
          · `OCTOS_RECONCILE_SECONDS`（默认 3600，`<= 0` 关闭）—— 距上次够久；
          · 期间攒够 `_RECONCILE_MIN_KEEPS` 次 keep —— 没有新提交就没有可查的刀；
          · 预算还剩得下这次测量（不足 15% 时留给收尾，那时本来就全卷量）。
        第一次只记时间点不测量：量一次的价钱是分钟级，而「刚 keep 的这批干净
        不干净」小域那一步已经付过钱了。

        默认值按大树的单价定（2026-10-08 提速批）：一次全卷在 github 规模要
        20–25 分钟，间隔比测量本身还短时这个门等于没在节流 —— 拉长到 1 小时、
        keep 提到 6，让对账的次数和它的单价比。"""
        flow = self.flow
        try:
            interval = float(os.environ.get("OCTOS_RECONCILE_SECONDS", "3600"))
        except ValueError:
            interval = 3600.0
        if interval <= 0:
            return
        now = time.time()
        if not self._reconcile_sha:
            self._reconcile_sha = flow.head()
            self._reconcile_at = now
            return
        if now - self._reconcile_at < interval or self._reconcile_keeps < _RECONCILE_MIN_KEEPS:
            return
        try:
            budget = float(getattr(flow, "budget", 0) or 0)
            if budget > 0 and float(flow.remaining()) < budget * 0.15:
                return
        except Exception:  # noqa: BLE001
            return
        self._reconcile_at = now     # 跑不起来也重新计时，别反复撞同一堵墙
        self._reconcile_keeps = 0
        self._run_reconcile(node_id)

    def _run_reconcile(self, node_id: str) -> None:
        """对账主流程：点名体检 → 记账 → 域外新红 → 二分 → 修前向（或 revert）。

        这一趟是**日常体检**，不是成绩单（2026-10-08 提速批，两条）：

          · 范围收窄成「曾经绿过 ∪ 从没量过」（`_sweep_specs`）—— 从没绿过、
            又量过的卷子不陪跑；
          · 判红即停（`--max-failures=1`）—— 要的是「哪份卷子红了」，不是
            每份里红了几条。文件隔离下它在**文件内部**收工，逐份照跑不误；
            `OCTOS_ARC_FILE_ISOLATION=0`（整批一次跑）时它会连后面的文件一起
            停下，域外新红的可见面变窄 —— 少看见，不会看错（记账、二分、
            revert 的判据都只认看得见的证据）。

        两条都让 summary 不再是「全卷完整测量」：所以有**记账守卫** ——
        只有一份没漏（full_coverage）、一条没被截断（all_passed）、且没有
        文件没收上来（load_errors）时，才进 `_book_final_measure`。半截测量
        拿去记账，封箱判据会把没量过的树当成量过了，成绩卡也报一个假的
        `total`。收尾/封箱的全卷量不做这两件事：那是成绩单，必须量全部。

        记账守卫之外：量全了但没量到全绿的那趟会顺手把**成绩卡**武装到这次的
        下界（`_arm_best_from_sweep`）—— 只动卡，封箱判据一个字不碰。
        """
        flow = self.flow
        all_specs = self._all_specs()
        if not all_specs:
            return
        specs = self._sweep_specs(all_specs)
        if not specs:
            log(f"[reconcile] nothing to sweep while {node_id} is accepted: "
                "every spec file on disk was measured and never went green")
            return
        full_coverage = len(specs) == len(all_specs)
        workers = final_workers(getattr(flow, "mem_limit", None))
        log(f"[reconcile] measuring the full suite while {node_id} is being accepted")
        try:
            # #5：对账是验证性测量 —— 允许复用，也**供**复用：它量完的这棵树
            # 紧接着就会被聚合验收用同一批卷子再问一次（见 `_probe_scope`）。
            summary = self._run_specs(specs, grader_like=True, workers=workers,
                                      fail_fast=True, allow_reuse=True)
        except Exception as exc:  # noqa: BLE001
            log(f"[reconcile] the full suite could not run: {exc}")
            return
        if summary is None or summary.error:
            log(f"[reconcile] the full suite could not run: "
                f"{str(getattr(summary, 'error', '') or 'no results')[:120]}")
            return
        # 先留住**上次量过的 HEAD**：二分的窗口是 `prior..HEAD`（弄红的那一刀就
        # 藏在这段提交里），而下面一行立刻把 `_reconcile_sha` 拨到这次的 HEAD
        # 给下一轮当起点 —— 不留住的话窗口恒空，肇事提交在生产里永远指认不出
        # 来（假 git 的单元测试无视范围参数，掩盖过这一条；github 决赛日志里
        # 也从来没有过 "trace back to" 行）。
        prior_sha = self._reconcile_sha
        self._reconcile_sha = flow.head()
        # 全卷观察并进红名单（和节点验收同一本账）；prior 是**跑之前**的名单，
        # 两者相减就是「这次看到的、之前不在账上的红」。点名体检没跑的卷子
        # 在这本账上按原语义处理：保留上一次的观察，不猜。
        failing, prior = self._absorb_scope(specs, summary)
        grouped = nodes_for_failures(summary.results, flow.spec_map)
        log(f"[reconcile] full suite {summary.passed}/{summary.total} "
            f"({len(specs)}/{len(all_specs)} spec file(s) measured)")
        complete = (full_coverage and summary.all_passed
                    and not getattr(summary, "load_errors", None))
        if complete:
            # 这次观察就是盘上这棵树的全卷像 —— 封箱判据和最好成绩卡都用它。
            self._book_final_measure(summary, grouped, note=f"reconcile after {node_id}")
        else:
            # 记账不动；量全了的话成绩卡武装到这个下界（见 `_arm_best_from_sweep`）。
            self._arm_best_from_sweep(summary, grouped, full_coverage, node_id)
        unknown = sorted(failing - prior)
        if not unknown:
            return
        # 回归只能发生在**曾经绿过的用例**上 —— 与 `_sweep_specs`「从没绿过的
        # 东西没有『变红』一说」是同一口径，这里精确到 (卷子, 用例)。从未绿过
        # 的红是「还没做到」，不是「被谁弄红」：它们的属主节点实现时自然会量
        # 它们。不收口的话，第一次对账会把整棵还没实现的树都记成「域外新红」
        # ——github 决赛实锤：52 份里记了 36 份，之后每一刀验收都被要求顺手修
        # 全树、每轮为此重扫 25 分钟（占全程 45%），且从未修成过任何一份。
        red_keys = {k for k in self._failure_keys(summary)
                    if k[0] in set(unknown) and (k[0], k[1]) in self.ever_green_tests}
        unknown = sorted({k[0] for k in red_keys})
        if not unknown:
            log("[reconcile] every newly red spec file holds no test that ever passed — "
                "they are not yet implemented (their owner nodes will measure them); "
                "nothing to bisect")
            return
        # #7 停损：对账这趟全卷也是**不带重置**量的（它本来就发生在别人的验收
        # 途中）—— 「用完不还原」的卷子会在这里冒充「域外新红」，而被当成的
        # 后果是：账本里记下一个无辜的肇事提交、下一步修复拿出一批回合去够它
        # （二分探针也白烧）。点过名的先按出厂态复查，确认是残渣就不进账。
        ghosts = self._ghost_reds(unknown, grader_like=True)
        if ghosts:
            unknown = [f for f in unknown if f not in ghosts]
            if not unknown:
                log("[reconcile] everything newly red is a spec that does not restore "
                    "what it changes (confirmed from factory state) — nothing to bisect")
                return
            red_keys = {k for k in red_keys if k[0] not in set(ghosts)}
        log(f"[reconcile] {len(unknown)} spec file(s) newly red outside the repaired domain: "
            f"{', '.join(unknown[:8])}")
        culprit = self._bisect_culprit(unknown, red_keys, prior_sha)
        if os.environ.get("OCTOS_RECONCILE_REVERT", "0") == "1":
            if self._revert_culprit(culprit, red_keys):
                return
            # revert 不成（或没指认出提交）→ 修前向兜底，账照记。
        self._record_forward_fix(culprit, red_keys)

    def _bisect_culprit(self, red_files: list[str], red_keys: set[tuple[str, str]],
                        since: str | None = None) -> dict | None:
        """哪一刀弄红了这些卷子 —— 在 `since..HEAD`（默认 `_reconcile_sha..HEAD`）
        的提交里二分。

        `since` 由调用方传**上次量过的 HEAD**：`_run_reconcile` 在测量之后立刻会
        把 `_reconcile_sha` 拨到这次的 HEAD（下一轮的窗口起点），所以等走到这里
        时它已经不再是窗口的起点 —— 不传 `since` 的话范围恒为空。

        对账是**旁观者**：探针要临时 checkout 别人的提交来取证，所以进来先
        快照、`finally` 无条件退回 —— 无论中途发生什么，树都停在进来时的状态。

        局限（诚实记账，不是保证）：
          · 只能指认这次对账窗口（上次量过的 HEAD）里的提交，更早的账翻不到；
          · 探针次数有上限（`OCTOS_RECONCILE_BISECT_PROBES`，默认 6），窗口里
            提交多的时候二分可能收敛不了 —— 那时返回 None，修前向照走；
          · `git checkout <sha> -- frontend backend` 不会删掉后来才新增的文件，
            「新文件导致的红」探不准，判红偏保守（探到红才算红）。
        """
        flow = self.flow
        if not red_keys:
            return None
        try:
            probes_budget = max(1, int(os.environ.get("OCTOS_RECONCILE_BISECT_PROBES", "6")))
        except ValueError:
            probes_budget = 6
        paths = self._spec_paths()
        probe = sorted({paths[name] for name in red_files if name in paths})
        if not probe:
            return None
        base = since or self._reconcile_sha
        try:
            out = getattr(flow.runtime.git.run(
                ["rev-list", "--reverse", f"{base}..HEAD"],
                check=False), "stdout", "") or ""
        except Exception:  # noqa: BLE001
            return None
        commits = [line.strip() for line in out.splitlines() if line.strip()]
        if not commits:
            return None
        snap = None
        snap_fn = getattr(flow, "snapshot_state", None)
        if callable(snap_fn):
            try:
                snap = snap_fn("reconcile bisect")
            except Exception as exc:  # noqa: BLE001
                log(f"[reconcile] bisect snapshot failed: {exc}")
        probes = 0

        def is_red(index: int) -> bool:
            nonlocal probes
            probes += 1
            try:
                flow.runtime.git.run(["checkout", commits[index], "--", "frontend", "backend"],
                                     check=False)
                summary = self._run_specs(probe, grader_like=True)
            except Exception as exc:  # noqa: BLE001
                log(f"[reconcile] bisect probe raised: {exc}")
                return False
            if summary is None or summary.error:
                return False
            return bool(self._failure_keys(summary) & red_keys)

        culprit_sha: str | None = None
        try:
            if is_red(0):
                # 窗口第一刀就是红的：更早的账这轮翻不到，指认第一刀。
                culprit_sha = commits[0]
            else:
                lo, hi = 0, len(commits) - 1   # commits[lo] 已知绿，hi 未知
                while lo + 1 < hi and probes < probes_budget:
                    mid = (lo + hi) // 2
                    if is_red(mid):
                        hi = mid
                    else:
                        lo = mid
                if lo + 1 == hi:
                    culprit_sha = commits[hi]
                else:
                    log(f"[reconcile] bisect used all {probes} probe(s) without resolving a single "
                        f"commit in a {len(commits)}-commit window")
        finally:
            if snap:
                try:
                    flow.restore_app(snap)
                except Exception as exc:  # noqa: BLE001
                    log(f"[reconcile] bisect restore failed: {exc}")
        if not culprit_sha:
            return None
        message, files = "", []
        try:
            message = (getattr(flow.runtime.git.run(
                ["show", "-s", "--format=%s", culprit_sha], check=False), "stdout", "") or "").strip()
            diff = getattr(flow.runtime.git.run(
                ["diff", "--name-only", f"{culprit_sha}~1", culprit_sha, "--", "frontend", "backend"],
                check=False), "stdout", "") or ""
            files = [line.strip() for line in diff.splitlines() if line.strip()]
        except Exception:  # noqa: BLE001
            pass
        log(f"[reconcile] the newly red spec(s) trace back to {culprit_sha[:8]} {message[:80]!r}"
            + (f" (changed: {', '.join(files[:6])})" if files else ""))
        return {"sha": culprit_sha, "message": message, "files": files}

    def _record_forward_fix(self, culprit: dict | None, red_keys: set[tuple[str, str]]) -> None:
        """修前向：把对账发现的域外红**并进下一次修复**，而不是回滚。

        对账找出的那一刀多半同时修好了它自己那份卷子 —— revert 它会把正确的
        部分一起丢掉，然后在更早的树上继续修。所以默认的处置是记住这份红和
        肇事提交：接下来任何碰到这些文件的批（`_extend_scope_with_forward` 把
        它们并进小域）都被要求**把行为修回来**，提示词里带着肇事提交的原话和
        它改了哪些文件。

        这就是「并入下一批」的物理形态：不新增一条并行通道，只让这批红在既有
        的批修循环里拿到它该有的位置 —— 修好算净胜、弄坏算弄坏，全部走既有
        判据。收尾阶段的全卷域天然覆盖它们，只有说明需要进提示词。
        """
        if not red_keys:
            return
        files = sorted({k[0] for k in red_keys if k[0]})
        self._forward_fix_keys = set(red_keys)
        self._forward_fix_files = set(files)
        if culprit and culprit.get("sha"):
            where = f"  Culprit commit: {culprit['sha'][:10]} {str(culprit.get('message', ''))[:110]}\n"
            if culprit.get("files"):
                where += f"  Files that commit changed: {', '.join(culprit['files'][:10])}\n"
        else:
            where = "  Culprit commit: not identified (the bisect window did not resolve one)\n"
        self._forward_fix_note = (
            f"\nAn earlier accepted change broke the behaviour these spec file(s) exercise: "
            f"{', '.join(files)}.\n{where}"
            f"  This is a **fix forward**: the behaviour must work again, but the change that broke "
            f"it also fixed other things — do not revert it wholesale, restore the behaviour it "
            f"broke. Check the specs above before you commit to the fix.\n")
        log(f"[reconcile] fix-forward recorded for {len(files)} spec file(s): {', '.join(files[:8])}")

    def _revert_culprit(self, culprit: dict | None, red_keys: set[tuple[str, str]]) -> bool:
        """`OCTOS_RECONCILE_REVERT=1` 的对照路：把肇事提交整个 revert 掉。

        默认路是修前向（revert 会连那一刀里**已经正确**的部分一起丢）。这个
        开关留着做 A/B。revert 之后重跑红卷确认：还有红的，剩下的键记进
        修前向 —— 半吊子状态不留在地上。返回 True = 处置已了结（不必兜底）。
        """
        if not culprit or not culprit.get("sha"):
            return False
        flow = self.flow
        sha = culprit["sha"]
        log(f"[reconcile] reverting the culprit commit {sha[:8]} ({str(culprit.get('message', ''))[:80]})")
        try:
            result = flow.runtime.git.run(["revert", "--no-edit", sha], check=False)
        except Exception as exc:  # noqa: BLE001
            log(f"[reconcile] revert raised: {exc}")
            result = None
        if getattr(result, "returncode", 1) != 0:
            try:
                flow.runtime.git.run(["revert", "--abort"], check=False)
            except Exception:  # noqa: BLE001
                pass
            log("[reconcile] git revert did not apply cleanly; falling back to fixing forward")
            return False
        paths = self._spec_paths()
        probe = sorted({paths[name] for name, _ in red_keys if name in paths})
        summary = None
        if probe:
            try:
                summary = self._run_specs(probe, grader_like=True)
            except Exception as exc:  # noqa: BLE001
                log(f"[reconcile] post-revert probe raised: {exc}")
        if summary is not None and not summary.error and summary.all_passed:
            log(f"[reconcile] revert confirmed: {len(probe)} spec file(s) green again")
            return True
        left = red_keys if (summary is None or summary.error) else set(self._failure_keys(summary))
        log(f"[reconcile] revert left {len(left)} test(s) red; folding the rest into the next repair")
        self._record_forward_fix(None, left or red_keys)
        return True

    def _extend_scope_with_forward(self, scope: list[str]) -> list[str]:
        """把小域**并上**对账记账的修前向卷子。

        修前向的红在别人的域里，小域天然看不见 —— 看不见就进不了批修，提示词
        里的说明也就没有着陆点。并进来之后：它们被量、被计进 `scope_fail`，
        修好算净胜、弄坏算弄坏，全部走既有机制，没有第二条判定通道。
        """
        if not self._forward_fix_files:
            return scope
        paths = self._spec_paths()
        out = list(scope)
        for name in sorted(self._forward_fix_files):
            if name in self.voided_specs:
                # 兜底：作废时账本就该销掉这一笔（`_void_specs`）。万一有别的
                # 写入口漏了，这里也不把一张作废的卷子塞回测量。
                continue
            rel = paths.get(name)
            if rel and rel not in out:
                out.append(rel)
        return out

    def _spec_paths(self) -> dict[str, str]:
        """盘上全部卷子：basename → tests_dir 相对路径。

        Playwright 报回来的是 work_dir 下的路径，稳定的口径是 basename（和
        `baseline_red`/`voided_specs` 一样）；这里把它换回能喂给 `run_specs` 的
        相对路径。"""
        tests_dir = getattr(self.flow, "tests_dir", None)
        if not tests_dir or not Path(tests_dir).is_dir():
            return {}
        root = Path(tests_dir)
        return {p.name: str(p.relative_to(root)) for p in root.rglob("*.spec.ts")}

    def _second_pass_check(self, node_id: str, own_specs: list[str], aggregate: bool) -> None:
        """#7 二遍跑检测：刚刚变绿的这几份卷子，**立刻原样复跑一遍**。

        判据与《出卷审计总报告》§四同一条：同一份库、不重置种子跑第二遍 ——
        一份「用完不还原」的卷子（改了密码不回改、建了就假定唯一、拿脏前置态
        当干净的「恢复原状」断言那一类）第二遍必红，而它红的理由不是实现，
        是它自己上一次跑完留下的残渣。实测：sheet 出卷 8/10 份二遍跑红、
        github ≥10 处。

        为什么钉在「首次变绿」这一刻：别处的红都分不清「残渣」还是「真被谁
        弄坏了」；只有这里，上一刻全绿、中间不重置、没有别刀 —— 红了只可能
        是卷子自己留下的，证人证词是干净的。这份证据进 `_nonrestoring_specs`。

        **检测不改判**：验收照走（判分机面对的也是新鲜库里跑第一遍），这批
        卷子照进基线。它只管一件事 —— 让收尾/对账在把同一份卷子的红记到
        实现头上之前先想到「可能是残渣」，并按名单从出厂态复查（`_ghost_reds`）。

        只对自产卷生效（跨项约定）：官方卷是判分机在跑的另一份拷贝，我们
        在这里给它贴标签，改变不了判分看到的任何东西。
        """
        if aggregate or not node_id or node_id in self._secondpass_done:
            return   # 「叶子首次变绿」—— 聚合点的验收不是这条检测的对象
        if not getattr(self.flow, "authored_suite", False) or self._stop():
            return
        specs = [s for s in (own_specs or []) if s]
        if not specs:
            return
        self._secondpass_done.add(node_id)
        # **故意不复位**：第二遍必须看见第一遍留下的东西 —— 那正是这条测量
        # 要看的东西。复位一下再跑，测的就成了别的东西。
        summary = self._run_specs(specs, grader_like=False)
        if summary is None or summary.error or self._app_broken(summary):
            log(f"[second-pass] {node_id}: the re-run could not be measured — no verdict "
                f"on the spec(s)")
            return
        if summary.all_passed:
            log(f"[second-pass] {node_id}: passes fresh and again on the immediate re-run")
            return
        reds = self._failure_keys(summary)
        names = sorted({k[0] for k in reds if k[0]})
        self._nonrestoring_specs.update(names)
        sample = sorted({k[1] for k in reds if k[1]})[:2]
        log(f"[second-pass] **{node_id}: its spec just passed, and fails on an immediate "
            f"re-run** ({summary.passed}/{summary.total}) — {', '.join(names)} does not "
            f"restore what it changes"
            + (f" (e.g. {', '.join(sample)})" if sample else ""))
        self._ledger_note(node_id, tried=[
            f"its spec passes fresh, red on the immediate re-run ({summary.passed}/"
            f"{summary.total}): {', '.join(names)} does not restore what it changes — "
            f"later reds in it are re-checked from factory state before repair"])
        self._ledger_write()

    def _ghost_reds(self, candidates, grader_like: bool,
                    confirmed: set[str] | None = None) -> set[str]:
        """#7 停损：把「二遍跑才红的自产卷」按出厂态**复查后**摘出来。

        只认 `_nonrestoring_specs` 点过名的：其余的红一个字都不碰。对点名的
        也**先复查再摘** —— 复位到出厂态、只跑这几份卷子：

          · 绿了 ⇒ 先前那份红是残渣（它自己上一遍留下的），是**幽灵红**，
            不该记到实现头上、更不该拿修复回合去够它；
          · 仍旧红 ⇒ 出厂态都过不了的真红，照旧留在红集里给修复。

        复查的前提是复位，所以复查本身会动运行时状态（复位 + 这几份卷子跑一遍
        留下的）—— 与 `_accept_batched` 入口的出厂态验收同一手，不是新动作。

        `confirmed`：本趟已复查过的残渣集。命中的直接认，不再花第二次复查 ——
        复查要跑卷子，别让一份残渣在同一趟收尾里反复收费。
        """
        suspects = {f for f in (candidates or ()) if f} & self._nonrestoring_specs
        if not suspects or not getattr(self.flow, "authored_suite", False):
            return set()
        known = {f for f in suspects if f in (confirmed or ())}
        fresh_ones = suspects - known
        if fresh_ones:
            paths = self._spec_paths()
            names = sorted(f for f in fresh_ones if f in paths)
            if names:
                self._reset_runtime_state(
                    "re-checking a spec that does not restore what it changes")
                summary = self._run_specs([paths[n] for n in names], grader_like=grader_like)
                if summary is not None and not summary.error and not self._app_broken(summary):
                    still_red = {k[0] for k in self._failure_keys(summary) if k[0]}
                    found = sorted(set(names) - still_red)
                    if found:
                        known.update(found)
                        log(f"[second-pass] ghost red(s) confirmed from factory state — "
                            f"{', '.join(found)} does not restore what it changes; not "
                            f"charged to implementation repair")
                    if still_red:
                        log(f"[second-pass] {', '.join(sorted(still_red))} is still red from "
                            f"factory state — a real red, staying charged")
                else:
                    log(f"[second-pass] the ghost re-check of {', '.join(names)} could not "
                        f"be measured — they stay charged")
        return known

    def _accepted(self, result: NodeResult, specs: list[str]) -> NodeResult:
        """验收通过的三行 —— 与旧路径逐字一致（置位、提交、进基线）。

        外加一句 P2 的对账门铃：每次验收都是一次 keep（`_commit` 产生了一个
        新提交），攒够几次之后就该跑一次全卷对账了 —— 小域视野看不见的红，
        只有全卷能看见（见 `_maybe_reconcile`）。
        """
        result.own_passed = result.passed = result.baseline_ok = True
        self._commit(result.node_id)
        self._extend_baseline(specs)
        self._reconcile_keeps += 1
        self._maybe_reconcile(result.node_id)
        return result

    # -- 基线 -------------------------------------------------------------

    def _extend_baseline(self, specs: list[str]) -> None:
        """刚通过的东西加入基线。**基线只增不减** —— 这就是不变式 ③ 的一半。"""
        for spec in specs:
            if spec not in self._baseline_seen:
                self._baseline_seen.add(spec)
                self.baseline_specs.append(spec)

    def _scope_specs(self, node_id: str, aggregate: bool, since: str | None = None) -> list[str]:
        """这次验收要面对多大范围的基线。

        **全量基线很贵** —— 每跑一次都要起前端构建加一次浏览器。所以全量只在两处跑：

          * **聚合点**：一个父节点结案的时候。全树 20–26 次，可接受。
          * **碰了共享文件**：契约里 `shared` 点名的那些文件，碰了就当它能弄坏任何人。

        其余时候只检查**依赖它的那些节点的 spec** —— 那是它唯一能弄红的东西，
        而这个集合是**已知的**，就是从契约和需求树算出来的 `forward_deps`。

        `since` 是调用方开工时的 HEAD，透传给 `_touched_shared`：分批修复会在节点
        中途提交，porcelain 对已提交的改动是干净的，只有 `since` 能让共享文件检查
        重新看见那些改动（见 `_touched_shared`）。
        """
        live = [s for s in self.baseline_specs if Path(s).name not in self.voided_specs]
        if aggregate or self._touched_shared(since=since):
            return live
        affected: set[str] = set()
        for dep in forward_deps(self.layers, node_id):
            affected.update(self.flow.spec_map.get(dep) or [])
        return sorted(affected & set(live))

    def _touched_shared(self, since: str | None = None) -> bool:
        """这个节点碰了契约点名的共享文件吗？

        `since` 是**这个节点开工前的 HEAD**。分批修复会在节点中途提交（每批一个
        验证点），提交之后 `git status --porcelain` 对那一批的改动就是干净的 ——
        共享文件检查会瞎掉，而它是唯一会把范围放大到全量基线的机制（瞎了就等于
        共享文件随便动）。给了 `since` 就换成 `git diff --name-only <since>`：
        「自开工以来动过哪些已跟踪文件」（含已提交的批），未跟踪的新文件再补一遍
        porcelain 的 `??` 行。不给 `since` 保持旧行为（porcelain 全量），
        既有调用点的语义一字不变。
        """
        shared = (self.contract.global_contract or {}).get("shared") or []
        if not isinstance(shared, list) or not shared:
            return False
        names = set()
        for item in shared:
            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            names.add(Path(text.strip().strip("`\"'")).name)
        changed: set[str] = set()

        def add_path(raw: str) -> None:
            path = raw.strip()
            if not path:
                return
            if " -> " in path:          # 重命名：两端都算碰过
                old, new = path.split(" -> ", 1)
                changed.add(Path(old.strip().strip('"')).name)
                path = new
            changed.add(Path(path.strip().strip('"')).name)

        try:
            # `git status --porcelain` 而不是 `git diff --name-only HEAD`（`since`
            # 没给的时候）。后者只列**已跟踪文件的修改**，看不见新建的文件 ——
            # 而节点在验收之前是不提交的（提交发生在 `_accept` 里），所以
            # 「新建一个共享模块」这个最常见的形态，在这道检查眼里是隐形的。
            # 契约点名的恰恰是这种东西：一个被很多节点共用的文件，多半是这次
            # 才被创建出来的。
            #
            # 失效的后果不是「少查一次」：这条是唯一会把范围放大到**全量基线**的
            # 机制（`_scope_specs` 的另一半），它瞎了，动共享文件的节点就只查
            # 「依赖我的那几条」，而它弄坏的多半在别的分支上。
            #
            # porcelain 的每一行是 `XY <path>`，重命名是 `XY <old> -> <new>`；
            # 两种都取路径部分。它同时覆盖已跟踪的改动和 `??` 未跟踪的新文件。
            if since:
                out = getattr(self.flow.runtime.git.run(["diff", "--name-only", since],
                                                        check=False), "stdout", "") or ""
                for line in out.splitlines():
                    add_path(line)
                out = getattr(self.flow.runtime.git.run(["status", "--porcelain"],
                                                        check=False), "stdout", "") or ""
                for line in out.splitlines():
                    if line.startswith("??"):
                        add_path(line[3:] if len(line) > 3 else line)
            else:
                out = getattr(self.flow.runtime.git.run(["status", "--porcelain"],
                                                        check=False), "stdout", "") or ""
                for line in out.splitlines():
                    add_path(line[3:] if len(line) > 3 else line)
        except Exception:  # noqa: BLE001
            return False
        return bool(changed & names)

    def _can_repair(self, deadline_guard: float, need: float | None = None) -> bool:
        """还有没有本钱再来一轮。**和旧路径同一条判据**：一轮修复要花多少是有观察值的。

        `need` 允许调用方给出**这一种回合**的观察值（批回合比整段修复短，
        见 `main.Flow.batch_repair_needs`）；不给就用整段修复的。
        """
        flow = self.flow
        if self._stop():
            return False
        if need is None:
            need = float(getattr(flow, "min_repair_seconds", 300))
            try:
                need = float(flow.repair_needs())
            except Exception:  # noqa: BLE001
                pass
        left = min(flow.remaining(), deadline_guard - time.time())
        if left < need:
            log(f"[layer] {left:.0f}s left, below the {need:.0f}s a repair needs — stopping repairs")
            return False
        return True

    def _accept_verdict(self, node_id: str, scope_state: tuple | None,
                        red_before: set[str] | None) -> str | None:
        """最近一次小域观察说「能不能验收」。没观察过（没小域/跑不起来）→ None 放行，
        与旧路径「基线跑不起来不算破坏」同义。判责用**开工那一刻**的红名单。"""
        if scope_state is None:
            return None
        summary, failing, prior = scope_state
        return self._scope_verdict(node_id, summary, failing, prior, red_before)

    def _probe_scope(self, scope: list[str]) -> "tuple | None":
        """跑一次小域并记账，返回 (summary, 红文件集, 跑前红名单)；跑不起来返回 None

        （与旧 `_baseline_break` 的软失败语义一致：跑不起来不算破坏/不拦验收）。

        #5：允许复用 —— 这是**背靠背重复**的正主（对账刚在同一个 sha 上量完整卷，
        紧接着聚合验收又来量同一批卷子；日志实锤 4m49s 的一对）。"""
        summary = self._run_specs(scope, grader_like=True, allow_reuse=True)
        if summary is None or summary.error:
            return None
        failing, prior = self._absorb_scope(scope, summary)
        return summary, failing, prior

    def _absorb_scope(self, scope: list[str], summary) -> "tuple[set[str], set[str]]":
        """记账半段 —— 这一轮跑到的卷子，它们的最新状态就是这里看到的：

          · 这轮红的           → 进红名单
          · 这轮绿的、且在名单里 → 摘掉（可能是后来某个节点修好了）
          · 这轮没跑的          → 保留上一次的观察，不猜

        返回 (这轮红的文件集, 跑之前的红名单)。从 `_baseline_break` 抽出来，
        是因为分批修复要在**每次批后**小域量分时记账 —— 但批级判定用的是
        测试级净胜（`_failure_keys`），不是这里的「谁弄红的」判词。

        「没跑到的用例」（`_never_ran`）在这本账里的位置：**当红是冤枉，当绿是
        假绿**，所以两头都必须划掉 —— 一份卷子只有**看全了**才算「这轮见过」，
        看得完整、且这轮不红，才准从红名单上摘掉。判红即停把红卷子截断之后，
        它照旧红着（进红名单），但永远不会被误摘。"""
        observed = {Path(s).name for s in scope}
        failing = {Path(r.file or "").name for r in summary.results
                   if not r.ok and not _never_ran(r)}
        seen = {Path(r.file or "").name for r in summary.results}
        partial = {Path(r.file or "").name for r in summary.results if _never_ran(r)}
        whole = (seen & observed) - partial      # 报回来了、请求跑过、一条没漏
        prior = set(self.baseline_red)
        self.baseline_red = (self.baseline_red - whole) | failing
        # 修前向的账在这里销：这轮**看全了的**绿卷子里有它 -> 那部分红已经消失，
        # 摘掉；全摘完了连说明一起清 —— 不然残留的说明会挂在后续无关的批上。
        # 没看全的不算绿：没跑到的用例里可能正有它。
        if self._forward_fix_keys:
            green = whole - failing
            kept = {(name, title) for name, title in self._forward_fix_keys if name not in green}
            if kept != self._forward_fix_keys:
                self._forward_fix_keys = kept
                if not kept:
                    self._forward_fix_files = set()
                    self._forward_fix_note = ""
                    log("[reconcile] the fix-forward spec(s) are green again; its note is dropped")
        return failing, prior

    def _scope_verdict(self, node_id: str, summary, failing: set[str], prior: set[str],
                       red_before: set[str] | None) -> str | None:
        """小域一轮的结果 → 判词：None = 可以验收；否则把**谁被我弄红了**说清楚。

        这是不变式 ③ 的失败信息 —— 它天然带视野：模型不需要被告知「要小心」，
        它直接看到自己破坏了什么。

        `red_before` 是**这个节点开工时**的红名单。给了就用它判「谁弄的」，
        不给了就拿当前这份（`prior`，跑之前那一刻的名单 —— 直接调用时的老行为）。
        见 `_accept_verdict` 的说明：判据必须钉在开工那一刻，否则修复轮会把责任洗掉。

        纯函数：不改任何状态（记账在 `_absorb_scope` 里做过了）。分批路径
        （`_accept_batched`）复用它来定验收。
        """
        before = prior if red_before is None else red_before
        newly_red = failing - before
        pre_existing = failing & before
        if summary.all_passed:
            return None
        if not newly_red:
            # 红的每一份都是**早就红的**。它不是这个节点弄的 —— 不再冤枉它，
            # 也不让它为一个不是自己造成的失败花掉修复轮（那正是连坐的起点）。
            log(f"[{node_id}] baseline is still red on {len(pre_existing)} pre-existing spec(s) "
                f"({', '.join(sorted(pre_existing)[:6])}); not charging this node for them")
            return None
        grouped = nodes_for_failures([r for r in summary.results
                                      if not r.ok and Path(r.file or "").name in newly_red],
                                     self.flow.spec_map)
        named = [k for k in grouped if k]
        if named:
            lines = [f"{len(newly_red)} spec file(s) that were passing before you started are "
                     f"failing now ({summary.passed}/{summary.total} still pass)."]
        else:
            lines = [f"Your change made {len(newly_red)} spec file(s) fail that were passing "
                     f"before you started ({summary.passed}/{summary.total} still pass)."]
        # **别替模型编一个它没做过的因果。**
        #
        # 记账挡住了「同一条红卷子连续冤枉后来人」，但**第一个看见它的人仍然要接**——
        # 因为不接就没人修。可它未必是肇事者：红卷子挂在别的分支上，而按依赖图
        # 这个节点根本不在它的上游（那多半是某个节点悄悄弄红了自己没查的那部分，
        # 见 `_scope_specs` 的空范围）。对着「Your change made...」去找一个自己没做的
        # 改动，修复轮就是白烧的。
        #
        # 所以：说清楚我们知道的（卷子是谁的、图上够不着），要求照旧（不绿不验收）。
        # 注意这**不是**免责 —— 依赖图是个模型不是事实，碰过共享文件的节点够得着任何人，
        # 所以仍然要它修。
        mine = set(forward_deps(self.layers, node_id)) | {node_id}
        foreign = sorted(str(k) for k in named if k not in mine)
        if foreign:
            lines.append(f"They belong to node(s) {', '.join(foreign[:6])}, which the dependency "
                         f"graph does not put downstream of you — so this may well be a change "
                         f"another node made without noticing. It still has to be green before you "
                         f"can be accepted: find the real cause first, then fix the code.")
        if pre_existing:
            # 说清楚哪些**不是**它的责任。不然模型会去修它们 —— 修不好，
            # 而它自己的修复轮就这么没了。
            lines.append(f"Ignore these {len(pre_existing)} spec file(s): they were already red "
                         f"before this node started and are not yours to fix "
                         f"({', '.join(sorted(pre_existing)[:6])}).")
        if named:
            lines.append("It broke: " + ", ".join(sorted(str(n) for n in named)[:12]))
        else:
            lines.append("It broke specs that are not mapped to a single node.")
        lines.append("These passed before your change. Fix your code so they pass again — do not "
                     "edit their specs, and do not delete the thing they exercise.")
        detail = failure_summaries(summary)
        if detail:
            lines.append(detail)
        return "\n".join(lines)

    def _baseline_break(self, node_id: str, aggregate: bool,
                        red_before: set[str] | None = None,
                        since: str | None = None) -> str | None:
        """基线还绿吗？红了就把**谁被我弄红了**说清楚（见 `_scope_verdict`）。

        `red_before`/`since` 的语义见 `_scope_verdict`/`_touched_shared`。
        """
        if not self.baseline_specs:
            return None  # 基线还是空的，没什么可破坏的
        scope = self._scope_specs(node_id, aggregate, since=since)
        if not scope:
            return None  # 这段时间里没有别的节点依赖我
        got = self._probe_scope(scope)
        if got is None:
            return None  # 基线跑不起来不算破坏
        summary, failing, prior = got
        return self._scope_verdict(node_id, summary, failing, prior, red_before)

    def _commit(self, node_id: str) -> None:
        self.flow.commit(f"{node_id}: accepted")

    # -- 修复提示词 -------------------------------------------------------

    def _repair_prompt(self, node_id: str, failures: str, aggregate: bool,
                       spec_files: list[str] | None = None) -> str:
        template = AGGREGATE_REPAIR_TURN if aggregate else NODE_REPAIR_TURN
        text = template.format(
            node_id=node_id,
            requirement=self._requirement_block(node_id),
            failures=failures,
            baseline_note=self._baseline_note(),
            contract=node_contract_block(self.contract, self.layers, node_id),
            forward=forward_deps_block(self.layers, node_id),
        )
        return (text + SMALL_STEP_NOTE + self._spec_defect_note()
                + self._impl_interface_note(node_id, spec_files)
                + self._ledger_block(node_id))

    def _impl_interface_note(self, node_id: str,
                             spec_files: list[str] | None = None) -> str:
        """实现→卷子 的静态接口对账，贴进修复回合（2026-10-08，只提示）。

        26/30 归因：判分方按 **role/名字** 找人，而实现可能无谓地把自家语义摘掉
        （原生 <button> 盖 role="menuitem"）或让装饰字形混进 textContent —— 两样
        都是静态读盘就看得见的形状，却没有任何现有机制会说出口：卷子→需求的
        `role_unbacked` 是反方向的，自家卷子也未必查那个角色。这一块把发现贴在
        失败清单旁边（提示，不是闸门）：不阻塞、不占轮次、不新增任何测量。

        范围只取这批失败卷子 + 本节点需求原文 —— 说得越具体越不像泛泛的唠叨。
        每次 (HEAD, 文件集) 只算一遍（`_impl_iface_cache`）；任何异常都当作
        「没有发现」——它是增值，不是前置。
        """
        try:
            from main import impl_interface_warnings   # 延迟导入：main ↔ 本模块是环
            flow = self.flow
            app_dir = str(getattr(flow, "output_dir", "") or "")
            tests_dir = Path(str(getattr(flow, "tests_dir", "") or ""))
            if not app_dir or not str(tests_dir):
                return ""
            files = spec_files
            if files is None:
                files = list((getattr(flow, "spec_map", None) or {}).get(node_id) or [])
            if not files:
                files = list(self._all_specs())[:24]
            if not files:
                return ""
            try:
                head = flow.head() or ""
            except Exception:  # noqa: BLE001
                head = ""
            cache_key = (head, tuple(sorted(Path(str(f)).name for f in files)))
            if cache_key not in self._impl_iface_cache:
                specs_text: dict[str, str] = {}
                for name in files[:24]:
                    # 生产口径是 tests_dir 相对路径（`_all_specs`）；个别 fixture 用
                    # "tests/x.spec.ts" 前缀 —— 直接路径取不到时退回按文件名找。
                    for candidate in (tests_dir / str(name), tests_dir / Path(str(name)).name):
                        try:
                            specs_text[Path(str(name)).name] = candidate.read_text(encoding="utf-8")
                            break
                        except OSError:
                            continue
                try:
                    requirement = flow.repair_requirements(node_id) or ""
                except Exception:  # noqa: BLE001
                    requirement = ""
                found = impl_interface_warnings(app_dir, specs_text,
                                                requirement_text=requirement)
                lines = [line for key in ("role_overrides", "glyph_text", "role_missing")
                         for line in (found.get(key) or [])]
                self._impl_iface_cache[cache_key] = lines
                if lines:
                    log(f"[impl-iface] {len(lines)} static interface warning(s) for "
                        f"{node_id} ride this repair turn (hints only, no gate)")
            lines = self._impl_iface_cache[cache_key]
            if not lines:
                return ""
            return ("\nInterface cross-check on this workspace (mechanical, read from the "
                    "spec files above and the frontend source on disk — evidence to weigh, "
                    "not a verdict):\n"
                    + "\n".join(f"- {line}" for line in lines) + "\n")
        except Exception:  # noqa: BLE001
            return ""

    def _requirement_block(self, node_id: str) -> str:
        """节点需求**原文** —— 修复回合的脊柱（#6 主脊换位）。

        与诊断回合同一来源（`flow.repair_requirements` = `describe_node` 的原文，
        一个字不筛；「抽取哪句重要」是主观判断，判错了没有任何测试会发现）。
        修复提示词此前只有失败清单和架构契约，任务陈述事实上是「让这些卷子
        变绿」—— 实测的后果是模型围着断言转：改一错一片、三轮回合耗尽、
        区域连坐（正赛那条杀伤链）。卷子红是**证据**，需求原文才是目标；这段
        把目标搬回主位，卷子降为证据。

        取不到原文（假 flow / 聚合节点不在需求树里）就不说这段 —— 不编内容；
        小步纪律（`SMALL_STEP_NOTE`）与它无关，照常拼在提示词尾部。

        上限 8000 是量出来的（两套真题全树复算）：github 70 节点最长 7397、
        sheet 28 节点最长 4247 —— 8000 之下**没有一个节点会被截断**，而 4000
        会切掉 8 个节点（7+1）的正文尾巴。与诊断块（`_diagnosis_requirement_block`）
        同口径：这是目标原文，截掉了就不是原文了。
        """
        try:
            text = self.flow.repair_requirements(node_id) or ""
        except Exception:  # noqa: BLE001
            text = ""
        text = text.strip()
        if not text:
            return ""
        return ("\nThe requirement text this work is held to — **this is the goal**, quoted "
                "verbatim:\n\n" + _clip(text, 8000) + "\n\n"
                "The failing specs below are **evidence about your code, not the goal**. A red "
                "spec tells you one thing the behaviour does not satisfy yet: work out which "
                "part of the requirement text above that is, and fix the code to match the "
                "text.\n")

    def _spec_defect_note(self) -> str:
        """把「卷子判错了」这条出口**说出来** —— 但只在它真的开着的时候。

        不说的后果是这条路一行都不会被走到（模型不知道它存在）；对着官方卷子
        说的后果更糟：它会让模型以为「过不去可以主张卷子错了」，而那条主张在
        `_maybe_void_spec` 里会被直接驳回，白烧一轮修复。

        所以提示词里的话和代码里的闸门**必须同源**：这里说出口开着，
        那边就真的开着（自产卷子 + 没超预算）。
        """
        if not getattr(self.flow, "authored_suite", False):
            return ""
        if len(self.voided_specs) >= _MAX_SPEC_VOIDS:
            return ""
        return ("\nIf — and only if — you have already tried twice and the code genuinely satisfies "
                "the requirement text, but the spec asserts something the **requirement document "
                "does not say**, say `SPEC DEFECT: <what the spec asserts that the requirement "
                "does not>` and stop, naming your evidence either way — the exact requirement "
                "text it contradicts, or the offending assertion as `<spec file>:<line>`. An "
                "assertion no implementation could ever pass (the spec's own code guarantees "
                "the failure) is the third form: the harness verifies that one by reading the "
                "spec itself, so the requirement text need not back it. Only your own spec "
                "can be voided this way, and only with one of those forms named as evidence.\n")

    def _baseline_note(self) -> str:
        """底线的完整清单 —— **已知通过**的那些 spec。

        `baseline_red` 里的不算：它们名义上还在基线里（还在被跑、还可能被修好），
        但「这些已经通过了，别弄红」对它们来说是句假话 —— 它们本来就是红的。
        把它们列进来，模型会花修复轮去查一份跟它无关的旧账。

        **作废的也不算**（与 `_scope_specs`/`_all_specs` 同一口径）：一份摘掉的
        卷子被写进「别弄红」清单，等于告诉模型去护一个已经不存在的验收 ——
        而且是它读盘读不到的（文件还在，但没人再跑它）。
        """
        if not self.baseline_specs:
            return ""
        green = [s for s in self.baseline_specs
                 if Path(s).name not in self.baseline_red
                 and Path(s).name not in self.voided_specs]
        if not green:
            return ""
        names = ", ".join(Path(s).name for s in green[:40])
        more = f" (+{len(green) - 40} more)" if len(green) > 40 else ""
        note = (f"\nThe baseline is the {len(green)} spec file(s) that already pass "
                f"({names}{more}). **Before you commit to a change, check that none of them turns "
                f"red.** They are on disk — read the ones your change could touch.\n")
        if self.baseline_red:
            note += (f"({len(self.baseline_red)} further spec file(s) in the baseline are known to be "
                     f"failing and are not yours to fix: "
                     f"{', '.join(sorted(self.baseline_red)[:6])}.)\n")
        return note

    # -- v5 台账：谁绿谁红、为什么红、试过哪几招、下一个修谁 ------------------
    #
    # 「一直大量修」的机制解释：每一轮的提示词只带着**这一次**的失败说话，上一轮
    # 试过什么、为什么没成、现在轮到谁，全在编排的局部变量里，模型看不见 ——
    # 于是每一轮都在从头再猜一次。台账把这份记忆落成文件（人看得见），并且**贴回
    # 提示词**（模型看得见）：修复回合从「再猜一次」变成「在已知之上往前走」。

    def _ledger_note(self, node_id: str, status: str | None = None, why: str = "",
                     tried: list[str] | None = None, hint: str = "") -> None:
        """往台账上记一笔。只写不判 —— 判据在各自的闸门里，这里只是记忆。"""
        if not node_id:
            return
        entry = self.ledger.setdefault(
            node_id, {"status": "open", "why": "", "tried": [], "hint": ""})
        if status:
            entry["status"] = status
        if why:
            entry["why"] = _clip(" ".join(str(why).split()), 240)
        for item in tried or []:
            item = _clip(" ".join(str(item).split()), 140)
            if item and item not in entry["tried"]:
                entry["tried"].append(item)
        del entry["tried"][6:]   # 只留最近六招：更早的早被更新的证据取代了
        if hint:
            entry["hint"] = _clip(" ".join(str(hint).split()), 240)

    def _ledger_block(self, node_id: str) -> str:
        """贴进提示词的台账摘录。没记录（第一次碰它）就返回空串。"""
        entry = self.ledger.get(node_id)
        if not entry:
            return ""
        lines = [f"\nThe harness keeps a ledger for `{node_id}`; this is already recorded — "
                 f"do not spend this turn re-establishing any of it:"]
        lines.append(f"- state: {entry.get('status', 'open')}")
        if entry.get("why"):
            lines.append(f"- why it is not green: {entry['why']}")
        if entry.get("tried"):
            lines.append("- already tried: " + "; ".join(entry["tried"]))
        if entry.get("hint"):
            lines.append(f"- diagnosis so far: {entry['hint']}")
        return "\n".join(lines) + "\n"

    def _ledger_write(self) -> None:
        """台账落盘（`<workspace>/.arc/ledger.md`，每回合读写）。

        写失败不打断施工 —— 和快照同一条纪律：辅助动作绝不变成新的故障点。
        """
        try:
            path = self.design_dir.parent / "ledger.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            order = list(getattr(self.layers, "order", []) or [])
            keys = [k for k in order if k in self.ledger]
            keys += sorted(k for k in self.ledger if k not in order)
            greens = [k for k in keys if self.ledger[k].get("status") == "green"]
            nxt = next((k for k in keys if k not in greens), "")
            rows = ["# 施工台账", "",
                    f"- 记在账上的要求点：{len(keys)} 个；绿：{len(greens)} 个",
                    f"- 下一个要修的：{nxt or '（没有待修的）'}", "",
                    "| 要求点 | 状态 | 为什么 | 试过 | 下一个动作 |",
                    "| --- | --- | --- | --- | --- |"]
            for key in keys:
                entry = self.ledger[key]
                rows.append("| {} | {} | {} | {} | {} |".format(
                    key,
                    entry.get("status", ""),
                    (entry.get("why", "") or "").replace("|", "/"),
                    "；".join(entry.get("tried") or []).replace("|", "/"),
                    "先修这个" if key == nxt else "",
                ))
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log(f"[ledger] could not write the ledger: {exc}")

    # -- v5 诊断队列：搁浅的红，一次一件，机器主动查三方 ----------------------

    def _queue_diagnosis(self, key: str, node_id: str, files: list[str],
                         reason: str, phase: str) -> None:
        """把一个修不动的红放进诊断队列（幂等：同一个 key 只进一次）。

        队列是**记账**，不是回合 —— 诊断自己花回合，所以有上限（`_diagnosis_cap`）
        和预算闸门（`diagnose_parked` 里问 `_can_repair`）。
        """
        if not _diagnose_enabled() or not key:
            return
        if key in self._diagnosis_done or key in self._diagnosis:
            return
        self._diagnosis[key] = {"node_id": node_id or "", "files": [f for f in files if f],
                                "reason": reason or "", "phase": phase}
        log(f"[diagnosis] queued {key}: {reason or 'parked'}")

    def _item_voided(self, item: dict) -> bool:
        """这件要诊断的红**整件都已作废**了吗 —— 是就别开回合。

        一件里只要还有一份活着的卷子，诊断照做；文件全在作废集里才算。
        （作废是「它从此不存在」：卷子摘了、节点按需求文本验收了。）
        """
        files = [f for f in (item.get("files") or []) if f]
        return bool(files) and all(f in self.voided_specs for f in files)

    def diagnose_parked(self, phase: str) -> bool:
        """把这一阶段搁置的红过一遍诊断。返回**是否动过树**（动了就要复量）。

        三件事在这里对齐（见 `DIAGNOSE_TURN`）：需求原文 vs 卷子断言 vs 实现。
        处置只有三种（`_diagnose_item`）：卷子错 → 取证后作废；实现难 → 就地补/
        回炉一次；理解跑题 → 记档、重读需求重做一次。
        """
        if not _diagnose_enabled():
            return False
        pending = [k for k, item in self._diagnosis.items() if item.get("phase") == phase]
        if not pending:
            return False
        changed = False
        done = 0
        deadline_guard = time.time() + max(300.0, self.flow.remaining() * 0.5)
        for key in pending:
            if self._stop() or done >= _diagnosis_cap():
                break
            if not self._can_repair(deadline_guard, need=self._batch_needs()):
                break
            item = self._diagnosis.pop(key, None)
            if item is None:
                continue
            if self._item_voided(item):
                # 这件红**整件都已作废**（卷子摘了、节点按需求文本验收了）——
                # 再为它开一个诊断回合是纯白烧。记成做过，别回队。
                self._diagnosis_done.add(key)
                log(f"[diagnosis] {key}: every spec on it was voided — nothing left to diagnose")
                continue
            # 先记成「做过」再处理：诊断给的红**不会**第二次进队列，
            # 循环因此有界（回炉也只会发生一次）。
            self._diagnosis_done.add(key)
            done += 1
            try:
                changed = self._diagnose_item(key, item) or changed
            except Exception as exc:  # noqa: BLE001
                log(f"[diagnosis] {key}: the diagnosis turn broke ({exc})")
        self._ledger_write()
        return changed

    def _diagnose_item(self, key: str, item: dict) -> bool:
        """诊断一件。返回是否动过树。"""
        flow = self.flow
        node_id = item.get("node_id") or ""
        files = list(item.get("files") or [])
        label = node_id or (", ".join(files) or key)
        by_name = {Path(p).name: p for paths in flow.spec_map.values() for p in (paths or [])}
        specs = [by_name[f] for f in files if f in by_name]
        if not specs and node_id:
            specs = list(flow.spec_map.get(node_id) or [])
        requirement = ""
        try:
            requirement = flow.repair_requirements(node_id or None) or ""
        except Exception:  # noqa: BLE001
            requirement = ""
        digest = ""
        if node_id:
            try:
                path = Path(flow.tests_dir) / "_requirements" / f"{node_id}.md"
                if path.is_file():
                    digest = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                digest = ""
        summary = self._run_specs(specs, grader_like=False) if specs else None
        if (summary is None or summary.error) and specs:
            summary = self._run_specs(specs, grader_like=True)
        if summary is not None and not summary.error:
            failures = failure_summaries(summary,
                                         files=set(files) if files else None)
        else:
            failures = ("The failing spec(s) could not be run just now. The last recorded reason: "
                        + str(item.get("reason") or "unknown"))
        prompt = DIAGNOSE_TURN.format(
            label=label,
            req_dir=getattr(flow, "req_dir", "") or "(the requirement documents)",
            requirement=_diagnosis_requirement_block(requirement, digest),
            specs=(", ".join(Path(s).name for s in specs)
                   or "(none — no spec file is mapped to this requirement yet)"),
            failures=failures,
            ledger=self._ledger_block(node_id) if node_id else "",
        )
        # 诊断是**只读 + 短回合**（#4b/#6a）：读文件、下判断、指出改哪儿，改由
        # 后面的修复回合做。实证见 `_diagnosis_timeout`。
        self._turn(prompt, f"diagnose {label}", timeout=self._diagnosis_timeout(),
                   read_only=True)
        self._diagnosis_turns += 1
        kind, claim = _diagnosis_verdict(self.last_turn_text)
        log(f"[diagnosis] {label}: {'no verdict' if kind == 'none' else kind} — {claim[:200]!r}")
        if kind == "spec":
            self._ledger_note(node_id or key, tried=["diagnosis: it says the spec is wrong"],
                              hint=claim)
            return self._void_from_diagnosis(node_id, claim, specs)
        if kind == "none":
            self._ledger_note(node_id or key, tried=["diagnosis turn"],
                              hint="the diagnosis turn ended without a verdict")
            return False
        # 实现问题 / 理解跑题：诊断回合是**只读**的（#4b）——它改不了树，
        # 「就地量一遍看绿没绿」那一步在这里没有意义，直接走回炉窗口。
        if not node_id:
            # 收尾那批诊断：只读回合没动过树，复量由收尾自己的全量复核去做。
            self._ledger_note(key, tried=["diagnosis turn"], hint=claim)
            return False
        # **给它一次重新开工的窗口**（每个要求点只给一次）——「回炉」。
        # 诊断已经把病根写出来了，这一轮不再是从头猜。`_accept_batched` 里
        # 净胜判定、快照回退、一次一题的粒度全部照常生效。
        if node_id and node_id not in self._requeued:
            specs_now = list(flow.spec_map.get(node_id) or [])
            if specs_now:
                self._requeued.add(node_id)
                self._off_track[node_id] = claim
                self._ledger_note(node_id, tried=[f"diagnosis: {claim}"], hint=claim)
                log(f"[diagnosis] {node_id}: one fresh repair window, on the diagnosed cause")
                result = self._accept_batched(node_id, specs_now, f"{node_id} (diagnosed)",
                                              aggregate=False, rework=True)
                if result.passed:
                    self._ledger_note(node_id, status="green", why="accepted after the diagnosis")
                    self._ledger_write()
                    self._aggregate_from(node_id)
                else:
                    self._ledger_note(node_id, status="parked", why=result.reason,
                                      tried=["diagnosis + one repair window"])
                    self._ledger_write()
                return True
        self._ledger_note(node_id, status="parked", why=claim, tried=["diagnosis turn"], hint=claim)
        return True

    def _void_specs(self, node_id: str, specs: list[str]) -> None:
        """作废一份卷子的**全部记账**，一处收口 —— 作废的意思是「它从此不存在」。

        只 `voided_specs.update()` 是不够的：作废之前，全卷对账可能已经把同一批
        卷子记进了「修前向」账本（`_forward_fix_files`），而那个账本会被
        `_extend_scope_with_forward` 并进之后的**每一个**小域 —— 作废的卷子于是
        又回到测量里，白烧轮次（彩排实测：REQ-7-2-1 作废后又被量 3 轮，约 45s）。
        所有提到它的账一起销：作废集、节点名下的映射、红名单、修前向账本与说明。

        这是**整节点**口径（模型主张「我这份卷子写错了」时，摘的是它名下的
        全部）；只摘点名几份的用法见 `_auto_void_dead_specs`，它复用同一套
        记账收口（`_drop_voided_bookkeeping`）。
        """
        names = {Path(s).name for s in specs}     # 与 `baseline_red` 同口径（basename）
        self.flow.spec_map.pop(node_id, None)     # 只摘自己名下的
        self._drop_voided_bookkeeping(names)

    def _drop_voided_bookkeeping(self, names: set[str]) -> None:
        """作废的**记账**收口（映射之外的四处）：作废集、红名单、修前向账本与说明。"""
        self.voided_specs.update(names)
        self.baseline_red -= names
        touched = names & self._forward_fix_files
        if touched:
            kept = {(name, title) for name, title in self._forward_fix_keys if name not in names}
            self._forward_fix_keys = kept
            self._forward_fix_files = {k[0] for k in kept}
            if not kept:
                self._forward_fix_note = ""
            else:
                # 说明里点名的文件随账本一起换 —— 修前向的提示词只许说还欠着的红。
                at = self._forward_fix_note.find("  Culprit commit:")
                if at >= 0:
                    self._forward_fix_note = (
                        "\nAn earlier accepted change broke the behaviour these spec file(s) "
                        f"exercise: {', '.join(sorted(self._forward_fix_files))}.\n"
                        + self._forward_fix_note[at:])
            log(f"[reconcile] the voided spec(s) {', '.join(sorted(touched))} are dropped from "
                f"the fix-forward book; {len(kept)} file(s) stay on it")

    def _auto_void_dead_specs(self, names: list[str], only_owner: str | None = None) -> list[str]:
        """#5b 卷子坏 → **自动改道申诉**：机器验过的死卷子不进实现修复队列。

        与模型主张那条出口（`_maybe_void_spec`）共用取证纪律，但两处门槛**本就
        该不同**：那是一句主张，所以要「先给代码两轮机会」；这是**机器自己读盘
        验过的结构性缺陷**（`spec_structural_defects` 只认语言层面「任何实现都
        过不了」的写法），拿实现去修一个语言上必然失败的断言，是白烧回合 ——
        没有人情要给，也不需要模型开口。

        只作废**点名的那几份**（`_void_specs` 是整节点口径，会 pop 掉整个映射；
        节点名下若还有活卷子，必须留住它们）。作废预算 `_MAX_SPEC_VOIDS` 照旧
        合用 —— 这是一条能删验收的通道，不管住它就成了「过不去就删卷子」。

        `names` 是 basename（与 `baseline_red`/`voided_specs` 同口径）；
        `only_owner` 给定时只在它名下作废（节点入口用），不给就按 spec_map 找属主
        （收尾全卷用）。返回真正被作废的名字。
        """
        if not names or not getattr(self.flow, "authored_suite", False):
            return []   # 官方卷子是判分机在跑的另一份拷贝，本地作废它只是把自己弄瞎
        owners: dict[str, list[str]] = {}
        for node, paths in list(self.flow.spec_map.items()):
            for path in (paths or []):
                name = Path(path).name
                if name in names:
                    owners.setdefault(name, []).append(node or "")
        voided: list[str] = []
        for name in names:
            if len(self.voided_specs) + len(voided) >= _MAX_SPEC_VOIDS:
                log(f"[void] the void budget ({_MAX_SPEC_VOIDS}) is spent — {name} stands")
                break
            mine = owners.get(name) or [""]
            if only_owner is not None and only_owner not in mine:
                continue
            first = self._mechanical_defects_for(name)
            log(f"[void] **VOIDING A MECHANICALLY DEAD SPEC** ({name}, owner "
                f"{', '.join(o or '(none)' for o in mine)}): it cannot pass for any "
                f"implementation — {first[:220]}")
            for owner in mine:
                own = list(self.flow.spec_map.get(owner) or [])
                kept = [s for s in own if Path(s).name != name]
                if kept:
                    self.flow.spec_map[owner] = kept
                else:
                    self.flow.spec_map.pop(owner, None)
            self._drop_voided_bookkeeping({name})
            self._ledger_note(mine[0], tried=[
                f"its spec {name} was voided as mechanically dead (no implementation can "
                f"pass it): {first[:100]}"])
            self._ledger_write()
            voided.append(name)
        return voided


    def _void_from_diagnosis(self, node_id: str, claim: str, specs: list[str]) -> bool:
        """诊断回合说「卷子错了」—— 和修复回合的主张走**同一套取证**。

        唯一的门槛差别：修复路径要求「先给代码两轮机会」（`_SPEC_DEFECT_FROM_ROUND`），
        诊断本来就发生在修不动之后，那道人情的门已经过了。取证的闸门一道不减 ——
        作废一份卷子就是删掉一条验收，凭一句主张不够，要有**白纸黑字**：
        需求文档里的原文（正向）、**机器自己验过的结构性缺陷**
        （`_mechanical_defect_evidence`：那卷子对任何实现都红，不需要需求背书），
        或模型指认的卷子行（反向，见 `_nonexistence_evidence`）。
        """
        if not node_id or not getattr(self.flow, "authored_suite", False):
            return False
        if len(claim or "") < _MIN_SPEC_DEFECT_CHARS:
            log(f"[diagnosis] {node_id}: the spec-defect claim is too thin to check "
                f"({len(claim or '')} chars) — the spec stands")
            return False
        if len(self.voided_specs) >= _MAX_SPEC_VOIDS:
            log(f"[diagnosis] {node_id}: the void budget ({_MAX_SPEC_VOIDS}) is spent — "
                f"the spec stands")
            return False
        specs = list(specs or self.flow.spec_map.get(node_id) or [])
        if not specs:
            return False
        evidence = self._requirement_evidence(_defect_tokens(claim))
        if evidence:
            log(f"[diagnosis] **VOIDING THE SPEC** ({node_id}): {claim[:200]!r}")
            for line in evidence[:4]:
                log(f"[diagnosis]   the requirement document says: {line}")
        else:
            mech = self._mechanical_defect_evidence(specs)
            if mech:
                log(f"[diagnosis] **VOIDING THE SPEC** ({node_id}; its own shell cannot pass for "
                    f"any implementation): {claim[:200]!r}")
                for line in mech[:4]:
                    log(f"[diagnosis]   the harness scanner read: {line}")
            else:
                hits = self._nonexistence_evidence(claim, specs)
                if not hits:
                    log(f"[diagnosis] {node_id}: no evidence — neither the requirement document nor a "
                        f"named spec line backs the claim; the spec stands: {claim[:140]!r}")
                    return False
                log(f"[diagnosis] **VOIDING THE SPEC** ({node_id}; the requirement is silent, the "
                    f"named spec line is the evidence): {claim[:200]!r}")
                for line in hits[:4]:
                    log(f"[diagnosis]   the spec line it names: {line}")
        self._void_specs(node_id, specs)
        scope = self._scope_specs(node_id, False)
        scope_state = self._probe_scope(scope) if scope else None
        verdict = self._accept_verdict(node_id, scope_state, set(self.baseline_red))
        if verdict is not None:
            self._ledger_note(node_id, status="parked", why=verdict,
                              tried=["diagnosis: spec voided"], hint=claim)
            self._ledger_write()
            log(f"[diagnosis] {node_id}: the spec was voided, but the baseline says otherwise "
                f"({verdict})")
            return True
        self._accepted(NodeResult(node_id=node_id), [])
        self.verdict[node_id] = True      # 与验收路径同口径，父节点才等得到它
        self._ledger_note(node_id, status="green",
                          why="its spec was voided as defective; accepted on the requirement text",
                          tried=["diagnosis: spec defect"], hint=claim)
        self._ledger_write()
        self._aggregate_from(node_id)
        return True

    # -- 契约缺陷：主张 → 取证 → 改（或不改） -------------------------------

    def _maybe_amend_contract(self, label: str) -> None:
        """模型说契约错了。**可以听，但主张先过取证。**

        事实是 spec 和需求文档 —— 那才是被判分的东西，契约只是它们的转述。
        所以裁决顺序是：抽出争议的对象 → 去文件里找它 → 文件站哪边就按哪边办。
        文件根本没提到的，一句主张不足以改契约。

        这就是「模型之间有矛盾怎么办」的答案：不看是谁说的、也不看说得多肯定，
        看文件里写着什么。
        """
        if self._amending or not self.contract.path:
            return
        claim = _defect_claim(self.last_turn_text)
        if not claim:
            return
        if len(self.amendments) >= self.max_amendments:
            log(f"[contract] {label} reports a defect again ({claim[:120]!r}) but the "
                f"amendment budget ({self.max_amendments}) is spent — the contract stands")
            return
        evidence = self._fact_evidence(_defect_tokens(claim))
        if not evidence:
            log(f"[contract] {label} claims a defect that nothing in the specs or the "
                f"requirement document supports: {claim[:160]!r} — the contract stands, "
                f"fix the code to match it")
            return
        log(f"[contract] {label} claims the contract is wrong: {claim[:160]!r}")
        for line in evidence[:4]:
            log(f"[contract]   the files say: {line}")
        self.amendments.append(claim)
        self._amending = True
        try:
            self._amend_contract(claim, evidence)
        finally:
            self._amending = False

    def _maybe_void_spec(self, node_id: str, round_index: int) -> str | None:
        """卷子判错了的出口。返回被作废的 spec 名（没作废就是 None）。

        **要解决的问题是「永远过不去」。** 自产卷子是我们自己写的模型产物，
        它可能断言了一个需求文档里根本没有的东西（错误的无障碍名字、错的路由、
        错的字段）。这时候代码是对的、卷子是错的 —— 而 `_accept` 只会一轮一轮
        地修代码，修不动就 `exhausted`，然后 `_abandon_region` 把**整个区域**
        连坐判死。惩罚落在一堆没做错的节点上，且日志里看不出区别。

        四道闸门，缺一不可：

          · **只有自产卷子**。官方卷子是判分机在跑的另一份拷贝，本地作废它只是
            把自己弄瞎 —— 隐藏测试照跑照判。
          · **第 2 轮之后**。前两轮先当代码问题修；「没做出来」比「卷子写错了」
            常见得多，先给代码公平的机会。
          · **主张要有据，三条通道**。①正向：抽取争议对象 → 去**需求文档**里
            找（`_fact_evidence` 是 spec + 需求文档都找，这里只认需求文档：
            拿卷子自己证明卷子错了，是循环论证）。②机械：卷子自身有**任何实现
            都过不了**的结构性缺陷（`_mechanical_defect_evidence`，机器读盘自己
            验的，不需要需求背书 —— 需求写没写都改变不了那条断言对每个实现都
            红）。③反向：「需求**没**说它」型主张 —— 关键对象按定义就不在需求
            文档里，证据在模型指认的**卷子行**上（`_nonexistence_evidence`：
            行号真实存在 + 指认处是断言行）。**只留正向通道时，「需求没说的
            东西」永远拿不出证据、永远驳回 —— 实测 2 次真实主张全死在这。**
            三条通道都拿不出证据，才驳回。
          · **有预算**（`_MAX_SPEC_VOIDS`）。这是一条能删验收的通道，不管住它，
            它就成了「过不去就把卷子删了」。

        作废是**记账**不是删文件：卷子留在盘上（还看得见、还能复盘），
        只是从这个节点的名下摘掉、并从基线里划掉。
        """
        if not getattr(self.flow, "authored_suite", False):
            return None
        claim = _spec_defect_claim(self.last_turn_text)
        if not claim:
            return None
        if round_index < _SPEC_DEFECT_FROM_ROUND:
            log(f"[{node_id}] claims its spec is wrong ({claim[:90]!r}) but this is repair round "
                f"{round_index + 1} — the code gets a fair try first; the spec stands")
            return None
        if len(self.voided_specs) >= _MAX_SPEC_VOIDS:
            log(f"[{node_id}] claims its spec is wrong again ({claim[:90]!r}) but the void budget "
                f"({_MAX_SPEC_VOIDS}) is spent — the spec stands, fix the code")
            return None
        specs = list(self.flow.spec_map.get(node_id) or [])
        if not specs:
            return None
        evidence = self._requirement_evidence(_defect_tokens(claim))
        if evidence:
            log(f"[{node_id}] **VOIDING ITS OWN SPEC**: {claim[:200]!r}")
            for line in evidence[:4]:
                log(f"[{node_id}]   the requirement document says: {line}")
        else:
            # 第二条通道：**机器自己验过的**结构性缺陷 —— 卷子按语言规则对每个
            # 实现都红（`_mechanical_defect_evidence`）。这条不需要需求背书。
            mech = self._mechanical_defect_evidence(specs)
            if mech:
                log(f"[{node_id}] **VOIDING ITS OWN SPEC** (its own shell cannot pass for any "
                    f"implementation): {claim[:200]!r}")
                for line in mech[:4]:
                    log(f"[{node_id}]   the harness scanner read: {line}")
            else:
                # 第三条通道：「需求**没**说它」型主张。证据按定义不在需求文档里
                # （不在，正是主张的内容），在模型指认的**卷子行**上 —— 读盘验证
                # 那一行真实存在、且真的是断言行。见 `_nonexistence_evidence`。
                line_hits = self._nonexistence_evidence(claim, specs)
                if not line_hits:
                    log(f"[{node_id}] claims its spec is wrong but neither the requirement document "
                        f"nor a valid spec-line reference backs it: {claim[:140]!r} — the spec "
                        f"stands, fix the code")
                    return None
                log(f"[{node_id}] **VOIDING ITS OWN SPEC** (the requirement is silent on it; the "
                    f"named spec line is the evidence): {claim[:200]!r}")
                for line in line_hits[:4]:
                    log(f"[{node_id}]   the spec line it names: {line}")
        self._void_specs(node_id, specs)
        log(f"[{node_id}] specs voided and removed from the baseline: {specs} — the node is now "
            f"accepted on the requirement text and the baseline check alone")
        return specs[0]

    def _requirement_evidence(self, tokens: list[str], limit: int = 8) -> list[str]:
        """去**需求文档**里找这些 token。

        和 `_fact_evidence` 的分别不是细节：那条同时查 spec 和需求文档，因为
        契约是两者的转述；这一条**只查需求文档**。要让「这份卷子写错了」成立，
        得需求文档说了算 —— 拿卷子自己证明卷子错，是循环论证。
        """
        hits: list[str] = []
        root = getattr(self.flow, "req_dir", None)
        if not tokens or not root or not Path(root).is_dir():
            return hits
        for path in sorted(Path(root).rglob("*")):
            if not path.is_file() or path.suffix.lower() not in _EVIDENCE_EXTS:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if any(token in line for token in tokens):
                    hits.append(f"{path.name}:{number}: {line.strip()[:220]}")
                    if len(hits) >= limit:
                        return hits
        return hits

    def _mechanical_defect_evidence(self, specs: list[str]) -> list[str]:
        """第二条取证通道：**机器自己验过的**结构性缺陷（#4 申诉疏通）。

        `spec_structural_defects`（main）只认语言层面「任何实现都过不了」的写法
        —— `expect(…)` 直接拿到 async 助手的 Promise，Playwright 对**每一个**
        实现都红。它是读卷子文件本身得出的结论，不经过任何模型主张：主张撞上它
        时，证据不是「模型说卷子错了」，是「这卷子按语言规则对每个实现都红」，
        因此**不需要需求原文正向背书** —— 需求写没写，都改变不了这条断言对每个
        实现失败的事实。（「与契约打架」那一半不在此列：#2② 实测冻结契约只到
        页面/路由粒度，可机械否定的控件级声明不存在，无判据不落码 ——
        《修复施工记录》事件 2。）

        `specs` 是 spec_map 的名字（tests_dir 相对路径，见 `authored_spec_map`）。
        """
        hits = [f"{name}: {line}"
                for name, lines in self._mechanical_defects(specs).items()
                for line in lines]
        return hits[:4]

    def _mechanical_defects(self, specs) -> dict[str, list[str]]:
        """读盘扫描：{卷子 basename: [缺陷说明…]} —— 只认「任何实现都过不了」的写法。

        扫描器本身在 `main.spec_structural_defects`（纯文本判据，无外部状态）；
        这里负责把名字解析成盘上的文件（basename 与 tests_dir 相对路径都收）。
        """
        from main import spec_structural_defects   # 延迟导入：main ↔ 本模块是环
        tests_dir = getattr(self.flow, "tests_dir", None)
        out: dict[str, list[str]] = {}
        for spec in specs or []:
            path = Path(spec)
            if not path.is_file() and tests_dir:
                path = Path(tests_dir) / spec
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found = spec_structural_defects(text)
            if found:
                out[path.name] = found
        return out

    def _mechanically_dead_files(self, files) -> set[str]:
        """这批文件里哪几份是**机器验过的死卷子**（basename 集合，见 #5b）。"""
        return set(self._mechanical_defects(list(files or [])))

    def _mechanical_defects_for(self, name: str) -> str:
        """单份卷子的第一句缺陷说明（没查出就是空串）—— 作废时的日志与账本用。"""
        lines = self._mechanical_defects([name]).get(Path(str(name)).name) or []
        return lines[0] if lines else ""

    def _nonexistence_evidence(self, claim: str, specs: list[str]) -> list[str]:
        """反向取证：「需求没说的东西」型主张，证据在**卷子的行**上找。

        正向取证（`_requirement_evidence`）要求争议对象出现在需求文档里。
        但主张若是「卷子断言了需求**没有**的东西」，关键对象按定义就不在需求
        文档里 —— 拿正向谓词裁它，结论永远是 "does not mention"，出口形同
        关闭（实测：模型引 `REQ-4-3-3.spec.ts:40-41`，这串东西在需求文档里
        永远搜不到，两次主张全死在这一步）。这类主张的可查证结构是**行号
        指认**，而指认本身是可以读盘核实的：

          · 引用的是本节点名下的卷子（只摘自己名下的，同既有防线）；
          · 行号在文件里真实存在（读盘验证，不认空口）；
          · 指认处确实是断言行（含 `expect(`）—— 点名一行 import 不算数。

        三条都过，证据行原文进日志、主张受理；任何一条不过，返回空，
        按原逻辑驳回（卷子站着，继续修代码）。这不是「多信模型一点」，
        是补上一条本来就该有的取证方向：「需求没写什么」和「需求写了什么」
        一样是可查证的 —— 卷子行里写着争议对象，需求文档里搜不到它。
        """
        hits: list[str] = []
        if not claim:
            return hits
        own = {Path(s).name for s in specs}
        tests_dir = getattr(self.flow, "tests_dir", None)
        if not tests_dir or not Path(tests_dir).is_dir():
            return hits
        seen: set[str] = set()
        for match in _SPEC_LINE_REF.finditer(claim):
            name = Path(match.group(1)).name
            if name in seen or name not in own:
                continue
            seen.add(name)
            first = int(match.group(2))
            last = int(match.group(3) or first)
            if last < first:
                first, last = last, first
            path = Path(tests_dir) / name
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            if first < 1 or first > len(lines):
                continue
            window = lines[first - 1:min(last, first + 20)]
            for offset, line in enumerate(window):
                if "expect(" in line:
                    hits.append(f"{name}:{first + offset}: {line.strip()[:220]}")
                    break
            if len(hits) >= 4:
                break
        return hits

    def _fact_evidence(self, tokens: list[str], limit: int = 8) -> list[str]:
        """去 spec 和需求文档里找这些 token —— **事实在文件里，不在谁的话里**。"""
        hits: list[str] = []
        if not tokens:
            return hits
        for root in (self.flow.tests_dir, self.flow.req_dir):
            if not root or not Path(root).is_dir():
                continue
            for path in sorted(Path(root).rglob("*")):
                if not path.is_file() or path.suffix.lower() not in _EVIDENCE_EXTS:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if any(token in line for token in tokens):
                        hits.append(f"{path.name}:{number}: {line.strip()[:220]}")
                        if len(hits) >= limit:
                            return hits
        return hits

    def _amend_contract(self, claim: str, evidence: list[str]) -> None:
        """**定向改那一处，不是重跑 L0。** 重跑一遍蓝图要读全部 spec，那是整个 run
        里最贵的几个回合之一；而争议通常只是某条路由的返回形状。"""
        self._turn(CONTRACT_AMEND_TURN.format(
            contract_path=self.contract_path,
            claim=claim,
            evidence="\n".join(evidence),
        ), "contract amend", timeout=self._long_turn())
        data = read_contract_file(self.contract_path)
        if not data:
            log("[contract] the amendment produced no readable contract; the one in force stands")
            return
        defects = contract_defects(data, self.layers)
        self.contract.global_contract = data
        log(f"[contract] amended: {summarize(data)}"
            + (f"; structural defects remain: {defects[:4]}" if defects else ""))
        # 已经按旧形状建好的节点现在可能对不上了 —— 那正是基线的活：谁的 spec 红了
        # 就修谁。这里只把话说清楚，不替它们改。
        log("[contract] nodes already built are now held to the amended shape; "
            "the baseline check will name any that no longer match")

    # -- 小工具 -----------------------------------------------------------

    def _turn(self, prompt: str, label: str, timeout: int,
              read_only: bool = False) -> tuple[bool, str]:
        """一个回合。异常绝不能成为压垮编排的那一环。

        回合的回话在这里落一次 —— 编排平时**不读模型说了什么**（信息都走文件：
        契约落盘、spec 跑分），只读一件事：它有没有报契约缺陷。

        `read_only=True`：这一回合把写类和 shell 工具从**请求体**里拿掉（见
        `main.turn` 的同名参数）——「别改」写在提示词里是可忽略的建议，
        工具面里没有它才是做不到。诊断回合用这一档（#4b）。

        结局的两个标志（超时 / 判失败）留给调用方决定「这一刀要不要退」：
        没跑完的回合盘上留的是半成品，两个标志各是一种没跑完（#5e）。
        """
        started = time.time()
        try:
            ok, text = self.flow.turn(prompt, max(60, int(timeout)), label,
                                      read_only=read_only)
        except Exception as exc:  # noqa: BLE001
            log(f"[layer] turn `{label}` raised: {exc}")
            self._last_turn_timed_out = False
            self._last_turn_failed = True
            return False, ""
        elapsed = time.time() - started
        # v5 超时止损：这一轮到底是被墙钟**烧完**的，还是自己交的活？
        # 判据：没跑成，且花掉的时间贴着上限（下限 30s 防止把「秒退」算成超时）。
        # 只有贴脸的那一种才走止损 —— 普通的失败照旧按加宽重试处理。
        limit = max(60, int(timeout))
        timed_out = bool(not ok and elapsed >= max(30.0, limit * 0.9))
        if timed_out:
            log(f"[layer] turn `{label}` was still running when the {limit}s wall clock ran "
                f"out ({elapsed:.0f}s spent)")
        self.last_turn_text = text or ""
        self._maybe_amend_contract(label)
        # 契约修订回合是 `_turn` 的**嵌套调用**（`_amend_contract` 也走这里），
        # 会把这两个标志覆盖成它自己的结局。回到外层时必须还原 —— 调用方读的是
        # **外层回合**的结局，否则一次修订就把「这一刀要不要退」的判据换掉了。
        self._last_turn_timed_out = timed_out
        self._last_turn_failed = not ok
        return ok, text

    def _run_specs(self, specs: list[str], grader_like: bool,
                   workers: int | None = None, fail_fast: bool = False,
                   allow_reuse: bool = False) -> object | None:
        """**每一次测量都走这里** —— 观测运行时状态文件和卷子绿名单的唯一入口（#5f/#1）。

        测量散在七八处（节点验收 / 抢修 / 全卷 / 对账探针），观测只挂一处：
        每多绕开一次，出厂态的观测集就少看见一个文件、绿名单就少记一笔账。

        `fail_fast` 判红即停（透传给 `Flow.run_specs`）：**只有对账主跑挂它**
        —— 那里要的是「哪份卷子红了」。喂净胜账的测量（批后小域、二分探针、
        出厂态复查）一条红都不能少看：挂了会把「看得见的红」变少，净胜判成
        假的（`_failure_keys` 的交集空掉 → 假净胜/假二分，见 `_run_reconcile`）。

        `allow_reuse`（#5 同树同卷集复用，2026-10-08 提速批）：这一次测量可以拿
        「**同一棵干净树上、同一批卷子**」的上一次结果顶账（钥匙与档见
        `_measure_state_key` / `_reuse_measurement`）。默认关 —— 只有**验证性**
        的测量开它（小域基线 `_probe_scope`、对账主跑、节点自己的首测）；故意的
        复测（`_second_pass_check` / `_ghost_reds` / 二分探针 / 收尾全卷量）
        **一律不开**，它们的价值就在「再量一次」。
        """
        if not specs:
            return None
        state = self._measure_state_key() if (allow_reuse and _reuse_enabled()) else None
        if state is not None:
            hit = self._reuse_measurement(state, grader_like, specs)
            if hit is not None:
                self._note_runtime_files(hit)
                self._note_spec_observations(hit)
                return hit
        try:
            summary = self.flow.run_specs(specs, workers=workers, grader_like=grader_like,
                                          fail_fast=fail_fast)
        except Exception as exc:  # noqa: BLE001
            log(f"[layer] spec run raised: {exc}")
            return None
        self._note_runtime_files(summary)
        self._note_spec_observations(summary)
        if state is not None:
            self._remember_measurement(state, grader_like, summary)
        return summary

    # -- #5 同树同卷集复用（2026-10-08 提速批） ------------------------------

    def _measure_state_key(self) -> str | None:
        """复用的钥匙：**干净树**上的 HEAD。脏树 / 问不出来 → None（不复用）。

        干净树 ⇒ 树内容就是 HEAD 的内容，两次测量的起点逐字节相同 —— 同一把
        钥匙下的测量可以互相顶账。树一脏，「这次量的是哪份内容」就没有名字了，
        那份测量谁也认领不了：**宁可多量一次，不拿别处的绿灯顶这一棵树的账**。
        """
        try:
            status = self.flow.runtime.git.run(["status", "--porcelain"], check=False)
            if (getattr(status, "stdout", "") or "").strip():
                return None
            head = str(self.flow.head() or "").strip()
        except Exception:  # noqa: BLE001
            return None
        return head or None

    def _reuse_measurement(self, state: str, grader_like: bool, specs: list[str]):
        """这个状态上量过这些卷子吗？量过 → 把那份结果原样拼回来（一个数不改）。

        **并集覆盖**才算命中：请求里每一份卷子都得在档里，少一份就整个不复用
        —— 半份旧 + 半份新拼出来的像，下游会当成一次完整测量记账。
        """
        entry = self._measure_memo.get((state, grader_like))
        if not entry:
            return None
        files = entry.get("files") or {}
        names = [Path(s).name for s in specs]
        if any(name not in files for name in names):
            return None
        results = [r for name in names for r in files[name]]
        if not results:
            return None
        passed = sum(1 for r in results if r.ok)
        log(f"[reuse] the tree at {state[:10]} with these {len(names)} spec file(s) was "
            f"measured already — reusing that measurement ({passed}/{len(results)}), "
            f"nothing re-run")
        return RunSummary(passed=passed, total=len(results), results=results,
                          stores_written=list(entry.get("stores") or []))

    def _remember_measurement(self, state: str, grader_like: bool, summary) -> None:
        """把这次测量存进档，按卷子分装 —— 同一棵树上的下一次请求可以顶账。

        不进档的三种：基建错（`error`/`killed`，那不是测量）、一份卷子都没报
        回来的、**被截断过的**（`_never_ran`：这一份没看完；存进去，下一次就
        拿半截当完整测量用了）。旧结果被新结果覆盖（同一棵树上量了两次，
        后一次才算最新的真实）。
        """
        if summary is None or getattr(summary, "error", None) \
                or getattr(summary, "killed", False):
            return
        per_file: dict[str, list] = {}
        cut: set[str] = set()
        for result in getattr(summary, "results", None) or []:
            name = Path(getattr(result, "file", "") or "").name
            if not name:
                continue
            if _never_ran(result):
                cut.add(name)
                continue
            per_file.setdefault(name, []).append(result)
        for name in cut:
            per_file.pop(name, None)
        if not per_file:
            return
        key = (state, grader_like)
        entry = self._measure_memo.get(key)
        if entry is None:
            entry = {"files": {}, "stores": []}
            self._measure_memo[key] = entry
            while len(self._measure_memo) > _MEMO_STATES:
                oldest = next(iter(self._measure_memo))
                if oldest == key:
                    break
                self._measure_memo.pop(oldest, None)
        entry["files"].update(per_file)
        stores = {str(s).replace("\\", "/") for s in (getattr(summary, "stores_written", None) or [])}
        if stores:
            entry["stores"] = sorted(set(entry["stores"]) | stores)

    # -- #1 绿过点名体检：记账与点名（2026-10-08 提速批） --------------------

    def _note_spec_observations(self, summary) -> None:
        """这一次测量给两本账留下的痕迹：量过的进 `observed_specs`，见过绿灯的
        进 `ever_green_specs` / `ever_green_tests`（只增不减 —— 绿过的卷子、绿过
        的用例永远是回归的候选，哪怕后来红了）。

        证据取**报回来的用例**（`results`），不是「请求跑的清单」：整批里前一份
        文件的红把后面的文件整份截成「没跑到」（`_never_ran`）、隔离循环中途
        起不来会停下 —— 这些都不许记成量过，下一轮它们还得按「从没量过」全量
        看见。宁可多量一份，不许漏看一份。

        判红即停的截断在本函数里是**允许**的：一份卷子在第一条红处被截断，
        只说明「它的开头是红的」—— 这一份的信息量已经到手了（它是红的、而且
        在体检里重跑一遍也只会再红一次）。等它的开头真绿了，那是它属主节点的
        测量（不挂判红即停）记的账，它同时回到绿名单。
        """
        per_file: dict[str, dict[str, int]] = {}
        for result in getattr(summary, "results", None) or []:
            name = Path(getattr(result, "file", "") or "").name
            if not name:
                continue
            slot = per_file.setdefault(name, {"ran": 0, "ok": 0})
            if _never_ran(result):
                continue
            slot["ran"] += 1
            if result.ok:
                slot["ok"] += 1
                self.ever_green_tests.add((name, getattr(result, "title", "") or ""))
        for name, slot in per_file.items():
            if slot["ran"] == 0:
                continue                      # 一份结果都没有：这一份根本没量到
            self.observed_specs.add(name)
            if slot["ok"]:
                self.ever_green_specs.add(name)

    def _sweep_specs(self, candidates: list[str]) -> list[str]:
        """#1 绿过点名体检：全量体检的范围 = 曾经绿过 ∪ 从没量过。

        回归只能发生在「曾经绿过」的卷子上（从没绿过的东西没有「变红」一说），
        而「从没量过」的必须付第一遍钱 —— 第一遍看见之后，红着的就转出体检
        名单，直到它绿过一次（那时它自动回来），或它的属主节点重新量它。

        只用在**日常体检**（对账）的候选集上：收尾/封箱的全卷量不做过滤 ——
        那是成绩单，一份都不能漏。
        """
        kept: list[str] = []
        skipped: list[str] = []
        for spec in candidates:
            name = Path(spec).name
            if name in self.ever_green_specs or name not in self.observed_specs:
                kept.append(spec)
            else:
                skipped.append(name)
        if skipped:
            log(f"[reconcile] sweep covers {len(kept)}/{len(candidates)} spec file(s): "
                f"green-before ∪ unseen (skipped {len(skipped)} never-green)")
        return kept

    # -- #5f 出厂态验收 -----------------------------------------------------

    def _note_runtime_files(self, summary) -> None:
        """把这次测量看到的「应用自己在运行时写过的文件」并进观测集。

        判据是**盘上证据**（`stores_written` = 一次 spec run 之后 `frontend/backend`
        上的 `git diff --name-only`）—— 不按文件名猜：`data.json` 只是我们这一族
        题目的样子，不是判据。换个题目换个名字，这条规则照样成立。

        上限 12 是 `mutated_by_tests` 的显示上限（那份清单同时供提示词的干扰说明
        用）；真 app 的运行时文件是 1~3 个，够用。
        """
        for rel in getattr(summary, "stores_written", None) or []:
            rel = str(rel).replace("\\", "/").strip()
            if rel:
                self._runtime_files.add(rel)

    def _factory_state_sha(self) -> str | None:
        """出厂参照 = 仓库的**根提交**（run 开始的那棵树）。

        实勘：平台模板不带 `.git`（`extracted-initial/*` 只有 .gitignore + 前后端），
        `ensure_repo` 的 `init` 提交就是「一条回合都还没跑过」的那一刻（D0 冒烟仓
        最底下正是 `init`）。拿它当参照，而不是「第一次测量时看到的样子」——
        后者可能已经被上一个回合写脏了。

        解析一次就缓存；解不出来（假 git / 仓库异常 / 多根且不干净）就当没有
        出厂态：这条路整体静默退出 —— 辅助动作绝不变成新的故障点（与快照同一条纪律）。
        """
        if self._factory_sha is not None:
            return self._factory_sha or None
        sha = ""
        try:
            res = self.flow.runtime.git.run(["rev-list", "--max-parents=0", "HEAD"], check=False)
            roots = (getattr(res, "stdout", "") or "").split()
            if len(roots) == 1:
                # 多根（模板自带历史的极端情况）不猜：宁可不复位，也不退到一棵
                # 不是「run 开始」的树上。
                sha = roots[0]
        except Exception as exc:  # noqa: BLE001
            log(f"[factory] could not resolve the factory commit: {exc}")
        self._factory_sha = sha
        return sha or None

    def _git_rc(self, args: list[str]) -> int:
        """复位用的 git 退出码。异常按 0（=「没差别」）处理 —— 辅助动作绝不
        因为 git 的一次抽风把一整轮验收搅黄。"""
        try:
            res = self.flow.runtime.git.run(args, check=False)
        except Exception:  # noqa: BLE001
            return 0
        return int(getattr(res, "returncode", 0) or 0)

    def _reset_runtime_state(self, why: str) -> bool:
        """#5f 出厂态复位：把观测到的运行时状态文件退回**出厂内容**。

        为什么（正赛 github 实锤）：旧 `data.json` × 新集合代码 → 启动即崩、连坐
        29 张。口径是「每卷从出厂态起跑」—— 判分环境预置的就是出厂数据（需求原文：
        the system pre-provisions …），拿我们跑了一半的状态称量，称出来的是
        「新代码 × 我们的残渣」，那不是判分机会看到的东西。

        **复位 ≠ 改种子**：只退「应用自己在运行时写过的文件」（观测集，见
        `_note_runtime_files`），模型对源码/种子的改动不在这条路上（红线）。

        返回「真的改了东西」—— 双态判据要用它（见 `_accept_batched` 的入口）：
        改了说明盘上那份「随包态」与出厂态确实不同。没改动时是一两次 git 查询
        就返回（绝大多数轮次走这条），不付测量的钱。
        """
        files = sorted(self._runtime_files)
        if not files:
            return False           # 一个运行时文件都没观测到：不去打扰 git
        sha = self._factory_state_sha()
        if not sha:
            return False
        if self._git_rc(["diff", "--quiet", sha, "--", *files]) == 0:
            return False           # 与出厂一致：不用动
        changed: list[str] = []
        for rel in files:
            if self._git_rc(["diff", "--quiet", sha, "--", rel]) == 0:
                continue
            self._git_rc(["checkout", sha, "--", rel])
            changed.append(rel)
        if changed:
            log(f"[factory] runtime state reset to its factory content before {why}: "
                f"{', '.join(changed)}")
        return bool(changed)

    def _all_specs(self) -> list[str]:
        """盘上全部卷子，**减去被作废的**。

        作废的卷子必须在这里也排除掉，否则那条路等于没走：节点那关放行了，
        终检又拿同一份错卷子把整个 run 判红 —— 「永远过不去」只是从节点推迟
        到了终检，而终检**没有修复轮**。

        排除不是「删掉」，是「不拿它评分」：文件还在，日志里有一句带需求原文
        的 VOIDING 记录，事后可查。
        """
        tests_dir = getattr(self.flow, "tests_dir", None)
        if not tests_dir or not Path(tests_dir).is_dir():
            return []
        return sorted(str(p.relative_to(tests_dir))
                      for p in Path(tests_dir).rglob("*.spec.ts")
                      if p.name not in self.voided_specs)

    def _implement_timeout(self, index: int, total: int) -> int:
        """节点份额 —— 和旧路径同一套账（结余、上限、地板）。"""
        flow = self.flow
        nodes_left = max(1, total - index + 1)
        try:
            surplus = float(flow.banked_surplus(index, total))
        except Exception:  # noqa: BLE001
            surplus = 0.0
        node_budget = min(flow.node_budget_cap, max(240.0, flow.remaining() / nodes_left + surplus))
        try:
            reserve = (1.0 - flow.implement_fraction) * node_budget
            if reserve >= flow.repair_needs():
                return int(min(flow.node_timeout, flow.implement_fraction * node_budget))
        except Exception:  # noqa: BLE001
            pass
        return int(min(flow.node_timeout, node_budget))

    def _repair_timeout(self) -> int:
        try:
            return max(60, int(min(self.flow.node_timeout, self.flow.repair_needs() * 1.5)))
        except Exception:  # noqa: BLE001
            return 600

    def _diagnosis_timeout(self) -> int:
        """诊断回合的墙钟上限：**短**（#4b/#6a）。

        实证（10-07 晚两条线）：走通的那次诊断 273s 就交了判决；两次失败的都烧满
        修复档的上限（675s）、顺手写了文件、判决为空。只读之后这一回合只剩
        「读 + 判断」，短上限够用 —— 烧满也是白烧。`OCTOS_DIAGNOSE_TIMEOUT` 可校准。
        """
        try:
            cap = max(60, int(os.environ.get("OCTOS_DIAGNOSE_TIMEOUT", "300")))
        except ValueError:
            cap = 300
        return min(self._repair_timeout(), cap)

    def _long_turn(self) -> int:
        """蓝图 / 骨架 / 展开 —— 这三类回合要读很多东西，给足时间。"""
        try:
            return max(300, int(min(self.flow.node_timeout, self.flow.remaining() * 0.25)))
        except Exception:  # noqa: BLE001
            return 900

    def _title(self, node_id: str) -> str:
        info = self.layers.by_id.get(node_id)
        if info is None:
            return node_id
        node = info.node
        for key in ("name", "title", "description"):
            value = node.get(key)
            if value:
                return str(value).strip().splitlines()[0][:110]
        return node_id


# ---------------------------------------------------------------- 纯函数

def _never_ran(result) -> bool:
    """这一条用例**根本没跑** —— 判红即停的截断在报告里留下的形状。

    实测（冒烟8，Playwright 1.63）：运行被提前停掉时，还没轮到的用例在 JSON
    报告里**有 spec 条目、没有 results**，`summarize_report` 于是填上
    `status="unknown"` —— 全库唯一一种「没有真实结果」的形状。它既不是红
    也不是绿：

      · 当红看 —— 整批模式（`OCTOS_ARC_FILE_ISOLATION=0`）下，第一条红之后
        每一份卷子都会整份变成「没跑的用例」，「域外新红」由 1 份炸成一片，
        二分与修前向都被这堆假红带偏；
      · 当绿看 —— 会把一个红透的卷子记成绿过。

    没有证据就是没有证据：判红、判绿、记账三处都要把它划掉。
    """
    return getattr(result, "status", "") == "unknown"


# #1/#2/#5 提速批（2026-10-08）。#5 复用的存档深度：只留最近这么多个「干净树
# 状态」的测量。TestOutcome 带着 rendered_page（整页可访问性快照），一份卷子
# 几十条用例，深了是白占内存 —— 而真正的重复永远是**背靠背**的那几对
# （对账刚量完的全卷 → 紧接着的聚合验收），深度 2~4 已经覆盖全形。
_MEMO_STATES = 4


def _reuse_enabled() -> bool:
    """#5 同树同卷集复用的总开关（`OCTOS_REUSE_MEASUREMENTS=0` 关）。

    留着它是因为这条优化的价值要用 **本地镜像 A/B** 实测校准（复用开/关各跑
    一遍，差出来的就是省下的），也是万一它在真机上出意外的急停绳。
    """
    return os.environ.get("OCTOS_REUSE_MEASUREMENTS", "1") != "0"


_DEFAULT_404 = re.compile(r"Cannot (GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)", re.I)
_PARAM = re.compile(r"/:[^/]+")
_STAR = re.compile(r"/\*.*$")

# 模型报契约缺陷的句式。提示词让它说 `CONTRACT DEFECT: <what>`。
_DEFECT = re.compile(r"CONTRACT\s+DEFECT\s*:?\s*(.{0,300})", re.I | re.S)
# 报**卷子**判错了的句式（只对自产卷子开放，见 `_maybe_void_spec`）。
_SPEC_DEFECT = re.compile(r"SPEC\s+DEFECT\s*:?\s*(.{0,400})", re.I | re.S)
# 争议的对象 —— 取证就是拿它们去文件里找。
_TOKEN = re.compile(
    r"`([^`\n]{2,90})`"
    r"|(/api/[\w\-/:{}.*]{1,120})"
    r"|(\b(?:GET|POST|PUT|PATCH|DELETE)\s+/[\w\-/:{}.*]{1,120})",
    re.I)
# 模型指认卷子行号的句式：`REQ-4-3-3.spec.ts:40-41`、`REQ-1-1-1.spec.ts:168`。
# 这类引用**进不了正向取证** —— 需求文档里永远没有自产卷子的文件名，拿去
# 需求文档搜必然落空。但它能锚住「越界断言」具体在哪一行（见
# `_nonexistence_evidence`），所以单独识别、单独验证。
_SPEC_LINE_REF = re.compile(
    r"([A-Za-z0-9_\-./]+\.(?:spec|test)\.[jt]s)\s*:\s*(\d{1,5})(?:\s*[-–—]\s*(\d{1,5}))?")
# 取证能读的文件。spec 是 .ts，需求文档是 yaml/json/md。
_EVIDENCE_EXTS = {".ts", ".js", ".json", ".yaml", ".yml", ".md", ".txt", ".feature"}
# 一句「卷子判错了」得写这么长才算数。作废一份卷子 = 删掉一条验收，
# 门槛不能低到「说一句就删」。
_MIN_SPEC_DEFECT_CHARS = 80
# 一次跑最多作废几份卷子（和契约修订同一个思路：有预算，不然它就成了逃避验收的通道）。
_MAX_SPEC_VOIDS = 2
# 第几轮之后才允许主张卷子有错。前两轮先老老实实修代码 ——
# 「模型没做出来」和「卷子写错了」在这两轮里是分不清的，而前者远更常见。
_SPEC_DEFECT_FROM_ROUND = 2
# 两次全卷对账之间至少攒几次 keep。没有新提交就没有可查的刀（对账的二分
# 只在「上次量过的 HEAD .. 现在」这个窗口里找肇事者）。3 → 6（2026-10-08
# 提速批）：大树一次对账 20–25 分钟，keep 太少等于验收几刀就重开一趟。
_RECONCILE_MIN_KEEPS = 6


# 诊断回合的判词：三方（需求原文 / 卷子 / 实现）里它指认哪一方错了。
# 句式见 `DIAGNOSE_TURN`：一行 `DIAGNOSIS: <结论>: <理由>`。
_DIAG_SPEC = re.compile(r"DIAGNOSIS:\s*SPEC\s+DEFECT\s*[:\-—]\s*(.{0,400})", re.I | re.S)
_DIAG_IMPL = re.compile(r"DIAGNOSIS:\s*IMPLEMENTATION\s*[:\-—]\s*(.{0,400})", re.I | re.S)
_DIAG_OFF = re.compile(r"DIAGNOSIS:\s*OFF\s*TRACK\s*[:\-—]\s*(.{0,400})", re.I | re.S)


def _diagnosis_requirement_block(requirement: str, digest: str = "") -> str:
    """诊断回合里的需求块：**原文为主，出题摘要为辅**。

    摘要（`_requirements/<leaf>.md`）是出题那一刻模型对需求的理解 —— 如果当时
    就跑题了，错就长在它身上。所以诊断必须先看到原文，摘要只在旁边做对照。
    两样都没有就返回空串（诊断回合自己按 `{req_dir}` 去读）。
    """
    parts: list[str] = []
    if (requirement or "").strip():
        parts.append("The requirement text this node is held to, quoted:\n\n"
                     + _clip(requirement, 8000))
    if (digest or "").strip():
        parts.append("The spec author's own digest of it, for comparison — the text above is "
                     "what counts, not this:\n\n" + _clip(digest, 3000))
    return ("\n" + "\n\n".join(parts) + "\n") if parts else ""


def _diagnosis_verdict(text: str) -> tuple[str, str]:
    """诊断回合说了什么。返回 (`spec`|`impl`|`off`|`none`, 理由)。

    三种结论对应三种处置（见 `Orchestrator._diagnose_item`）：卷子错 → 取证后
    作废；实现缺 → 就地看代码、能补就补；跑题 → 记档、重读需求重做一次。
    """
    low = text or ""
    for kind, pattern in (("spec", _DIAG_SPEC), ("impl", _DIAG_IMPL), ("off", _DIAG_OFF)):
        match = pattern.search(low)
        if match:
            return kind, " ".join(match.group(1).split())[:400]
    return "none", ""


def _defect_claim(text: str) -> str | None:
    """模型这一轮有没有报契约缺陷，报的是什么。"""
    match = _DEFECT.search(text or "")
    if not match:
        return None
    return " ".join(match.group(1).split())[:300]


def _spec_defect_claim(text: str) -> str | None:
    """模型这一轮有没有报「这份卷子判错了」，报的是什么。

    **门槛比契约缺陷高**：那条要求有白纸黑字为证（`_fact_evidence`），这条
    还额外要求主张本身写得够长 —— 作废一份卷子就是删掉一条验收，而
    `SPEC DEFECT: 我觉得不对` 这种长度的主张，取证无从谈起。
    """
    match = _SPEC_DEFECT.search(text or "")
    if not match:
        return None
    claim = " ".join(match.group(1).split())[:400]
    return claim if len(claim) >= _MIN_SPEC_DEFECT_CHARS else None


def _defect_tokens(claim: str) -> list[str]:
    """从主张里抽出可取证的对象：路由、反引号里的名字。"""
    out: list[str] = []
    for groups in _TOKEN.findall(claim or ""):
        for item in groups:
            item = (item or "").strip()
            if item and item not in out:
                out.append(item)
    return out[:6]


def _probe_path(path: str) -> str:
    """把契约里的路由模板变成一个可以真的请求的路径。

    `:id` → `1`，`*` 通配段丢掉。目的只是问一句「这个路由挂上了吗」，
    所以取不到数据不算失败，只有挂不上才算。
    """
    path = _STAR.sub("", path or "/")
    path = _PARAM.sub("/1", path)
    return path if path.startswith("/") else "/" + path
