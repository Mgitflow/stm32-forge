# -*- coding: utf-8 -*-
"""tests/test_studio_core.py — studio 门面层：Skill 契约 / SkillResult / SkillContext / Registry / Workspace。

core 此前零测试（2026-09-18 记账），本文件补齐 studio 层全覆盖；不依赖真实 config，
Registry 走 tmp studio.yaml fixture。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.studio.context import SkillContext
from src.studio.registry import SkillRegistry, register_skill, registered_names
from src.studio.result import SkillResult
from src.studio.skill import Skill
from src.studio.workspace import IDLE_MODE, StudioWorkspace


# ── fixtures ────────────────────────────────────────────────

@pytest.fixture()
def clean_registry():
    """保存/恢复全局注册表，防测试 skill 泄漏到其他用例。"""
    import src.studio.registry as reg_mod

    saved = dict(reg_mod._SKILL_CLASSES)
    yield reg_mod
    reg_mod._SKILL_CLASSES.clear()
    reg_mod._SKILL_CLASSES.update(saved)


def _make_skill(name: str, *, payload: dict | None = None, boom: bool = False) -> type[Skill]:
    """动态建 Skill 子类并注册（register_skill 按 cls.name 收录）。"""
    cls = type(
        f"_Skill_{name}",
        (Skill,),
        {
            "name": name,
            "title": f"测试技能 {name}",
            "description": "测试用",
            "input_schema": {"req": "string"},
            "output_schema": {"out": "string"},
            "run": _run_factory(payload or {"out": "ok"}, boom),
        },
    )
    return register_skill(cls)


def _run_factory(payload: dict, boom: bool):
    async def run(self, ctx, **params):  # noqa: ANN001, ANN202
        if boom:
            raise RuntimeError("技能炸了")
        return SkillResult.ok_result(payload)
    return run


@pytest.fixture()
def tmp_registry(tmp_path: Path, clean_registry):
    """tmp studio.yaml + 预注册技能 → 已实例化的 Registry。"""
    _make_skill("alpha", payload={"out": "A"})
    _make_skill("beta", payload={"out": "B"})
    yaml_file = tmp_path / "studio.yaml"
    yaml_file.write_text(
        "studio:\n"
        "  skills:\n"
        "    alpha:\n"
        "      name: 阿尔法\n"
        "      description: 测试技能A\n"
        "      status: active\n"
        "      endpoint: /skill/tt-alpha\n"
        "    beta:\n"
        "      name: 贝塔\n"
        "      status: reserved\n"
        "    ghost:\n"
        "      name: 幽灵\n"
        "      status: active\n",
        encoding="utf-8",
    )
    return SkillRegistry(config_path=yaml_file)


# ── Skill 契约 ──────────────────────────────────────────────

def test_skill_subclass_requires_name(clean_registry):
    with pytest.raises(TypeError):
        type("_NoName", (Skill,), {})


def test_skill_is_abstract(clean_registry):
    class _Concrete(Skill):
        name = "abs_test"

    with pytest.raises(TypeError):  # 未实现 run()
        _Concrete()


# ── SkillResult ─────────────────────────────────────────────

def test_result_factories_and_serialization():
    ok = SkillResult.ok_result({"k": "v"}, status="partial", artifacts={"f": "x"}, next_skills=["beta"])
    d = ok.to_dict()
    assert d["ok"] is True and d["data"] == {"k": "v"} and d["status"] == "partial"
    assert d["artifacts"] == {"f": "x"} and d["next_skills"] == ["beta"]

    bad = SkillResult.fail("炸了")
    assert bad.ok is False and bad.error == "炸了" and bad.status == "failed"
    assert bad.to_dict()["elapsed_ms"] == 0.0


# ── SkillContext ────────────────────────────────────────────

def test_context_get_set_health():
    ctx = SkillContext(agent_name="tester")
    assert ctx.get("missing", "默认") == "默认"
    ctx.set("kb", object())
    assert ctx.get("kb") is not None
    h = ctx.health()
    assert h["kb"] is False                 # set() 只进 deps，不动顶层属性
    assert h["deps"]["kb"] is True          # deps 注入反映在 deps 键
    ctx.kb = object()                       # 顶层属性注入（外部装配方式）
    assert ctx.health()["kb"] is True
    assert h["transport"] is False and h["cache"] is False


# ── SkillRegistry ───────────────────────────────────────────

def test_registry_init_respects_status(tmp_registry):
    assert tmp_registry.has("alpha")           # active → 实例化
    assert not tmp_registry.has("beta")        # reserved → 只登记元数据不实例化
    assert not tmp_registry.has("ghost")       # 配置有但未注册 → 跳过
    assert tmp_registry.names == ["alpha"]     # names = 已实例化集合


def test_registry_list_skills_and_manifest(tmp_registry):
    rows = {r["name"]: r for r in tmp_registry.list_skills()}
    assert rows["alpha"]["active"] is True and rows["alpha"]["title"] == "阿尔法"
    assert rows["beta"]["active"] is False and rows["beta"]["status"] == "reserved"

    manifest = {m["name"]: m for m in tmp_registry.to_manifest_skills()}
    assert manifest["alpha"]["input_schema"] == {"req": "string"}
    assert manifest["alpha"]["async_only"] is True and manifest["alpha"]["dangerous"] is False


def test_registry_resolve_endpoint(tmp_registry):
    assert tmp_registry.resolve_endpoint("/skill/tt-alpha") == "alpha"
    assert tmp_registry.resolve_endpoint("/skill/beta") == "beta"  # 未配 endpoint → 默认 /skill/{key}
    assert tmp_registry.resolve_endpoint("/nope") is None


def test_registry_check_capabilities(tmp_registry):
    rep = tmp_registry.check_capabilities(["alpha", "ghost_only"])
    assert rep["ok"] is False and rep["missing"] == ["ghost_only"]
    assert tmp_registry.check_capabilities(["alpha", "beta"])["ok"] is True  # 注册类也算


def test_registered_names_module_level(clean_registry):
    _make_skill("solo_skill")
    assert "solo_skill" in registered_names()


# ── StudioWorkspace ─────────────────────────────────────────

def test_workspace_initial_state(tmp_registry):
    ws = StudioWorkspace(SkillContext(agent_name="tester"), registry=tmp_registry)
    assert ws.current_mode == IDLE_MODE
    status = ws.get_status()
    assert status["agent"] == "tester" and status["skill_count"] == 1
    assert status["available_modes"] == ["alpha"]


def test_workspace_set_mode(tmp_registry):
    ws = StudioWorkspace(SkillContext(), registry=tmp_registry)
    assert ws.set_mode("ghost")["ok"] is False          # 未实例化的技能不可切
    assert ws.set_mode("alpha")["ok"] is True
    assert ws.set_mode(IDLE_MODE)["ok"] is True and ws.current_mode == IDLE_MODE


def test_workspace_run_and_history(tmp_registry):
    ws = StudioWorkspace(SkillContext(), registry=tmp_registry)
    res = asyncio.run(ws.run("alpha", {}))
    assert res.ok is True and res.data == {"out": "A"} and res.elapsed_ms > 0
    assert ws.get_status()["recent_runs"][0]["skill"] == "alpha"

    miss = asyncio.run(ws.run("nope"))
    assert miss.ok is False and "未实例化" in miss.error


def test_workspace_run_catches_exception(clean_registry):
    _make_skill("boom_skill", boom=True)
    reg = SkillRegistry(config_path=_yaml_of(clean_registry, {"boom_skill": "active"}))
    ws = StudioWorkspace(SkillContext(), registry=reg)
    res = asyncio.run(ws.run("boom_skill"))
    assert res.ok is False and "RuntimeError" in res.error and res.status == "failed"


def test_workspace_run_current_requires_mode(tmp_registry):
    ws = StudioWorkspace(SkillContext(), registry=tmp_registry)
    assert asyncio.run(ws.run_current()).ok is False     # idle 下拒绝


def test_workspace_history_capped(tmp_registry):
    ws = StudioWorkspace(SkillContext(), registry=tmp_registry)
    fake = SkillResult.ok_result({})
    for _ in range(60):
        ws._record("alpha", fake)
    assert len(ws._history) == ws._history_limit == 50


def _yaml_of(clean_registry, statuses: dict) -> Path:
    import tempfile

    f = Path(tempfile.mkdtemp()) / "studio.yaml"
    lines = ["studio:", "  skills:"]
    for name, status in statuses.items():
        lines += [f"    {name}:", f"      status: {status}"]
    f.write_text("\n".join(lines), encoding="utf-8")
    return f
