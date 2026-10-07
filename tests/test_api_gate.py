# -*- coding: utf-8 -*-
"""tests/test_api_gate.py — api 层可测面：_gate_helpers（指标/配置/UI 目录）+ mcp_bridge（JSON-RPC 桥）。

server.py 本体（573 行）依赖运行中的 workspace 与真实生成链路，不在单测覆盖内；
mcp_bridge 用真实 ThreadingHTTPServer 做一次 HTTP 往返验证。
"""
from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from src.api import _gate_helpers as gh
from src.api.mcp_bridge import McpBridge, make_mcp_handler


# ── _gate_helpers：请求指标 ─────────────────────────────────

@pytest.fixture()
def clean_stats():
    saved = dict(gh._REQUEST_STATS)
    saved_paths = dict(gh._REQUEST_STATS.get("by_path") or {})
    gh._REQUEST_STATS["total"] = 0
    gh._REQUEST_STATS["by_path"] = {}
    gh._REQUEST_STATS["errors"] = 0
    yield gh._REQUEST_STATS
    gh._REQUEST_STATS.clear()
    gh._REQUEST_STATS.update(saved)
    gh._REQUEST_STATS["by_path"] = saved_paths


def test_record_request_and_metrics(clean_stats):
    gh._record_request("/api/x", 10.0)
    gh._record_request("/api/x", 30.0)
    gh._record_request("/api/y", 50.0)
    m = gh._collect_metrics()
    assert m["agent"] == "stm32-forge" and m["version"] == gh.APP_VERSION
    stats = m["metrics"]
    assert stats["total"] == 3
    assert stats["by_path"]["/api/x"] == 2
    assert 10.0 <= stats["avg_elapsed_ms"] <= 50.0   # 移动平均落在样本区间
    assert stats["uptime_s"] >= 0.0


# ── _gate_helpers：配置读取（无 config → 默认值）────────────

def test_get_server_config_defaults(monkeypatch):
    import infrastructure.config as cfg

    monkeypatch.setattr(cfg, "_settings_cache", {}, raising=False)
    host, port, token = gh._get_server_config()
    assert host == "127.0.0.1" and port == 8000 and token == ""   # fail-safe 默认


def test_get_shared_ui_dir_env_priority(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SHARED_UI_DIR", str(tmp_path))
    assert gh._get_shared_ui_dir() == tmp_path     # env 优先于 config 默认


# ── mcp_bridge ──────────────────────────────────────────────

def _bridge_all() -> McpBridge:
    return McpBridge(handlers={
        "code_gen": lambda **kw: {"code": f"// {kw.get('requirement', '')}"},
        "compile_check": lambda **kw: {"report": "0 errors"},
    })


def test_mcp_tools_list_filtered_by_handlers():
    full = _bridge_all().tools_list()
    assert [t["name"] for t in full["tools"]] == ["code_gen", "compile_check"]
    partial = McpBridge(handlers={"code_gen": lambda **kw: {}}).tools_list()
    assert [t["name"] for t in partial["tools"]] == ["code_gen"]  # 未注册 handler 的工具不暴露


def test_mcp_tools_call_success_and_errors():
    br = _bridge_all()
    out = br.tools_call("code_gen", {"requirement": "点灯"})
    assert out["content"][0]["text"] == json.dumps({"code": "// 点灯"}, ensure_ascii=False)

    assert br.tools_call("nope", {}) == {"error": "unknown tool: nope"}

    def _boom(**kw):
        raise ValueError("参数坏了")
    br2 = McpBridge(handlers={"code_gen": _boom})
    assert "参数坏了" in br2.tools_call("code_gen", {})["error"]


def test_mcp_dispatch_routing():
    br = _bridge_all()
    assert br.dispatch({"method": "tools/list"})["tools"]
    out = br.dispatch({"method": "tools/call", "params": {"name": "code_gen", "arguments": {"requirement": "x"}}})
    assert "content" in out
    assert "unsupported method" in br.dispatch({"method": "resources/list"})["error"]


def test_mcp_http_roundtrip():
    """真实 HTTP 往返：ThreadingHTTPServer 随机端口 + JSON-RPC POST。"""
    br = _bridge_all()
    server = make_mcp_handler(br)
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        # 正常调用
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps({"method": "tools/call", "params": {"name": "code_gen", "arguments": {"requirement": "LED"}}}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["content"][0]["type"] == "text"
        assert "LED" in body["content"][0]["text"]

        # 坏 JSON → error 而非 5xx
        req2 = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp", data=b"not-json", method="POST",
        )
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            assert json.loads(resp2.read().decode("utf-8")) == {"error": "invalid JSON"}
    finally:
        httpd.shutdown()
        httpd.server_close()
