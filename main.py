"""
SAFE Course Generator - HeyGen Proxy Backend
Deployed on Railway: https://safe-course-server-production.up.railway.app

Endpoints:
  POST /create-video          - Submit a single script to HeyGen
  GET  /video-status/<id>     - Poll status of a single video
  POST /batch-create-videos   - Submit multiple scripts at once
  POST /batch-video-status    - Poll status of multiple videos at once
  GET  /get-avatars           - List available HeyGen avatars
  POST /compose-video         - Compose branded MP4 with title + bullets + avatar overlay
  GET  /compose-status/<job>  - Check composition status and download result
"""

import os
import uuid
import tempfile
import subprocess
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from PIL import Image, ImageDraw, ImageFont
import io

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

    results = []

    # Fire all HeyGen submissions in parallel. HeyGen rate limits are
    # generous enough for 10-20 concurrent submissions.
    with ThreadPoolExecutor(max_workers=10) as executor:
        future_to_index = {}
        for item in videos:
            # Preserve the caller's index so the frontend can match results
            # back to module positions — even when those indexes are sparse
            # (e.g. only avatar modules out of a mixed list).
            idx = item.get("index", 0)
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
            results.append(result)

    # Sort by original index so the frontend receives a predictable order.
    results.sort(key=lambda r: r.get("index", 0))

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


# ============================================================
# VIDEO COMPOSITION
# Composes a branded MP4: dark background + title + bullets (left)
# overlaid with the avatar video (right), matching the SCORM
# player's visual layout.
#
# Uses ffmpeg for video processing and Pillow for generating
# the background image. No headless browser required - keeps it
# lightweight enough to run on Railway Hobby plan.
# ============================================================

# Canvas dimensions match the SCORM player layout
CANVAS_W = 1920
CANVAS_H = 1080
BG_COLOR_RGB = (8, 6, 15)  # #08060f
TEXT_COLOR = (255, 255, 255)
BULLET_COLOR = (176, 126, 248)  # #b07ef8
MUTED_COLOR = (209, 209, 217)

# Avatar video position (left side of canvas) - 3:4 aspect ratio
AVATAR_W = 700
AVATAR_H = 933  # 3:4 aspect ratio
AVATAR_X = 120
AVATAR_Y = (CANVAS_H - AVATAR_H) // 2

# Text area (right side of canvas)
TEXT_X = 900
TEXT_Y_START = 300
TEXT_MAX_WIDTH = CANVAS_W - TEXT_X - 120

# In-memory job store (simple dict, since we don't need persistence
# across restarts for single-user use)
_compose_jobs = {}
_compose_jobs_lock = threading.Lock()


