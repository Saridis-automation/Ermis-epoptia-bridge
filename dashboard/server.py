"""Standalone dashboard: python -m dashboard.server --port 8010."""

import argparse
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from flask import Flask, jsonify
from threading import Event, Lock, Thread
from time import monotonic
from copy import deepcopy

import epoptia_throttle
from .adapter import map_snapshot, timestamp
from .provider import LocalEpoptiaProvider, empty_snapshot
from .schedule import freshness_seconds, refresh_delay


def create_app(provider=None, clock=None, timer=monotonic, response_wait=0.1, cache_dir=None,
               halted=None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    clock = clock or (lambda: datetime.now(timezone.utc))
    halted = halted or epoptia_throttle.halted

    provider = provider if provider is not None else LocalEpoptiaProvider(clock=clock, cache_dir=cache_dir)
    optional_provider = provider if (callable(getattr(type(provider), 'refresh_optional', None))
                                    and not hasattr(type(provider), 'refresh_sources')) else None
    initial = provider.initial_snapshot if optional_provider else None
    cached = map_snapshot(initial, clock()) if initial else None
    next_optional = 0
    core_provider = provider if callable(getattr(type(provider), "refresh_core", None)) else None
    core_due = {tool: 0 for tool in getattr(provider, "refresh_sources", ("production_overview", "workstation_wip"))}
    next_read = 0
    busy = False
    lock = Lock()

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    def next_due():
        return timer() + refresh_delay(clock())

    def refresh_optional():
        nonlocal next_optional
        try:
            asyncio.run(optional_provider.refresh_optional())
        except Exception:
            # Optional failures never publish a base model or transport diagnostics.
            pass
        finally:
            with lock:
                next_optional = next_due()

    def refresh():
        nonlocal cached, next_read
        try:
            model = map_snapshot(provider(), clock())
        except Exception:
            model = deepcopy(cached) if cached else map_snapshot(empty_snapshot(clock()), clock())
            if cached is None:
                model["observed_at"] = None
            model["data_status"] = "offline"
        with lock:
            cached = model
            next_read = next_due()

    def refresh_core(tool):
        try:
            asyncio.run(core_provider.refresh_core(tool))
        finally:
            with lock:
                core_due[tool] = next_due()

    def run_cycle(tools, legacy, optional):
        # One worker, one source at a time: Epoptia never sees parallel scans.
        nonlocal busy
        try:
            for tool in tools:
                if halted() is not None:
                    break
                refresh_core(tool)
            if legacy and halted() is None:
                refresh()
            if optional and halted() is None:
                refresh_optional()
        finally:
            with lock:
                busy = False

    def schedule():
        nonlocal busy
        # A halt (HTTP 429/403) stops all refreshes until a person clears it.
        if halted() is not None:
            return
        with lock:
            if busy:
                return
            now = timer()
            tools = [tool for tool, due in core_due.items() if now >= due] if core_provider else []
            legacy = not core_provider and now >= next_read
            optional = bool(optional_provider) and now >= next_optional
            if not (tools or legacy or optional):
                return
            busy = True
        Thread(target=run_cycle, args=(tools, legacy, optional), daemon=True,
               name='dashboard-refresh').start()

    stopped = Event()
    def scheduler():
        while not stopped.is_set():
            schedule()
            stopped.wait(0.25)
    app.extensions['dashboard_stop'] = stopped
    Thread(target=scheduler, daemon=True, name='dashboard-scheduler').start()

    def with_halt(model):
        record = halted()
        if record is not None:
            model["data_status"] = "halted"
            model["halt"] = {key: record.get(key) for key in ("halted_at", "http_status", "path")}
        return model

    @app.get("/api/dashboard")
    def dashboard():
        now = clock()
        with lock:
            model = deepcopy(cached)
        if core_provider:
            model = map_snapshot(core_provider.core_snapshot(), clock())
        if model is None:
            model = map_snapshot(empty_snapshot(now), now)
            model.update(data_status="loading", observed_at=None)
        elif not core_provider and model["data_status"] in ("live", "partial"):
            if (clock() - timestamp(model["observed_at"])).total_seconds() > freshness_seconds(clock()):
                model["data_status"] = "stale"
        if optional_provider:
            optional = optional_provider.optional_snapshot()
            if optional is not None:
                try:
                    enrichment = map_snapshot(optional, clock())
                except (ValueError, TypeError, AttributeError, KeyError):
                    # Corrupt optional data must never break the cached core API.
                    return jsonify(with_halt(model))
                for key in ('urgent_orders', 'urgent_orders_status'):
                    model[key] = enrichment[key]
                for key in ('overdue_work', 'completed_today'):
                    model['today'][key] = enrichment['today'][key]
                for key in ('urgent_orders', 'overdue_work', 'completed_today'):
                    model['field_status'][key] = enrichment['field_status'][key]
                    if key in enrichment['field_observed_at']:
                        model['field_observed_at'][key] = enrichment['field_observed_at'][key]
        return jsonify(with_halt(model))

    @app.get("/health")
    def health():
        return jsonify(status="ok", component="dashboard")

    return app


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=port_number, default=8010)
    args = parser.parse_args(argv)
    try:
        from waitress import serve
    except ImportError:
        parser.exit(1, "Install dashboard/requirements.txt in the project venv first.\n")
    serve(create_app(cache_dir=Path(__file__).parent / ".cache"), host="127.0.0.1", port=args.port, threads=4,
          connection_limit=64, backlog=64, channel_timeout=30,
          cleanup_interval=5, max_request_header_size=16384,
          max_request_body_size=65536, expose_tracebacks=False,
          clear_untrusted_proxy_headers=True, ident="dashboard")


if __name__ == "__main__":
    main()
