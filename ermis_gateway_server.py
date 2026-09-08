"""Separate loopback HTTP gateway; adds no MCP tools."""
import asyncio
import json
import logging
import time
from flask import Flask, g, jsonify, request, send_from_directory
from ermis_gateway import Gateway
from ermis_gateway_voice import VoiceSessions


class BoundedHTTP:
    """Enforce limits before the WSGI adapter buffers incoming bodies."""
    def __init__(self, app, body_timeout=10):
        self.app = app
        self.body_timeout = body_timeout

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = 65536 if scope["path"] == "/voice/session" else 4096
        deadline = time.monotonic() + self.body_timeout
        received = 0

        class Rejected(Exception):
            def __init__(self, status):
                self.status = status

        async def bounded_receive():
            nonlocal received
            try:
                message = await asyncio.wait_for(receive(), max(0, deadline - time.monotonic()))
            except TimeoutError:
                raise Rejected(408) from None
            if message["type"] == "http.disconnect":
                raise Rejected(400)
            received += len(message.get("body", b""))
            if received > limit:
                raise Rejected(413)
            return message

        try:
            for name, value in scope.get("headers", []):
                if name == b"content-length":
                    if not value.isdigit() or len(value) > 10:
                        raise Rejected(400)
                    if int(value) > limit:
                        raise Rejected(413)
            await self.app(scope, bounded_receive, send)
        except Rejected as error:
            await send({"type": "http.response.start", "status": error.status,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"cache-control", b"no-store"),
                                    (b"x-content-type-options", b"nosniff")]})
            await send({"type": "http.response.body",
                        "body": b'{"ok":false,"status":"invalid_request"}'})


def create_app(gateway=None, voice=None):
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 4096
    gateway = gateway or Gateway()
    voice = voice or VoiceSessions(gateway)
    logger = logging.getLogger("ermis.gateway")
    app.log_exception = lambda info: logger.error('{"event":"internal_error"}')
    app.extensions["gateway_voice"] = voice

    @app.before_request
    def local_only():
        g.started = time.monotonic()
        if request.path == "/voice/session":
            request.max_content_length = 65536
        origin = request.headers.get("Origin")
        browser_route = request.path.startswith("/voice/")
        if (request.remote_addr not in ("127.0.0.1", "::1")
                or request.host not in ("127.0.0.1:8002", "localhost:8002", "[::1]:8002")
                or (origin is not None and (not browser_route or origin != "http://" + request.host))
                or request.headers.get("Sec-Fetch-Site") == "cross-site"):
            return jsonify(ok=False, status="forbidden"), 403

    @app.after_request
    def finish(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'")
        # Only fixed endpoint names and numeric metadata; never URLs, IDs or payloads.
        logger.info(json.dumps({"event": "http_request", "endpoint": request.endpoint or "unmatched",
                               "status": response.status_code,
                               "duration_ms": round((time.monotonic() - g.started) * 1000)}))
        return response

    @app.get("/")
    def index():
        return send_from_directory("gateway_static", "index.html")

    @app.get("/voice.js")
    def javascript():
        return send_from_directory("gateway_static", "voice.js")

    @app.get("/voice.css")
    def stylesheet():
        return send_from_directory("gateway_static", "voice.css")

    @app.get("/health")
    def health():
        return jsonify(ok=True, service="ermis-gateway", status="alive")

    @app.post("/voice/session")
    def voice_session():
        result, code = voice.create(request.get_json())
        return jsonify(result), code

    @app.post("/voice/tool")
    @app.post("/voice/confirm")
    @app.post("/voice/stop")
    def voice_action():
        result, code = voice.handle(request.path.rsplit("/", 1)[-1], request.get_json())
        return jsonify(result), code

    @app.post("/request")
    def submit():
        return jsonify(asyncio.run(gateway.request(request.get_json())))

    @app.post("/confirm")
    def confirm():
        return jsonify(asyncio.run(gateway.confirm(request.get_json())))

    @app.errorhandler(400)
    @app.errorhandler(413)
    @app.errorhandler(415)
    def invalid(error):
        return jsonify(ok=False, status="invalid_request"), error.code

    @app.errorhandler(500)
    def unexpected(error):
        return jsonify(ok=False, status="internal_error"), 500

    return app


if __name__ == "__main__":
    import threading
    import uvicorn
    from uvicorn.middleware.wsgi import WSGIMiddleware
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    app = create_app()

    def expire_sessions():
        while True:
            time.sleep(30)
            app.extensions["gateway_voice"].reap()

    threading.Thread(target=expire_sessions, daemon=True).start()
    uvicorn.run(BoundedHTTP(WSGIMiddleware(app)), host="127.0.0.1", port=8002,
                proxy_headers=False, access_log=False)
