"""
SAFE Course Generator - HeyGen Proxy Backend
Deployed on Railway: https://safe-course-server-production.up.railway.app

Endpoints:
  POST /create-video          - Submit a single script to HeyGen
  GET  /video-status/<id>     - Poll status of a single video
  POST /batch-create-videos   - Submit multiple scripts at once (NEW)
  POST /batch-video-status    - Poll status of multiple videos at once (NEW)
  GET  /get-avatars           - List available HeyGen avatars
"""

import os
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

HEYGEN_API_KEY = os.environ.get("HEYGEN_API_KEY")
HEYGEN_BASE = "https://api.heygen.com"

DEFAULT_AVATAR = "26f5fc9be1fc47eab0ef65df30d47a4e"
DEFAULT_VOICE = "cf36ed19c8ce4dc9b25ee37d0a68bb1b"
MAX_SCRIPT_CHARS = 5000
BACKGROUND_COLOR = "#08060f"
VIDEO_WIDTH = 1920
VIDEO_HEIGHT = 1080


def _build_heygen_payload(script, avatar_id, voice_id):
    """Build the v2/video/generate request body for HeyGen."""
    return {
        "video_inputs": [
            {
                "character": {
                    "type": "avatar",
                    "avatar_id": avatar_id or DEFAULT_AVATAR,
                    "avatar_style": "normal",
                },
                "voice": {
                    "type": "text",
                    "input_text": script[:MAX_SCRIPT_CHARS],
                    "voice_id": voice_id or DEFAULT_VOICE,
                },
                "background": {
                    "type": "color",
                    "value": BACKGROUND_COLOR,
                },
            }
        ],
        "dimension": {"width": VIDEO_WIDTH, "height": VIDEO_HEIGHT},
    }