def _get_font(size, bold=False):
    """Load a font file with fallbacks. Returns PIL ImageFont."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(text, font, max_width):
    """Wrap text to fit within max_width. Returns list of line strings."""
    words = text.split()
    lines = []
    current = []
    for word in words:
        test_line = " ".join(current + [word])
        bbox = font.getbbox(test_line)
        width = bbox[2] - bbox[0]
        if width <= max_width:
            current.append(word)
        else:
            if current:
                lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _generate_background(title, bullets_text):
    """
    Build the dark-purple background image with title and bullets.
    Returns a PIL Image at CANVAS_W x CANVAS_H.
    """
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), BG_COLOR_RGB)
    draw = ImageDraw.Draw(img)

    # Optional: subtle purple glow behind text area
    # (Drawing a radial gradient is expensive; skip for now - solid bg is fine)

    # Draw the title
    title_font = _get_font(56, bold=True)
    title_lines = _wrap_text(title or "", title_font, TEXT_MAX_WIDTH)
    y = TEXT_Y_START
    for line in title_lines:
        draw.text((TEXT_X, y), line, font=title_font, fill=TEXT_COLOR)
        y += 72
    y += 30  # space after title

    # Parse bullets (split by line, strip common bullet prefixes)
    bullet_lines = []
    for raw in (bullets_text or "").split("\n"):
        stripped = raw.strip()
        if not stripped:
            continue
        # Remove common prefix chars like -, •, *, ✓, etc.
        cleaned = stripped.lstrip("-•*✓✔→▸· \t")
        bullet_lines.append(cleaned)

    # Draw bullets
    bullet_font = _get_font(30)
    for bullet in bullet_lines:
        wrapped = _wrap_text(bullet, bullet_font, TEXT_MAX_WIDTH - 60)
        # Gradient bullet marker (simplified: solid colored rectangle)
        draw.rectangle([TEXT_X, y + 18, TEXT_X + 30, y + 22], fill=BULLET_COLOR)

        # Bullet text
        for i, line in enumerate(wrapped):
            draw.text((TEXT_X + 50, y), line, font=bullet_font, fill=MUTED_COLOR)
            y += 44
        y += 22  # spacing between bullets
        # Thin separator line
        draw.line([(TEXT_X, y), (CANVAS_W - 120, y)], fill=(255, 255, 255, 20), width=1)
        y += 14

    return img


def _compose_worker(job_id, avatar_video_url, title, bullets_text, round_corners=True, upload_temp_dir=None):
    """
    Background worker. avatar_video_url can be:
      - http(s):// URL to download
      - file:// path to a pre-uploaded local file
    """
    temp_dir = tempfile.mkdtemp(prefix="compose_")
    try:
        # 1. Get the avatar video — either from URL or already-uploaded file
        if avatar_video_url.startswith("file://"):
            # File was uploaded directly; just use the path
            _update_job(job_id, status="reading_avatar")
            avatar_path = avatar_video_url.replace("file://", "")
            if not os.path.exists(avatar_path):
                raise Exception(f"Uploaded file missing: {avatar_path}")
        else:
            # Download from URL
            _update_job(job_id, status="downloading_avatar")
            avatar_path = os.path.join(temp_dir, "avatar.mp4")
            with requests.get(avatar_video_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(avatar_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

        # 2. Generate the background image with title + bullets
        _update_job(job_id, status="generating_background")
        bg_img = _generate_background(title, bullets_text)
        bg_path = os.path.join(temp_dir, "background.png")
        bg_img.save(bg_path, "PNG")

        # 3. Use ffmpeg to compose: bg as base layer, avatar scaled + placed on top
        _update_job(job_id, status="composing_video")
        output_path = os.path.join(temp_dir, "output.mp4")

        # Filter chain:
        # - Scale avatar video to AVATAR_W x AVATAR_H (preserving aspect)
        # - Zoom slightly and crop to hide chair/legs (matches our CSS `scale(1.25)`)
        # - Overlay on background at AVATAR_X, AVATAR_Y
        # Crop the avatar so head/shoulders fill the slot.
        # HeyGen renders at 1920x1080. Avatars are typically positioned
        # right-of-center horizontally and upper-third vertically (face).
        # We crop tightly around that focal point.
        #
        # Approach: scale the source to be much larger than the slot,
        # then crop a window centered on the face area.
        SOURCE_W = 1920
        SOURCE_H = 1080
        # Target focal point in source coordinates (face position)
        FOCAL_X_PCT = 0.62   # 62% from left (Caroline-style framing)
        FOCAL_Y_PCT = 0.38   # 38% from top (face area)
        # How much of the source to capture (smaller = tighter zoom)
        CROP_W_PCT = 0.30    # capture 30% of source width
        CROP_H_PCT = 0.65    # capture 65% of source height (head + upper body)

        # Crop window in source coordinates
        crop_w_src = int(SOURCE_W * CROP_W_PCT)
        crop_h_src = int(SOURCE_H * CROP_H_PCT)
        crop_x_src = max(0, int(SOURCE_W * FOCAL_X_PCT) - crop_w_src // 2)
        crop_y_src = max(0, int(SOURCE_H * FOCAL_Y_PCT) - crop_h_src // 2)

        # Make sure crop stays inside the frame
        if crop_x_src + crop_w_src > SOURCE_W:
            crop_x_src = SOURCE_W - crop_w_src
        if crop_y_src + crop_h_src > SOURCE_H:
            crop_y_src = SOURCE_H - crop_h_src

        filter_complex = (
            f"[1:v]crop={crop_w_src}:{crop_h_src}:{crop_x_src}:{crop_y_src},"
            f"scale={AVATAR_W}:{AVATAR_H}:flags=lanczos[avatar];"
            f"[0:v][avatar]overlay={AVATAR_X}:{AVATAR_Y}:shortest=1[out]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", bg_path,  # input 0: background image (looped)
            "-i", avatar_path,             # input 1: avatar video
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "1:a?",               # audio from avatar (optional)
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise Exception(f"ffmpeg failed: {result.stderr[-800:]}")

        # 4. Read the final MP4 into memory and clean up
        _update_job(job_id, status="finalizing")
        with open(output_path, "rb") as f:
            final_bytes = f.read()

        _update_job(job_id, status="completed", result_bytes=final_bytes)

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        _update_job(job_id, status="failed", error=f"{str(e)[:300]} | {tb[-600:]}")
    finally:
        # Cleanup temp dirs (compose work + upload, if separate)
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            if upload_temp_dir and upload_temp_dir != temp_dir:
                shutil.rmtree(upload_temp_dir, ignore_errors=True)
        except Exception:
            pass


def _update_job(job_id, **kwargs):
    with _compose_jobs_lock:
        if job_id not in _compose_jobs:
            _compose_jobs[job_id] = {}
        _compose_jobs[job_id].update(kwargs)


@app.route("/compose-video", methods=["POST"])
def compose_video():
    """
    Kick off a composition job. Two ways to provide the avatar video:

    OPTION A: Multipart file upload (preferred — bypasses HeyGen URL expiration)
      POST with form fields:
        - video: the MP4 file
        - title: module title
        - bullets: newline-separated bullets

    OPTION B: JSON with URL (legacy — works if URL hasn't expired)
      POST with JSON body:
        {"video_url": "...", "title": "...", "bullets": "..."}

    Returns: {"job_id": "...", "status": "queued"}
    """
    # Detect mode by content type
    if request.files and 'video' in request.files:
        # File upload mode
        video_file = request.files['video']
        title = request.form.get("title", "")
        bullets = request.form.get("bullets", "")

        # Save uploaded video to a temp file the worker can read
        temp_dir = tempfile.mkdtemp(prefix="upload_")
        upload_path = os.path.join(temp_dir, "avatar.mp4")
        video_file.save(upload_path)

        job_id = str(uuid.uuid4())
        _update_job(job_id, status="queued")

        # Pass local file path to worker (with file:// scheme so worker knows to skip download)
        thread = threading.Thread(
            target=_compose_worker,
            args=(job_id, "file://" + upload_path, title, bullets),
            kwargs={"upload_temp_dir": temp_dir},
            daemon=True,
        )
        thread.start()

        return jsonify({"job_id": job_id, "status": "queued"}), 200

    # JSON URL mode (legacy)
    body = request.get_json() or {}
    video_url = body.get("video_url")
    title = body.get("title", "")
    bullets = body.get("bullets", "")

    if not video_url:
        return jsonify({"error": "video_url required (or upload 'video' as multipart)"}), 400

    job_id = str(uuid.uuid4())
    _update_job(job_id, status="queued")

    thread = threading.Thread(
        target=_compose_worker,
        args=(job_id, video_url, title, bullets),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "queued"}), 200


@app.route("/compose-status/<job_id>", methods=["GET"])
def compose_status(job_id):
    """
    Poll a composition job's status. If completed, returns a download URL.
    If caller passes ?download=1 and job is complete, streams the MP4 directly.
    """
    with _compose_jobs_lock:
        job = _compose_jobs.get(job_id)

    if not job:
        return jsonify({"error": "job not found"}), 404

    status = job.get("status", "unknown")

    if request.args.get("download") == "1" and status == "completed":
        result_bytes = job.get("result_bytes")
        if not result_bytes:
            return jsonify({"error": "result not available"}), 500
        return send_file(
            io.BytesIO(result_bytes),
            mimetype="video/mp4",
            as_attachment=True,
            download_name=f"composed-{job_id[:8]}.mp4",
        )

    response = {"job_id": job_id, "status": status}
    if status == "failed":
        response["error"] = job.get("error", "unknown error")
    elif status == "completed":
        response["download_url"] = f"/compose-status/{job_id}?download=1"
        response["size_bytes"] = len(job.get("result_bytes", b""))

    return jsonify(response), 200


@app.route("/compose-cleanup/<job_id>", methods=["POST"])
def compose_cleanup(job_id):
    """Client calls this after downloading to free server memory."""
    with _compose_jobs_lock:
        if job_id in _compose_jobs:
            del _compose_jobs[job_id]
    return jsonify({"ok": True}), 200


@app.route("/compose-debug", methods=["POST"])
def compose_debug():
    """
    Diagnostic: run a compose job SYNCHRONOUSLY and return:
    - ffmpeg stdout/stderr
    - output dimensions (probed with ffprobe)
    - background PNG dimensions
    - avatar video dimensions
    Use this to figure out what's actually happening during composition.
    """
    body = request.get_json() or {}
    video_url = body.get("video_url")
    title = body.get("title", "Test Title")
    bullets = body.get("bullets", "Bullet one\nBullet two")

    if not video_url:
        return jsonify({"error": "video_url is required"}), 400

    temp_dir = tempfile.mkdtemp(prefix="debug_")
    diag = {"steps": []}

    try:
        # Download avatar
        avatar_path = os.path.join(temp_dir, "avatar.mp4")
        with requests.get(video_url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(avatar_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        diag["avatar_size_bytes"] = os.path.getsize(avatar_path)
        diag["steps"].append("avatar downloaded")

        # Probe avatar
        probe_avatar = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,pix_fmt,codec_name",
             "-of", "default=noprint_wrappers=1", avatar_path],
            capture_output=True, text=True, timeout=30
        )
        diag["avatar_probe"] = probe_avatar.stdout
        diag["steps"].append("avatar probed")

        # Generate background
        bg_img = _generate_background(title, bullets)
        bg_path = os.path.join(temp_dir, "background.png")
        bg_img.save(bg_path, "PNG")
        diag["bg_size_bytes"] = os.path.getsize(bg_path)
        diag["bg_dimensions"] = f"{bg_img.width}x{bg_img.height}"
        diag["steps"].append("background generated")

        # Compose
        output_path = os.path.join(temp_dir, "output.mp4")
        SOURCE_W = 1920
        SOURCE_H = 1080
        FOCAL_X_PCT = 0.62
        FOCAL_Y_PCT = 0.38
        CROP_W_PCT = 0.30
        CROP_H_PCT = 0.65

        crop_w_src = int(SOURCE_W * CROP_W_PCT)
        crop_h_src = int(SOURCE_H * CROP_H_PCT)
        crop_x_src = max(0, int(SOURCE_W * FOCAL_X_PCT) - crop_w_src // 2)
        crop_y_src = max(0, int(SOURCE_H * FOCAL_Y_PCT) - crop_h_src // 2)
        if crop_x_src + crop_w_src > SOURCE_W:
            crop_x_src = SOURCE_W - crop_w_src
        if crop_y_src + crop_h_src > SOURCE_H:
            crop_y_src = SOURCE_H - crop_h_src

        filter_complex = (
            f"[1:v]crop={crop_w_src}:{crop_h_src}:{crop_x_src}:{crop_y_src},"
            f"scale={AVATAR_W}:{AVATAR_H}:flags=lanczos[avatar];"
            f"[0:v][avatar]overlay={AVATAR_X}:{AVATAR_Y}:shortest=1[out]"
        )
        diag["filter_complex"] = filter_complex

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", bg_path,
            "-i", avatar_path,
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-map", "1:a?",
            "-c:v", "libx264",
            "-preset", "ultrafast",  # faster for debug
            "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
            output_path,
        ]
        diag["ffmpeg_cmd"] = " ".join(cmd)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        diag["ffmpeg_returncode"] = result.returncode
        diag["ffmpeg_stderr_tail"] = result.stderr[-3000:] if result.stderr else ""
        diag["steps"].append("ffmpeg ran")

        # Probe output
        if os.path.exists(output_path):
            diag["output_size_bytes"] = os.path.getsize(output_path)
            probe_out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,pix_fmt,codec_name",
                 "-of", "default=noprint_wrappers=1", output_path],
                capture_output=True, text=True, timeout=30
            )
            diag["output_probe"] = probe_out.stdout
        else:
            diag["output_exists"] = False

        return jsonify(diag), 200

    except Exception as e:
        import traceback
        diag["exception"] = str(e)
        diag["traceback"] = traceback.format_exc()[-2000:]
        return jsonify(diag), 500
    finally:
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
