"""出卷之后的最后一道：A 档机械体检 + B 档独立找茬回合（2026-10-01）。

背景一句话：出卷人和答卷人是同一个模型，它的卷子会**系统性地避开它自己实现
里会打折扣的维度**。github 3.22 分那次的形状：169 个用例里只有 1 个跨浏览器
上下文的检查，而同一份实现恰好写了「每个会话一份平行世界」的存储 —— 单会话
的卷子结构上测不出会话隔离。缺的不是认真，是科目。

两道工序：
    · A 档（`suite_audit`）：卷面机械体检。需求原文带持久化/多账号的句型，而
      卷子从头到尾没开过第二个浏览器上下文（`newContext(`），就是一份缺考清单。
    · B 档（`audit_authored_suite`）：卷子冻结之前的独立找茬回合。只拿需求原文
      和卷子，按出卷时的同一分组补写缺失的检查，补不上的如实进完成信号。
      2026-10-07 起缺考清单升为**硬任务**：每条必须有交代 —— 补上检查，或把
      带理由的豁免写进 `_audit/suite-audit-waivers.md`；还漏交代的追一个小回合
      （只追一次），再要不到就点名进报告和完成信号。

这里钉的是**判据**，不是模型写出来的内容：
    · 触发词只认通用英文句型（红线下不许出现任何任务的字段名）
    · 体检只数 `newContext(`、`test(` 和两组动作词法（创建/写数据），不解析卷子语义
    · 找茬回合与出卷共用时间闸门；`OCTOS_ARC_AUDIT_TESTS=0` 只关回合不关体检
    · 缺考的交代只有两种：盘面差（补上了）/ 豁免文件里的理由（写了为什么）；
      光有节点 ID 不算交代；追账只追一次，不循环
    · 审计是增值不是前置：它崩了，卷子照常交下去

2026-10-01 第二轮复盘加了一类科目：「建了要用」（create-then-use）。sheet 47.54
那次的第二个形状：建工作簿→写单元格命中 undoStack 崩溃，官方评分期 500 连发
9 分钟，而自家 138/138 全绿 —— 建卷不写字、写卷不创建，交集为空，中心路径在
自家卷面上结构性不可见。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from layers import layerize  # noqa: E402
import main as M  # noqa: E402


# ---------------------------------------------------------------- 假件

def _tree():
    """两分支的小树：根上有持久化契约（会渗进每个叶子的出卷依据），
    一支带多账号措辞，另一支带数字规则。"""
    return {
        "id": "ROOT", "name": "App", "type": "FOLDER",
        "description": "Comments are persisted on the server and visible to every signed-in user.",
        "children": [
            {"id": "REQ-1", "name": "Board", "type": "FOLDER", "description": "Posts board.",
             "children": [
                 {"id": "REQ-1-1", "name": "Post", "type": "ATOMIC",
                  "description": "A comment written by one account is still there when "
                                 "another account opens the board."},
             ]},
            {"id": "REQ-2", "name": "Profile", "type": "FOLDER", "description": "Profile page.",
             "children": [
                 {"id": "REQ-2-1", "name": "Name", "type": "ATOMIC",
                  "description": "The display name is between 2 and 32 characters."},
             ]},
        ],
    }


class _AuditFlow:
    """跑得动 `author_acceptance_suite`（含审计段）的最小 Flow。

    `turn` 的戏肉和 test_author_suite 里的假件同一个道理：被测的方法只认磁盘。
    这里多两个机关：`author_skips` 让出卷回合故意漏掉某些节点，`audit_writes`
    让找茬回合把漏掉的补上 —— 用来钉「盘点放在审计之后」这条顺序。
    """

    def __init__(self, output_dir, budget=10_000, elapsed=0, node_timeout=2400,
                 author_skips=(), audit_writes=(), audit_body="// spec\n"):
        self.output_dir = Path(output_dir)
        self.budget = budget
        self._elapsed = elapsed
        self.node_timeout = node_timeout
        self.author_skips = set(author_skips)
        self.audit_writes = tuple(audit_writes)
        self.audit_body = audit_body
        self.labels: list[str] = []
        self.prompts: list[str] = []

    def remaining(self):
        return self.budget - self._elapsed

    def turn(self, prompt, timeout, label, expect_verification=True):
        self.labels.append(label)
        self.prompts.append(prompt)
        assert expect_verification is False, "出卷/找茬回合不跑构建，别被记「没验证就完成」"
        suite = self.output_dir / M.AUTHOR_SUITE_DIR
        if label.startswith("author specs"):
            for line in prompt.splitlines():
                head = line.strip().split()[0] if line.strip() else ""
                if head.startswith("REQ-") and head not in self.author_skips:
                    (suite / f"{head}.spec.ts").write_text("// spec\n", encoding="utf-8")
        elif label.startswith("audit specs"):
            for leaf in self.audit_writes:
                (suite / f"{leaf}.spec.ts").write_text(self.audit_body, encoding="utf-8")
        return True, "written"

    _authoring_timeout = M.Flow._authoring_timeout
    author_acceptance_suite = M.Flow.author_acceptance_suite
    audit_authored_suite = M.Flow.audit_authored_suite


# ---------------------------------------------------------------- 触发词

def test_persistence_wording_triggers_the_fresh_session_category():
    """「在服务器上持久化」这类句型 = 必须换个浏览器会话再验一次。"""
    trig = M.suite_audit_triggers("A new issue is persisted on the server.")
    assert trig["fresh_session"] is True
    assert trig["multi_actor"] is False


def test_two_signed_in_identities_trigger_the_multi_actor_category():
    """同一个句型的两个**不同**身份 = 多账号场景（不写死任何名字）。"""
    text = "alice is signed in as owner, then bob is signed in as guest"
    assert M.suite_audit_triggers(text)["multi_actor"] is True
    # 同一个身份重复出现不算
    same = "Alice is signed in as owner. Alice is again signed in as owner."
    assert M.suite_audit_triggers(same)["multi_actor"] is False


def test_numeric_and_format_wording_triggers_the_boundary_category():
    assert M.suite_audit_triggers("The password is between 12 and 128 characters.")["format_rule"]
    assert M.suite_audit_triggers("It must contain at least one uppercase letter.")["format_rule"]


def test_plain_requirement_text_triggers_nothing():
    trig = M.suite_audit_triggers("A button named 'New workbook' creates one.")
    assert not any(trig.values()), "没有句型就不许平白要求科目"


def test_the_digest_intro_does_not_trigger_anything_by_itself():
    """依据的固定开场白是给写卷人的说明，不是需求原文 —— 它自己带着
    「a persistence rule」这种词眼，切不干净就是给每个节点记一笔假科目
    （校准实验实测：切掉之前 47/47 全中）。"""
    text = M.DIGEST_INTRO + "\n## This node\nA button named 'New workbook' creates one."
    assert not any(M.suite_audit_triggers(text).values())


def test_node_ids_are_not_format_rules():
    """校准实验实测：裸的 `\\d+-\\d+` 把 "REQ-3-4"、"REQ-1-1-2" 这些**节点编号**
    全算成了数字区间，47/47 假触发。带单位词尾的真区间仍然要认。"""
    text = "Depends on: REQ-3-4 (View a page) and REQ-1-1-2. Opens from REQ-2-1."
    assert M.suite_audit_triggers(text)["format_rule"] is False
    assert M.suite_audit_triggers("The name is 3-30 characters.")["format_rule"] is True
    assert M.suite_audit_triggers("A code of 4 to 8 digits.")["format_rule"] is True


def test_the_trigger_vocabulary_carries_no_task_words():
    """红线：触发词必须是通用英文句型。一旦出现某次任务的字段名，规则就变成
    了面向那次任务的硬编码 —— 这条测试就是防它悄悄溜进来的。"""
    vocabulary = " ".join(M.FRESH_SESSION_HINTS + M.MULTI_ACTOR_HINTS).lower()
    for word in ("issue", "pull request", "workbook", "cell", "sheet",
                 "repository", "comment", "board", "profile"):
        assert word not in vocabulary, f"触发词里混进了任务字段：{word}"


# ---------------------------------------------------------------- 机械体检

def test_a_single_session_spec_is_flagged_when_the_text_implies_persistence():
    """这是 github 3.22 那次的形状：卷子全绿，但没有一行离开过第一个会话。"""
    digests = {"REQ-1": "The comment persists on the server."}
    specs = {"REQ-1": "test('shows it', async () => { await expect(page.getByText('x')).toBeVisible(); });"}
    audit = M.suite_audit(["REQ-1"], digests, specs)
    assert audit["gap_nodes"] == ["REQ-1"]
    gap = audit["nodes"]["REQ-1"]["gaps"][0]
    assert "leaves the first browser session" in gap
    assert audit["nodes"]["REQ-1"]["tests"] == 1


def test_a_second_browser_context_clears_the_gap():
    digests = {"REQ-1": "The comment persists on the server."}
    specs = {"REQ-1": "const other = await page.context().browser().newContext();"}
    audit = M.suite_audit(["REQ-1"], digests, specs)
    assert audit["gap_nodes"] == []
    assert audit["with_second_context"] == ["REQ-1"]


def test_a_missing_spec_is_a_gap_of_its_own():
    audit = M.suite_audit(["REQ-1"], {}, {})
    assert audit["gap_nodes"] == ["REQ-1"]
    assert audit["missing_specs"] == ["REQ-1"]
    assert "no spec file" in audit["nodes"]["REQ-1"]["gaps"][0]


def test_a_spec_without_triggers_is_never_flagged():
    digests = {"REQ-1": "A button named 'New workbook' creates one."}
    specs = {"REQ-1": "test('x', async () => {});"}
    audit = M.suite_audit(["REQ-1"], digests, specs)
    assert audit["gap_nodes"] == []


def test_boundary_rules_are_hinted_even_without_a_hard_gap():
    """数字/格式规则没法机械判定卷子对错，所以不进缺考清单，但要进找茬提示。"""
    digests = {"REQ-1": "The name is between 2 and 32 characters."}
    specs = {"REQ-1": "test('x', async () => {});"}
    audit = M.suite_audit(["REQ-1"], digests, specs)
    assert audit["gap_nodes"] == [], "格式规则不该把节点打成缺考"
    assert "boundary" in M.suite_audit_hints(audit, ["REQ-1"])


def test_hints_say_so_when_nothing_is_flagged():
    audit = M.suite_audit(["REQ-1"], {"REQ-1": "A button."}, {"REQ-1": "test('x', async () => {});"})
    assert "nothing flagged" in M.suite_audit_hints(audit, ["REQ-1"])


def test_the_material_reader_tolerates_files_that_are_not_there(tmp_path):
    suite = tmp_path / "acceptance"
    (suite / "_requirements").mkdir(parents=True)
    (suite / "_requirements" / "REQ-1.md").write_text("text", encoding="utf-8")
    (suite / "REQ-1.spec.ts").write_text("// spec", encoding="utf-8")
    digests, specs = M.read_suite_material(
        suite, ["REQ-1", "REQ-2"],
        {"REQ-1": "_requirements/REQ-1.md", "REQ-2": "_requirements/REQ-2.md"})
    assert digests == {"REQ-1": "text"}
    assert specs == {"REQ-1": "// spec"}


# ---------------------------------------------------------------- 建了要用

def _creator(button: str = "New blank workbook") -> str:
    return (f"test('t', async () => {{ await page.getByRole('button', "
            f"{{ name: '{button}' }}).click(); }});")


def _writer() -> str:
    return "test('t', async () => { await page.getByRole('textbox').fill('42'); });"


def test_the_sheet_shape_leaves_a_journey_gap():
    """2026-10-01 sheet 那次的形状：建卷不写字、写卷不创建，各自全绿。"""
    audit = M.suite_audit(["REQ-1", "REQ-2"],
                          {"REQ-1": "A button creates a new blank workbook.",
                           "REQ-2": "Typing in a cell stores the value."},
                          {"REQ-1": _creator(), "REQ-2": _writer()})
    journey = audit["journey"]
    assert journey["creators"] == ["REQ-1"] and journey["writers"] == ["REQ-2"]
    assert journey["bridging"] == [] and journey["gap"] is True
    # 三个出口全接上：日志行、找茬提示、完成信号
    assert "no spec creating or importing an entity and then using it" in M.suite_audit_line(audit)
    assert "suite-level" in M.suite_audit_hints(audit, ["REQ-1"])
    assert "no spec creates or imports" in M.suite_audit_completion_note(audit)
    assert "journey gap" in M.render_suite_audit(audit)


def test_one_spec_that_creates_and_then_writes_closes_the_gap():
    """github 卷面的形状：每条创建流程都在同一个 test() 里用起来。"""
    audit = M.suite_audit(["REQ-1"], {"REQ-1": "x"},
                          {"REQ-1": _creator() + "\n" + _writer()})
    assert audit["journey"]["bridging"] == ["REQ-1"]
    assert audit["journey"]["gap"] is False


def test_menu_commands_are_not_entity_creation():
    """校准实验：sheet 真实卷面里 REQ-5-1-2「建筛选器＋写数据」差点造出假桥 ——
    菜单项是对已有实体下的命令，不是实体出生；排除 menuitem，假桥塌掉，真缺口才现形。"""
    menu = ("test('t', async () => { await page.getByRole('menuitem', "
            "{ name: 'Create filter' }).click(); await page.getByRole('textbox').fill('x'); });")
    audit = M.suite_audit(["REQ-1", "REQ-2"], {"REQ-1": "a", "REQ-2": "b"},
                          {"REQ-1": _creator("Import CSV"), "REQ-2": menu})
    assert audit["journey"]["creators"] == ["REQ-1"], "menuitem 不算创建动作"
    assert audit["journey"]["gap"] is True, "假桥不许盖住缺口"


def test_an_import_counts_as_creating():
    """上传文件即导入：setInputFiles 单独出现也算创建动作。"""
    upload = "test('t', async () => { await input.setInputFiles({ name: 'a.csv' }); });"
    audit = M.suite_audit(["REQ-1", "REQ-2"], {"REQ-1": "a", "REQ-2": "b"},
                          {"REQ-1": upload, "REQ-2": _writer()})
    assert audit["journey"]["creators"] == ["REQ-1"]
    assert audit["journey"]["gap"] is True


def test_one_sided_suites_are_not_journey_gaps():
    """只有一边时不许平白报缺口（没有写动作就无从要求「用了它」）。"""
    only_create = M.suite_audit(["REQ-1"], {"REQ-1": "a"}, {"REQ-1": _creator()})
    only_write = M.suite_audit(["REQ-1"], {"REQ-1": "a"}, {"REQ-1": _writer()})
    assert only_create["journey"]["gap"] is False
    assert only_write["journey"]["gap"] is False


def test_hints_stay_silent_for_batches_with_no_creators():
    """缺口提示只发给含创建卷的批次 —— 其他批次照旧「nothing flagged」。"""
    audit = M.suite_audit(["REQ-1", "REQ-2"], {"REQ-1": "a", "REQ-2": "b"},
                          {"REQ-1": _creator(), "REQ-2": _writer()})
    hints = M.suite_audit_hints(audit, ["REQ-2"])
    assert "suite-level" not in hints
    assert "nothing flagged" in hints


# ---------------------------------------------------------------- 越界断言

def test_aria_wiring_the_digest_never_mentions_is_flagged():
    """G1 偏严家族的形状：需求一个字没提 aria，卷子却断言关联（github 那次
    的候选都是这么被冤枉的）。需求自己写了 aria 就不算越界。"""
    spec = "test('t', async () => { await expect(box).toHaveAttribute('aria-invalid', 'true'); });"
    without = "The field shows the message 'Name is required' when it is empty."
    with_req = "The field is marked invalid (aria-invalid) while the message shows."
    flagged = M.suite_audit(["REQ-1"], {"REQ-1": without}, {"REQ-1": spec})
    assert flagged["overreach"]["aria_without_requirement"] == ["REQ-1"]
    clean = M.suite_audit(["REQ-1"], {"REQ-1": with_req}, {"REQ-1": spec})
    assert clean["overreach"]["aria_without_requirement"] == [], "需求写了 aria 就不算越界"


def test_whole_page_text_assertions_are_flagged():
    """整页文本全等是 sheet 那 3 处动态文本误伤的来源。"""
    contain = "test('t', async () => { await expect(page.locator('body')).toContainText(total); });"
    inner = "test('t', async () => { const s = await page.locator('body').innerText(); });"
    for spec in (contain, inner):
        audit = M.suite_audit(["REQ-1"], {"REQ-1": "x"}, {"REQ-1": spec})
        assert audit["overreach"]["whole_page_text"] == ["REQ-1"], spec


def test_name_matches_without_exact_are_counted():
    """撞名 strict 误杀的形状：裸 name 成片出现（≥3）才进提示 —— 1~2 处是
    两张正赛卷面上都存在的零星写法，提示到那个粒度就退化成噪音。"""
    spec = ("test('t', async () => { await page.getByRole('button', { name: 'Go' }).click(); "
            "await page.getByRole('link', { name: 'Home' }).click(); "
            "await page.getByRole('button', { name: 'Next' }).click(); "
            "await page.getByRole('button', { name: 'Save', exact: true }).click(); });")
    audit = M.suite_audit(["REQ-1"], {"REQ-1": "x"}, {"REQ-1": spec})
    assert audit["overreach"]["loose_name_matches"] == {"REQ-1": 3}
    assert audit["totals"]["overreach"] == 1
    # 零星一两处不亮；正则形式的 name 本就不需要 exact
    small = ("test('t', async () => { await page.getByRole('button', { name: 'Go' }).click(); "
             "await page.getByRole('button', { name: /save/i }).click(); });")
    quiet = M.suite_audit(["REQ-1"], {"REQ-1": "x"}, {"REQ-1": small})
    assert quiet["overreach"]["loose_name_matches"] == {}
    assert quiet["totals"]["overreach"] == 0


def test_option_visibility_assertions_are_flagged():
    """REQ-4-3-3 款铁案：原生 select 收起时 option 不渲染，`toBeVisible` 永不可满足。
    点击（.click()）不算 —— 只有可见性断言是这个形状。"""
    spec = ("test('t', async () => { await expect(page.getByRole('option', "
            "{ name: 'main' })).toBeVisible(); });")
    audit = M.suite_audit(["REQ-1"], {"REQ-1": "x"}, {"REQ-1": spec})
    assert audit["overreach"]["option_visibility"] == ["REQ-1"]
    assert "native" in M.suite_audit_hints(audit, ["REQ-1"])
    clicked = ("test('t', async () => { await page.getByRole('option', "
               "{ name: 'main' }).click(); });")
    quiet = M.suite_audit(["REQ-1"], {"REQ-1": "x"}, {"REQ-1": clicked})
    assert quiet["overreach"]["option_visibility"] == []


def test_a_clean_spec_triggers_no_overreach():
    spec = ("test('t', async () => { await page.getByRole('button', "
            "{ name: 'Create account', exact: true }).click(); "
            "await expect(page.getByText('Welcome')).toBeVisible(); });")
    audit = M.suite_audit(["REQ-1"], {"REQ-1": "A button named 'Create account' appears."},
                          {"REQ-1": spec})
    assert audit["overreach"]["nodes"] == []
    assert audit["totals"]["overreach"] == 0
    assert "asserting beyond" not in M.suite_audit_line(audit)
    assert M.suite_audit_completion_note(audit) == ""


def test_the_overreach_reaches_every_surface():
    """三个出口都要出现：日志行、找茬提示、完成信号、报告。"""
    spec = "test('t', async () => { await page.locator('body').innerText(); });"
    audit = M.suite_audit(["REQ-1"], {"REQ-1": "x"}, {"REQ-1": spec})
    assert "asserting beyond the requirement text" in M.suite_audit_line(audit)
    assert "whole-page text" in M.suite_audit_hints(audit, ["REQ-1"])
    assert "assert beyond the requirement text" in M.suite_audit_completion_note(audit)
    assert "beyond text" in M.render_suite_audit(audit)


# ------------------------------------------------- 技法赌注（2026-10-07，第三族）
#
# 判据在两张真实排练卷面上校准：sheet 10 张亮 2 张、github 52 张亮 9 张，每张
# 都能对上出卷审计里已确认的形状 —— 不许出现「整片亮灯」的粗判。

def test_writing_straight_into_a_grid_cell_is_flagged():
    """sheet REQ-5-2-1 的形状：真实卷面把格子包进 `cell(page, coord)` 小助手，
    写入藏在助手后面 —— 要先顺着助手找到 gridcell，才看得见 `.fill()`。
    需求说「双击格子出内联编辑器」时，对格子直接 fill 是必红的赌注。"""
    helper = ("function cell(page: Page, coord: string) { "
              "return page.getByRole('gridcell', { name: coord }); }\n"
              "test('t', async () => { await cell(page, 'K8').fill('Paused'); });")
    digest = ("Double-clicking a grid cell displays an inline text box with the "
              "accessible name 'Edit <cell coordinate>'.")
    audit = M.suite_audit(["REQ-1"], {"REQ-1": digest}, {"REQ-1": helper})
    assert audit["technique"]["cell_fill"] == ["REQ-1"]
    assert audit["nodes"]["REQ-1"]["technique"]["cell_fill"] == 1
    # 双击进编辑器、往名字明确的输入框里写字 —— 不亮（点击不是写入）
    inline = ("test('t', async () => { await page.getByRole('gridcell', { name: 'K8' })"
              ".dblclick(); await page.getByRole('textbox', { name: 'Edit K8' })"
              ".fill('Paused'); });")
    quiet = M.suite_audit(["REQ-1"], {"REQ-1": digest}, {"REQ-1": inline})
    assert quiet["technique"]["cell_fill"] == []


def test_select_option_needs_the_text_to_fix_the_control_type():
    """原生 select 和自定义下拉是两种合规实现，`.selectOption(` 只对前者成立。
    摘要点名过控件类型就不算赌注 —— 校准：github 11 份用 selectOption 的卷子
    里 9 份的摘要点名了 dropdown/combobox 一类说法。"""
    spec = ("test('t', async () => { await page.getByLabel('Operator')"
            ".selectOption('Greater than'); });")
    unbacked = M.spec_technique_bets(spec, "Choose the operator from the control.")
    assert unbacked.get("select_option") == 1
    backed = M.spec_technique_bets(spec, "A native select control lists the operators.")
    assert "select_option" not in backed


def test_a_computed_color_assertion_needs_a_stated_value():
    """样式名不是色值：原文说『Red fill』，断言 rgb(...) 断的是实现的自由
    （sheet REQ-7-2-1 的原文给了 rgb，所以它不亮 —— 只有没给值的才亮）。"""
    spec = ("test('t', async () => { await expect(row).toHaveCSS('background-color', "
            "'rgb(254, 226, 226)'); });")
    named = M.spec_technique_bets(spec, "Over-budget rows are painted with a Red fill.")
    assert named.get("style_color") == 1
    stated = M.spec_technique_bets(
        spec, "Over-budget rows have the background color rgb(254, 226, 226).")
    assert "style_color" not in stated


def test_a_digit_format_regex_needs_the_digest_to_state_one():
    """摘要里的数字常只是编号（REQ-6-3-1、Scenario 1）—— 剥掉编号后仍算
    「没有数字」才亮（校准踩过两回：不剥编号这条永远不亮）。"""
    spec = ("test('t', async () => { await expect(page.getByText(/[0-9]+\\s+commit/i))"
            ".toBeVisible(); });")
    unbacked = M.spec_technique_bets(
        spec, "REQ-6-3-1: the page shows commit information.\nScenario 1: it loads.")
    assert unbacked.get("format_regex") == 1
    stated = M.spec_technique_bets(spec, "The page shows 3 commits.")
    assert "format_regex" not in stated


def test_a_role_call_passing_a_foreign_control_is_exempt():
    """github 15 处 searchbox 里 14 处是全局搜索框（名字 'Search'，本节点摘要
    没引它）—— 角色写在别的节点摘要里，那是路过不是赌注。真赌注是 REQ-5-3-1：
    摘要引了 `Search assignees`、给的角色是 textbox，卷子按 searchbox 定位。"""
    global_call = ("test('t', async () => { await page.getByRole('searchbox', "
                   "{ name: 'Search', exact: true }).fill('x'); });")
    passing = M.spec_technique_bets(global_call, "Enter a repository name to locate it.")
    assert "role_unbacked" not in passing
    own = ("test('t', async () => { await page.getByRole('searchbox', "
           "{ name: 'Search assignees' }).fill('bob'); });")
    flagged = M.spec_technique_bets(own, "The user fills `Search assignees` with a name.")
    assert flagged.get("role_unbacked") == ["searchbox"]
    backed = M.spec_technique_bets(own, "A searchbox named `Search assignees` filters it.")
    assert "role_unbacked" not in backed


def test_a_seeded_name_is_exempt_when_any_digest_quotes_it():
    """种子对象常写在别的节点摘要里（acme-docs 在 REQ-3-3、secret-research 在
    REQ-5-5），本节点引用它合法。要抓的是外推：原文引了 `...-s1`，卷子用
    `...-s9` —— 整套摘要里都没有的名字。"""
    sibling = {"REQ-1": "x", "REQ-2": "The repository `search-target-s1` holds the code."}
    quoted = "test('t', async () => { await page.getByText('search-target-s1').click(); });"
    audit = M.suite_audit(["REQ-1", "REQ-2"], sibling, {"REQ-1": quoted})
    assert audit["technique"]["fixed_names"] == [], "兄弟节点引过的名字不算外推"
    extrapolated = "test('t', async () => { await page.getByText('search-target-s9').click(); });"
    audit2 = M.suite_audit(["REQ-1", "REQ-2"], sibling, {"REQ-1": extrapolated})
    assert audit2["technique"]["fixed_names"] == ["REQ-1"]
    assert audit2["nodes"]["REQ-1"]["technique"]["fixed_names"] == ["search-target-s9"]


def test_the_technique_bets_reach_every_surface():
    spec = "test('t', async () => { await page.getByLabel('Op').selectOption('a'); });"
    audit = M.suite_audit(["REQ-1"], {"REQ-1": "Choose the operator."}, {"REQ-1": spec})
    assert "betting on a DOM shape" in M.suite_audit_line(audit)
    assert "selectOption" in M.suite_audit_hints(audit, ["REQ-1"])
    assert "bet on a DOM shape" in M.suite_audit_completion_note(audit)
    text = M.render_suite_audit(audit)
    assert "DOM-shape bets" in text and "selectOption" in text


def test_a_clean_spec_bets_on_nothing():
    spec = ("test('t', async () => { await page.getByRole('button', "
            "{ name: 'Create account', exact: true }).click(); });")
    audit = M.suite_audit(["REQ-1"], {"REQ-1": "A button named 'Create account' appears."},
                          {"REQ-1": spec})
    assert audit["technique"]["nodes"] == []
    assert "betting on a DOM shape" not in M.suite_audit_line(audit)
    assert "DOM-shape bets" not in M.render_suite_audit(audit)
    assert M.suite_audit_completion_note(audit) == ""


# ---------------------------------------------------------------- 完成信号

def test_the_completion_note_is_silent_when_there_is_nothing_to_report():
    assert M.suite_audit_completion_note(None) == ""
    clean = M.suite_audit(["REQ-1"], {"REQ-1": "A button named 'New workbook' creates one."},
                          {"REQ-1": "test('x', async () => {});"})
    assert M.suite_audit_completion_note(clean) == ""


def test_the_completion_note_names_the_gap_nodes():
    digests = {"REQ-1": "The comment persists on the server."}
    specs = {"REQ-1": "test('x', async () => {});"}
    note = M.suite_audit_completion_note(M.suite_audit(["REQ-1"], digests, specs))
    assert "1/1" in note and "REQ-1" in note


# ---------------------------------------------------------------- 提示词

def test_the_authoring_prompt_carries_the_five_mandatory_categories():
    """A 档的出题侧：五类科目写进提示词，一个都不能少。"""
    text = M.ACCEPTANCE_AUTHOR_PROMPT
    assert "Five further checks are mandatory" in text
    assert "newContext" in text, "跨会话验证的写法必须给出"
    assert "TWO sessions" in text, "多账号场景必须点破"
    assert "boundary cases" in text, "边界值必须点破"
    assert "aria-describedby" in text and "aria-invalid" in text, "错误关联必须点破"
    assert "not using what it made" in text, "第五类：建了要用必须点破"
    assert "creates or imports" in text, "第五类的触发条件必须给出"


def test_the_audit_prompt_also_carries_the_journey_category():
    """找茬侧同样要拿着第五类科目去逐句读卷子。"""
    text = M.ACCEPTANCE_SUITE_AUDIT_PROMPT
    assert "never uses what was created in the same test" in text
    assert "or from inside a single spec file" in text, "跨卷子盲区要在开场点破"


def test_the_audit_prompt_formats_with_its_three_inputs():
    """占位符只有三个；`.format` 抛 KeyError 会让整个审计段静默失踪。"""
    text = M.ACCEPTANCE_SUITE_AUDIT_PROMPT.format(
        index="  REQ-1  Alpha", files="  REQ-1\n    spec: x.spec.ts",
        mechanical="  - REQ-1: gap")
    assert "{mechanical}" not in text and "{index}" not in text and "{files}" not in text
    assert "REQ-1" in text and "gap" in text
    # 找茬回合的底线规则必须在
    assert "Never delete" in text
    assert "frozen" in text, "要写明这是冻结前的最后机会"


# ---------------------------------------------------------------- 流程

def test_auditing_opens_one_turn_per_group_after_authoring(tmp_path):
    tree = _tree()
    layers = layerize(tree)
    flow = _AuditFlow(tmp_path)
    flow.author_acceptance_suite(tree, list(layers.order))

    groups = M.authoring_groups(layers, layers.leaves)
    author_labels = [l for l in flow.labels if l.startswith("author specs")]
    audit_labels = [l for l in flow.labels if l.startswith("audit specs")]
    assert len(author_labels) == len(groups)
    assert len(audit_labels) == len(groups)
    assert flow.labels[: len(groups)] == author_labels, "找茬必须发生在出卷之后"
    # 找茬提示词带上了机械体检的缺考清单（这一组的节点被根上的持久化契约标了）
    first_audit_prompt = flow.prompts[len(author_labels)]
    assert "REQ-1-1" in first_audit_prompt
    assert "leaves the first browser session" in first_audit_prompt


def test_a_spec_the_audit_writes_counts_in_the_final_map(tmp_path):
    """盘点在审计之后：找茬回合补出来的卷子同样算数（补出来的就是卷子）。"""
    tree = _tree()
    layers = layerize(tree)
    flow = _AuditFlow(tmp_path, author_skips=("REQ-2-1",), audit_writes=("REQ-2-1",))
    mapping = flow.author_acceptance_suite(tree, list(layers.order))
    assert mapping.get("REQ-2-1") == ["REQ-2-1.spec.ts"]


def test_the_audit_has_teeth_before_the_freeze(tmp_path):
    """缺考的节点在找茬提示词里被点名，且没卷子的节点会被要求现写一份。"""
    tree = _tree()
    layers = layerize(tree)
    flow = _AuditFlow(tmp_path, author_skips=("REQ-2-1",))
    flow.author_acceptance_suite(tree, list(layers.order))
    audit_prompts = "".join(p for l, p in zip(flow.labels, flow.prompts)
                            if l.startswith("audit specs"))
    assert "REQ-2-1" in audit_prompts
    assert "none yet" in audit_prompts, "没卷子的节点要说明「现在写一份」"


def test_the_audit_can_be_switched_off_but_the_scan_still_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOS_ARC_AUDIT_TESTS", "0")
    tree = _tree()
    layers = layerize(tree)
    flow = _AuditFlow(tmp_path)
    mapping = flow.author_acceptance_suite(tree, list(layers.order))
    assert not [l for l in flow.labels if l.startswith("audit specs")]
    assert mapping.get("REQ-1-1") == ["REQ-1-1.spec.ts"], "关掉找茬不影响出卷"
    report = tmp_path / M.AUTHOR_SUITE_DIR / M.SUITE_AUDIT_DIR / M.SUITE_AUDIT_FILE
    assert report.is_file(), "体检是纯文本分析，无论开关都跑"
    assert flow.suite_audit["totals"]["nodes"] == len(layers.leaves)


def test_the_report_lands_on_disk_and_names_the_gaps(tmp_path):
    tree = _tree()
    layers = layerize(tree)
    flow = _AuditFlow(tmp_path)
    flow.author_acceptance_suite(tree, list(layers.order))
    report = (tmp_path / M.AUTHOR_SUITE_DIR / M.SUITE_AUDIT_DIR / M.SUITE_AUDIT_FILE)
    text = report.read_text(encoding="utf-8")
    assert "| REQ-1-1 |" in text
    # 根上的持久化契约渗进两个叶子的出卷依据，两份单会话卷子都该被标出来
    assert flow.suite_audit["gap_nodes"] == ["REQ-1-1", "REQ-2-1"]
    assert "REQ-1-1" in M.suite_audit_completion_note(flow.suite_audit)


def test_a_failing_audit_never_takes_the_suite_down_with_it(tmp_path):
    """审计是增值，不是前置条件：它崩了只会少一份报告。"""
    tree = _tree()
    flow = _AuditFlow(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("audit exploded")

    flow.audit_authored_suite = boom
    mapping = flow.author_acceptance_suite(tree, list(layerize(tree).order))
    assert mapping.get("REQ-1-1") == ["REQ-1-1.spec.ts"]
    assert flow.suite_audit is None


def test_the_shared_object_report_is_rewritten_after_the_audit_turns(tmp_path):
    """找茬回合会改卷子 —— 公共对象报告与完成信号必须是**改完之后**的盘面。

    回合开始前那份扫描只用来喂本批提示词；如果拿它当最终盘点，审计刚补上的
    复原动作（或刚引入的没复原的手）就会与世界脱节：报告说假话，完成信号也假。
    """
    tree = _tree()
    layers = layerize(tree)
    req = tmp_path / "requirements"
    req.mkdir()
    (req / "requirements.yaml").write_text(
        "GIVEN the seeded workbook `Q3 Sales` is already open for the user.\n",
        encoding="utf-8")
    flow = _AuditFlow(tmp_path, author_skips=("REQ-2-1",), audit_writes=("REQ-2-1",),
                      audit_body="await page.getByText('Q3 Sales').click();\n"
                                 "await page.getByLabel('Edit A1').fill('changed');\n")
    flow.req_dir = req
    flow.author_acceptance_suite(tree, list(layers.order))

    report = tmp_path / M.AUTHOR_SUITE_DIR / M.SUITE_AUDIT_DIR / M.SUITE_SHARED_FILE
    text = report.read_text(encoding="utf-8")
    assert "Q3 Sales" in text, "清单从需求原文点名"
    assert "REQ-2-1.spec.ts" in text, "回合改完的盘面才是报告（改前它还没碰过公共对象）"
    note = M.suite_audit_completion_note(flow.suite_audit)
    assert "REQ-2-1.spec.ts" in note and "shared" in note


# ---------------------------------------------------------------- 缺考处置（硬任务，2026-10-07）

def _disp_audit(gap_nodes, filled=(), waived=None, undisposed=(), journey_gap=False):
    """手搓一份带处置信息的体检盘面 —— 这一节测的是口径，不是检测本身。"""
    return {
        "gap_nodes": list(gap_nodes),
        "nodes": {leaf: {"gaps": ["persistence/storage language stated, but no check ever "
                                 "leaves the first browser session"]}
                  for leaf in gap_nodes},
        "journey": {"gap": journey_gap, "creators": [], "writers": [], "bridging": []},
        "totals": {"nodes": 3},
        "gap_disposition": {"filled": list(filled), "waived": dict(waived or {}),
                            "undisposed": list(undisposed)},
    }


def test_a_waiver_needs_words_not_just_a_node_id(tmp_path):
    """豁免的判据是「写了为什么」—— 光有 ID 的行不算交代；格式容错但理由不容错。"""
    suite = tmp_path / M.AUTHOR_SUITE_DIR
    (suite / M.SUITE_AUDIT_DIR).mkdir(parents=True)
    (suite / M.SUITE_AUDIT_DIR / M.SUITE_WAIVER_FILE).write_text(
        "# 缺考豁免\n\n"
        "- REQ-1-2-2: the digest states the value exists, not a rendering format\n"
        "* REQ-3-1-1：需求只说该功能存在，没有可机械断言的形状\n"
        "**REQ-4-1**: doubled-star form still parses\n"
        "- REQ-9: \n"
        "- not a waiver line\n", encoding="utf-8")
    waivers = M.read_gap_waivers(suite)
    assert set(waivers) == {"REQ-1-2-2", "REQ-3-1-1", "REQ-4-1"}
    assert "rendering format" in waivers["REQ-1-2-2"]
    assert "可机械断言" in waivers["REQ-3-1-1"]
    assert M.read_gap_waivers(tmp_path) == {}, "文件不存在 = 一条都没交代"


def test_the_disposition_splits_filled_waived_and_undisposed():
    """「补上了」看盘面差，不看回合说了什么；说了没做到的不算。"""
    digests = {leaf: "The comment persists on the server and is visible to every "
                     "signed-in user." for leaf in ("REQ-1", "REQ-2", "REQ-3")}
    single = {leaf: "test('x', async () => {});" for leaf in digests}
    before = M.suite_audit(list(digests), digests, single)
    fixed = dict(single, **{"REQ-1": "await page.context().browser().newContext();"})
    final = M.suite_audit(list(digests), digests, fixed)
    disp = M.gap_disposition(before, final, {"REQ-2": "no mechanical check can pin it"})
    assert disp["filled"] == ["REQ-1"]
    assert disp["waived"] == {"REQ-2": "no mechanical check can pin it"}
    assert disp["undisposed"] == ["REQ-3"]


def test_the_journey_gap_is_dispositioned_under_the_reserved_id():
    """全套级「建了要用」缺考用保留 ID `suite` 交代 —— 和节点缺考同一本账。"""
    before = {"gap_nodes": [], "journey": {"gap": True}}
    still_open = {"gap_nodes": [], "journey": {"gap": True}}
    waived = M.gap_disposition(before, still_open,
                               {"suite": "no entity of this kind exists yet"})
    assert waived["waived"] == {"suite": "no entity of this kind exists yet"}
    assert waived["undisposed"] == []
    open_gap = M.gap_disposition(before, still_open, {})
    assert open_gap["undisposed"] == ["suite"]
    filled = M.gap_disposition(before, {"gap_nodes": [], "journey": {"gap": False}}, {})
    assert filled["filled"] == ["suite"]


def test_the_disposition_note_names_the_items_and_the_waivers_file(tmp_path):
    audit = M.suite_audit(["REQ-1", "REQ-2"],
                          {"REQ-1": "The comment persists on the server.",
                           "REQ-2": "The comment persists on the server."},
                          {"REQ-1": "test('x', async () => {});",
                           "REQ-2": "await page.context().browser().newContext();"})
    note = M.suite_gap_disposition_note(audit, ["REQ-1"], tmp_path)
    assert "MUST be dispositioned" in note and "REQ-1" in note
    assert "REQ-2" not in note, "没缺考的节点不进来"
    assert M.SUITE_WAIVER_FILE in note
    assert M.suite_gap_disposition_note(audit, ["REQ-2"], tmp_path) == "", "没缺考就不添噪音"


def test_the_completion_note_says_waived_versus_unaccounted():
    audit = _disp_audit(["REQ-2", "REQ-3"], waived={"REQ-2": "no mechanical check"},
                        undisposed=["REQ-3"])
    note = M.suite_audit_completion_note(audit)
    assert "got neither a check nor a written reason" in note and "REQ-3" in note
    assert "waived with a written reason" in note and "REQ-2" in note
    settled = _disp_audit(["REQ-2"], waived={"REQ-2": "no mechanical check"}, undisposed=[])
    note = M.suite_audit_completion_note(settled)
    assert "waived with a written reason" in note
    assert "got neither" not in note, "豁免过的条目不许再当没交代说"


def test_the_completion_note_distinguishes_the_journey_disposition():
    waived = _disp_audit([], waived={"suite": "the entity is read-only in this app"},
                         journey_gap=True)
    assert "waived with a written reason" in M.suite_audit_completion_note(waived)
    open_gap = _disp_audit([], undisposed=["suite"], journey_gap=True)
    assert "no written reason was given" in M.suite_audit_completion_note(open_gap)


def test_the_report_lists_every_disposition():
    audit = _disp_audit(["REQ-3"], filled=["REQ-1"], waived={"REQ-2": "not checkable"},
                        undisposed=["REQ-3"])
    text = M.render_suite_audit(audit)
    assert "flagged-item dispositions" in text
    assert "filled during the audit: 1 (REQ-1)" in text
    assert "waived with a written reason: 1" in text and "REQ-2: not checkable" in text
    assert "no disposition at all" in text and "REQ-3" in text
    settled = M.render_suite_audit(
        _disp_audit(["REQ-2"], waived={"REQ-2": "not checkable"}, undisposed=[]))
    assert "every item on the list is answered" in settled
    assert "**no disposition at all" not in settled


def test_the_audit_prompt_makes_the_missing_check_list_mandatory():
    text = M.ACCEPTANCE_SUITE_AUDIT_PROMPT.format(
        index="  REQ-1  Alpha", files="  REQ-1\n    spec: x.spec.ts",
        mechanical="  - REQ-1: persistence/storage language stated")
    assert "mandatory, not advisory" in text
    assert "waiver with a reason" in text
    assert "persistence/storage language stated" in text


def test_the_disposition_prompt_asks_once_per_item():
    text = M.ACCEPTANCE_DISPOSITION_PROMPT.format(
        waivers="/x/_audit/suite-audit-waivers.md",
        items="- REQ-9: persistence/storage language stated")
    assert "no disposition yet" in text
    assert "REQ-9" in text and "suite-audit-waivers.md" in text
    assert "never delete" in text, "追账回合同样不许动已有的测试"


def test_the_disposition_round_asks_once_about_unanswered_items(tmp_path):
    """找茬跑完还有条目没交代 → 只追一次，逐条要个说法（排练实测：软提示下
    补 21 条测试只消掉 1 条缺考）。"""
    tree = _tree()
    layers = layerize(tree)
    flow = _AuditFlow(tmp_path)
    flow.author_acceptance_suite(tree, list(layers.order))
    rounds = [i for i, l in enumerate(flow.labels) if l.startswith("disposition round")]
    assert len(rounds) == 1, "只追一次，不循环"
    last_audit = max(i for i, l in enumerate(flow.labels) if l.startswith("audit specs"))
    assert rounds[0] > last_audit, "追账发生在找茬之后"
    prompt = flow.prompts[rounds[0]]
    for leaf in flow.suite_audit["gap_nodes"]:
        assert leaf in prompt, "每条没交代的都要点名"
    assert "REQ-1-1.spec.ts" in prompt, "点名要带上卷子路径"
    assert M.SUITE_WAIVER_FILE in prompt
    assert flow.suite_audit["gap_disposition"]["undisposed"], "没写豁免就还是没交代"


def test_a_written_waiver_closes_the_missing_check(tmp_path):
    """写了理由的缺考算交代过：不再追账，完成信号如实说「豁免」。"""
    tree = _tree()
    layers = layerize(tree)
    flow = _AuditFlow(tmp_path)
    suite = tmp_path / M.AUTHOR_SUITE_DIR
    (suite / M.SUITE_AUDIT_DIR).mkdir(parents=True)
    (suite / M.SUITE_AUDIT_DIR / M.SUITE_WAIVER_FILE).write_text(
        "- REQ-1-1: the outcome is only a visual claim, nothing a locator can pin\n"
        "- REQ-2-1: same wording family as REQ-1-1\n", encoding="utf-8")
    flow.author_acceptance_suite(tree, list(layers.order))
    assert not [l for l in flow.labels if l.startswith("disposition round")], \
        "都交代了就不用追账"
    disp = flow.suite_audit["gap_disposition"]
    assert set(disp["waived"]) == {"REQ-1-1", "REQ-2-1"}
    assert disp["undisposed"] == []
    note = M.suite_audit_completion_note(flow.suite_audit)
    assert "waived with a written reason" in note
    assert "got neither" not in note
    report = (suite / M.SUITE_AUDIT_DIR / M.SUITE_AUDIT_FILE).read_text(encoding="utf-8")
    assert "waived with a written reason" in report


def test_the_disposition_is_off_when_the_audits_are(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOS_ARC_AUDIT_TESTS", "0")
    tree = _tree()
    flow = _AuditFlow(tmp_path)
    flow.author_acceptance_suite(tree, list(layerize(tree).order))
    assert not [l for l in flow.labels if l.startswith("disposition round")]
    assert "gap_disposition" not in flow.suite_audit, "关掉审计回到只报不追的老行为"


# ---------------------------------------------------------------- 扫描器进站（2026-10-07）

# 和排练里那份真卷同一个形状（`排练\sheet-出卷-1/acceptance/REQ-6-1-1.spec.ts`，
# 决赛排练实测对任何实现都红）：`async function` 助手直接进 expect()，拿到的是
# Promise，Playwright 对 locator 断言直接拒收。
_DEFECT_SPEC = """\
import { test, expect } from '@playwright/test';

