"""Local orchestration policy. No credential loading or business registrations."""
import asyncio
from dataclasses import dataclass
import json
import re
import secrets
import threading
import time

from service_control import ALLOWED_SERVICES
from epoptia_write import CONTRACTS, execute_write, validate_write


@dataclass(frozen=True)
class Action:
    server: str
    tool: str
    fields: tuple = ()
    write: bool = False


LOGIN_ACTIONS = frozenset(
    prefix + verb for prefix in ("epoptia_login_", "epoptia_browser_login_")
    for verb in ("start", "status", "diagnose", "stop", "finalize", "sandbox_probe"))


# Reviewed policy, independent of upstream discovery and model output.
ACTIONS = {
    **{name: Action("Ermis_System", name, (), not name.endswith(("_status", "_diagnose", "_sandbox_probe")))
       for name in LOGIN_ACTIONS},
    **{name: Action("Epoptia_MES", name,
                   (contract.id_field, contract.expected_field, contract.new_field), True)
       for name, contract in CONTRACTS.items()},
    "install_epoptia_vnc_dependencies": Action("Ermis_System", "install_epoptia_vnc_dependencies", (), True),
    "epoptia_browser_access_check": Action("Ermis_System", "epoptia_browser_access_check"),
    "chromium_runtime_smoke": Action("Ermis_System", "chromium_runtime_smoke"),
    "epoptia_browser_inspect": Action("Ermis_System", "epoptia_browser_inspect"),
    "production_overview": Action("Epoptia_MES", "production_overview"),
    "wol_status": Action("Epoptia_MES", "get_wol_status", ("wol_id",)),
    "calendar_target_dates": Action("Epoptia_MES", "calendar_target_dates"),
    "wol_details": Action("Epoptia_MES", "get_wol_details", ("wol_id",)),
    "workorder_progress": Action("Epoptia_MES", "inspect_workorder_progress", ("workorder_id",)),
    "list_wols": Action("Epoptia_MES", "list_wols"),
    "due_wols": Action("Epoptia_MES", "due_wols"),
    "workstation_wip": Action("Epoptia_MES", "workstation_wip"),
    "station_wip": Action("Epoptia_MES", "workstation_wip", ("workstation",)),
    "health": Action("Ermis_System", "ermis_health"),
    "git_status": Action("Ermis_System", "ermis_git_status"),
    "service_status": Action("Ermis_System", "ermis_service_status", ("service",)),
    "job_status": Action("Ermis_System", "ermis_codex_status", ("job_id",)),
    "job_logs": Action("Ermis_System", "ermis_codex_logs", ("job_id",)),
    "restart_service": Action("Ermis_System", "ermis_service_control", ("service",), True),
    "install_chromium_dependencies": Action("Ermis_System", "install_chromium_dependencies", (), True),
}
ENDPOINTS = {"Epoptia_MES": "http://127.0.0.1:8000/mcp",
             "Ermis_System": "http://127.0.0.1:8001/mcp"}
SESSION = re.compile(r"[a-zA-Z0-9_-]{16,128}\Z")
# Epoptia overview/WIP reads can take 60–65s; bound the entire MCP exchange.
EPOPTIA_READ_TIMEOUT_SECONDS = 120
EPOPTIA_OVERALL_TIMEOUT_SECONDS = 120


def route(text):
    """Full matches only: ambiguity and compound instructions never execute."""
    if type(text) is not str or not 1 <= len(text) <= 512:
        raise ValueError
    text = text.strip().lower().rstrip("?.")
    phrases = {
        "show production overview": "production_overview",
        "production overview": "production_overview",
        "show git status": "git_status", "git status": "git_status",
        "check system health": "health", "system health": "health",
        "list work orders": "list_wols", "show due work orders": "due_wols",
        "show workstation work in progress": "workstation_wip",
    }
    if text in phrases:
        return phrases[text], {}
    match = re.fullmatch(r"what is running in ([\w -]{1,80}) now", text)
    if match:
        return "station_wip", {"workstation": match[1]}
    match = re.fullmatch(r"(?:show )?wol (status|details) ([1-9][0-9]{0,14})", text)
    if match:
        return "wol_" + match[1], {"wol_id": int(match[2])}
    match = re.fullmatch(r"(?:show )?workorder progress ([1-9][0-9]{0,14})", text)
    if match:
        return "workorder_progress", {"workorder_id": int(match[1])}
    match = re.fullmatch(r"(restart|status) service ([a-z0-9.-]+)", text)
    if match:
        return ("restart_service" if match[1] == "restart" else "service_status"), {"service": match[2]}
    match = re.fullmatch(r"(?:show )?job (status|logs) ([0-9a-f]{32})", text)
    if match:
        return "job_" + match[1], {"job_id": match[2]}
    raise ValueError


