import os
import requests
from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins="*")

SYNTHESIA_API_KEY = os.environ.get("SYNTHESIA_API_KEY", "")
SYNTHESIA_BASE_URL = "https://api.synthesia.io/v2"

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}


def cors_response(data, status=200):
    resp = make_response(jsonify(data), status)
    for key, value in CORS_HEADERS.items():
        resp.headers[key] = value
    return resp


def get_auth_headers():
    return {
        "Authorization": SYNTHESIA_API_KEY,
        "Content-Type": "application/json",
    }


@app.after_request
def add_cors_headers(response):
    for key, value in CORS_HEADERS.items():
        response.headers[key] = value
    return response


@app.route("/", methods=["GET", "OPTIONS"])
def health_check():
    if request.method == "OPTIONS":
        return cors_response({}, 200)
    return cors_response({"status": "ok", "service": "Synthesia API Proxy"})


@app.route("/create-video", methods=["POST", "OPTIONS"])
def create_video():
    if request.method == "OPTIONS":
        return cors_response({}, 200)
    try:
        payload = request.get_json(force=True, silent=True) or {}
        response = requests.post(
            f"{SYNTHESIA_BASE_URL}/videos",
            json=payload,
            headers=get_auth_headers(),
        )
        return cors_response(response.json(), response.status_code)
    except requests.RequestException as e:
        return cors_response({"error": str(e)}, 502)


@app.route("/video-status/<video_id>", methods=["GET", "OPTIONS"])
def video_status(video_id):
    if request.method == "OPTIONS":
        return cors_response({}, 200)
    try:
        response = requests.get(
            f"{SYNTHESIA_BASE_URL}/videos/{video_id}",
            headers=get_auth_headers(),
        )
        return cors_response(response.json(), response.status_code)
    except requests.RequestException as e:
        return cors_response({"error": str(e)}, 502)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