async function menuButton(page: Page, label: string) {
  return page.getByRole('button', { name: label });
}

test('the toolbar offers the menu', async ({ page }) => {
  await page.goto('/');
  await expect(menuButton(page, 'Open')).toBeVisible();
});
"""

# 同一份卷子，多开一个浏览器会话 —— 把它从「缺考」那边摘出去，只留下缺陷。
_DEFECT_SPEC_WITH_SESSION = _DEFECT_SPEC.replace(
    "  await page.goto('/');",
    "  await page.goto('/');\n"
    "  const second = await page.context().browser().newContext();\n"
    "  await second.close();")

_SAFE_SPEC = "test('x', async () => {});\n"


def test_the_scanner_catches_the_shape_that_killed_the_rehearsal_spec():
    """机械判据本身：`async function` 助手进 expect() 必抓；合规写法不许误报。"""
    defects = M.spec_structural_defects(_DEFECT_SPEC)
    assert defects and "menuButton" in defects[0], "助手名字要点出来"
    assert "for **every** implementation" in defects[0], "原话给的是修法，不是判词"
    ok = ("async function h() { return 1; }\n"
          "await expect(h()).resolves.toBe(1);\n"
          "function loc() { return 1; }\n"
          "await expect(loc()).toBeVisible();\n")
    assert M.spec_structural_defects(ok) == [], "await 进 expect / .resolves / 非 async 都不算"


def test_a_spec_defect_rides_the_same_disposition_channel(tmp_path):
    """过不了的断言和缺考**同一本账**：修掉看盘面差，修不掉要么改卷子要么写豁免。"""
    digests = {leaf: "The toolbar shows a menu button." for leaf in ("REQ-1", "REQ-2")}
    before = M.suite_audit(list(digests), digests,
                           {"REQ-1": _DEFECT_SPEC, "REQ-2": _SAFE_SPEC})
    assert before["defect_nodes"] == ["REQ-1"]
    assert before["gap_nodes"] == [], "这批料只出缺陷，不出缺考 —— 两条通道各测各的"
    fixed = M.suite_audit(list(digests), digests,
                          {"REQ-1": _SAFE_SPEC, "REQ-2": _SAFE_SPEC})
    assert M.gap_disposition(before, fixed, {})["filled"] == ["REQ-1"], "改好了算交代"
    still = M.gap_disposition(before, before, {})
    assert still["undisposed"] == ["REQ-1"] and still["waived"] == {}
    waived = M.gap_disposition(before, before, {"REQ-1": "the helper is awaited before expect"})
    assert waived["undisposed"] == []
    assert waived["waived"] == {"REQ-1": "the helper is awaited before expect"}


def test_the_disposition_note_names_the_broken_assertion(tmp_path):
    """找茬回合的硬任务清单：缺陷条目带着扫描器原话（连同修法）。"""
    digests = {leaf: "The toolbar shows a menu button." for leaf in ("REQ-1", "REQ-2")}
    audit = M.suite_audit(list(digests), digests,
                          {"REQ-1": _DEFECT_SPEC, "REQ-2": _SAFE_SPEC})
    note = M.suite_gap_disposition_note(audit, ["REQ-1"], tmp_path)
    assert "MUST be dispositioned" in note and "REQ-1" in note
    assert "handed a Promise" in note and "menuButton" in note
    assert M.suite_gap_disposition_note(audit, ["REQ-2"], tmp_path) == "", \
        "干净卷子不添噪音"


def test_the_chase_items_carry_the_broken_assertion(tmp_path):
    """追账回合逐条要说法：缺陷条目给的是同一个欠条格式（欠什么 + 卷子在哪）。"""
    digests = {"REQ-1": "The toolbar shows a menu button."}
    audit = M.suite_audit(list(digests), digests, {"REQ-1": _DEFECT_SPEC})
    items = M.disposition_items(audit, ["REQ-1"], tmp_path)
    assert "REQ-1" in items and "handed a Promise" in items and "menuButton" in items
    assert "none yet" in items, "卷子还没落盘就说不出来在哪"
    (tmp_path / "REQ-1.spec.ts").write_text(_DEFECT_SPEC, encoding="utf-8")
    assert "REQ-1.spec.ts" in M.disposition_items(audit, ["REQ-1"], tmp_path)


def test_the_completion_note_calls_the_defect_by_its_name():
    """完成信号不许含糊：没交代的缺陷点名，写了豁免的说豁免，审计关掉也要如实报。"""
    audit = M.suite_audit(["REQ-1"], {"REQ-1": "The toolbar shows a menu button."},
                          {"REQ-1": _DEFECT_SPEC})
    audit["gap_disposition"] = M.gap_disposition(audit, audit, {})
    note = M.suite_audit_completion_note(audit)
    assert "an assertion no implementation can pass" in note and "REQ-1" in note
    assert "got neither" not in note, "缺考那半句不许串到缺陷头上"
    audit["gap_disposition"] = M.gap_disposition(audit, audit,
                                                 {"REQ-1": "false positive: awaited upstream"})
    note = M.suite_audit_completion_note(audit)
    assert "kept on a written reason" in note and "REQ-1" in note
    off = M.suite_audit(["REQ-1"], {"REQ-1": "The toolbar shows a menu button."},
                        {"REQ-1": _DEFECT_SPEC})
    assert "no implementation can pass" in M.suite_audit_completion_note(off), \
        "只报不追的老口径同样要报出来"


def test_the_report_lists_the_broken_assertion_with_its_leaf():
    audit = M.suite_audit(["REQ-9"], {"REQ-9": "The toolbar shows a menu button."},
                          {"REQ-9": _DEFECT_SPEC})
    audit["gap_disposition"] = M.gap_disposition(audit, audit, {})
    text = M.render_suite_audit(audit)
    assert "assertions no implementation can pass" in text
    assert "[REQ-9]" in text and "menuButton" in text
    assert "no disposition at all" in text and "REQ-9" in text


def test_a_defect_the_audit_writes_is_chased_like_a_gap(tmp_path):
    """端到端：找茬回合补上的卷子带着过不了的断言，只出缺陷、不出缺考 ——
    追账回合照样点名（#1 进站要的就是这个：缺陷不许停在做提示）。"""
    tree = _tree()
    flow = _AuditFlow(tmp_path, author_skips=("REQ-2-1",), audit_writes=("REQ-2-1",),
                      audit_body=_DEFECT_SPEC_WITH_SESSION)
    flow.author_acceptance_suite(tree, list(layerize(tree).order))
    audit = flow.suite_audit
    assert audit["defect_nodes"] == ["REQ-2-1"]
    assert "REQ-2-1" not in audit["gap_nodes"], "开了第二个会话，缺考那边已经结清"
    disp = audit["gap_disposition"]
    assert disp["undisposed"] == ["REQ-1-1", "REQ-2-1"], "缺考 + 缺陷同列一张单子"
    chase = [p for l, p in zip(flow.labels, flow.prompts)
             if l.startswith("disposition round")]
    assert len(chase) == 1
    assert "REQ-2-1" in chase[0] and "handed a Promise" in chase[0]
    assert "REQ-2-1.spec.ts" in chase[0]
    note = M.suite_audit_completion_note(audit)
    assert "an assertion no implementation can pass" in note and "REQ-2-1" in note
    report = (tmp_path / M.AUTHOR_SUITE_DIR / M.SUITE_AUDIT_DIR /
              M.SUITE_AUDIT_FILE).read_text(encoding="utf-8")
    assert "assertions no implementation can pass" in report and "[REQ-2-1]" in report


