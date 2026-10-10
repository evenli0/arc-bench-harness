"""`octos/core/scope.py` 的对位测试。

移植自 `crates/octos-core/src/session_scope.rs` 的 `#[cfg(test)]` 模块 —— 但只取
**非多租户**的那部分（多租户构造器尚未移植，见 `octos/core/scope.py` 的模块文档）。
多租户专属的用例（`mt_default` 家族的 8 条）留到补构造器时一起移植，不在这里假装覆盖。

Rust 侧用 `abs()` 造平台相关的绝对路径（Windows 上加 `C:` 前缀）。这里照搬，
但把「测试根不存在」这类用例改成基于临时目录，免得污染开发机。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import PurePath

import pytest

from octos.core.scope import (
    IN_WORKSPACE,
    OUT_OF_SCOPE,
    DEFAULT_MULTI_TENANT_SHARED_ZONE_NAMES,
    SESSION_SCOPE_SCHEMA_VERSION,
    ScopeMode,
    SessionScope,
    SessionScopeError,
    canonical_root_lossy,
    canonicalize_lossy,
    is_safe_session_id,
    lexical_normalise,
)


def _abs(p: str) -> str:
    """Rust 侧 `abs()` 的等价物：Windows 上加 `C:` 前缀。"""
    if os.name == "nt":
        return "C:" + p
    return p


def _same_path(a, b) -> bool:
    """Rust 的 `Path` / `PathBuf` `PartialEq` 是**逐分量**比较，不是逐字符串。

    `PathBuf::from("C:\\\\a\\\\b") == PathBuf::from("C:/a/b")` 在 Rust 里为**真** ——
    两者的 `components()` 序列相同。Python 的 `str` 比较逐字符，直接用 `==` 会比 Rust
    更严，在 Windows 上把纯粹的分隔符差异误报成不等。

    这是与 `_starts_with` 同族的保真度陷阱：**路径不能用字符串语义比**。
    实现侧（`scope.py`）已经全部走 `PurePath.parts`，这里把测试侧也拉齐。

    退到 `PurePath(...).parts`：分隔符归一（与 Rust 同）、大小写敏感（与 Rust 同）。
    **不要**换成 `PureWindowsPath.__eq__` —— 它大小写不敏感，比 Rust 宽。
    """
    return PurePath(str(a)).parts == PurePath(str(b)).parts


def _missing_root() -> str:
    """一个**保证不存在**的绝对路径（nonce 防止在开发机上碰巧存在）。"""
    return os.path.join(
        tempfile.gettempdir(), f"scope-missing-{os.getpid()}-{uuid.uuid4().hex}"
    )


# ── solo 构造与访问器 ─────────────────────────────────────────────────────

def test_solo_collapses_workspace_to_cwd_and_no_shared_zones():
    cwd = _abs("/home/yc/my-project")
    scope = SessionScope.solo(cwd)
    assert scope.root == cwd
    assert scope.workspace == cwd
    assert scope.shared_zones_for_classification() == ()


def test_with_workspace_preserves_root_and_mode_policy():
    cwd = _abs("/home/yc/my-project")
    child = os.path.join(cwd, ".octos", "work", "subagent-0")
    scope = SessionScope.solo(cwd).with_workspace(child)
    assert scope.root == cwd
    assert scope.workspace == child
    assert scope.mode == ScopeMode.SOLO


def test_with_workspace_rejects_workspace_escape():
    cwd = _abs("/home/yc/my-project")
    with pytest.raises(SessionScopeError) as exc:
        SessionScope.solo(cwd).with_workspace(_abs("/home/yc/other-project"))
    assert exc.value.variant == "WorkspaceEscapesRoot"


def test_with_workspace_rejects_parent_dir_escape():
    """`<root>/../escape` 在词法上能过 `starts_with(root)`，解析后却在外面。

    `..` 那道守卫必须拦住它。
    """
    cwd = _abs("/home/yc/my-project")
    with pytest.raises(SessionScopeError) as exc:
        SessionScope.solo(cwd).with_workspace(os.path.join(cwd, "..", "other", "subagent"))
    assert exc.value.variant == "WorkspaceEscapesRoot"


def test_with_workspace_rejects_symlinked_workspace_escape():
    """词法上在 root 内、但某一环是解析到 root 之外的符号链接 —— must be rejected.

    只做词法检查的旧版本放行过这条，是个沙箱逃逸（#1250）。

    Windows 上创建目录符号链接需要开发者模式或提权；拿不到就跳过，并且
    **明确报出来**，不要伪装成通过。
    """
    tmp = tempfile.mkdtemp(prefix="scope-sym-")
    root = os.path.join(tmp, "root")
    outside = os.path.join(tmp, "outside")
    os.makedirs(os.path.join(root, ".octos"))
    os.makedirs(outside)
    link = os.path.join(root, ".octos", "work")
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"该主机无法创建目录符号链接（{exc}）；此用例未执行")

    escaping = os.path.join(link, "subagent")
    os.makedirs(escaping)
    with pytest.raises(SessionScopeError) as exc:
        SessionScope.solo(os.path.realpath(root)).with_workspace(escaping)
    assert exc.value.variant == "WorkspaceEscapesRoot"


def test_refuses_relative_root():
    with pytest.raises(SessionScopeError) as exc:
        SessionScope.solo("relative/data")
    assert exc.value.variant == "RootNotAbsolute"


def test_with_skill_read_zones_rejects_relative():
    scope = SessionScope.solo(_abs("/home/yc/p"))
    with pytest.raises(SessionScopeError) as exc:
        scope.with_skill_read_zones(["rel/dir"])
    assert exc.value.variant == "SkillReadZoneNotAbsolute"


def test_with_granted_dir_rejects_relative():
    scope = SessionScope.solo(_abs("/home/yc/p"))
    with pytest.raises(SessionScopeError) as exc:
        scope.with_granted_dir("rel")
    assert exc.value.variant == "GrantedDirNotAbsolute"


# ── 规范化 ───────────────────────────────────────────────────────────────

def test_canonical_root_and_candidate_match_for_missing_root():
    """根整体不存在时，候选路径必须**保持根的表示形式**。

    Windows 上从候选往上走会到 `C:\\`，产出带 verbatim（`\\\\?\\C:\\...`）的路径；
    根必须走同一套回溯，否则两种表示之间的包含比较会失败。
    """
    missing_root = _missing_root()
    assert not os.path.exists(missing_root), f"测试根必须不存在: {missing_root}"

    canonical_root = canonical_root_lossy(missing_root)
    canonical_child = canonicalize_lossy(os.path.join(missing_root, "src", "main.rs"))
    assert canonical_child.startswith(canonical_root), (
        f"候选 {canonical_child} 必须保持根的表示形式 {canonical_root}"
    )


def test_canonical_root_and_candidate_match_for_missing_root_behind_symlink():
    """上一条的 Unix 伴例。

    那条 rooted 在 `/`，祖先回溯立刻停下、规范化是恒等操作，所以它其实只约束 Windows。
    把不存在的根放到**符号链接**后面，同一种不对称在 Unix 上就会出现 —— macOS 默认
    就是这形状（`/tmp`、`/var` 都解析进 `/private`）。没有这条，非 Windows 的回归
    是看不见的。
    """
    tmp = tempfile.mkdtemp(prefix="scope-symroot-")
    real = os.path.join(tmp, "real")
    link_parent = os.path.join(tmp, "linkparent")
    os.makedirs(real)
    os.makedirs(link_parent)
    link = os.path.join(link_parent, "workspace-link")
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"该主机无法创建目录符号链接（{exc}）；此用例未执行")

    # 守住前提：链接若解析到自身，下面的断言会因错误的理由而成立
    assert os.path.realpath(link) != link, "符号链接必须解析到别处，这条测试才有牙"

    missing_root = os.path.join(link, "not-created-yet")
    assert not os.path.exists(missing_root), f"测试根必须不存在: {missing_root}"

    canonical_root = canonical_root_lossy(missing_root)
    canonical_child = canonicalize_lossy(os.path.join(missing_root, "src", "main.rs"))
    assert canonical_child.startswith(canonical_root)


# ── 词法规范化 ───────────────────────────────────────────────────────────

def test_lexical_normalise_collapses_curdir_and_refuses_parentdir():
    # 用 `_same_path` 而不是 `==`：规范化产出的分隔符随平台变（Windows 上是 `\\`），
    # 而 Rust 的 `Path` 相等是逐分量的，不比字符串。
    assert _same_path(lexical_normalise(_abs("/a/./b")), _abs("/a/b"))
    assert lexical_normalise(_abs("/a/../b")) is None
    assert lexical_normalise(_abs("/a/b/..")) is None


def test_path_equality_is_by_component_not_by_string():
    """上一条的前提，单独钉住 —— 免得有人"顺手"把 `_same_path` 换回 `==`。

    Rust 侧：`PathBuf::from(r"C:\\a\\b") == PathBuf::from("C:/a/b")` 为真。
    Python 侧：`"C:\\\\a\\\\b" == "C:/a/b"` 为假。两条都必须成立，这个错配才是真的。
    """
    if os.name != "nt":
        pytest.skip("分隔符差异只在 Windows 上出现；POSIX 上这条无从谈起")
    assert r"C:\a\b" != "C:/a/b", "前提：Python 的 str 比较逐字符，会判不等"
    assert _same_path(r"C:\a\b", "C:/a/b"), "分量比较必须判相等（与 Rust 一致）"
    # 但大小写仍然敏感 —— 这是 `PurePath.parts` 与 `PureWindowsPath.__eq__` 的分界
    assert not _same_path(r"C:\A", r"C:\a"), "Rust 的 Path 相等大小写敏感，不能放宽"


# ── is_safe_session_id ───────────────────────────────────────────────────

def test_refuses_unsafe_session_id():
    for bad in ("../escape", "/abs", "foo/bar", "..", ".", "with space", ""):
        assert not is_safe_session_id(bad), f"{bad!r} 应当被拒"


def test_accepts_topic_suffix_in_session_id():
    assert is_safe_session_id("web-123#slides")
    assert is_safe_session_id("web-1")
    assert is_safe_session_id("a_b-c9")
    # 非 ASCII 必须被拒（逐字节判定，不是逐字符）
    assert not is_safe_session_id("会话1")
    assert not is_safe_session_id("\\")
    assert not is_safe_session_id("a\x00b")


# ── 判定 ─────────────────────────────────────────────────────────────────

def test_solo_classify_path_in_workspace_for_anything_under_cwd():
    cwd = _abs("/home/yc/my-project")
    scope = SessionScope.solo(cwd)
    assert scope.classify_lexical_path(os.path.join(cwd, "src", "main.rs")) == IN_WORKSPACE


def test_solo_classify_path_in_granted_dir():
    cwd = _abs("/home/yc/my-project")
    grant = _abs("/tmp/scratch")
    scope = SessionScope.solo(cwd, [grant])
    result = scope.classify_lexical_path(os.path.join(grant, "foo.txt"))
    assert result.kind == "in_granted_dir"
    assert result.granted_dir == grant
    assert result.allows_write, "granted_dir 是用户显式授权的，可读写"


def test_solo_classify_skill_dir_is_read_only():
    cwd = _abs("/home/yc/my-project")
    skill = _abs("/opt/octos/skills/deep-search")
    scope = SessionScope.solo(cwd).with_skill_read_zones([skill])
    result = scope.classify_lexical_path(os.path.join(skill, "SKILL.md"))
    assert result.kind == "in_skill_dir"
    assert result.allows_read
    assert not result.allows_write, "技能目录只读"


def test_workspace_wins_over_skill_dir_when_nested():
    """判定次序：workspace > granted_dirs > skill_read_zones > shared_zones。

    skill_dir 嵌在 workspace 里时，必须判成 workspace（更高信任）。
    """
    cwd = _abs("/home/yc/my-project")
    skill = os.path.join(cwd, ".octos", "skills", "x")
    scope = SessionScope.solo(cwd).with_skill_read_zones([skill])
    assert scope.classify_lexical_path(os.path.join(skill, "SKILL.md")) == IN_WORKSPACE


def test_classify_path_refuses_parent_dir_components():
    cwd = _abs("/home/yc/my-project")
    scope = SessionScope.solo(cwd)
    escaping = os.path.join(cwd, "..", "..", "..", "..", "etc", "passwd")
    assert scope.classify_lexical_path(escaping) == OUT_OF_SCOPE


# ── 新增：Rust 侧没覆盖的两条语义 ────────────────────────────────────────

def test_prefix_comparison_is_by_component_not_by_string():
    """`Path::starts_with` 是**逐分量**比较，不是字符串前缀。

    `<root>/abc` 不是 `<root>/ab` 的下级。用 `str.startswith` 实现就会把
    `/w/abc/secret` 判成 `/w/ab` 作用域内 —— 这是一条静默的越权读。
    """
    root = _abs("/w/ab")
    scope = SessionScope.solo(root)
    assert scope.classify_lexical_path(os.path.join(root, "inside.txt")) == IN_WORKSPACE
    # 字符串前缀会命中，分量前缀不会
    assert root.startswith(_abs("/w/ab")) and os.path.join(_abs("/w/abc"), "secret") .startswith(root)
    assert scope.classify_lexical_path(os.path.join(_abs("/w/abc"), "secret")) == OUT_OF_SCOPE
    assert scope.classify_lexical_path(_abs("/w/abc")) == OUT_OF_SCOPE


def test_out_of_scope_is_the_default_deny():
    """没声明过的路径一律拒绝 —— 没有「几乎合法」这一档。

    （被删掉的 `InRootButOutsideZones` 变体就是为这个：命名共享区之后，root 之内、
    已声明区之外的路径全部是 OutOfScope。）
    """
    cwd = _abs("/home/yc/my-project")
    scope = SessionScope.solo(cwd)
    for stray in ("/etc/passwd", "/home/yc/other/file", "/"):
        assert scope.classify_lexical_path(_abs(stray)) == OUT_OF_SCOPE
    assert not OUT_OF_SCOPE.allows_read
    assert not OUT_OF_SCOPE.allows_write


def test_classification_wire_shape_matches_serde():
    """`tag = "kind"`、`rename_all = "snake_case"` —— 诊断日志按这个形状读。"""
    assert IN_WORKSPACE.to_wire() == {"kind": "in_workspace"}
    assert OUT_OF_SCOPE.to_wire() == {"kind": "out_of_scope"}
    grant = _abs("/tmp/g")
    result = SessionScope.solo(_abs("/p"), [grant]).classify_lexical_path(
        os.path.join(grant, "f")
    )
    assert result.to_wire() == {"kind": "in_granted_dir", "granted_dir": grant}


def test_schema_version_is_two():
    """v2 = PR-A 加入 `skill_read_zones`（只读白名单）。追加式，老调用方传空列表即保持 v1。"""
    assert SESSION_SCOPE_SCHEMA_VERSION == 2
    assert DEFAULT_MULTI_TENANT_SHARED_ZONE_NAMES == ("research", "skills")
