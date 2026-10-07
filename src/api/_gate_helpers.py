"""S API 网关辅助函数：版本、默认主机端口与请求统计等公共配置。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from contracts.manifest import AgentManifest

APP_VERSION = "1.0.0"

_DEFAULT_HOST = "127.0.0.1"

_DEFAULT_PORT = 8000

_REQUEST_STATS: dict[str, Any] = {
    "started_at": time.time(),
    "total": 0,
    "by_path": {},       # path -> count
    "errors": 0,         # 5xx 计数
    "last_request_at": 0.0,
}
_STATS_LOCK = threading.Lock()

def _record_request(path: str, elapsed_ms: float) -> None:
    """记录一次请求（线程安全）。"""
    with _STATS_LOCK:
        _REQUEST_STATS["total"] += 1
        _REQUEST_STATS["by_path"][path] = _REQUEST_STATS["by_path"].get(path, 0) + 1
        _REQUEST_STATS["last_request_at"] = time.time()
        _REQUEST_STATS.setdefault("avg_elapsed_ms", 0.0)
        # 简单移动平均
        n = _REQUEST_STATS["total"]
        _REQUEST_STATS["avg_elapsed_ms"] = (
            (_REQUEST_STATS["avg_elapsed_ms"] * (n - 1) + elapsed_ms) / n
        )

def _collect_metrics() -> dict[str, Any]:
    """收集指标快照（/metrics 端点）。"""
    with _STATS_LOCK:
        stats = dict(_REQUEST_STATS)
        stats["by_path"] = dict(stats["by_path"])
        stats["uptime_s"] = round(time.time() - stats["started_at"], 1)
    return {"agent": "stm32-forge", "version": APP_VERSION, "metrics": stats}

def _load_settings() -> dict[str, Any]:
    """读取 config/settings.yaml（委托 infrastructure.config 统一入口）。"""
    from infrastructure.config import load_settings

    return load_settings()

def _get_server_config() -> tuple[str, int, str]:
    """
    解析服务配置，优先级：settings.yaml api 段 > 默认值。

    Returns:
        (host, port, api_token)。api_token 为空字符串表示未配置 token。
    """
    settings = _load_settings()
    api_cfg = settings.get("api", {}) or {}

    host = api_cfg.get("host", _DEFAULT_HOST)
    port = api_cfg.get("port", _DEFAULT_PORT)
    token = str(api_cfg.get("token", "") or "").strip()
    return str(host), int(port), token

def _get_shared_ui_dir() -> Path:
    """公共 UI 目录：环境变量 > config 统一默认（集中化，不写死本机路径）。"""
    env_dir = os.environ.get("AGENT_SHARED_UI_DIR")
    if env_dir:
        return Path(env_dir)
    try:
        from infrastructure.config import SHARED_UI_DIR

        return Path(SHARED_UI_DIR)
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[2] / "agent-c-chamber" / "shared" / "ui"

def _build_manifest(workspace: Any) -> AgentManifest:
    """
    构建 AgentManifest（供 SE 框架注册，主入口 /manifest 返回）。

    能力清单从工作室注册表动态生成——studio.yaml 三态声明的技能
    全部进入 manifest，保证"声明 == 实现"，自检不会踩空。
    endpoint 以实际配置端口为准（修复：此前写死 8000 与真实 8001 不符）。
    """
    from contracts.manifest import AgentManifest, AgentRole, Capability, SkillSchema

    registry = workspace.registry
    studio_skills = [
        SkillSchema(**s) for s in registry.to_manifest_skills()
    ]
    cfg_host, cfg_port, _ = _get_server_config()
    endpoint = f"http://localhost:{cfg_port}"
    return AgentManifest(
        name="stm32-forge",
        role=AgentRole.LEFT_BRAIN,
        version=APP_VERSION,
        description="技术工作室：STM32/APM32 嵌入式代码生成引擎（识别套式 + 工程生成 + 真编译）",
        endpoint=endpoint,
        capabilities=[
            Capability(
                name="code_gen",
                description="嵌入式代码生成（识别套式：开发板简单逻辑模板优先 + 通用功能模板补缺 + 缺失报告）",
                skills=[
                    SkillSchema(
                        name="code_gen",
                        description="嵌入式代码生成（识别套式 + 真编译，产出可烧录工程）",
                        input_schema={"requirement": "string", "mcu": "string", "compile": "boolean"},
                        output_schema={"source_code": "string", "status": "string"},
                        async_only=True,
                    ),
                ],
            ),
            Capability(
                name="studio",
                description="技术工作室技能（代码生成/硬件设计/测试/部署等），与 manifest 声明自动对齐",
                skills=studio_skills,
            ),
        ],
        max_concurrent=1,
    )