def test_a_defect_only_suite_still_gets_a_disposition_book(tmp_path):
    """只出缺陷、没有缺考的卷面同样要挂账（挂账条件里带着 defect_nodes）——
    不挂账 = 缺陷只停在报告里，这轮连账本都不会开。"""
    tree = _tree()
    flow = _AuditFlow(tmp_path, audit_writes=("REQ-1-1", "REQ-2-1"),
                      audit_body=_DEFECT_SPEC_WITH_SESSION)
    flow.author_acceptance_suite(tree, list(layerize(tree).order))
    audit = flow.suite_audit
    assert audit["gap_nodes"] == [], "两张卷子都开了第二个浏览器会话，缺考那边已结清"
    assert audit["defect_nodes"] == ["REQ-1-1", "REQ-2-1"]
    assert "gap_disposition" in audit, "缺陷单独就能把账本挂上"
    assert audit["gap_disposition"]["undisposed"] == ["REQ-1-1", "REQ-2-1"]
    assert audit["gap_disposition"]["filled"] == [], "还在单子上的不算补上"
    assert [l for l in flow.labels if l.startswith("disposition round")], "追账回合开得起来"


# ---------------------------------------------------------------- 字面量对账（2026-10-07）

# 卷面钉死的名字/角色对不上需求原文的头两档（硬任务）：外推名与没背书的角色。
# 校准：全排练 356 份真卷里这两档共 12 处命中，逐条是真；fill/type 写进去的数据、
# 自建对象、全局路过控件都够不着判据（详见 `_extrapolated_names` 的 docstring）。
# 第三档（对格子直接 fill 而原文交代了双击编辑框）见本段末尾。
_LIT_DIGEST = ("The selector has a textbox named `Search people`. The system pre-provisions "
               "repository `demo-widget-repo-s1`.")
