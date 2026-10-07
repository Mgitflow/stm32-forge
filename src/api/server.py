"""Agent-S Studio HTTP 服务：基于标准库 http.server，暴露 /skill/{name} 执行端点与共享 UI 的 /api/* 端点。"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

# 直接运行时，将项目根目录加入模块搜索路径
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from infrastructure.logger import setup_logging
from src.api import _chat_adapter, ui_adapter
from src.api._gate_helpers import (
    _build_manifest,
    _collect_metrics,
    _get_server_config,
    _get_shared_ui_dir,
    _load_settings,
    _record_request,
)
from src.api._intelligence import build_workspace  # 智能层网关（「LLM 可剥离」接缝）

if TYPE_CHECKING:
    from src.studio.workspace import StudioWorkspace

logger = setup_logging()

# 安全收口：默认仅绑定本机回环；需要局域网访问时显式 --host 0.0.0.0 + 配 token
# 端口统一：组织排序 S 第一 → S 主服务 8000（PORT_MAP 权威基准）

# 项目版本统一入口（与 pyproject / config/studio.yaml 对齐）
APP_VERSION = "1.0.0"

# ── 可观测性：轻量请求指标（落地，无外部依赖）──
_REQUEST_STATS: dict[str, Any] = {
    "started_at": time.time(),
    "total": 0,
    "by_path": {},       # path -> count
    "errors": 0,         # 5xx 计数
    "last_request_at": 0.0,
}

# ── 受保护端点（鉴权白名单，模块级集中登记）──
# 新增端点必须在此登记，否则默认不受保护（fail-open 风险）。
_PROTECTED_GET = frozenset({"/metrics", "/manifest", "/capabilities", "/studio/selfcheck"})
_PROTECTED_POST_PREFIX = ("/skill/", "/api/code/", "/api/chat")
_PROTECTED_POST = frozenset({
    "/api/voice/speak",
    "/api/mcu",
    "/api/mute",
    "/api/force_chat",
    "/api/force_code",
    "/api/force_doc",
    "/api/search_mode",
})


def _chat_reply(workspace: StudioWorkspace | None, data: dict[str, Any]) -> dict[str, Any]:
    """/api/chat 兜底：智能层剥离（无 workspace）时返回确定性提示，否则走 LLM 聊天。"""
    if workspace is None:
        return {
            "reply": (
                "当前为确定性骨架模式（不含 LLM 聊天层）。\n"
                "请用 /api/code/generate 直接生成代码。"
            ),
            "emotion": {"mood": "neutral", "intensity": 0.5},
        }
    return asyncio.run(_chat_adapter.chat(workspace, data))


async def _sse_deterministic_chat(data: dict[str, Any]) -> Any:
    """智能层剥离时 /api/chat/stream 的确定性降级：一条提示 + done。"""
    from src.api.ui_adapter import _sse_event

    yield _sse_event({
        "token": "当前为确定性骨架模式（不含 LLM 聊天层）。请用 /api/code/generate 直接生成代码。",
    })
    yield _sse_event({"done": True, "emotion": {"mood": "neutral", "intensity": 0.5}})


class _StudioHandler(BaseHTTPRequestHandler):
    """Studio HTTP 请求处理器。"""

    @property
    def _workspace(self) -> StudioWorkspace | None:
        """类型收窄访问：http.server 的 self.server 类型为 BaseServer，运行期实为 _StudioServer。

        智能层剥离（开源仓）时为 None——各 studio/chat 端点据此降级为确定性模式。
        """
        srv = self.server
        assert isinstance(srv, _StudioServer), "server 必须是 _StudioServer"
        return srv.workspace

    def _check_auth(self) -> bool:
        """
        鉴权检查：受保护端点必须通过 token 或回环来源。

        - 配置了 api.token → 要求 Authorization: Bearer <token>（常量时间比较）
        - 未配置 token → 仅允许回环来源（127.0.0.1 / ::1 / localhost）
        """
        token = getattr(self.server, "api_token", "") or ""
        if token:
            provided = self.headers.get("Authorization", "")
            return secrets.compare_digest(provided, f"Bearer {token}")
        host = self.client_address[0] if self.client_address else ""
        return host in ("127.0.0.1", "::1", "localhost")

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        """统一返回 JSON 响应。"""
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._maybe_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _maybe_cors_headers(self) -> None:
        """CORS 白名单 Origin 回显（防 localhost CSRF）。

        重设计：不再发 `Access-Control-Allow-Origin: *`（token 为空时，
        任意恶意网页可借浏览器跨域触发生成/编译/改 MCU）。改为：请求 Origin 命中
        白名单（cors_allowed_origins）才回显该 Origin；白名单含 `*` 才回显 `*`。
        """
        allowed = getattr(self.server, "cors_allowed_origins", None) or []
        origin = self.headers.get("Origin", "")
        if "*" in allowed:
            self._send_cors_headers("*")
        elif origin and origin in allowed:
            self._send_cors_headers(origin)

    def _send_cors_headers(self, origin: str) -> None:
        """发送 CORS 头（回显指定 Origin）。"""
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Vary", "Origin")

    def do_OPTIONS(self) -> None:
        """CORS 预检请求。"""
        self.send_response(204)
        self._maybe_cors_headers()
        self.end_headers()

    def _send_not_found(self) -> None:
        """返回 404。"""
        self._send_json({"ok": False, "error": "not found"}, status=404)

    def _send_static(self, file_path: Path, allowed_root: Path | None = None) -> None:
        """
        发送静态文件。

        安全修复：传入 allowed_root 时强制校验 file_path 解析后必须位于
        allowed_root 之内，杜绝路径穿越（此前 /audio/ 可直接读取任意文件）。
        """
        try:
            resolved = file_path.resolve()
        except OSError:
            self._send_not_found()
            return
        if allowed_root is not None:
            try:
                root = allowed_root.resolve()
            except OSError:
                self._send_not_found()
                return
            if not resolved.is_relative_to(root):
                logger.warning("路径穿越拦截: %s (root=%s)", resolved, root)
                self._send_not_found()
                return
        if not resolved.exists() or not resolved.is_file():
            self._send_not_found()
            return
        content_type, _ = mimetypes.guess_type(str(resolved))
        content_type = content_type or "application/octet-stream"
        try:
            data = resolved.read_bytes()
        except Exception:
            self._send_json({"ok": False, "error": "static read failed"}, status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._maybe_cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _read_json_body(self) -> dict[str, Any] | None:
        """读取并解析 POST JSON body（含 1MB 上限，防大 body 打爆内存）。"""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None
        if content_length <= 0 or content_length > 1024 * 1024:
            return None
        try:
            body = self.rfile.read(content_length).decode("utf-8")
            data = json.loads(body)
            if not isinstance(data, dict):
                return None
            return data
        except Exception:
            return None

    def do_GET(self) -> None:
        """处理 GET 请求（可观测性：请求统计 + 流程跟踪 + request_id 贯穿）。"""
        from uuid import uuid4

        from infrastructure.request_id import set_request_id

        set_request_id(f"{os.getpid():x}-{uuid4().hex[:8]}")
        t0 = time.perf_counter()
        path = self.path.split("?", 1)[0]
        try:
            self._handle_get(path)
        finally:
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            _record_request(path, elapsed)
            from infrastructure.tracer import trace

            trace("http", "ok" if self.server else "fail", path, elapsed_ms=elapsed)

    def _handle_get(self, path: str) -> None:
        """GET 请求分发（原 do_GET 逻辑）。"""
        # 安全收口：受保护端点统一鉴权（放在分发前，防 elif 链漏网）
        if path in _PROTECTED_GET and not self._check_auth():
            self._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return
        # 公共 UI 静态资源（限定在共享 UI 目录内）
        ui_dir = _get_shared_ui_dir()
        if path == "/" or path == "/index.html":
            self._send_static(ui_dir / "index.html", allowed_root=ui_dir)
            return
        if path in ("/app.js", "/styles.css"):
            self._send_static(ui_dir / path.lstrip("/"), allowed_root=ui_dir)
            return

        if path == "/health":
            self._send_json({"status": "ok", "agent": "stm32-forge"})
        elif path == "/api/health":
            self._send_json(ui_adapter.get_health())
        elif path == "/api/system":
            self._send_json(ui_adapter.system_info())
        elif path == "/skills":
            ws = self._workspace
            studio_info = {
                "studio": {
                    "name": "Agent-S-Studio",
                    "version": APP_VERSION,
                    "role": "左脑 / 技术工作室",
                },
            }
            if ws is None:
                # 智能层剥离（开源仓）：无技能列表，确定性模式
                self._send_json({**studio_info, "skills": [], "mode": "deterministic"})
            else:
                self._send_json({**studio_info, "skills": ws.registry.list_skills()})
        elif path == "/studio/status":
            ws = self._workspace
            if ws is None:
                self._send_json({
                    "ok": True,
                    "mode": "deterministic",
                    "skill_count": 0,
                    "available_modes": [],
                })
            else:
                self._send_json({"ok": True, **ws.get_status()})
        elif path == "/studio/selfcheck":
            ws = self._workspace
            if ws is None:
                self._send_json({"ok": True, "mode": "deterministic", "note": "智能层剥离，纯确定性骨架"})
            else:
                declared = [s["name"] for s in ws.registry.list_skills()]
                self._send_json({"ok": True, **ws.registry.check_capabilities(declared)})
        elif path == "/manifest":
            # SE 框架动态发现：主入口统一暴露（不再需要独立 8200 服务）
            ws = self._workspace
            if ws is None:
                self._send_json({
                    "name": "stm32-forge",
                    "mode": "deterministic",
                    "note": "智能层剥离（无 workspace），/manifest 仅骨架信息",
                })
            else:
                self._send_json(_build_manifest(ws).to_dict())
        elif path == "/metrics":
            # 可观测性：请求统计指标（轻量落地）
            self._send_json(_collect_metrics())
        elif path == "/capabilities":
            ws = self._workspace
            if ws is None:
                self._send_json({"capabilities": [], "mode": "deterministic"})
            else:
                self._send_json({"capabilities": [s["name"] for s in ws.registry.list_skills()]})
        elif path.startswith("/audio/"):
            # TTS 生成的音频文件（data/tts/cache/）
            # 安全修复：fname 只取文件名（防 ../ 穿越），并限定在 _TTS_DIR 内
            from infrastructure.tts import _TTS_DIR

            fname = Path(path[len("/audio/"):]).name
            self._send_static(_TTS_DIR / fname, allowed_root=_TTS_DIR)
        else:
            self._send_not_found()

    def do_POST(self) -> None:
        """处理 POST 请求（可观测性：请求统计 + 流程跟踪 + request_id 贯穿）。"""
        from uuid import uuid4

        from infrastructure.request_id import set_request_id

        set_request_id(f"{os.getpid():x}-{uuid4().hex[:8]}")
        t0 = time.perf_counter()
        path = self.path.split("?", 1)[0]
        try:
            self._handle_post(path)
        finally:
            elapsed = round((time.perf_counter() - t0) * 1000, 1)
            _record_request(path, elapsed)
            from infrastructure.tracer import trace

            trace("http", "ok" if self.server else "fail", path, elapsed_ms=elapsed)

    def _handle_post(self, path: str) -> None:
        """POST 请求分发（原 do_POST 逻辑）。"""
        workspace = self._workspace

        # 安全收口：受保护 POST 端点统一鉴权（/skill 真编译、/voice/speak TTS）
        if (
            path.startswith(_PROTECTED_POST_PREFIX)
            or path in _PROTECTED_POST
        ) and not self._check_auth():
            self._send_json({"ok": False, "error": "unauthorized"}, status=401)
            return

        # UI 适配端点
        ui_routes: dict[str, Any] = {
            "/api/chat": lambda d: _chat_reply(workspace, d),
            "/api/code/generate": lambda d: asyncio.run(ui_adapter.code_generate(workspace, d)),
            "/api/code/compile": lambda d: asyncio.run(ui_adapter.code_compile(workspace, d)),
            "/api/mcu": ui_adapter.set_mcu,
            "/api/mute": ui_adapter.set_mute,
            "/api/force_chat": lambda _: ui_adapter.toggle_mode("force_chat"),
            "/api/force_code": lambda _: ui_adapter.toggle_mode("force_code"),
            "/api/force_doc": lambda _: ui_adapter.toggle_mode("force_doc"),
            "/api/search_mode": lambda _: ui_adapter.toggle_mode("search_mode"),
            "/api/voice/speak": ui_adapter.voice_speak,
        }

        if path == "/api/chat/stream":
            data = self._read_json_body()
            if data is None:
                self._send_json({"ok": False, "error": "invalid body"}, status=400)
                return
            self._send_sse("chat", workspace, data)
            return

        if path == "/api/code/stream":
            data = self._read_json_body()
            if data is None:
                self._send_json({"ok": False, "error": "invalid body"}, status=400)
                return
            self._send_sse("code", workspace, data)
            return

        if path in ui_routes:
            data = self._read_json_body() or {}
            try:
                result = ui_routes[path](data)
                self._send_json(result)
            except Exception as e:
                # 安全收口：内部异常细节只进日志，不回传客户端
                logger.exception(f"UI route {path} failed: {type(e).__name__}: {e}")
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        # Studio 技能端点
        if path.startswith("/skill/"):
            if workspace is None:
                # 智能层剥离（开源仓）：无技能可执行
                self._send_json({"ok": False, "error": "智能层剥离，无技能可执行"}, status=404)
                return
            data = self._read_json_body() or {}
            name = workspace.registry.resolve_endpoint(path)
            if name is None:
                name = path[len("/skill/"):].strip("/")
            if not name:
                self._send_json({"ok": False, "error": "missing skill name"}, status=400)
                return

            result = asyncio.run(workspace.run(name, {
                "requirement": data.get("requirement", ""),
                "artifacts": data.get("artifacts", {}),
                "mcu": (data.get("config") or {}).get("mcu", ""),
            }))
            status_code = 200 if result.status != "failed" else 422
            self._send_json(result.to_dict(), status=status_code)
            return

        self._send_not_found()

    def _send_sse(self, kind: str, workspace: Any, data: dict[str, Any]) -> None:
        """
        发送 SSE 流（统一入口）。

        kind: "chat"（聊天流）或 "code"（代码生成流）。
        流式端点共用：先发 SSE 头，再在事件循环内消费 async 生成器，
        每个 chunk 立即 flush（时刻输出，不攒批）。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        if kind == "chat":
            if workspace is None:
                # 智能层剥离（开源仓）：chat 流降级为确定性提示（一次 done）
                generator = _sse_deterministic_chat(data)
            else:
                generator = _chat_adapter.chat_stream(workspace, data)
        else:
            generator = ui_adapter.code_generate_stream(workspace, data)
        # 修复：客户端断开时 wfile.write 抛 BrokenPipeError，
        # 此前未捕获直接让线程异常退出。捕获后静默结束该 SSE 会话。
        try:
            asyncio.run(self._drain_stream(generator))
        except (BrokenPipeError, ConnectionResetError, OSError):
            logger.debug("SSE 客户端断开，结束流式会话")

    async def _drain_stream(self, generator: Any) -> None:
        """在事件循环内消费 async 生成器并逐 chunk 写出（不攒批）。"""
        async for chunk in generator:
            self.wfile.write(chunk)
            self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:
        """简化访问日志，避免打印到 stderr 干扰主进程。"""
        pass