def validate(name, arguments):
    if type(name) is not str or name not in ACTIONS or type(arguments) is not dict:
        raise ValueError
    action = ACTIONS[name]
    if name in LOGIN_ACTIONS:
        from epoptia_browser import validate_login_arguments
        validate_login_arguments(name.removeprefix("epoptia_browser_login_").removeprefix("epoptia_login_"), arguments)
        return action
    if name in CONTRACTS:
        validate_write(name, arguments)
        return action
    optional = {'include_actual_production_completion'} if name == 'workorder_progress' else set()
    if name == 'calendar_target_dates':
        from calendar_target_dates import validate_filters
        if set(arguments) - {'wol_ids', 'workorder_ids', 'limit'}:
            raise ValueError
        validate_filters(**arguments)
        return action
    if name == 'epoptia_browser_inspect':
        optional = {'scope'}
    if not set(action.fields) <= set(arguments) or set(arguments) - set(action.fields) - optional:
        raise ValueError
    for key, value in arguments.items():
        if key in ("wol_id", "workorder_id"):
            valid = type(value) is int and 1 <= value <= 999999999999999
        elif key == 'include_actual_production_completion':
            valid = type(value) is bool
        elif key == 'scope':
            valid = type(value) is str and value in {'session', 'dates', 'smoke'}
        elif key == "service":
            valid = type(value) is str and value in ALLOWED_SERVICES
        elif key == "workstation":
            valid = type(value) is str and re.fullmatch(r"[\w -]{1,80}", value) and value.strip()
        else:
            valid = type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value)
        if not valid:
            raise ValueError
    return action


class UpstreamDiagnosticError(RuntimeError):
    """Contains locally selected labels, never upstream exception text."""
    def __init__(self, stage, category):
        super().__init__("Upstream failed")
        self.stage, self.category = stage, category


def _error_category(error, stage):
    from httpx2 import ConnectError, ConnectTimeout, ReadTimeout, HTTPStatusError, ProtocolError
    from mcp.shared.exceptions import MCPError
    from pydantic import ValidationError

    # AnyIO wraps transport failures. Inspect types, never exception messages.
    if isinstance(error, BaseExceptionGroup):
        categories = [_error_category(child, stage) for child in error.exceptions]
        return next((c for c in categories if c != "unexpected_error"), "unexpected_error")
    if isinstance(error, UpstreamDiagnosticError):
        return error.category
    for cls, category in ((ConnectTimeout, "connect_timeout"), (ConnectError, "connect_error"),
                          (ReadTimeout, "read_timeout"), (TimeoutError, "read_timeout"),
                          (ProtocolError, "protocol_error"), (ValidationError, "protocol_error"),
                          (json.JSONDecodeError, "protocol_error")):
        if isinstance(error, cls):
            return category
    if isinstance(error, HTTPStatusError):
        code = error.response.status_code
        return f"http_status_{code}" if type(code) is int and 100 <= code <= 599 else "protocol_error"
    if isinstance(error, MCPError):
        return stage
    return "unexpected_error"