_LIT_SPEC = ("test('finds people', async ({ page }) => {\n"
             "  await page.getByRole('searchbox', { name: 'Search people' }).fill('ana');\n"
             "  await page.getByRole('link', { name: 'demo-widget-repo-s2' }).click();\n"
             "});\n")


def test_a_name_extrapolated_from_a_quoted_one_is_the_hard_class():
    """原文钉 `x-s1`、卷子写 `x-s2` —— 基名在原文里、名字不在，是硬任务那一档；
    纯自造数据（fill 的值）够不着它，仍留在提示层。"""
    digest = "The system pre-provisions repository `demo-widget-repo-s1` for the scenario."
    spec = ("await page.getByRole('link', { name: 'demo-widget-repo-s1' }).click();\n"
            "await page.getByRole('link', { name: 'demo-widget-repo-s2' }).click();\n"
            "await page.getByLabel('Widget title').fill('typed-thing-value');\n")
    low = digest.lower()
    assert M._extrapolated_names(spec, low, corpus=low) == \
        {"demo-widget-repo-s2": "demo-widget-repo"}
    assert "typed-thing-value" in M._unbacked_names(spec, low, low), "提示层照旧收"
    assert M._extrapolated_names(spec, low, low) == {"demo-widget-repo-s2": "demo-widget-repo"}


