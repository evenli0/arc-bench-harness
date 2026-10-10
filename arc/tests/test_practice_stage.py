"""排练开关：`OCTOS_ARC_PRACTICE_STAGE=specs` → 一轮在**出卷体检之后**收工。

它存在的理由：单独练出卷这一站（真实需求 + 真模型）时，蓝图/施工那些段落
不练也照样要花几小时和真金白银。这个开关只决定「停在哪一站」，不改判据 ——
所以测试钉两头：设了要停、而且走和正常收工一样的收尾；没设一步都不许多停。
正赛平台从不设置它。
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402


class Host:
    """`_practice_stop` 真正伸手要的那一面。多一个都不给。"""

    def __init__(self, tmp_path):
        self.output_dir = tmp_path
        self.tests_dir = tmp_path / "acceptance"
        self.spec_map = {"REQ-1-1-1": ["acceptance/REQ-1-1-1.spec.ts"]}
        self.calls = []
        self.events = type("E", (), {"mark_run_completed": lambda self, msg: None})()

    def postflight(self):
        self.calls.append("postflight")


def test_practice_stage_defaults_to_a_normal_run(monkeypatch):
    monkeypatch.delenv("OCTOS_ARC_PRACTICE_STAGE", raising=False)
    assert M.practice_stage() == ""


def test_practice_stage_normalizes_the_spelling(monkeypatch):
    monkeypatch.setenv("OCTOS_ARC_PRACTICE_STAGE", " Specs ")
    assert M.practice_stage() == "specs"


def test_the_specs_stage_stops_and_shuts_down_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("OCTOS_ARC_PRACTICE_STAGE", "specs")
    host = Host(tmp_path)
    assert M.Flow._practice_stop(host) is True
    assert host.calls == ["postflight"], "提前收工也要走和正常收工相同的收尾"


def test_a_normal_run_never_takes_the_practice_exit(tmp_path, monkeypatch):
    monkeypatch.delenv("OCTOS_ARC_PRACTICE_STAGE", raising=False)
    host = Host(tmp_path)
    assert M.Flow._practice_stop(host) is False
    assert host.calls == []


def test_the_run_actually_consults_the_practice_exit():
    """run() 里确实有这个出口 —— 方法写对了但没被调用的话，开关就是个摆设。"""
    src = inspect.getsource(M.Flow.run)
    assert "_practice_stop()" in src