def _create_single_video(script, avatar_id, voice_id):
    """
    Submit one video to HeyGen. Returns dict with video_id on success,
    or error info on failure. Used by both single and batch endpoints.
    """
    if not script or not script.strip():
        return {"success": False, "error": "Empty script"}

    try:
        response = requests.post(
            f"{HEYGEN_BASE}/v2/video/generate",
            headers={
                "X-Api-Key": HEYGEN_API_KEY,
                "Content-Type": "application/json",
            },
            json=_build_heygen_payload(script, avatar_id, voice_id),
            timeout=30,
        )
        data = response.json()

        if response.status_code != 200:
            return {
                "success": False,
                "error": data.get("message", f"HTTP {response.status_code}"),
                "details": data,
            }

        video_id = data.get("data", {}).get("video_id")
        if not video_id:
            return {"success": False, "error": "No video_id in response", "details": data}

        return {"success": True, "video_id": video_id}

    except requests.exceptions.Timeout:
        return {"success": False, "error": "HeyGen API timeout"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _get_single_status(video_id):
    """Fetch status for one video. Returns normalized status dict."""
    try:
        response = requests.get(
            f"{HEYGEN_BASE}/v1/video_status.get",
            headers={"X-Api-Key": HEYGEN_API_KEY},
            params={"video_id": video_id},
            timeout=15,
        )
        data = response.json()

        if response.status_code != 200:
            return {
                "video_id": video_id,
                "status": "error",
                "error": data.get("message", f"HTTP {response.status_code}"),
            }

        video_data = data.get("data", {})
        return {
            "video_id": video_id,
            "status": video_data.get("status", "unknown"),
            "video_url": video_data.get("video_url"),
            "thumbnail_url": video_data.get("thumbnail_url"),
            "duration": video_data.get("duration"),
            "error": video_data.get("error"),
        }

    except requests.exceptions.Timeout:
        return {"video_id": video_id, "status": "error", "error": "Status check timeout"}
    except Exception as e:
        return {"video_id": video_id, "status": "error", "error": str(e)}


# ============================================================
# EXISTING ENDPOINTS (kept as-is for backwards compatibility)
# ============================================================

@app.route("/create-video", methods=["POST"])
def create_video():
    """Submit a single script to HeyGen."""
    body = request.get_json() or {}
    result = _create_single_video(
        script=body.get("script", ""),
        avatar_id=body.get("avatar_id"),
        voice_id=body.get("voice_id"),
    )

    if result["success"]:
        return jsonify({"video_id": result["video_id"]}), 200
    return jsonify({"error": result["error"], "details": result.get("details")}), 500


@app.route("/video-status/<video_id>", methods=["GET"])
def video_status(video_id):
    """Poll status of a single video."""
    result = _get_single_status(video_id)
    return jsonify(result), 200


@app.route("/get-avatars", methods=["GET"])
def get_avatars():
    """List available HeyGen avatars."""
    try:
        response = requests.get(
            f"{HEYGEN_BASE}/v2/avatars",
            headers={"X-Api-Key": HEYGEN_API_KEY},
            timeout=15,
        )
        return jsonify(response.json()), response.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ============================================================
# NEW BATCH ENDPOINTS
# ============================================================

@app.route("/batch-create-videos", methods=["POST"])
def batch_create_videos():
    """
    Submit multiple videos in parallel.

    Request body:
      {
        "videos": [
          {"script": "...", "avatar_id": "...", "voice_id": "...", "index": 0},
          {"script": "...", "avatar_id": "...", "voice_id": "...", "index": 1},
          ...
        ]
      }

    Response:
      {
        "results": [
          {"index": 0, "success": true, "video_id": "..."},
          {"index": 1, "success": false, "error": "..."},
          ...
        ]
      }

    One failure does NOT fail the whole batch — frontend can retry individual
    items. This protects the user from losing all their HeyGen credits if
    one script has an issue.
    """
    body = request.get_json() or {}
    videos = body.get("videos", [])

    if not videos:
        return jsonify({"error": "No videos provided"}), 400

    if len(videos) > 50:
        return jsonify({"error": "Max 50 videos per batch"}), 400

    results = [None] * len(videos)

    # Fire all HeyGen submissions in parallel. HeyGen rate limits are
    # generous enough for 10-20 concurrent submissions.
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_index = {}
        for item in videos:
            idx = item.get("index", videos.index(item))
            future = executor.submit(
                _create_single_video,
                item.get("script", ""),
                item.get("avatar_id"),
                item.get("voice_id"),
            )
            future_to_index[future] = idx

        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            result = future.result()
            result["index"] = idx
            results[idx] = result

    return jsonify({"results": results}), 200


@app.route("/batch-video-status", methods=["POST"])
def batch_video_status():
    """
    Check status of multiple videos in one request.

    Request body:
      {"video_ids": ["id1", "id2", "id3", ...]}

    Response:
      {
        "statuses": [
          {"video_id": "id1", "status": "completed", "video_url": "..."},
          {"video_id": "id2", "status": "processing"},
          ...
        ]
      }

    Runs all status checks in parallel so polling 10 videos takes ~1 request
    time instead of 10x that.
    """
    body = request.get_json() or {}
    video_ids = body.get("video_ids", [])

    if not video_ids:
        return jsonify({"error": "No video_ids provided"}), 400

    if len(video_ids) > 50:
        return jsonify({"error": "Max 50 video_ids per request"}), 400

    statuses = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_id = {
            executor.submit(_get_single_status, vid): vid
            for vid in video_ids
        }
        for future in as_completed(future_to_id):
            statuses.append(future.result())

    # Sort statuses to match input order for predictable frontend handling
    id_order = {vid: i for i, vid in enumerate(video_ids)}
    statuses.sort(key=lambda s: id_order.get(s["video_id"], 999))

    return jsonify({"statuses": statuses}), 200


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/", methods=["GET"])
def health():
    """Railway health check."""
    return jsonify({
        "status": "ok",
        "service": "SAFE Course Generator - HeyGen Proxy",
        "has_api_key": bool(HEYGEN_API_KEY),
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
