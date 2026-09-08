"""Separate loopback HTTP gateway; adds no MCP tools."""
import asyncio
from flask import Flask, jsonify, request
from ermis_gateway import Gateway


def create_app(gateway=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4096
    gateway = gateway or Gateway()

    @app.before_request
    def local_only():
        if (request.remote_addr not in ("127.0.0.1", "::1")
                or request.host not in ("127.0.0.1:8002", "localhost:8002", "[::1]:8002")
                or "Origin" in request.headers):
            return jsonify(ok=False, status="forbidden"), 403

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

    return app


if __name__ == "__main__":
    import uvicorn
    from uvicorn.middleware.wsgi import WSGIMiddleware
    uvicorn.run(WSGIMiddleware(create_app()), host="127.0.0.1", port=8002,
                proxy_headers=False, access_log=False)
