"""v5 增量维护：起跑先勘察、图纸围着老楼画、卷子一题一状态、一次修一张、卡住即诊断。

这一版的每一条改动都对应一个**实测过的失败形状**（正赛日志）：

  · 体检不合格就把 app 整个搬走从零建     → 一个大部分能用的楼被拆了，40-80 倍代价
  · 旧卷子留在盘上继续判分                 → 注定失败的旧账吃掉修复回合
  · 两份卷子抢同一个固定资源               → 双方都没写错，永远有一个红
  · 一个节点修不动 → 区域连坐               → 没做错的需求点一起丢分
  · 修复一批三张卷子、超时了就再修一遍     → 「一直大量修」，模型心里没数
  · 修不动就是修不动，没人问「到底谁错了」 → 卷子错 / 实现缺 / 理解跑题，三种病一副药

判据全部落在测试里，而不是「跑一次看看」。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402
import acceptance as acceptance_mod  # noqa: E402
import contract as contract_mod  # noqa: E402
import orchestrator as orch  # noqa: E402
from test_orchestrator import _flow, _make, _stub_prompt, _summary, _tree  # noqa: E402,F401


# ---------------------------------------------------------------- 起跑不清场

def _workspace(root: Path, *, real: bool) -> Path:
    for name in ("frontend", "backend"):
        d = root / name / "src"
        d.mkdir(parents=True, exist_ok=True)
        (d / "app.ts").write_text("export const placeholder = 1;\n" * (3 if not real else 1500),
                                  encoding="utf-8")
    return root


def test_a_scaffold_is_recognised_by_its_substance(tmp_path):
    """毛坯和真 app 的分别：`frontend/` + `backend/` 里源码的体量。"""
    assert M.scaffold_like(_workspace(tmp_path / "empty", real=False)) is True
    assert M.scaffold_like(_workspace(tmp_path / "real", real=True)) is False


def test_half_an_app_is_not_a_scaffold(tmp_path):
    """只有一半（没有 backend/）不算「像样的 app」，也不算毛坯 —— 不拆。"""
    root = tmp_path / "half"
    (root / "frontend").mkdir(parents=True)
    assert M.scaffold_like(root) is False


def test_discard_is_only_allowed_for_an_empty_scaffold(tmp_path, monkeypatch):
    monkeypatch.delenv("ARCBENCH_KEEP_TEMPLATE", raising=False)
    empty = _workspace(tmp_path / "empty", real=False)
    real = _workspace(tmp_path / "real", real=True)
    allowed, why = M.discard_allowed(empty)
    assert allowed and "scaffold" in why
    allowed, why = M.discard_allowed(real)
    assert not allowed, "有体量的 app 一律保留：" + why


def test_keep_template_is_the_master_switch(tmp_path, monkeypatch):
    """`ARCBENCH_KEEP_TEMPLATE=1`：连毛坯都不拆 —— 人在现场判断的总闸。"""
    monkeypatch.setenv("ARCBENCH_KEEP_TEMPLATE", "1")
    empty = _workspace(tmp_path / "empty", real=False)
    allowed, why = M.discard_allowed(empty)
    assert not allowed
    assert "KEEP_TEMPLATE" in why


# ---------------------------------------------------------------- 卷子工程化

def _spec(suite: Path, stem: str, body: str) -> Path:
    path = suite / f"{stem}.spec.ts"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_conflict_scan_catches_two_specs_writing_one_name(tmp_path):
    """两份卷子往同一个固定名字上写 —— 后跑的那个永远失败。"""
    suite = tmp_path / "acceptance"
    suite.mkdir()
    _spec(suite, "REQ-1-1-1",
          "await page.getByLabel('Name').fill('EVO-M01-RENAME-OK');\n")
    _spec(suite, "REQ-1-1-2",
          "await page.getByLabel('Name').fill('EVO-M01-RENAME-OK');\n")
    _spec(suite, "REQ-2-1-1",
          "await page.getByLabel('Name').fill('only-this-one-uses-it');\n")

    conflicts = M.scan_suite_conflicts(suite)
    assert "EVO-M01-RENAME-OK" in conflicts
    assert len(conflicts["EVO-M01-RENAME-OK"]) == 2
    assert "only-this-one-uses-it" not in conflicts, "只有一份卷子用的名字不是冲突"


def test_one_spec_writing_a_name_twice_is_not_a_conflict(tmp_path):
    """同一份卷子里写两次不算抢 —— 那是它自己的事。"""
    suite = tmp_path / "acceptance"
    suite.mkdir()
    _spec(suite, "REQ-1-1-1",
          "await page.getByLabel('A').fill('same-name');\n"
          "await page.getByLabel('B').fill('same-name');\n")
    assert M.scan_suite_conflicts(suite) == {}


def test_the_conflict_report_names_the_offending_specs(tmp_path):
    suite = tmp_path / "acceptance"
    suite.mkdir()
    _spec(suite, "REQ-1", "await page.fill('#a', 'shared-thing');\n")
    _spec(suite, "REQ-2", "await page.fill('#b', 'shared-thing');\n")
    conflicts = M.scan_suite_conflicts(suite)

    hint = M.conflict_hints(conflicts, ["REQ-1"])
    assert "shared-thing" in hint and "REQ-1.spec.ts" in hint
    assert "One test, one state" not in hint      # 提示词只在出卷时讲规则
    assert M.conflict_hints(conflicts, ["REQ-9"]) == "", "不在这组里的卷子不进它的提示词"


def test_the_author_prompt_demands_one_test_one_state():
    """出卷硬规矩必须写在提示词里 —— 检查项没被要求过，就不是模型的错。"""
    text = M.ACCEPTANCE_AUTHOR_PROMPT
    assert "One test, one state" in text
    assert "creates the objects it works on" in text
    assert "pre-provisions that object" in text and "shared" in text


def test_the_blueprint_prompt_marks_everything_carry_change_or_new():
    """图纸围着老楼画：沿用 / 改造 / 新增 三条出身，沿用条目的名字**来盘上**。"""
    text = contract_mod.BLUEPRINT_PROMPT_EXTEND
    for origin in ('"carry"', '"change"', '"new"'):
        assert origin in text
    assert "not** from your imagination" in text, "沿用条目的名字不许凭想象"
    assert "{survey_path}" in text, "图纸要读勘察报告"
    assert "carry-over areas" in text and "first" in text, "沿用区排在最前"
    assert 'say `CONTRACT WRITTEN`' in text


def test_the_fresh_build_prompts_stay_as_they_were():
    """空地上盖楼用原版提示词 —— 老楼那套三段式不许渗进从零打。"""
    assert '"carry"' not in contract_mod.BLUEPRINT_PROMPT
    assert "{survey_path}" not in contract_mod.BLUEPRINT_PROMPT


def test_the_skeleton_step_fills_gaps_instead_of_restructuring():
    """骨架那一步在旧楼上叫「体检＋补齐」，不是重建。"""
    text = orch.SKELETON_TURN_EXTEND
    assert "not a new skeleton" in text
    assert "Do not restructure" in text
    assert "{survey_path}" in text, "补齐样式要照勘察报告里老楼的样子来"
    assert "{contract_path}" in text and "{smoke_port}" in text


def test_specs_from_an_earlier_round_are_moved_out_not_deleted(tmp_path):
    suite = tmp_path / "acceptance"
    suite.mkdir()
    keep = _spec(suite, "REQ-1-1-1", "// this round's node\n")
    stale = _spec(suite, "REQ-9-9-9", "// last round's node\n")

    moved = M.quarantine_foreign_specs(suite, {"REQ-1-1-1"},
                                       tmp_path / ".arc" / "inherited-specs")

    assert moved == ["REQ-9-9-9.spec.ts"]
    assert keep.is_file(), "这一轮的卷子原地不动"
    assert not stale.is_file(), "旧卷子不再留在判分目录里"
    assert (tmp_path / ".arc" / "inherited-specs" / "REQ-9-9-9.spec.ts").is_file(), "移走不是删掉"


# ---------------------------------------------------------------- 诊断判词

def test_the_diagnosis_verdict_reads_the_three_outcomes():
    spec, claim = orch._diagnosis_verdict(
        "DIAGNOSIS: SPEC DEFECT: the spec asserts a toast the requirement never mentions")
    assert spec == "spec" and "toast" in claim

    impl, _ = orch._diagnosis_verdict(
        "DIAGNOSIS: IMPLEMENTATION: the PATCH route is missing")
    assert impl == "impl"

    off, _ = orch._diagnosis_verdict(
        "DIAGNOSIS: OFF TRACK: it built a modal where the text asks for a panel")
    assert off == "off"

    assert orch._diagnosis_verdict("no verdict here")[0] == "none"
    assert orch._diagnosis_verdict("")[0] == "none"


def test_the_diagnosis_prompt_quotes_the_requirement_and_the_ledger():
    """诊断提示词必须同时带着需求原文和台账 —— 否则它又在从头猜。"""
    block = orch._diagnosis_requirement_block("REQ-1 must survive a reload.",
                                              "digest: the node is about reloads")
    assert "REQ-1 must survive a reload." in block
    assert "what counts" in block, "摘要要说明自己只是转述"
    text = orch.DIAGNOSE_TURN.format(label="REQ-1", req_dir="reqs", requirement=block,
                                     specs="REQ-1.spec.ts", failures="f",
                                     ledger="- state: parked")
    assert "REQ-1 must survive a reload." in text
    assert "- state: parked" in text


# ---------------------------------------------------------------- 台账

def test_the_ledger_records_why_and_what_was_tried(_flow):
    o = _make(_flow)
    o._ledger_note("REQ-1", status="red", why="0/2 specs passed", tried=["implement + 3 turns"])
    o._ledger_note("REQ-1", tried=["batch repair a.spec.ts"])
    o._ledger_note("REQ-1", status="green", why="accepted")
    o._ledger_write()

    path = Path(_flow.output_dir) / ".arc" / "ledger.md"
    text = path.read_text(encoding="utf-8")
    assert "| REQ-1 | green | accepted |" in text
    assert "implement + 3 turns" in text and "batch repair a.spec.ts" in text


def test_the_ledger_reaches_the_repair_prompt(_flow):
    """台账要贴回提示词 —— 「模型心里有数」的机制本体。"""
    o = _make(_flow)
    o._ledger_note("REQ-1", status="red", why="the form never submits", tried=["implement + 2"])
    o._ledger_note("REQ-1", hint="the route it posts to does not exist")

    block = o._ledger_block("REQ-1")
    assert "already recorded" in block
    assert "the form never submits" in block
    assert "implement + 2" in block
    assert "the route it posts to does not exist" in block
    assert o._ledger_block("REQ-9") == "", "没记录过的节点不贴空话"


def test_the_ledger_says_who_is_next(_flow):
    o = _make(_flow)
    o._ledger_note("1.1.1", status="green", why="accepted")
    o._ledger_note("1.1.2", status="parked", why="repairs exhausted")
    o._ledger_write()
    text = (Path(_flow.output_dir) / ".arc" / "ledger.md").read_text(encoding="utf-8")
    assert "下一个要修的：1.1.2" in text
    assert "先修这个" in text, "台账上要能一眼看出下一个修谁"


# ---------------------------------------------------------------- 一次只修一张

def test_one_repair_batch_is_one_spec_file(_flow):
    o = _make(_flow)
    assert o._batch_cap() == 1, "v5 默认：一次只动一张卷子"


def test_the_old_batch_size_is_still_reachable(_flow, monkeypatch):
    monkeypatch.setenv("OCTOS_REPAIR_BATCH_MAX", "3")
    o = _make(_flow)
    assert o._batch_cap() == 3


# ---------------------------------------------------------------- 搁置 → 诊断

def test_a_timed_out_repair_turn_parks_instead_of_retrying(_flow, monkeypatch):
    """超时止损：被墙钟烧完的回合不算「修过了」，不按常规加宽重试。"""
    monkeypatch.setenv("OCTOS_ATOMIC_REPAIR", "1")
    o = _stub_prompt(_make(_flow))
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"]}
    _flow.spec_runner = lambda specs, grader_like: _summary(passed=0, failed=["1.1.1.spec.ts"])
    turns = []

    def fake_turn(prompt, label, timeout):
        turns.append(label)
        o._last_turn_timed_out = "batch repair" in label
        return (not o._last_turn_timed_out), ""

    o._turn = fake_turn
    o.implement_leaf("1.1.1", 1, 3)

    repairs = [t for t in turns if "batch repair" in t]
    assert len(repairs) == 1, f"超时后不再开第二条修复回合：{turns}"
    assert "1.1.1" in o._diagnosis, "搁置的红进诊断队列"
    assert o.ledger["1.1.1"]["status"] == "parked"


def test_a_queued_diagnosis_runs_once_and_only_for_its_phase(_flow, monkeypatch):
    monkeypatch.setenv("OCTOS_DIAGNOSE", "1")
    o = _make(_flow)
    _flow.reply = "DIAGNOSIS: IMPLEMENTATION: the route is missing"
    o._queue_diagnosis("1.1.1", "1.1.1", ["1.1.1.spec.ts"], "repairs exhausted", "node")
    o._queue_diagnosis("files:a.spec.ts", "1.1.2", ["a.spec.ts"], "no net gain", "final")

    assert o.diagnose_parked("node") is not None
    labels = [label for label, _ in _flow.turns]
    assert labels == ["diagnose 1.1.1"], "收尾队列不在节点阶段消化"
    assert _flow.turns[0][1].count("DIAGNOSIS") >= 1

    o.diagnose_parked("node")            # 已经做过的不再做
    assert [label for label, _ in _flow.turns] == ["diagnose 1.1.1"]


def test_the_diagnose_switch_turns_the_queue_off(_flow, monkeypatch):
    monkeypatch.setenv("OCTOS_DIAGNOSE", "0")
    o = _make(_flow)
    o._queue_diagnosis("1.1.1", "1.1.1", ["1.1.1.spec.ts"], "repairs exhausted", "node")
    assert o._diagnosis == {}
    assert o.diagnose_parked("node") is False


def test_a_spec_defect_claim_without_evidence_keeps_the_spec(_flow, monkeypatch):
    """诊断说卷子错了 —— 没有白纸黑字照样驳回（作废 = 删掉一条验收）。"""
    monkeypatch.setenv("OCTOS_DIAGNOSE", "1")
    o = _make(_flow)
    _flow.authored_suite = True
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"]}
    _flow.reply = ("DIAGNOSIS: SPEC DEFECT: " + "the spec wants a green toast. " * 6)
    o._queue_diagnosis("1.1.1", "1.1.1", ["1.1.1.spec.ts"], "repairs exhausted", "node")

    assert o.diagnose_parked("node") is False
    assert o.voided_specs == set()
    assert "1.1.1" in _flow.spec_map, "卷子还在"


def test_a_backed_spec_defect_claim_voids_the_spec_and_accepts_the_node(_flow, monkeypatch):
    """主张 + 需求文档里的原文为证 → 作废，节点按需求文本验收。"""
    monkeypatch.setenv("OCTOS_DIAGNOSE", "1")
    o = _make(_flow)
    _flow.authored_suite = True
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"]}
    _flow.baseline_specs = []
    req = Path(_flow.req_dir)
    req.mkdir(parents=True, exist_ok=True)
    (req / "requirements.yaml").write_text(
        "- id: 1.1.1\n  description: The button reads `Save draft` exactly.\n",
        encoding="utf-8")
    _flow.reply = ("DIAGNOSIS: SPEC DEFECT: the spec asserts a confirmation toast, and the "
                   "requirement text for `Save draft` never mentions any toast at all. " * 2)
    o._queue_diagnosis("1.1.1", "1.1.1", ["1.1.1.spec.ts"], "repairs exhausted", "node")

    assert o.diagnose_parked("node") is True
    assert o.voided_specs == {"1.1.1.spec.ts"}
    assert "1.1.1" not in _flow.spec_map
    assert o.verdict["1.1.1"] is True, "作废之后节点按需求文本 + 基线验收"
    assert o.ledger["1.1.1"]["status"] == "green"


def test_a_diagnosis_that_finds_the_code_at_fault_gets_one_repair_window(_flow, monkeypatch):
    """实现缺东西 → 就地量一遍；还没绿就**回炉一次**（每个要求点只给一次）。"""
    monkeypatch.setenv("OCTOS_DIAGNOSE", "1")
    o = _make(_flow)
    _flow.spec_map = {"1.1.1": ["1.1.1.spec.ts"]}
    _flow.reply = "DIAGNOSIS: IMPLEMENTATION: the PATCH route is missing"
    _flow.spec_runner = lambda specs, grader_like: _summary(passed=0, failed=["1.1.1.spec.ts"])
    windows = []

    def fake_accept(node_id, specs, label, aggregate, since_sha=None, rework=False):
        windows.append((node_id, label))
        return orch.NodeResult(node_id=node_id, passed=False, exhausted=True, reason="still red")

    o._accept_batched = fake_accept
    o._queue_diagnosis("1.1.1", "1.1.1", ["1.1.1.spec.ts"], "repairs exhausted", "node")

    o.diagnose_parked("node")
    assert windows == [("1.1.1", "1.1.1 (diagnosed)")], "回炉一次"
    assert o._diagnosis_turns == 1, "诊断回合真的开过"
    assert o.ledger["1.1.1"]["status"] == "parked"

    # 同一个要求点再来一次诊断（换 key 直接投喂）：回炉窗口不再给。
    assert o._diagnose_item("second look", {"node_id": "1.1.1",
                                            "files": ["1.1.1.spec.ts"],
                                            "reason": "again", "phase": "node"}) is True
    assert len(windows) == 1, "回炉只给一次 —— 修不动就是修不动"
    assert o.ledger["1.1.1"]["status"] == "parked"


def test_the_diagnosis_budget_is_bounded(_flow, monkeypatch):
    monkeypatch.setenv("OCTOS_DIAGNOSE", "1")
    monkeypatch.setenv("OCTOS_DIAGNOSE_MAX", "2")
    o = _make(_flow)
    _flow.reply = "DIAGNOSIS: OFF TRACK: it misread the text"
    for i in range(5):
        o._queue_diagnosis(f"N{i}", f"N{i}", [f"N{i}.spec.ts"], "stuck", "node")

    o.diagnose_parked("node")
    assert len([label for label, _ in _flow.turns]) == 2, "一次最多两件，剩下的留在队列里"
    assert len(o._diagnosis) == 3


def test_the_diagnosis_turn_is_read_only_and_short(_flow, monkeypatch):
    """诊断回合**只看不改、而且短**（#4b / #6a）。

    实测的驳回形状：走通的那次诊断 273s 交判决；两次失败的都烧满修复档
    （675s）、顺手写了文件、判决为空 —— 诊断被当成了一次修复在跑。两处对症：
    ① `read_only=True`：写类和 shell 工具从**请求里**拿掉（提示词说「别改」
    是可以被忽略的建议，工具面里没有才是做不到）；② 墙钟压到
    `OCTOS_DIAGNOSE_TIMEOUT`（默认 300s），不再吃修复档的 675s。
    """
    monkeypatch.setenv("OCTOS_DIAGNOSE", "1")
    monkeypatch.setenv("OCTOS_DIAGNOSE_TIMEOUT", "240")
    o = _make(_flow)
    _flow.repair_needs = lambda: 1000.0          # 修复档很宽（1200s）……
    _flow.reply = "DIAGNOSIS: OFF TRACK: it misread the text"
    o._queue_diagnosis("1.1.1", "1.1.1", ["1.1.1.spec.ts"], "stuck", "node")

    o.diagnose_parked("node")
    assert _flow.read_only_turns == ["diagnose 1.1.1"], "诊断回合只读"
    assert dict(_flow.turn_timeouts)["diagnose 1.1.1"] == 240, "……诊断只拿 240s"
    assert o._repair_timeout() == 1200, "对照：修复档本身没被压"


def test_a_fully_voided_diagnosis_item_never_opens_a_turn(_flow, monkeypatch):
    """作废后应**立即**退出后续所有测量（#4c）。

    彩排实测：卷子作废之后，同一件红又被量了 3 轮、约 45s 白烧 —— 作废是
    「它从此不存在」，再为它烧回合没有对象可修。整件全作废的，记成做过、
    不回队、一个回合都不开。
    """
    monkeypatch.setenv("OCTOS_DIAGNOSE", "1")
    o = _make(_flow)
    o.voided_specs.add("1.1.1.spec.ts")
    o._queue_diagnosis("1.1.1", "1.1.1", ["1.1.1.spec.ts"], "repairs exhausted", "node")

    assert o.diagnose_parked("node") is False
    assert _flow.turns == [], "整件已作废：诊断回合都不该开"
    assert "1.1.1" in o._diagnosis_done, "记成做过，不回队"
    assert o._diagnosis == {}


def test_a_partly_voided_item_still_gets_its_diagnosis(_flow, monkeypatch):
    """边界：一件里还有一份活着的卷子 → 诊断照做（只跳全死的）。"""
    monkeypatch.setenv("OCTOS_DIAGNOSE", "1")
    o = _make(_flow)
    o.voided_specs.add("a.spec.ts")
    _flow.reply = "DIAGNOSIS: OFF TRACK: it misread the text"
    o._queue_diagnosis("files:a+b", "1.1.1", ["a.spec.ts", "b.spec.ts"], "stuck", "node")

    o.diagnose_parked("node")
    assert [label for label, _ in _flow.turns] == ["diagnose 1.1.1"]


# ---------------------------------------------------------------- 全流程顺序

def _stage_recorder(o):
    order = []
    for name in ("survey", "blueprint", "skeleton", "expand_all", "implement_leaves",
                 "finalize", "_ledger_write"):
        setattr(o, name, (lambda n: lambda: order.append(n))(name))
    o.diagnose_parked = lambda phase: order.append(f"diagnose:{phase}")
    return order


def test_the_run_starts_by_surveying_the_existing_app(_flow):
    """v5 流程的形状：**勘察 → 蓝图 → 骨架 → 实现 → 诊断 → 收尾 → 落台账**。

    勘察排在最前是有意的 —— 蓝图围着老楼画，所以它必须先看过老楼；
    诊断排在收尾之前，是为了让「搁置的红」在最终全量复核里被算进去。
    """
    o = _make(_flow)
    order = _stage_recorder(o)

    o.run(_tree())

    assert order == ["survey", "blueprint", "skeleton", "expand_all", "implement_leaves",
                     "diagnose:node", "finalize", "_ledger_write"]


def test_a_clean_start_has_nothing_to_survey(_flow):
    """空地上盖楼：没有老楼可看，勘察不占回合。"""
    _flow.has_app = lambda: False
    o = _make(_flow)
    order = _stage_recorder(o)

    o.run(_tree())

    assert "survey" not in order
    assert order[0] == "blueprint"


# ---------------------------------------------------------------- v5.1 公共对象点名

def _shared_requirement(root: Path) -> Path:
    """需求原文：点名一个种子对象（含占位句的干扰）。名字来自句子，不来自列表。"""
    req = root / "requirements"
    req.mkdir(parents=True, exist_ok=True)
    (req / "requirements.yaml").write_text(
        "GIVEN the evaluation seed contains the seeded workbook `Q3 Sales` with a worksheet "
        "`Sheet1` and a worksheet `Sheet2`.\n"
        "AND an attempt to reuse the seeded workbook `Q3 Sales` is rejected with \"Workbook "
        "name already exists\".\n"
        "AND the observable result for \"the requested workflow,the requested workflow\" uses "
        "the same seeded names and values (`Q3 Sales`).\n",
        encoding="utf-8")
    return req


def _seeded_app(root: Path) -> Path:
    app = root / "app"
    (app / "backend").mkdir(parents=True, exist_ok=True)
    (app / "backend" / "seed.js").write_text(
        "const seedWorkbook = { name: 'Q3 Sales', sheet: 'Sheet1', header: 'Amount' };\n",
        encoding="utf-8")
    return app


def test_the_shared_object_roll_call_names_what_the_text_pre_provisions(tmp_path):
    """点名清单来自需求原文的「种子」句子；动过它又没复原的卷子被点出来。"""
    req = _shared_requirement(tmp_path)
    app = _seeded_app(tmp_path)
    suite = tmp_path / "acceptance"
    suite.mkdir()
    _spec(suite, "REQ-1-1-1",
          "await page.getByText('Q3 Sales', { exact: true }).click();\n"
          "await expect(grid()).toHaveText('Region');\n")          # 只读：不该被点
    _spec(suite, "REQ-5-2-1",
          "await page.getByText('Q3 Sales', { exact: true }).click();\n"
          "await page.getByLabel('Edit C3').fill('100');\n")        # 改过不还：要点名

    scan = M.scan_shared_objects(suite, req, app)

    assert "Q3 Sales" in scan["objects"] and "Sheet1" in scan["objects"]
    assert scan["objects"]["Q3 Sales"]["sources"] == ["requirement", "app"]
    assert not any("requested workflow" in name for name in scan["objects"]), \
        "占位句不是对象名"
    assert "Amount" in scan["app_only"], "只有盘上种子代码有的名字：只报告，不判"
    assert "Workbook name already exists" in scan["quoted_only"], \
        "引号里的报错文案只进报告：把它当对象名，判据就被稀释成「每份卷子都碰过公共对象」"
    assert "Workbook name already exists" not in scan["objects"]
    flagged = [f["spec"] for f in scan["flags"]]
    assert "REQ-5-2-1.spec.ts" in flagged, "动过公共对象又没复原的卷子要点名"
    assert "REQ-1-1-1.spec.ts" not in flagged, "只读不写的不算"
    report = M.render_shared_objects(scan)
    assert "REQ-5-2-1.spec.ts" in report and "`Q3 Sales`" in report
    assert "只报告" in report and "Workbook name already exists" in report


def test_a_spec_that_puts_the_shared_object_back_is_not_flagged(tmp_path):
    """有复原动作（生命周期钩子 / restore 调用）的卷子自证清白，不被点。"""
    req = _shared_requirement(tmp_path)
    suite = tmp_path / "acceptance"
    suite.mkdir()
    _spec(suite, "REQ-5-2-1",
          "test.afterEach(async ({ page }) => {\n"
          "  await page.request.post('/api/test/restore-seed');\n"
          "});\n"
          "await page.getByText('Q3 Sales', { exact: true }).click();\n"
          "await page.getByLabel('Edit C3').fill('100');\n")

    scan = M.scan_shared_objects(suite, req, None)

    assert scan["flags"] == [], "卷子自己收过场了，不该被点"


def test_the_roll_call_reads_the_present_tense_and_curly_quotes(tmp_path):
    """决赛原文的写法：`The system pre-provisions workbook \\`EVO-…\\`` + 弯引号文案。

    只认过去时 pre-provisioned、只认 ASCII 引号的话，整份清单会在决赛文本上哑掉 ——
    而它哑掉的方式是「一条都不报」，看上去和「没有公共对象」一模一样。
    """
    req = tmp_path / "requirements"
    req.mkdir()
    (req / "requirements.yaml").write_text(
        "content: The visitor is unauthenticated and the system pre-provisions workbook "
        "`EVO-M01-RENAME-OK` with active worksheet `IdentityLog` and cell F3 value `unreviewed`, "
        "and the message “recovered” is unrelated filler text of the sentence.\n",
        encoding="utf-8")

    scan = M.scan_shared_objects(tmp_path / "acceptance", req, None)

    assert "EVO-M01-RENAME-OK" in scan["objects"], "现在时 pre-provisions 也是种子句"
    assert "IdentityLog" in scan["objects"]
    assert "unreviewed" in scan["objects"]
    assert "recovered" not in scan["objects"], "弯引号里的散文不是对象名"


def test_single_character_backticks_do_not_desync_the_pairing(tmp_path):
    """`` `6` and `14` `` —— 单字符串要是匹配不上，配对错位会把 " and " 当成名字。"""
    req = tmp_path / "requirements"
    req.mkdir()
    (req / "requirements.yaml").write_text(
        "The seeded sheet holds values `6` and `14`.\n", encoding="utf-8")

    names, quoted = M.requirement_shared_names(req)

    assert names == {} and quoted == {}, "单字符串不是名字，夹在中间的连接词更不是"


def test_the_shared_object_hints_only_mention_the_batch(tmp_path):
    """找茬提示只带本批的账 —— 别批的卷子不归这一回合管。"""
    req = _shared_requirement(tmp_path)
    suite = tmp_path / "acceptance"
    suite.mkdir()
    _spec(suite, "REQ-5-2-1",
          "await page.getByText('Q3 Sales', { exact: true }).click();\n"
          "await page.getByLabel('Edit C3').fill('100');\n")
    _spec(suite, "REQ-3-1-1",
          "await page.getByText('Q3 Sales', { exact: true }).click();\n"
          "await page.getByLabel('Edit D1').fill('north-value');\n")
    scan = M.scan_shared_objects(suite, req, None)
    assert len(scan["flags"]) == 2, "两份都动过公共对象"

    hint = M.shared_object_hints(scan, ["REQ-5-2-1"])

    assert "SHARED, PRE-PROVISIONED OBJECTS" in hint
    assert "REQ-5-2-1.spec.ts" in hint
    assert "REQ-3-1-1.spec.ts" not in hint, "别批的卷子不进这一批的提示词"


def test_the_author_prompt_warns_the_starting_state_can_disagree_with_itself():
    """起点一致性写进出卷硬规矩：需求自己都可能自相矛盾，卷子不许借别的节点的说法。"""
    text = M.ACCEPTANCE_AUTHOR_PROMPT
    assert "The starting state is one" in text
    assert "cannot both be true in a single run" in text


def test_the_diagnosis_turn_sends_it_to_the_neighbours_first():
    """诊断先查邻居：全卷共用一份存储，别人的遗留物不是这个 app 的错。"""
    text = orch.DIAGNOSE_TURN
    assert "check the neighbours" in text
    assert "never put it back" in text


def test_the_full_suite_runs_serially_like_the_grader(monkeypatch):
    """收尾全量复核与官方判分同口径：默认 workers=1；提速要显式开口，并有代价。"""
    monkeypatch.delenv("OCTOS_ARC_FINAL_WORKERS", raising=False)
    assert acceptance_mod.GRADER_WORKERS == 1
    assert acceptance_mod.final_workers(None) == 1
    assert acceptance_mod.final_workers(2 << 30) == 1, "内存再宽也不自动提速"
    monkeypatch.setenv("OCTOS_ARC_FINAL_WORKERS", "4")
    assert acceptance_mod.final_workers(None) == 4

    assert M.Flow.GRADER_WORKERS == 1
    assert M.Flow.worker_parity_note(1) == "", "与判分同口径就不必打警报"
    assert "grading runs 1" in M.Flow.worker_parity_note(4)

    root = Path(M.__file__).parent
    for source in ("main.py", "orchestrator.py"):
        text = (root / source).read_text(encoding="utf-8")
        assert 'environ.get("OCTOS_ARC_FINAL_WORKERS"' not in text, \
            f"{source} 绕过了 acceptance.final_workers 自己读提速开关"
        assert "final_workers" in text


def test_the_completion_note_carries_the_shared_object_flags():
    """收尾上报不许含糊：动过公共对象的卷子数要在完成信号里说清。"""
    audit = {"shared_flags": [{"spec": "REQ-5-2-1.spec.ts", "objects": ["Q3 Sales"],
                               "writes": 2, "shared_writes": []}]}
    note = M.suite_audit_completion_note(audit)
    assert "REQ-5-2-1.spec.ts" in note
    assert "shared" in note and "restoring" in note


def _audit_with(spec_text, digest="A button saves the workbook."):
    """一份卷子跑一遍机械体检，返回 (audit, line, hints, report)。"""
    audit = M.suite_audit(["REQ-9-9-9"], {"REQ-9-9-9": digest}, {"REQ-9-9-9": spec_text})
    return (audit,
            M.suite_audit_line(audit),
            M.suite_audit_hints(audit, ["REQ-9-9-9"]),
            M.render_suite_audit(audit))


def test_an_async_helper_handed_to_expect_is_flagged_as_unpassable():
    """`expect(helper())` 而 helper 是 async —— Promise 进 expect，Playwright 拒绝，
    任何实现都救不了。这条是硬缺陷：体检报告、日志行、找茬提示词三处都要说。

    真模型决赛排练实测：模型写的 REQ-6-1-1 卷子 3 处都这么写。
    """
    spec = """