async def call_existing(server, tool, arguments):
    if server == "Ermis_System" and tool in LOGIN_ACTIONS:
        validate(tool, arguments)
        from epoptia_browser import login_command
        return await login_command(tool.removeprefix("epoptia_browser_login_").removeprefix("epoptia_login_"), **arguments)
    if (server, tool) == ("Ermis_System", "install_epoptia_vnc_dependencies"):
        validate("install_epoptia_vnc_dependencies", arguments)
        from epoptia_vnc_dependencies import install
        return await asyncio.to_thread(install)
    if (server, tool) == ("Ermis_System", "epoptia_browser_access_check"):
        validate("epoptia_browser_access_check", arguments)
        return {"ok": False, "status": "access_denied"}
    if (server, tool) == ('Ermis_System', 'chromium_runtime_smoke'):
        validate('chromium_runtime_smoke', arguments)
        return {'ok': False, 'status': 'service_context_required'}
    if (server, tool) == ('Ermis_System', 'install_chromium_dependencies'):
        validate('install_chromium_dependencies', arguments)
        from chromium_dependencies import install
        return await asyncio.to_thread(install)
    if (server, tool) == ('Ermis_System', 'epoptia_browser_inspect'):
        # Internal dispatch: no new MCP registration or self-HTTP round trip.
        validate('epoptia_browser_inspect', arguments)
        from epoptia_browser import inspect_scope
        return await inspect_scope(**arguments)
    if server != "Epoptia_MES":
        return await _call_existing(server, tool, arguments)
    diagnostic = {"stage": "connect_error"}
    try:
        return await _call_existing(server, tool, arguments, diagnostic)
    except Exception as error:
        stage, category = diagnostic.get("http_failure",
            (diagnostic["stage"], _error_category(error, diagnostic["stage"])))
        raise UpstreamDiagnosticError(stage, category) from None


