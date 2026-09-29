import os
import requests
import epoptia_throttle
from flask import Flask, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

BASE_URL = os.getenv("EPOPTIA_BASE_URL")
API_KEY = os.getenv("EPOPTIA_API_KEY")

HEADERS = {
    "X-Auth-Token": API_KEY,
    "Accept": "application/json"
}


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "message": "Epoptia Bridge is running"
    })


@app.route("/epoptia/test")
def test_epoptia():
    try:
        response = epoptia_throttle.call(requests.get,
            f"{BASE_URL}/api/3.03/test-connection",
            headers=HEADERS,
            timeout=10
        )

        return jsonify({
            "epoptia_status": response.status_code,
            "response": response.json()
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def find_wol(wol_id):
    # Start from the newest page because recent WOLs are there.
    first_response = epoptia_throttle.call(requests.get,
        f"{BASE_URL}/api/3.03/workorderlines",
        headers=HEADERS,
        params={"page": 1, "limit": 100},
        timeout=20
    )
    first_response.raise_for_status()
    first_data = first_response.json()

    total_pages = first_data.get("numberOfPages", 0)

    for page in range(total_pages, 0, -1):
        response = epoptia_throttle.call(requests.get,
            f"{BASE_URL}/api/3.03/workorderlines",
            headers=HEADERS,
            params={"page": page, "limit": 100},
            timeout=20
        )
        response.raise_for_status()
        data = response.json()

        for wol in data.get("workorderLines", []):
            if str(wol.get("workorderline_id")) == str(wol_id):
                return wol

    return None


@app.route("/wol/<int:wol_id>")
def get_wol(wol_id):
    try:
        wol = find_wol(wol_id)

        if wol is None:
            return jsonify({
                "message": "WOL not found",
                "workorderline_id": wol_id
            }), 404

        routing = wol.get("erp_routing", [])

        completed = []
        in_progress = []
        not_started = []

        for step in routing:
            item = {
                "workstation": step.get("workstationName"),
                "job": (
                    step.get("job_tag", {}).get("name")
                    if isinstance(step.get("job_tag"), dict)
                    else None
                ),
                "status": step.get("status"),
                "qty_done": step.get("qty_done")
            }

            if step.get("status") == "completed":
                completed.append(item)
            elif step.get("status") in ["started", "paused", "in_progress"]:
                in_progress.append(item)
            else:
                not_started.append(item)

        return jsonify({
            "workorderline_id": wol.get("workorderline_id"),
            "description": wol.get("description"),
            "production_status": wol.get("production_status"),
            "quantity": wol.get("quantity"),
            "target_day": wol.get("target_day"),
            "client": wol.get("client", {}).get("name"),
            "completed": completed,
            "in_progress": in_progress,
            "not_started": not_started
        })

    except Exception as e:
        return jsonify({
            "error": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=False)
