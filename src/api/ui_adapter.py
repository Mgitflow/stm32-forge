"""Agent-E 共享 UI 的 Agent-S 适配层：把 UI 的 /api/* 端点映射到 S 的能力，对不具备的能力提供兜底。"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, cast

from infrastructure.config import DEFAULT_CHIP_NAME, MODEL_TIFA

logger = logging.getLogger(__name__)

# 运行时 UI 状态（模式开关、当前 MCU 等）
# mcu 默认值取自基础设施配置（AGENT_S_CHIP_NAME 环境变量，默认 APM32F407VGT6）
# 加锁：ThreadingHTTPServer 多线程读写 force_* 互斥逻辑需原子化
_ui_state: dict[str, Any] = {
    "mcu": DEFAULT_CHIP_NAME,
    "force_chat": False,
    "force_code": False,
    "force_doc": False,
    "search_mode": False,
    "tts_muted": False,
}
_ui_lock = threading.Lock()


def get_health() -> dict[str, Any]:
    """返回与 Agent-E UI 兼容的健康信息。"""
    return {
        "status": "ok",
        "agent": "stm32-forge",
        "lm_studio": {
            "model": MODEL_TIFA,
            "status": "unknown",
        },
        "agent_s": {
            "status": "online",
            "details": {
                "peripherals": [
                    "GPIO", "TIM", "UART", "SPI", "I2C",
                    "ADC", "DMA", "CAN", "RTC", "PWM", "EXTI",
                    "IWDG", "WWDG", "DAC", "ETH", "USB", "SDIO",
                ],
            },
        },
        "mcu": _ui_state["mcu"],
        "force_chat": _ui_state["force_chat"],
        "force_code": _ui_state["force_code"],
        "force_doc": _ui_state["force_doc"],
        "search_mode": _ui_state["search_mode"],
        "analyzer_available": True,
        "tts_muted": _ui_state["tts_muted"],
    }


def _is_code_intent(text: str) -> bool:
    """简单判断是否为代码/嵌入式意图。"""
    keywords = [
        "gpio", "tim", "uart", "usart", "spi", "i2c", "adc", "dma", "can", "rtc",
        "hal", "stm32", "pwm", "波特率", "推挽", "开漏", "上拉", "下拉",
        "中断", "nvic", "时钟", "pll", "hse", "寄存器", "keil",
    ]
    lower = text.lower()
    return any(k in lower for k in keywords)

def _sse_event(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def code_generate(workspace: Any, data: dict[str, Any]) -> dict[str, Any]:
    """兼容 /api/code/generate。

    Agent-E 通过 E→S 通道调用（对接）：E 传
    {intent, mcu, peripherals, emotion_context, thinking_chain, detail_level}。
    peripherals 透传给 code_gen 流水线（未来多外设消费），
    返回统一 E 契约 {status, code, explanation, warnings}，并保留旧 {code, passed, logs}。
    """
    intent = data.get("intent") or data.get("message", "")
    peripherals = data.get("peripherals") or []
    mcu = data.get("mcu") or _ui_state["mcu"]
    if workspace is None:
        # 智能层剥离（开源仓）：确定性降级，不依赖 code_gen 技能/LLM
        from ._intelligence import deterministic_generate

        return deterministic_generate(intent, mcu)
    result = await workspace.run("code_gen", {
        "requirement": intent,
        "mcu": mcu,
        "peripherals": peripherals,
        # E→S 衔接通道：透传 R1 思考链（会话级），生成时注入参考
        "thinking_chain": data.get("thinking_chain", ""),
        # 修复（断链）：此前 session_id/emotion_context 被忽略，E 发了 S 没接。
        # 现贯通 session_id（会话缓存 thought_context）+ emotion_context（情感上下文）。
        "session_id": data.get("session_id", ""),
        "emotion_context": data.get("emotion_context", ""),
    })
    code = result.artifacts.get("source_code", "")
    passed = result.status in ("success", "partial")
    explanation = result.data.get("explanation")
    if not isinstance(explanation, str):
        explanation = str(explanation) if explanation else None
    if not explanation:
        explanation = "代码已生成" if passed else (result.error or "生成失败")
    warnings_raw = result.artifacts.get("warnings", [])
    if isinstance(warnings_raw, list):
        warnings = [str(w) for w in warnings_raw]
    else:
        warnings = [str(warnings_raw)] if warnings_raw else []
    return {
        "code": code,
        "passed": passed,
        "logs": [str(result.error)] if result.error else [],
        # --- E 契约字段（Agent-E code_bridge 消费，必须可 JSON 序列化） ---
        "status": "ok" if passed else "error",
        "explanation": explanation,
        "warnings": warnings,
    }


async def code_compile(workspace: Any, data: dict[str, Any]) -> dict[str, Any]:
    """兼容 /api/code/compile，走 code_gen 技能的公开 compile_check 入口。"""
    code = data.get("code", "")
    if not code:
        return {"passed": False, "errors": ["代码为空"]}
    if workspace is None:
        # 智能层剥离（开源仓）：编译依赖 code_gen 技能（含完整编译管线），无智能层时降级
        return {"passed": False, "errors": ["智能层剥离，编译需完整 code_gen 技能"]}

    skill = workspace.get_skill("code_gen")
    if skill is None:
        return {"passed": False, "errors": ["Agent-S 代码技能未初始化"]}

    try:
        return cast(dict[str, Any], await skill.compile_check(code))
    except Exception as e:  # noqa: BLE001
        logger.exception("Compile check failed")
        return {"passed": False, "errors": [f"检查异常: {e}"]}


async def code_generate_stream(workspace: Any, data: dict[str, Any]) -> Any:
    """
    兼容 /api/code/stream（SSE 流式代码生成）。

    事件：meta（规划）→ token（代码流）→ done（含校验结果 / idle_timeout）。
    """
    intent = data.get("intent") or data.get("message", "")
    if not intent:
        yield _sse_event({"error": "缺少需求", "done": True})
        return

    if workspace is None:
        # 智能层剥离（开源仓）：确定性降级——一次性生成，包装成 done 事件
        from ._intelligence import deterministic_generate

        result = deterministic_generate(intent, data.get("mcu") or _ui_state["mcu"])
        yield _sse_event({
            "done": True,
            "code": result.get("code", ""),
            "approved": result.get("passed", False),
            "elapsed_ms": 0,
        })
        return

    skill = workspace.get_skill("code_gen")
    if skill is None:
        yield _sse_event({"error": "code_gen 技能未初始化", "done": True})
        return
    if not hasattr(skill, "stream_run"):
        yield _sse_event({"error": "code_gen 不支持流式", "done": True})
        return

    try:
        async for event in skill.stream_run(workspace.ctx, requirement=intent, project=False):
            etype = event.get("type")
            if etype == "meta":
                yield _sse_event({"meta": event.get("plan", {})})
            elif etype == "token":
                yield _sse_event({"token": event["token"]})
            elif etype == "done":
                yield _sse_event({
                    "done": True,
                    "code": event.get("code", ""),
                    "approved": event.get("approved", False),
                    "idle_timeout": event.get("idle_timeout", False),
                    "elapsed_ms": event.get("elapsed_ms", 0),
                })
            elif etype == "error":
                yield _sse_event({"error": event.get("error", "未知错误"), "done": True})
    except Exception as e:  # noqa: BLE001
        yield _sse_event({"error": f"流式生成异常: {e}", "done": True})


def system_info() -> dict[str, Any]:
    """兼容 /api/system，返回系统资源快照。"""
    try:
        import psutil
        mem = psutil.virtual_memory()
        return {
            "cpu": f"{psutil.cpu_percent(interval=0.1):.0f}%",
            "ram": f"{mem.percent:.0f}%",
            "gpu": "--",
        }
    except Exception:
        return {"cpu": "--", "ram": "--", "gpu": "--"}


def set_mcu(data: dict[str, Any]) -> dict[str, Any]:
    """兼容 /api/mcu。"""
    mcu = data.get("mcu", _ui_state["mcu"])
    with _ui_lock:
        _ui_state["mcu"] = mcu
    return {"mcu": mcu, "status": "ok"}


def toggle_mode(key: str) -> dict[str, Any]:
    """兼容 force_chat / force_code / force_doc / search_mode 切换（读-改-写原子化）。"""
    with _ui_lock:
        if key not in _ui_state:
            return {"error": f"unknown mode: {key}"}
        # 互斥：开启 force_code 时关闭 force_chat/force_doc
        if key in ("force_code", "force_chat", "force_doc") and not _ui_state[key]:
            for k in ("force_code", "force_chat", "force_doc"):
                if k != key:
                    _ui_state[k] = False
        _ui_state[key] = not _ui_state[key]
        return {key: _ui_state[key], "status": "ok"}


def set_mute(data: dict[str, Any]) -> dict[str, Any]:
    """兼容 /api/mute。Agent-S 当前无 TTS，仅记录状态。"""
    muted = bool(data.get("muted", False))
    with _ui_lock:
        _ui_state["tts_muted"] = muted
    return {"muted": muted, "status": "ok"}


def voice_speak(data: dict[str, Any]) -> dict[str, Any]:
    """兼容 /api/voice/speak。上线：edge-tts 生成音频返回 URL（UI 播放）。

    未安装 edge-tts / 生成失败时返回 not_implemented，UI 回退浏览器 TTS。
    """
    text = (data.get("text") or "").strip()
    if not text:
        return {"url": None, "status": "error", "error": "文本为空"}
    from infrastructure.tts import synthesize

    audio_path = synthesize(text, data.get("voice"))
    if not audio_path:
        return {"url": None, "status": "not_implemented"}
    # 转成基于项目根的可访问 URL（/audio/ 静态端点服务）
    from pathlib import Path

    rel = Path(audio_path).name
    return {"url": f"/audio/{rel}", "status": "ok"}
