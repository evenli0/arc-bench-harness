"""`octos/core/truncate.py` 的对位测试。

前 12 条是从 `crates/octos-core/src/utils.rs` 的 `#[cfg(test)]` 模块逐条移植的，
包括那条「特征化钉子」（`should_emit_exact_legacy_marker_when_truncating`）——
它钉住的是结构化重构**之前**抓下来的旧字符串，重构必须保持逐字节一致。

最后两条是**新增**的：Rust 侧没有覆盖 f32/f64 分歧点，而现有 fixture 恰好全落在
不分歧的取值上。见 `octos/core/truncate.py` 的模块文档。
"""

from __future__ import annotations

import pytest

from octos.core.truncate import (
    TruncatedBy,
    _f32,
    tool_output_limit,
    truncate_head_tail,
    truncate_head_tail_report,
    truncate_utf8,
    truncated_utf8,
)


# ── truncate_utf8 / truncated_utf8 ────────────────────────────────────────

def test_truncate_no_op():
    assert truncate_utf8("hello", 10, "...") == "hello"


def test_truncate_ascii():
    assert truncate_utf8("abcdefghij", 5, "...") == "abcde..."


def test_truncate_utf8_boundary():
    # 你好世 = 9 字节，在第 7 字节切要回退到第 6 字节
    assert truncate_utf8("你好世", 7, "...") == "你好..."


def test_truncated_utf8_no_op():
    assert truncated_utf8("hello", 10, "...") == "hello"


def test_truncated_utf8_ascii():
    assert truncated_utf8("abcdefghij", 5, "...") == "abcde..."


def test_truncated_utf8_boundary():
    assert truncated_utf8("你好世", 7, "...") == "你好..."


# ── truncate_head_tail 基本行为 ───────────────────────────────────────────

def test_head_tail_no_op():
    assert truncate_head_tail("short text", 100, 0.5) == "short text"


def test_head_tail_split():
    s = "a" * 100 + "b" * 100
    result = truncate_head_tail(s, 100, 0.5)
    assert result.startswith("aaa")
    assert result.endswith("bbb")
    assert "bytes omitted" in result
    assert len(result) <= 150  # 100 + 分隔符开销


def should_emit_exact_legacy_marker_when_truncating():
    """特征化钉子：结构化重构前抓下的旧字符串，必须逐字节保持。

    Rust 侧叫 `should_emit_exact_legacy_marker_when_truncating`。
    """
    s = "h" * 100 + "t" * 100
    expect = "h" * 25 + "\n\n... [150 bytes omitted] ...\n\n" + "t" * 25
    assert truncate_head_tail(s, 100, 0.5) == expect


def test_head_tail_preserves_utf8():
    s = "你" * 50 + "好" * 50
    result = truncate_head_tail(s, 100, 0.5)
    assert "bytes omitted" in result
    # 不抛异常、不产出非法 UTF-8：能编码回来就是证据
    result.encode("utf-8")


# ── tool_output_limit ────────────────────────────────────────────────────

def test_tool_output_limit():
    assert tool_output_limit("read_file") == 50_000
    assert tool_output_limit("shell") == 30_000
    assert tool_output_limit("unknown_tool") == 50_000


def test_news_fetch_limit_is_at_least_100k_bytes():
    """回归：`news_fetch` 的 JSON 一次打包几十条标题，很容易越过 50K 全局默认。

    输出被**中段省略**后，kimi 一类模型会把标记误读成「结果不完整」，然后带着漂移的
    `categories=` 参数重试 —— 会话 `web-1779494658716-mxrxe8`（账本 seq 214-562）上
    观察到的正是这个螺旋。防止未来被悄悄调小。
    """
    assert tool_output_limit("news_fetch") >= 100_000


# ── 结构化报告 ───────────────────────────────────────────────────────────

def should_report_untruncated_when_input_at_exact_limit():
    s = "a" * 100
    r = truncate_head_tail_report(s, 100, 0.7)
    assert not r.truncated
    assert r.truncated_by is None
    assert r.content == s
    assert r.total_bytes == 100
    assert r.output_bytes == 100
    assert r.omitted_bytes == 0
    assert r.max_len == 100


def should_report_bytes_truncation_when_one_byte_over_limit():
    s = "a" * 101
    r = truncate_head_tail_report(s, 100, 0.7)
    assert r.truncated
    assert r.truncated_by is TruncatedBy.BYTES
    assert r.total_bytes == 101
    assert r.max_len == 100
    assert r.output_bytes == len(r.content.encode("utf-8"))
    assert r.omitted_bytes > 0
    assert f"... [{r.omitted_bytes} bytes omitted] ..." in r.content, (
        "报告与内联标记必须在省略数上一致"
    )


def should_report_untruncated_when_input_empty():
    r = truncate_head_tail_report("", 0, 0.7)
    assert not r.truncated
    assert r.truncated_by is None
    assert r.content == ""
    assert r.total_bytes == 0
    assert r.output_bytes == 0
    assert r.omitted_bytes == 0