import { test, expect } from '@playwright/test';

async function freezeButton(page, rows) {
  return page.getByRole('button', { name: `Frozen rows: ${rows}`, exact: true });
}

test('freezes', async ({ page }) => {
  await expect(freezeButton(page, '1')).toBeVisible();
});
"""
    audit, line, hints, report = _audit_with(spec)
    assert audit["defect_nodes"] == ["REQ-9-9-9"]
    assert "no implementation can pass" in line
    assert "Promise" in hints and "plain function" in hints
    assert "no implementation can pass" in report


def test_awaiting_the_helper_or_using_a_plain_helper_is_clean():
    """`expect(await helper())`、非 async 的 helper、以及 `.resolves` 都不算缺陷 ——
    否则这条机械检查会天天误伤，没人再信它。"""
    awaited = """
import { test, expect } from '@playwright/test';
async function freezeButton(page) { return page.getByRole('button', { name: 'View' }); }
test('freezes', async ({ page }) => {
  await expect(await freezeButton(page)).toBeVisible();
});
"""
    plain = """
import { test, expect } from '@playwright/test';
function freezeButton(page) { return page.getByRole('button', { name: 'View' }); }
test('freezes', async ({ page }) => {
  await expect(freezeButton(page)).toBeVisible();
});
"""
    resolves = """
import { test, expect } from '@playwright/test';
async function freezeButton(page) { return page.getByRole('button', { name: 'View' }); }
test('freezes', async ({ page }) => {
  expect(freezeButton(page)).resolves.toBeTruthy();
});
"""
    for spec in (awaited, plain, resolves):
        audit, line, hints, _report = _audit_with(spec)
        assert audit["defect_nodes"] == [], spec
        assert "no implementation can pass" not in line
        assert "Promise" not in hints


def test_the_author_prompt_forbids_async_locator_helpers_inside_expect():
    """根因也堵在提示词里：返回 locator 的 helper 写成非 async（或 await 再传）。

    这不是任务知识 —— 是 Playwright 的语言规则，和 `exact: true` 同类。
    """
    prompt = M.ACCEPTANCE_AUTHOR_PROMPT
    assert "never `async`" in prompt
    assert "await" in prompt and "Promise" in prompt
