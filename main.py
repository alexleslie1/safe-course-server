from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type", "Authorization"], methods=["GET", "POST", "OPTIONS"])

HEYGEN_API_KEY = os.environ.get("HEYGEN_API_KEY", "")
DEFAULT_AVATAR_ID = "26f5fc9be1fc47eab0ef65df30d47a4e"
DEFAULT_VOICE_ID = "cf36ed19c8ce4dc9b25ee37d0a68bb1b"

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/", methods=["GET", "OPTIONS"])
def health():
    return jsonify({"service": "SAFE Course Generator — HeyGen Proxy", "status": "ok"})

@app.route("/create-video", methods=["POST", "OPTIONS"])
def create_video():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
    try:
        body = request.get_json()
        title = body.get("title", "SAFE Enablement Video")
        script = body.get("script", "")
        avatar_id = body.get("avatar_id", DEFAULT_AVATAR_ID)
        voice_id = body.get("voice_id", DEFAULT_VOICE_ID)

        if not script:
            return jsonify({"error": "No script provided"}), 400

        script = script[:5000]

        payload = {
            "video_inputs": [
                {
                    "character": {
                        "type": "avatar",
                        "avatar_id": avatar_id,
                        "avatar_style": "normal"
                    },
                    "voice": {
                        "type": "text",
                        "input_text": script,
                        "voice_id": voice_id,
                        "speed": 1.0
                    },
                    "background": {
                        "type": "transparent"
                    }
                }
            ],
            "dimension": {
                "width": 1920,
                "height": 1080
            },
            "title": title
        }

        resp = requests.post(
            "https://api.heygen.com/v2/video/generate",
            json=payload,
            headers={
                "X-Api-Key": HEYGEN_API_KEY,
                "Content-Type": "application/json"
            }
        )

        if resp.ok:
            data = resp.json()
            video_id = data.get("data", {}).get("video_id")
            return jsonify({
                "success": True,
                "video_id": video_id,
                "status": "processing",
                "message": "Video submitted to HeyGen successfully"
            })
        else:
            return jsonify({"error": f"HeyGen error {resp.status_code}: {resp.text}"}), resp.status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/video-status/<video_id>", methods=["GET", "OPTIONS"])
def video_status(video_id):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
    try:
        resp = requests.get(
            f"https://api.heygen.com/v1/video_status.get?video_id={video_id}",
            headers={"X-Api-Key": HEYGEN_API_KEY}
        )
        if resp.ok:
            data = resp.json().get("data", {})
            return jsonify({
                "video_id": video_id,
                "status": data.get("status"),
                "video_url": data.get("video_url", ""),
                "thumbnail_url": data.get("thumbnail_url", ""),
                "duration": data.get("duration", 0)
            })
        else:
            return jsonify({"error": f"HeyGen error {resp.status_code}"}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/get-avatars", methods=["GET", "OPTIONS"])
def get_avatars():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
    try:
        resp = requests.get(
            "https://api.heygen.com/v2/avatars",
            headers={"X-Api-Key": HEYGEN_API_KEY}
        )
        if resp.ok:
            return jsonify(resp.json())
        else:
            return jsonify({"error": f"HeyGen error {resp.status_code}: {resp.text}"}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