class _StudioServer(ThreadingHTTPServer):
    """持有 Workspace 的自定义 HTTPServer（线程池：慢请求不阻塞后续请求）。"""

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseHTTPRequestHandler],
        workspace: StudioWorkspace | None = None,
        api_token: str = "",
        cors_enabled: bool = False,
        cors_allowed_origins: list[str] | None = None,
    ) -> None:
        super().__init__(server_address, RequestHandlerClass)
        # 智能层剥离（开源仓）时 build_workspace 为 None → workspace 为 None（确定性模式）
        self.workspace: StudioWorkspace | None
        if workspace is not None:
            self.workspace = workspace
        elif build_workspace is not None:
            self.workspace = build_workspace()
        else:
            self.workspace = None
        self.api_token = api_token
        self.cors_enabled = cors_enabled
        self.cors_allowed_origins = cors_allowed_origins or []
        # SSE 长连接线程设为 daemon，避免阻塞进程退出
        self.daemon_threads = True


def _nerve_root_register(port: int) -> None:
    """神经根注册（P1 细节层）：S 向文件地面登记。

    失败安全：C 守护未启动 / shared 不可达 → 静默降级，不阻断 S 启动。
    """
    try:
        # shared 路径（组织公共底座，已迁入 C）
        _shared_dir = (
            Path(__file__).resolve().parent.parent.parent.parent / "agent-c-chamber" / "shared"
        )
        if str(_shared_dir) not in sys.path:
            sys.path.insert(0, str(_shared_dir))
        from shared.nerve_root_client import boot_register

        boot_register("agent-s", "descending", port)
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 神经根注册跳过（不阻断启动）: {type(e).__name__}: {e}")


