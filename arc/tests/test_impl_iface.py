"""实现→卷子 的静态接口对账（2026-10-08）—— 每一根钉子对应一条判据：

    role 覆盖   原生语义元素上带 role= 覆盖（值是原生 role 的不算）→ 说出口；
                input 按 type 判隐式角色、<a> 要有 href 才算 link，认不准就不说
    装饰字形    几何字形出现在源码文本里 → 说出口（26/30 的 ▸ 形状）
    role 缺席   卷子按角色 R 找人 + 需求原文点名过 R 一族 + frontend/ 源码里
                一个字都没提过 → 说出口；两个条件缺一个都沉默
    只提示      不扫 node_modules/dist；没有 frontend/ 目录 → 三个键全空
    进提示词    发现贴着失败清单进修复回合（`_batch_repair_prompt`）；没发现
                时一个字都不加；提示是给模型的，不阻塞、不占轮次

背景（26/30 归因，2026-10-08）：判分方按 role/名字找人，而实现可能无谓地摘掉
自家语义（`<button role="menuitem">` 让 button 查找落空）或把装饰字形混进
textContent（精确文本断言被一个 ▸ 弄红）。这两条在真卷面上都有实测失分。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402


# ---------------------------------------------------------------- 假件

def _frontend(tmp_path, files: dict) -> Path:
    """在 tmp_path/frontend/ 下落一棵源码树；返回 app 根（output_dir 口径）。"""
    for rel, text in files.items():
        p = tmp_path / "frontend" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def _rel(*parts) -> str:
    return str(Path("frontend", *parts))


# ---------------------------------------------------------------- role 覆盖

def test_a_role_override_on_a_native_button_is_reported(tmp_path):
    app = _frontend(tmp_path, {"src/Menu.tsx": (
        'export const Menu = () => (\n'
        '  <button role="menuitem" onClick={pick}>Sort</button>\n'
        ');\n')})

    out = M.impl_interface_warnings(app, {})

    assert len(out["role_overrides"]) == 1
    line = out["role_overrides"][0]
    assert _rel("src", "Menu.tsx") in line
    assert '"menuitem"' in line and "`button`" in line, "两边角色都要点名"
    assert "nest" in line, "给的是改法（两顶牌子都留着），不是单纯判红"


def test_no_op_roles_generic_containers_and_bare_anchors_are_not_reported(tmp_path):
    """同值 role、role="none"、div 这类无隐式角色的容器、没有 href 的 <a> —— 都不是
    「摘自家牌子」，一句都不说。标准 ARIA 的正统组合（form→search 搜索地标、
    li→option 的 APG listbox 模式）同样不说 —— 两张官方种子盘上校准过。"""
    app = _frontend(tmp_path, {"src/Ok.tsx": (
        '<button role="button">Save</button>\n'
        '<div role="button">Custom</div>\n'
        '<li role="none"><button>Item</button></li>\n'
        '<a role="button">No href, no link role to lose</a>\n'
        '<span role="menu">plain container</span>\n'
        '<form role="search" onSubmit={go}><input /></form>\n'
        '<ul role="listbox"><li role="option">One</li></ul>\n')})

    out = M.impl_interface_warnings(app, {})

    assert out["role_overrides"] == []


def test_input_role_comes_from_its_type_and_anchor_needs_href(tmp_path):
    app = _frontend(tmp_path, {"src/Fields.tsx": (
        '<a href="/x" role="button">Go</a>\n'
        '<input type="checkbox" role="switch" />\n'
        '<input type="text" role="searchbox" />\n'
        '<input role="searchbox" />\n')})   # 没有字面 type：认不准，不说

    out = M.impl_interface_warnings(app, {})

    assert len(out["role_overrides"]) == 3, "认不准的那一条（无字面 type 的 input）不进来"
    overrides = "\n".join(out["role_overrides"])
    assert "`link`" in overrides, "带 href 的 <a> 盖 button：link 查找会落空"
    assert "`checkbox`" in overrides, "checkbox 盖成 switch，checkbox 查找会落空"
    assert "`textbox`" in overrides, "text input 盖成 searchbox，textbox 查找会落空"


# ---------------------------------------------------------------- 装饰字形

def test_decorative_glyphs_are_reported_once_per_file(tmp_path):
    app = _frontend(tmp_path, {
        "src/Card.tsx": '<span className="caret">▸</span><span>▾</span>\n',
        "src/Plain.tsx": "export const Plain = () => <p>just words</p>;\n",
    })

    out = M.impl_interface_warnings(app, {})

    glyphs = out["glyph_text"]
    assert len(glyphs) == 2, "同一文件同一字形只报一次；另一个文件没字形不报"
    assert all(_rel("src", "Card.tsx") in g for g in glyphs)
    assert "textContent" in glyphs[0] and "pseudo-element" in glyphs[0]


# ---------------------------------------------------------------- role 缺席

def test_a_role_the_requirement_names_but_no_source_exposes_is_reported(tmp_path):
    app = _frontend(tmp_path, {"src/App.tsx": "export const App = () => <div>Hello</div>;\n"})
    specs = {"REQ-9.spec.ts": (
        "await expect(page.getByRole('menuitem', { name: 'Sort', exact: true })).toBeVisible();")}
    requirement = "The toolbar offers a menu; every menu item sorts the grid by its column."

    out = M.impl_interface_warnings(app, specs, requirement_text=requirement)

    assert len(out["role_missing"]) == 1
    line = out["role_missing"][0]
    assert "REQ-9.spec.ts" in line and "menuitem" in line
    assert "resolves to nothing" in line, "措辞给的是排查方向，不是判词"


def test_the_missing_role_check_stays_silent_when_either_side_disagrees(tmp_path):
    specs = {"REQ-9.spec.ts": "page.getByRole('menuitem', { name: 'Sort' });"}

    # 源码提过这一族（哪怕只是 role="menu"）→ 静态判不出真假，不说
    app_with_menu = _frontend(tmp_path / "a", {
        "src/Menu.tsx": '<div role="menu"><button>Sort</button></div>\n'})
    out = M.impl_interface_warnings(app_with_menu, specs,
                                    requirement_text="The toolbar offers a menu of actions.")
    assert out["role_missing"] == []

    # 源码一片空白，但需求原文没点名过这个角色 → 那是卷子过界，另一条线管
    app_clean = _frontend(tmp_path / "b", {"src/App.tsx": "export const App = () => <div/>;\n"})
    out = M.impl_interface_warnings(app_clean, specs,
                                    requirement_text="The grid sorts on header click.")
    assert out["role_missing"] == []


# ---------------------------------------------------------------- 范围与静默

def test_node_modules_and_dist_are_never_scanned(tmp_path):
    app = _frontend(tmp_path, {
        "node_modules/lib/x.tsx": '<button role="menuitem">Sort ▸</button>\n',
        "dist/bundle.js": 'x.innerHTML = "<button role=\\"menuitem\\">Sort</button>";\n',
        "src/App.tsx": "export const App = () => <div/>;\n",
    })

    out = M.impl_interface_warnings(app, {})

    assert out["role_overrides"] == [] and out["glyph_text"] == []


def test_without_a_frontend_directory_nothing_is_reported(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "server.js").write_text("<button role=\"menuitem\">x</button>",
                                                    encoding="utf-8")

    out = M.impl_interface_warnings(tmp_path, {"REQ-9.spec.ts": "page.getByRole('menuitem');"},
                                    requirement_text="a menu with menu items")

    assert out == {"role_overrides": [], "glyph_text": [], "role_missing": []}


# ---------------------------------------------------------------- 进提示词

def test_the_interface_note_rides_into_the_batch_repair_prompt(tmp_path):
    """发现要贴着失败清单进修复回合；没有发现时提示词一个字都不加。"""
    from test_final_batch import SuiteFlow, _make, _summary

    flow = SuiteFlow(tmp_path, spec_map={"N1": ["a.spec.ts"]}, spec_files=("a.spec.ts",))
    (flow.tests_dir / "a.spec.ts").write_text(
        "await expect(page.getByRole('button', { name: 'Sort', exact: true })).toBeVisible();",
        encoding="utf-8")
    flow.repair_requirements = lambda node_id=None: "The toolbar offers a Sort control."
    _frontend(flow.output_dir, {"src/Menu.tsx": '<button role="menuitem">Sort</button>\n'})
    o = _make(flow)

    text = o._batch_repair_prompt("N1", ["a.spec.ts"],
                                  _summary(passed=0, failed=["a.spec.ts"]), None, aggregate=False)

    assert "Interface cross-check" in text
    assert "menuitem" in text
    assert text.index("fails-a.spec.ts") < text.index("Interface cross-check"), \
        "提示贴在失败清单之后 —— 先说发生了什么，再说旁边看得见什么"


def test_the_interface_note_is_silent_on_a_clean_workspace(tmp_path):
    from test_final_batch import SuiteFlow, _make, _summary

    flow = SuiteFlow(tmp_path, spec_map={"N1": ["a.spec.ts"]}, spec_files=("a.spec.ts",))
    (flow.tests_dir / "a.spec.ts").write_text("// spec", encoding="utf-8")
    flow.repair_requirements = lambda node_id=None: "Plain requirement text."
    _frontend(flow.output_dir, {"src/App.tsx": "export const App = () => <div/>;\n"})
    o = _make(flow)

    text = o._batch_repair_prompt("N1", ["a.spec.ts"],
                                  _summary(passed=0, failed=["a.spec.ts"]), None, aggregate=False)

    assert "Interface cross-check" not in text
