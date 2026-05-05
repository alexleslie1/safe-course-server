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
import cv2
import numpy as np

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


def _detect_face_focal(video_path):
    """
    Open the video, sample a few frames, and find a face.
    Returns (focal_x_pct, focal_y_pct) in 0-1 source coordinates,
    or None if no face is found in any sampled frame.

    We sample multiple frames because the very first frame might be
    a fade-in or have eyes closed, which can throw off detection.
    """
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        if width == 0 or height == 0:
            cap.release()
            return None

        # Sample frames at 1s, 2s, 3s, 4s, 5s — skip very first frame
        sample_seconds = [1, 2, 3, 4, 5]
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )

        face_centers = []  # list of (x_pct, y_pct, area_pct) tuples

        for sec in sample_seconds:
            frame_idx = int(sec * fps)
            if frame_idx >= total_frames:
                continue
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = face_cascade.detectMultiScale(
                gray,
                scaleFactor=1.1,
                minNeighbors=5,
                minSize=(80, 80),  # ignore tiny detections
            )
            if len(faces) == 0:
                continue

            # Pick the largest face if multiple detected (closest to camera)
            largest = max(faces, key=lambda f: f[2] * f[3])
            x, y, w, h = largest
            cx = x + w / 2
            cy = y + h / 2
            area_pct = (w * h) / (width * height)
            face_centers.append((cx / width, cy / height, area_pct))

        cap.release()

        if not face_centers:
            return None

        # Average the centers across sampled frames for stability
        avg_x = sum(c[0] for c in face_centers) / len(face_centers)
        avg_y = sum(c[1] for c in face_centers) / len(face_centers)

        return (avg_x, avg_y)

    except Exception as e:
        # Face detection is optional — never let it crash the whole compose
        print(f"Face detection failed: {e}")
        return None


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