def run_server(
    host: str | None = None,
    port: int | None = None,
    workspace: StudioWorkspace | None = None,
) -> None:
    """启动 Studio HTTP 服务。"""
    # 效果跟踪器：启动即初始化（data/logs/trace.jsonl，运行时数据）
    from infrastructure.tracer import init_tracer

    init_tracer()

    # 统一芯片真相源：settings.yaml 的 chip.active 优先（若环境变量未显式设置）
    settings = _load_settings()
    chip_active = (settings.get("chip") or {}).get("active")
    if chip_active and not os.environ.get("AGENT_S_CHIP_NAME"):
        os.environ["AGENT_S_CHIP_NAME"] = chip_active

    # ── 启动审核官：环境变量/标准文件/知识目录/LLM/芯片包 五层校验 ──
    # 接线：validate_startup 此前写了没人调，现作为启动前置审核。
    # 审核不阻断启动（保留模板兜底），但逐条报告风险项。
    try:
        from infrastructure.config import KNOWLEDGE_BASE, LM_STUDIO_URL, STANDARD_PATHS
        from infrastructure.config_validator import validate_startup

        audit_ok, audit_warnings = validate_startup({
            "standard_paths": STANDARD_PATHS,
            "knowledge_dirs": {"base": KNOWLEDGE_BASE},
            "workspace_root": "",
            "llm_url": LM_STUDIO_URL,
            "check_llm": True,
        })
        print("── 启动审核官 ──")
        if audit_warnings:
            for w in audit_warnings:
                print(f"  ⚠ {w}")
            print(f"  审核: {'通过' if audit_ok else '有风险项（服务继续，注意上述警告）'}")
        else:
            print("  审核: 全部通过 ✓")
    except Exception as e:  # noqa: BLE001
        print(f"  ⚠ 启动审核异常（不阻断启动）: {type(e).__name__}: {e}")

    cfg_host, cfg_port, cfg_token = _get_server_config()
    bind_host = host if host is not None else cfg_host
    bind_port = port if port is not None else cfg_port
    _api_cfg = _load_settings().get("api") or {}
    cors_enabled = bool(_api_cfg.get("cors_enabled", False))
    cors_allowed_origins = list(_api_cfg.get("cors_allowed_origins") or [])

    ui_dir = _get_shared_ui_dir()
    ui_hint = f", UI dir: {ui_dir}" if ui_dir.exists() else ", UI not found"

    # ── 神经根注册（P1 细节层，）：文件队列持久层客户端，失败安全 ──
    _nerve_root_register(bind_port)

    server = _StudioServer(
        (bind_host, bind_port),
        _StudioHandler,
        workspace=workspace,
        api_token=cfg_token,
        cors_enabled=cors_enabled,
        cors_allowed_origins=cors_allowed_origins,
    )
    auth_hint = ", token auth: ON" if cfg_token else ", token auth: OFF (loopback only)"
    cors_hint = ", CORS: ON" if cors_allowed_origins else ""
    print(f"[Agent-S Studio] running on http://{bind_host}:{bind_port}{ui_hint}{auth_hint}{cors_hint}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run_server()