def should_keep_utf8_boundaries_when_multibyte_chars_straddle_the_cut():
    """4 字节标量：字节预算落在字符中间，两个切点都必须退到字符边界而不是崩掉。"""
    s = "\U0001f980" * 200  # 800 字节
    r = truncate_head_tail_report(s, 101, 0.7)
    assert r.truncated
    marker = f"\n\n... [{r.omitted_bytes} bytes omitted] ...\n\n"
    assert marker in r.content, "被截断的内容必须含省略标记"
    head, tail = r.content.split(marker, 1)
    assert all(c == "\U0001f980" for c in head), f"头部必须只含完整字符: {head!r}"
    assert all(c == "\U0001f980" for c in tail), f"尾部必须只含完整字符: {tail!r}"
    assert r.total_bytes == 800
    assert r.output_bytes == len(r.content.encode("utf-8"))
    # 只保留完整字符：留下的载荷 + 省略数 恰好覆盖输入
    assert len(head.encode("utf-8")) + len(tail.encode("utf-8")) + r.omitted_bytes == r.total_bytes


def should_match_wrapper_content_when_report_and_wrapper_share_inputs():
    """门面等价性：`truncate_head_tail` 必须是报告的薄投影 —— 一份实现，两个门面。

    属性式覆盖：fixture 从两侧跨过限流、多字节内容、越界比例。
    """
    fixtures = [
        "",
        "short",
        "a" * 99,
        "a" * 100,
        "a" * 101,
        "x" * 10_000,
        "\U0001f980" * 400,
        "你好世界" * 500,
        "head\n" + "mid " * 2_000 + "\ntail",
    ]
    for s in fixtures:
        for max_len in (0, 10, 49, 50, 51, 100, 1_000, 30_000):
            for ratio in (0.0, 0.3, 0.5, 0.7, 0.9, 1.5):
                report = truncate_head_tail_report(s, max_len, ratio)
                assert truncate_head_tail(s, max_len, ratio) == report.content, (
                    f"门面与报告必须共用一份实现 "
                    f"(len={len(s.encode('utf-8'))}, max_len={max_len}, ratio={ratio})"
                )


def should_emit_only_marker_when_budget_below_marker_overhead():
    """退化区间：`max_len` 低于分隔符预留开销时，载荷为零 —— 输出只有省略标记，
    且（既有行为）比 `max_len` 本身还长。报告仍须自洽：全部省略、标记 N 准确。
    经 `tool_output_limit` 不可达（最小 20K）。
    """
    s = "z" * 100
    r = truncate_head_tail_report(s, 10, 0.7)
    assert r.truncated
    assert r.omitted_bytes == 100
    assert r.content == "\n\n... [100 bytes omitted] ...\n\n"
    assert r.truncated_by is TruncatedBy.BYTES
    assert r.output_bytes == len(r.content.encode("utf-8"))


def should_clamp_reported_head_ratio_when_ratio_out_of_range():
    s = "a" * 300
    hi = truncate_head_tail_report(s, 100, 5.0)
    assert hi.head_ratio == 0.9
    assert hi.content == truncate_head_tail(s, 100, 5.0)
    lo = truncate_head_tail_report(s, 100, -1.0)
    assert lo.head_ratio == 0.1


# ── 新增：f32 分歧点（Rust 侧未覆盖） ─────────────────────────────────────

def should_use_f32_head_budget_not_f64():
    """`head_budget` 必须在 f32 里算，不能顺手用 Python 的 f64。

    `available = 90`（即 `max_len = 140`）是实测的分歧点：f32 给 63，f64 给 62。
    `omitted_bytes` 两种算法**相同**（头+尾总量固定），分歧只体现在切点位置 ——
    所以断言必须盯住头部长度，不能只断言省略数。

    运行区间 `[19950, 199950]` 里有 2.437% 的取值属于这一类，但六个真实工具上限和
    `utils.rs` 的全部 fixture 都恰好落在不分歧的取值上。也就是说：把这里改成 f64，
    其余测试**照样全绿** —— 这条是唯一守得住它的测试。
    """
    assert _f32(0.7) != 0.7, "前提：f32(0.7) 与 f64(0.7) 不是同一个数"

    s = "a" * 200
    r = truncate_head_tail_report(s, 140, 0.7)
    assert r.truncated
    assert r.omitted_bytes == 110  # 两种算法都一样，所以它证明不了什么
    head, _ = r.content.split("\n\n... [110 bytes omitted] ...\n\n", 1)
    assert len(head) == 63, (
        f"头部应是 63 字节（f32），得到 {len(head)}（62 即为误用了 f64）"
    )
    # 交叉验证：纯 f64 确实会给出 62
    assert int(90 * 0.7) == 62