def test_the_technique_bets_expose_the_extrapolated_subset():
    digest = "The system pre-provisions repository `demo-widget-repo-s1` for the scenario."
    spec = "await page.getByRole('link', { name: 'demo-widget-repo-s2' }).click();\n"
    bets = M.spec_technique_bets(spec, digest, corpus=digest)
    assert bets["extrapolated_names"] == {"demo-widget-repo-s2": "demo-widget-repo"}
    assert "demo-widget-repo-s2" in bets["fixed_names"], "提示层的总名单不变"


def test_a_role_the_text_never_backs_is_a_literal_too():
    """卷子按 `searchbox` 定位、原文写的是 textbox —— 角色的赌注进同一本账。"""
    audit = M.suite_audit(["REQ-7"], {"REQ-7": _LIT_DIGEST}, {"REQ-7": _LIT_SPEC})
    lines = audit["nodes"]["REQ-7"]["literals"]
    assert any("role `searchbox`" in l and "use the role the text names" in l for l in lines)
    assert any("`demo-widget-repo-s2`" in l and "`demo-widget-repo`" in l for l in lines)


def test_a_literal_mismatch_rides_the_same_disposition_channel():
    """和缺考、结构性缺陷同一本账：修掉看盘面差，修不掉改卷子或写豁免。"""
    digests = {"REQ-7": _LIT_DIGEST}
    audit = M.suite_audit(["REQ-7"], digests, {"REQ-7": _LIT_SPEC})
    assert audit["literal_nodes"] == ["REQ-7"]
    assert audit["gap_nodes"] == [] and audit["defect_nodes"] == [], "这批料只出字面量欠账"
    assert M.gap_disposition(audit, audit, {})["undisposed"] == ["REQ-7"]
    waived = M.gap_disposition(audit, audit, {"REQ-7": "false positive: the text says textbox"})
    assert waived["undisposed"] == [] and waived["waived"]["REQ-7"]
    fixed = M.suite_audit(["REQ-7"], digests, {"REQ-7": "test('x', async () => {});"})
    assert M.gap_disposition(audit, fixed, {})["filled"] == ["REQ-7"]


