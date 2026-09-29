"""Offline guard for test runs: blocks real network and privileged commands.

unittest.mock patches applied by tests replace these wrappers, so mocked tests
still work; anything unmocked that tries to escape raises GuardViolation.
"""
import os
import socket
import subprocess

LOG = os.environ.get("ERMIS_GUARD_LOG")
BLOCKED_PORTS = {8000, 8001, 8010, 8080, 8081, 80, 443}
BLOCKED_WORDS = ("sudo", "systemctl", "apparmor_parser", "aa-status", "aa-enforce",
                 "aa-complain", "bootstrap.sh", "tunnel-client", "ermis-admin", "journalctl")


class GuardViolation(RuntimeError):
    pass


def _record(kind, detail):
    if LOG:
        with open(LOG, "a") as fh:
            fh.write(f"{os.getpid()} {kind} {detail}\n")
    raise GuardViolation(f"offline guard blocked {kind}: {detail}")


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _check_addr(sock, address):
    if sock.family == socket.AF_UNIX:
        return
    host, port = address[0], address[1]
    if host not in ("127.0.0.1", "::1", "localhost") or port in BLOCKED_PORTS:
        _record("network", f"{host}:{port}")


def connect(self, address):
    _check_addr(self, address)
    return _connect(self, address)


def connect_ex(self, address):
    _check_addr(self, address)
    return _connect_ex(self, address)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex

_getaddrinfo = socket.getaddrinfo


def getaddrinfo(host, *args, **kwargs):
    if host not in (None, "127.0.0.1", "::1", "localhost"):
        _record("dns", str(host))
    return _getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = getaddrinfo


def _check_cmd(args):
    if not isinstance(args, (str, bytes)) and args and os.path.basename(str(args[0])) == "git":
        _record("command", " ".join(map(str, args))[:200])
    text = args if isinstance(args, (str, bytes)) else " ".join(map(str, args))
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    for word in BLOCKED_WORDS:
        if word in text:
            _record("command", text[:200])


_Popen_init = subprocess.Popen.__init__


def _popen_init(self, args, *a, **kw):
    _check_cmd(args)
    return _Popen_init(self, args, *a, **kw)


subprocess.Popen.__init__ = _popen_init

_system = os.system


def _os_system(cmd):
    _check_cmd(cmd)
    return _system(cmd)


os.system = _os_system