def _compose_worker(job_id, avatar_video_url, title, bullets_text, round_corners=True, upload_temp_dir=None, timed_bullets=None, scenes=None):
    """
    Background worker. avatar_video_url can be:
      - http(s):// URL to download
      - file:// path to a pre-uploaded local file

    scenes: optional list of scene dicts. Each scene has:
      - type: "bullets" or "image"
      - startAt: float seconds when this scene becomes active
      - title: optional string (per-scene title override)
      - bullets: list of strings (if type=bullets)
      - imagePath: local file path (if type=image)

    timed_bullets: legacy list of {text, appearAt} objects (used if scenes not provided)
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
        # If timed bullets are provided, generate background with title only -
        # individual bullets will be drawn progressively via ffmpeg drawtext below
        bg_bullets = "" if (timed_bullets and len(timed_bullets) > 0) else bullets_text
        bg_img = _generate_background(title, bg_bullets)
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
        # Use face detection to find the actual face position in this video,
        # so we crop tightly around it regardless of which avatar was rendered.

        # First: detect the actual source video dimensions
        cap_check = cv2.VideoCapture(avatar_path)
        SOURCE_W = int(cap_check.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
        SOURCE_H = int(cap_check.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
        cap_check.release()
        print(f"[compose {job_id[:8]}] Source video: {SOURCE_W}x{SOURCE_H}", flush=True)

        _update_job(job_id, status="detecting_face")
        detected = _detect_face_focal(avatar_path)
        if detected is not None:
            face_x_pct, face_y_pct = detected
            print(f"[compose {job_id[:8]}] Face detected at ({face_x_pct:.3f}, {face_y_pct:.3f})", flush=True)
            # Position so face is in upper third of cropped slot (room for shoulders below)
            FOCAL_X_PCT = face_x_pct
            FOCAL_Y_PCT = face_y_pct + 0.15  # shift focal point down so face appears upper-third
        else:
            print(f"[compose {job_id[:8]}] No face detected, using fallback", flush=True)
            FOCAL_X_PCT = 0.50
            FOCAL_Y_PCT = 0.40

        # How much of the source to capture (smaller = tighter zoom)
        # We want the slot's aspect ratio (700:933 = 0.75) to match the crop's aspect ratio
        # so we don't squish/stretch the avatar
        SLOT_ASPECT = AVATAR_W / AVATAR_H  # 0.75 (portrait)

        # Pick crop height as % of source, then derive width to maintain slot aspect
        CROP_H_PCT = 0.85  # capture 85% of source height (most of body)
        crop_h_src = int(SOURCE_H * CROP_H_PCT)
        crop_w_src = int(crop_h_src * SLOT_ASPECT)

        # If crop_w > source width, shrink everything proportionally
        if crop_w_src > SOURCE_W:
            crop_w_src = SOURCE_W
            crop_h_src = int(crop_w_src / SLOT_ASPECT)

        # Position crop window so face is at FOCAL_X_PCT, FOCAL_Y_PCT of source
        crop_x_src = int(SOURCE_W * FOCAL_X_PCT) - crop_w_src // 2
        crop_y_src = int(SOURCE_H * FOCAL_Y_PCT) - crop_h_src // 2

        # Clamp inside frame bounds
        crop_x_src = max(0, min(crop_x_src, SOURCE_W - crop_w_src))
        crop_y_src = max(0, min(crop_y_src, SOURCE_H - crop_h_src))

        print(f"[compose {job_id[:8]}] Crop: {crop_w_src}x{crop_h_src} at ({crop_x_src}, {crop_y_src})", flush=True)

        # ============================================================
        # Build scene rendering filters
        # Two paths:
        #   A) scenes provided (modern): render each scene as a time-bounded
        #      block (title + bullets or image). Earlier filter pipeline
        #      drew everything onto bg image; now we draw dynamically.
        #   B) timed_bullets only (legacy): draw bullets with gte(t,X) cues
        # ============================================================

        # Find a usable bold and regular font
        bold_font = None
        regular_font = None
        for cand in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]:
            if os.path.exists(cand):
                bold_font = cand
                break
        for cand in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]:
            if os.path.exists(cand):
                regular_font = cand
                break
        if not bold_font:
            bold_font = regular_font
        if not regular_font:
            regular_font = bold_font

        # Helper to write text to a temp file and return path.
        # Using textfile= bypasses all the escaping pain that text= has
        # (apostrophes, colons, special chars all become non-issues).
        _text_counter = [0]
        def text_to_file(s):
            _text_counter[0] += 1
            path = os.path.join(temp_dir, f"text_{_text_counter[0]}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(s)
            return path

        # Layout constants matching the SCORM player look
        TITLE_X = 900
        TITLE_Y = 300
        TITLE_FONT_SIZE = 56
        BULLETS_Y_START = 420
        BULLETS_LINE_HEIGHT = 80
        BULLET_X = 900
        BULLET_FONT_SIZE = 30
        IMAGE_X = 900
        IMAGE_Y = 250
        IMAGE_MAX_W = 880
        IMAGE_MAX_H = 600

        scene_filter_parts = []
        extra_inputs = []   # list of (path, ffmpeg_input_index)
        scene_specs = []   # populated only in scenes path; used by image overlay logic

        if scenes and len(scenes) > 0 and bold_font:
            # Build scenes with start/end times. Last scene runs to infinity.
            for i, scene in enumerate(scenes):
                start = max(0, float(scene.get("startAt") or 0))
                end = float(scenes[i + 1]["startAt"]) if i + 1 < len(scenes) else 1e9
                scene_specs.append({
                    "scene": scene,
                    "start": start,
                    "end": end,
                })

            # Suppress the static bg title since each scene draws its own
            # (we'll regenerate background without title)
            for spec in scene_specs:
                scene = spec["scene"]
                start = spec["start"]
                end = spec["end"]
                t_filter = f"between(t,{start:.2f},{end:.2f})"

                # Title (per-scene)
                scene_title = (scene.get("title") or "").strip()
                if scene_title:
                    title_path = text_to_file(scene_title)
                    scene_filter_parts.append(
                        f"drawtext=fontfile='{bold_font}':textfile='{title_path}':"
                        f"x={TITLE_X}:y={TITLE_Y}:fontsize={TITLE_FONT_SIZE}:fontcolor=white:"
                        f"enable='{t_filter}'"
                    )

                stype = scene.get("type") or "bullets"
                if stype == "bullets":
                    bullets_list = scene.get("bullets") or []
                    for j, b in enumerate(bullets_list):
                        # Bullets can be strings (legacy) or objects {text, appearAt}
                        if isinstance(b, str):
                            btext = b.strip()
                            bullet_appear_at = start  # legacy: appears at scene start
                        elif isinstance(b, dict):
                            btext = (b.get("text") or "").strip()
                            bullet_appear_at = max(start, float(b.get("appearAt") or start))
                        else:
                            continue

                        if not btext:
                            continue

                        # Each bullet stays visible from its appearAt until scene end
                        bullet_filter = f"between(t,{bullet_appear_at:.2f},{end:.2f})"
                        y_pos = BULLETS_Y_START + j * BULLETS_LINE_HEIGHT
                        bullet_path = text_to_file(btext)

                        scene_filter_parts.append(
                            f"drawbox=x={BULLET_X}:y={y_pos + 18}:w=20:h=4:color=0xb07ef8@1.0:t=fill:"
                            f"enable='{bullet_filter}'"
                        )
                        scene_filter_parts.append(
                            f"drawtext=fontfile='{regular_font}':textfile='{bullet_path}':"
                            f"x={BULLET_X + 40}:y={y_pos}:fontsize={BULLET_FONT_SIZE}:fontcolor=0xd1d1d9:"
                            f"enable='{bullet_filter}'"
                        )
                elif stype == "image" and scene.get("imagePath"):
                    img_path = scene["imagePath"]
                    if os.path.exists(img_path):
                        # Each image becomes an extra ffmpeg input (index 2+)
                        input_idx = len(extra_inputs) + 2  # 0=bg, 1=avatar, 2+=images
                        extra_inputs.append((img_path, input_idx))
                        # We'll add a complex filter post-loop that scales + overlays this image
                        spec["image_input_idx"] = input_idx

            # Regenerate bg WITHOUT title since per-scene titles handle it now
            bg_img2 = _generate_background("", "")
            bg_img2.save(bg_path, "PNG")

            # Build image overlay filters (must be done in the [base][img]overlay format
            # rather than as a single drawtext-style chain)
        elif timed_bullets and bold_font:
            # Legacy timed bullets path
            for i, b in enumerate(timed_bullets):
                text = (b.get("text") or "").strip()
                if not text:
                    continue
                appear_at = max(0, float(b.get("appearAt") or 0))
                y_pos = BULLETS_Y_START + i * BULLETS_LINE_HEIGHT
                legacy_path = text_to_file(text)
                scene_filter_parts.append(
                    f"drawbox=x={BULLET_X}:y={y_pos + 18}:w=20:h=4:color=0xb07ef8@1.0:t=fill:"
                    f"enable='gte(t,{appear_at})'"
                )
                scene_filter_parts.append(
                    f"drawtext=fontfile='{regular_font}':textfile='{legacy_path}':"
                    f"x={BULLET_X + 40}:y={y_pos}:fontsize={BULLET_FONT_SIZE}:fontcolor=0xd1d1d9:"
                    f"enable='gte(t,{appear_at})'"
                )

        # Compose the filter graph
        # Start: bg (input 0) + avatar (input 1) -> overlay -> [base]
        # Then: apply text/box filters from scene_filter_parts to [base]
        # Then: overlay each image input
        text_chain = ("," + ",".join(scene_filter_parts)) if scene_filter_parts else ""

        if extra_inputs:
            # Build filter graph step by step with named labels
            # [1:v] -> crop+scale -> [avatar]
            # [0:v][avatar] -> overlay + text chain -> [base0]
            # then for each image: [base_n][img:v] -> scale + overlay -> [base_{n+1}]
            graph_parts = [
                f"[1:v]crop={crop_w_src}:{crop_h_src}:{crop_x_src}:{crop_y_src},"
                f"scale={AVATAR_W}:{AVATAR_H}:flags=lanczos[avatar]"
            ]
            graph_parts.append(
                f"[0:v][avatar]overlay={AVATAR_X}:{AVATAR_Y}:shortest=1{text_chain}[base0]"
            )

            # Add overlay for each image
            current_label = "base0"
            for spec in scene_specs:
                if "image_input_idx" not in spec:
                    continue
                input_idx = spec["image_input_idx"]
                start = spec["start"]
                end = spec["end"]
                next_label = f"base_img{input_idx}"
                # Scale image to fit IMAGE_MAX_W x IMAGE_MAX_H preserving aspect
                graph_parts.append(
                    f"[{input_idx}:v]scale={IMAGE_MAX_W}:{IMAGE_MAX_H}:force_original_aspect_ratio=decrease[img{input_idx}]"
                )
                graph_parts.append(
                    f"[{current_label}][img{input_idx}]overlay={IMAGE_X}:{IMAGE_Y}:enable='between(t,{start:.2f},{end:.2f})'[{next_label}]"
                )
                current_label = next_label

            graph_parts.append(f"[{current_label}]null[out]")
            filter_complex = ";".join(graph_parts)
        else:
            # No image overlays — simple chain
            filter_complex = (
                f"[1:v]crop={crop_w_src}:{crop_h_src}:{crop_x_src}:{crop_y_src},"
                f"scale={AVATAR_W}:{AVATAR_H}:flags=lanczos[avatar];"
                f"[0:v][avatar]overlay={AVATAR_X}:{AVATAR_Y}:shortest=1{text_chain}[out]"
            )

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", bg_path,  # input 0: background image (looped)
            "-i", avatar_path,             # input 1: avatar video
        ]
        # Extra image inputs (for image scenes)
        for img_path, _idx in extra_inputs:
            cmd.extend(["-loop", "1", "-i", img_path])

        cmd.extend([
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
        ])

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

        # Parse scenes (modern format)
        scenes_json = request.form.get("scenes", "")
        scenes = []
        if scenes_json:
            try:
                import json
                scenes = json.loads(scenes_json)
            except Exception:
                scenes = []

        # Parse legacy timedBullets (only used if no scenes provided)
        timed_bullets_json = request.form.get("timedBullets", "")
        timed_bullets = []
        if timed_bullets_json and not scenes:
            try:
                import json
                timed_bullets = json.loads(timed_bullets_json)
            except Exception:
                timed_bullets = []

        # Save uploaded video and any scene images to temp dir
        temp_dir = tempfile.mkdtemp(prefix="upload_")
        upload_path = os.path.join(temp_dir, "avatar.mp4")
        video_file.save(upload_path)

        # Save image attachments referenced by scenes
        for scene in scenes:
            ref = scene.get("imageRef")
            if ref and ref in request.files:
                img_file = request.files[ref]
                ext = os.path.splitext(img_file.filename or "")[1] or ".png"
                img_path = os.path.join(temp_dir, f"{ref}{ext}")
                img_file.save(img_path)
                scene["imagePath"] = img_path  # local path for worker

        job_id = str(uuid.uuid4())
        _update_job(job_id, status="queued")

        thread = threading.Thread(
            target=_compose_worker,
            args=(job_id, "file://" + upload_path, title, bullets),
            kwargs={
                "upload_temp_dir": temp_dir,
                "timed_bullets": timed_bullets,
                "scenes": scenes,
            },
            daemon=True,
        )
        thread.start()

        return jsonify({"job_id": job_id, "status": "queued"}), 200

    # JSON URL mode (legacy — no scene support, just timedBullets)
    body = request.get_json() or {}
    video_url = body.get("video_url")
    title = body.get("title", "")
    bullets = body.get("bullets", "")
    timed_bullets = body.get("timedBullets", []) or []
    scenes = body.get("scenes", []) or []

    if not video_url:
        return jsonify({"error": "video_url required (or upload 'video' as multipart)"}), 400

    job_id = str(uuid.uuid4())
    _update_job(job_id, status="queued")

    thread = threading.Thread(
        target=_compose_worker,
        args=(job_id, video_url, title, bullets),
        kwargs={"timed_bullets": timed_bullets, "scenes": scenes},
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