def test_the_literal_owes_show_up_in_every_surface(tmp_path):
    audit = M.suite_audit(["REQ-7"], {"REQ-7": _LIT_DIGEST}, {"REQ-7": _LIT_SPEC})
    note = M.suite_gap_disposition_note(audit, ["REQ-7"], tmp_path)
    assert "MUST be dispositioned" in note
    assert "searchbox" in note and "demo-widget-repo-s2" in note
    items = M.disposition_items(audit, ["REQ-7"], tmp_path)
    assert "searchbox" in items and "demo-widget-repo-s2" in items
    assert "the requirement text does not back" in M.suite_audit_completion_note(audit), \
        "审计关掉 = 只报不追，也要如实报"
    audit["gap_disposition"] = M.gap_disposition(audit, audit, {})
    txt = M.suite_audit_completion_note(audit)
    assert "use a name, role or interaction the requirement text does not back" in txt
    assert "REQ-7" in txt
    waived = M.gap_disposition(audit, audit, {"REQ-7": "false positive"})
    audit["gap_disposition"] = waived
    assert "kept on a written reason" in M.suite_audit_completion_note(audit)
    report = M.render_suite_audit(audit)
    assert "flagged-item dispositions" in report and "no disposition at all: none" in report


# 第三档（#2② 的落地形状）：原文自己交代了进格子方式（双击 → 内联编辑框），卷子
# 却直接对格子元素 fill。校准：全排练 sheet 系 8 套 × 1 节点命中（全是出卷审计已
# 判定的强条件项），github 系（无网格）0 命中；词表是通用 UI 说法，无任务字段。
_CELLFILL_DIGEST = ("Data entry goes through the current grid. Double-clicking a grid cell "
                    "displays an inline text box where the value is typed.")