async def _call_existing(server, tool, arguments, diagnostic=None):
    # Use existing protected local services, never import mcp_server/.env.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from httpx2 import AsyncClient, Timeout
    overall_timeout = 30
    http_timeout = 30
    if server == "Epoptia_MES":
        overall_timeout = EPOPTIA_OVERALL_TIMEOUT_SECONDS
        http_timeout = Timeout(30, read=EPOPTIA_READ_TIMEOUT_SECONDS)
    options = {}
    if diagnostic is not None:
        async def track_stage(request):
            if request.method == "POST":
                # Only our outbound method is retained, never payloads or URLs.
                diagnostic.pop("http_failure", None)
                method = json.loads(request.content).get("method")
                diagnostic["stage"] = {
                    "initialize": "initialize_failed",
                    "notifications/initialized": "initialized_notification_failed",
                    "tools/list": "tools_list_failed",
                    "tools/call": "tools_call_failed",
                }.get(method, "protocol_error")
        async def track_status(response):
            code = response.status_code
            if response.request.method == "POST" and type(code) is int and 300 <= code <= 599:
                # The SDK may replace HTTPStatusError with a generic MCP error.
                diagnostic.setdefault("http_failure", (diagnostic["stage"], f"http_status_{code}"))
        options["event_hooks"] = {"request": [track_stage], "response": [track_status]}
    async with asyncio.timeout(overall_timeout), AsyncClient(
            trust_env=False, follow_redirects=False, timeout=http_timeout, **options) as client:
        async with streamable_http_client(ENDPOINTS[server], http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                if diagnostic is not None:
                    diagnostic["stage"] = "initialize_failed"
                await session.initialize()
                if diagnostic is not None:
                    diagnostic["stage"] = "tools_call_failed"
                result = await session.call_tool(tool, arguments)
                if diagnostic is not None:
                    diagnostic["stage"] = "tools_call_failed"
                if result.is_error:
                    if diagnostic is None:
                        raise RuntimeError("Upstream failed")
                    raise UpstreamDiagnosticError("tools_call_failed", "tools_call_failed")
                if diagnostic is not None:
                    diagnostic["stage"] = "protocol_error"
                # Existing tools provide structured, bounded business/system results.
                data = result.structured_content
                if data is None and len(result.content) == 1 and result.content[0].type == "text":
                    # Existing unparameterized dict tools use JSON text in this SDK.
                    try:
                        data = json.loads(result.content[0].text)
                    except (ValueError, TypeError):
                        if diagnostic is None:
                            raise RuntimeError("Unexpected upstream response") from None
                        raise UpstreamDiagnosticError("protocol_error", "protocol_error") from None
                if not isinstance(data, dict):
                    if diagnostic is None:
                        raise RuntimeError("Unexpected upstream response")
                    raise UpstreamDiagnosticError("protocol_error", "protocol_error")
                return data


class Gateway:
    def __init__(self, invoke=call_existing, clock=time.monotonic):
        self.invoke = invoke
        self.clock = clock
        self.pending = {}
        self.lock = threading.Lock()

    async def execute(self, name, arguments):
        action = validate(name, arguments)
        if name in CONTRACTS:
            # Never forward these disabled writes to an upstream MCP or browser.
            return await execute_write(name, arguments)
        args = dict(arguments)
        if name == "restart_service":
            args["operation"] = "restart"
        try:
            result = await self.invoke(action.server, action.tool, args)
        except Exception as error:
            if name == "install_epoptia_vnc_dependencies":
                return {"ok": False, "status": "failed"}
            if name == "epoptia_browser_access_check":
                return {"ok": False, "status": "navigation_failed"}
            if name == "chromium_runtime_smoke":
                return {"ok": False, "status": "launch_failed"}
            if name == "install_chromium_dependencies":
                return {"ok": False, "status": "outcome_unknown"}
            failure = {"ok": False, "status": "outcome_unknown" if action.write else "upstream_unavailable",
                       "retry_safe": not action.write}
            if action.server == "Epoptia_MES":
                failure.update(failure_stage=error.stage if isinstance(error, UpstreamDiagnosticError)
                               else "unexpected_error",
                               error_category=_error_category(error, "unexpected_error"))
            return failure
        if name == "install_epoptia_vnc_dependencies":
            from epoptia_vnc_dependencies import sanitize_result
            return sanitize_result(result)
        if name == "epoptia_browser_access_check":
            from epoptia_browser_access_check import sanitize_result
            return sanitize_result(result)
        if name == "chromium_runtime_smoke":
            from chromium_runtime_smoke import sanitize_result
            return sanitize_result(result)
        if name == "install_chromium_dependencies":
            from chromium_dependencies import sanitize_result
            return sanitize_result(result)
        return {"ok": True, "status": "completed", "action": name, "result": result}

    async def request(self, body):
        try:
            if type(body) is not dict or type(body.get("session_id")) is not str or not SESSION.fullmatch(body["session_id"]):
                raise ValueError
            if set(body) == {"session_id", "text"}:
                name, arguments = route(body["text"])
            elif set(body) == {"session_id", "action", "arguments"}:
                name, arguments = body["action"], body["arguments"]
            else:
                raise ValueError
            action = validate(name, arguments)
        except (ValueError, TypeError):
            return {"ok": False, "status": "unsupported_request"}
        if not action.write:
            return await self.execute(name, arguments)
        with self.lock:
            now = self.clock()
            self.pending = {k: v for k, v in self.pending.items() if v[0] > now}
            if len(self.pending) >= 256:
                return {"ok": False, "status": "busy"}
            token = secrets.token_urlsafe(32)
            self.pending[token] = (now + 120, body["session_id"], name, dict(arguments))
        return {"ok": True, "status": "confirmation_required", "confirmation_id": token,
                "expires_in_seconds": 120, "action": name, "arguments": dict(arguments),
                "prompt": ("Browser login " + name.rsplit("_", 1)[1] +
                           "? Enrollment remains fail-closed until a reviewed authentication signal and private supervisor backend are configured."
                           if name in LOGIN_ACTIONS else name + ": " + json.dumps(arguments, ensure_ascii=True) + ". "
                           "Execution is currently disabled pending a verified authenticated write transport."
                           if name in CONTRACTS else
                           "Install x11vnc, noVNC, websockify, and xauth if needed for a loopback-only SSH-tunneled Epoptia enrollment window? This makes no Epoptia data changes."
                           if name == "install_epoptia_vnc_dependencies" else
                           "Install the fixed Playwright/Chromium Ubuntu runtime dependency set? "
                           "This makes no Epoptia data changes."
                           if name == "install_chromium_dependencies" else
                           "Restart service " + arguments["service"] + "?")}

    async def confirm(self, body):
        if (type(body) is not dict or set(body) != {"session_id", "confirmation_id", "approved"}
                or type(body["session_id"]) is not str or type(body["confirmation_id"]) is not str
                or type(body["approved"]) is not bool):
            return {"ok": False, "status": "invalid_confirmation"}
        with self.lock:
            pending = self.pending.get(body["confirmation_id"])
            if not pending or pending[1] != body["session_id"]:
                return {"ok": False, "status": "invalid_confirmation"}
            del self.pending[body["confirmation_id"]]
        if pending[0] <= self.clock():
            return {"ok": False, "status": "invalid_confirmation"}
        if not body["approved"]:
            return {"ok": True, "status": "cancelled"}
        return await self.execute(pending[2], pending[3])
