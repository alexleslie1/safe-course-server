from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os

app = Flask(__name__)
CORS(app, origins="*", allow_headers=["Content-Type", "Authorization"], methods=["GET", "POST", "OPTIONS"])

SYNTHESIA_API_KEY = os.environ.get("SYNTHESIA_API_KEY", "")
AVATAR_ID = "avatar_2b2f974b-78fc-4f63-a5bc-b4dcea6e9151"

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

@app.route("/", methods=["GET", "OPTIONS"])
def health():
    return jsonify({"service": "Synthesia API Proxy", "status": "ok"})

@app.route("/create-video", methods=["POST", "OPTIONS"])
def create_video():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
    try:
        body = request.get_json()
        title = body.get("title", "SAFE Enablement Video")
        script = body.get("script", "")
        avatar_id = body.get("avatar_id", AVATAR_ID)

        if not script:
            return jsonify({"error": "No script provided"}), 400

        script = script[:3000]

        # First get available backgrounds from Synthesia
        payload = {
            "title": title,
            "visibility": "private",
            "input": [
                {
                    "avatar": avatar_id,
                    "avatarSettings": {
                        "horizontalAlign": "center",
                        "scale": 1.0,
                        "style": "rectangular",
                        "seamless": False
                    },
                    "background": "off_white",
                    "scriptText": script
                }
            ]
        }

        resp = requests.post(
            "https://api.synthesia.io/v2/videos",
            json=payload,
            headers={
                "Authorization": SYNTHESIA_API_KEY,
                "Content-Type": "application/json"
            }
        )

        if resp.ok:
            data = resp.json()
            return jsonify({
                "success": True,
                "video_id": data.get("id"),
                "status": data.get("status"),
                "message": "Video submitted to Synthesia successfully"
            })
        else:
            return jsonify({"error": f"Synthesia error {resp.status_code}: {resp.text}"}), resp.status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/video-status/<video_id>", methods=["GET", "OPTIONS"])
def video_status(video_id):
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
    try:
        resp = requests.get(
            f"https://api.synthesia.io/v2/videos/{video_id}",
            headers={"Authorization": SYNTHESIA_API_KEY}
        )
        if resp.ok:
            data = resp.json()
            return jsonify({
                "video_id": video_id,
                "status": data.get("status"),
                "download_url": data.get("download", ""),
                "duration": data.get("duration", 0)
            })
        else:
            return jsonify({"error": f"Synthesia error {resp.status_code}"}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/get-avatars", methods=["GET", "OPTIONS"])
def get_avatars():
    if request.method == "OPTIONS":
        return jsonify({"status": "ok"}), 200
    try:
        resp = requests.get(
            "https://api.synthesia.io/v2/avatars",
            headers={"Authorization": SYNTHESIA_API_KEY}
        )
        if resp.ok:
            return jsonify(resp.json())
        else:
            return jsonify({"error": f"Synthesia error {resp.status_code}: {resp.text}"}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