_CELLFILL_SPEC = ("test('enters a value', async ({ page }) => {\n"
                 "  await page.getByRole('gridcell', { name: 'Total' }).fill('42');\n"
                 "});\n")


def test_a_double_click_inline_editor_in_the_text_is_what_makes_it_a_defect():
    assert M._grid_editor_stated(_CELLFILL_DIGEST.lower())
    assert not M._grid_editor_stated("double-clicking a row selects it"), \
        "只提双击（选中用途）不算交代了进格子的方式"
    assert not M._grid_editor_stated("the page shows an inline text box"), \
        "没有双击也不算"


def test_a_gridcell_fill_contradicting_the_stated_editor_is_the_hard_class():
    """原文交代了双击出编辑框 → 直接对格子 fill 进硬任务；原文没交代进格子方式时，
    同一个写法只是没据的赌注，留在提示层（赌注未必错）。"""
    low = _CELLFILL_DIGEST.lower()
    bets = M.spec_technique_bets(_CELLFILL_SPEC, low, corpus=low)
    assert bets["cell_fill"] == 1 and bets["cell_fill_contradicted"] == 1
    quiet = "The grid lists rows and computes a total."
    quiet_bets = M.spec_technique_bets(_CELLFILL_SPEC, quiet, corpus=quiet)
    assert quiet_bets.get("cell_fill") == 1 and "cell_fill_contradicted" not in quiet_bets


