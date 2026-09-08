"""Local orchestration policy. No credential loading or business registrations."""
import asyncio
from dataclasses import dataclass
import re
import secrets
import threading
import time

from service_control import ALLOWED_SERVICES


@dataclass(frozen=True)
class Action:
    server: str
    tool: str
    fields: tuple = ()
    write: bool = False


# Reviewed policy, independent of upstream discovery and model output.
ACTIONS = {
    "production_overview": Action("Epoptia_MES", "production_overview"),
    "wol_status": Action("Epoptia_MES", "get_wol_status", ("wol_id",)),
    "wol_details": Action("Epoptia_MES", "get_wol_details", ("wol_id",)),
    "workorder_progress": Action("Epoptia_MES", "inspect_workorder_progress", ("workorder_id",)),
    "list_wols": Action("Epoptia_MES", "list_wols"),
    "due_wols": Action("Epoptia_MES", "due_wols"),
    "workstation_wip": Action("Epoptia_MES", "workstation_wip"),
    "health": Action("Ermis_System", "ermis_health"),
    "git_status": Action("Ermis_System", "ermis_git_status"),
    "service_status": Action("Ermis_System", "ermis_service_status", ("service",)),
    "job_status": Action("Ermis_System", "ermis_codex_status", ("job_id",)),
    "job_logs": Action("Ermis_System", "ermis_codex_logs", ("job_id",)),
    "restart_service": Action("Ermis_System", "ermis_service_control", ("service",), True),
}
ENDPOINTS = {"Epoptia_MES": "http://127.0.0.1:8000/mcp",
             "Ermis_System": "http://127.0.0.1:8001/mcp"}
SESSION = re.compile(r"[a-zA-Z0-9_-]{16,128}\Z")


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
    if set(arguments) != set(action.fields):
        raise ValueError
    for key, value in arguments.items():
        if key in ("wol_id", "workorder_id"):
            valid = type(value) is int and 1 <= value <= 999999999999999
        elif key == "service":
            valid = type(value) is str and value in ALLOWED_SERVICES
        else:
            valid = type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value)
        if not valid:
            raise ValueError
    return action


async def call_existing(server, tool, arguments):
    # Use existing protected local services, never import mcp_server/.env.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from httpx2 import AsyncClient
    async with asyncio.timeout(30), AsyncClient(
            trust_env=False, follow_redirects=False, timeout=30) as client:
        async with streamable_http_client(ENDPOINTS[server], http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                if result.is_error:
                    raise RuntimeError("Upstream failed")
                # Existing tools provide structured, bounded business/system results.
                data = result.structured_content
                if not isinstance(data, dict):
                    raise RuntimeError("Unexpected upstream response")
                return data


class Gateway:
    def __init__(self, invoke=call_existing, clock=time.monotonic):
        self.invoke = invoke
        self.clock = clock
        self.pending = {}
        self.lock = threading.Lock()

    async def execute(self, name, arguments):
        action = validate(name, arguments)
        args = dict(arguments)
        if action.write:
            args["operation"] = "restart"
        try:
            result = await self.invoke(action.server, action.tool, args)
        except Exception:
            return {"ok": False, "status": "outcome_unknown" if action.write else "upstream_unavailable",
                    "retry_safe": not action.write}
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
                "prompt": "Restart service " + arguments["service"] + "?"}

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