def test_the_gridcell_contradiction_rides_the_same_disposition_channel():
    """第三档和缺考/缺陷/前两档字面量走同一本账：欠条、盘面差、完成信号全认。"""
    audit = M.suite_audit(["REQ-7"], {"REQ-7": _CELLFILL_DIGEST},
                          {"REQ-7": _CELLFILL_SPEC})
    lines = audit["nodes"]["REQ-7"]["literals"]
    assert any("routes cell entry through a double-click inline editor" in l for l in lines)
    assert audit["literal_nodes"] == ["REQ-7"]
    assert audit["gap_nodes"] == [] and audit["defect_nodes"] == []
    assert M.gap_disposition(audit, audit, {})["undisposed"] == ["REQ-7"]
    audit["gap_disposition"] = M.gap_disposition(audit, audit, {})
    assert "does not back" in M.suite_audit_completion_note(audit)
    waived = M.gap_disposition(audit, audit, {"REQ-7": "the grid renders editable cells"})
    assert waived["undisposed"] == [] and waived["waived"]["REQ-7"]


def _lit_tree():
    """只带一条引用固定名的需求原文的小树 —— 没有持久化句型（不招惹缺考）。"""
    return {
        "id": "ROOT", "name": "App", "type": "FOLDER",
        "description": "The product pre-provisions repository `demo-widget-repo-s1`.",
        "children": [
            {"id": "REQ-7", "name": "Widgets", "type": "FOLDER", "description": "Widget pages.",
             "children": [
                 {"id": "REQ-7-1", "name": "Open", "type": "ATOMIC",
                  "description": "Opening a widget shows its repository."},
             ]},
        ],
    }


def test_an_extrapolated_name_written_by_the_audit_is_chased(tmp_path):
    """端到端：找茬回合补的卷子写了外推名 —— 只出字面量欠账，追账回合照样点名。"""
    tree = _lit_tree()
    flow = _AuditFlow(tmp_path, author_skips=("REQ-7-1",), audit_writes=("REQ-7-1",),
                      audit_body="await page.getByRole('link', "
                                 "{ name: 'demo-widget-repo-s2' }).click();\n")
    flow.author_acceptance_suite(tree, list(layerize(tree).order))
    audit = flow.suite_audit
    assert audit["literal_nodes"] == ["REQ-7-1"]
    assert audit["defect_nodes"] == []
    assert audit["gap_disposition"]["undisposed"] == ["REQ-7-1"], "挂账条件认字面量欠账"
    chase = [p for l, p in zip(flow.labels, flow.prompts)
             if l.startswith("disposition round")]
    assert len(chase) == 1
    assert "demo-widget-repo-s2" in chase[0] and "only quotes the `demo-widget-repo` family" in chase[0]


def test_a_written_waiver_closes_a_defect_too(tmp_path):
    """缺陷的豁免出口和缺考同一句话：写了为什么就结清，不再追账。"""
    tree = _tree()
    suite = tmp_path / M.AUTHOR_SUITE_DIR
    (suite / M.SUITE_AUDIT_DIR).mkdir(parents=True)
    (suite / M.SUITE_AUDIT_DIR / M.SUITE_WAIVER_FILE).write_text(
        "- REQ-1-1: scanner false positive — the helper is awaited before expect\n"
        "- REQ-2-1: same shape as REQ-1-1, waived for the same reason\n",
        encoding="utf-8")
    flow = _AuditFlow(tmp_path, audit_writes=("REQ-1-1", "REQ-2-1"),
                      audit_body=_DEFECT_SPEC_WITH_SESSION)
    flow.author_acceptance_suite(tree, list(layerize(tree).order))
    disp = flow.suite_audit["gap_disposition"]
    assert disp["undisposed"] == [] and len(disp["waived"]) == 2
    assert not [l for l in flow.labels if l.startswith("disposition round")], \
        "都交代了就不用追账"
    assert "kept on a written reason" in M.suite_audit_completion_note(flow.suite_audit)
