

import io
import json
import logging
import os
import random
import time
import uuid
from pathlib import Path
from flask import Response
from datetime import datetime, timezone
import base64
import qrcode
import qrcode.constants
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, flash, redirect, render_template, request, send_file, url_for,jsonify
from upstash_redis import Redis
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from sheets import SheetService


load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)



def _load_environment_file(base_dir: Path):
    dotenv_path = base_dir / ".env"
    if not dotenv_path.exists():
        return
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    log.info("Loaded .env from %s", dotenv_path)


BASE_DIR = Path(__file__).resolve().parent
_load_environment_file(BASE_DIR)

app = Flask(__name__)


#sheet service class instance
sheet_service = SheetService()

flask_env = os.environ.get("FLASK_ENV", "development")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
if not app.config["SECRET_KEY"]:
    if flask_env == "development":
        app.config["SECRET_KEY"] = "dev-only-insecure-key"
        log.warning("SECRET_KEY not set — using insecure dev key.")
    else:
        raise RuntimeError("SECRET_KEY environment variable is required.")

app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.config["TRANSFER_TTL_SECONDS"] = 900

configured_upload_folder = os.environ.get("UPLOAD_FOLDER")
if configured_upload_folder:
    upload_folder_path = Path(configured_upload_folder).expanduser()
    if not upload_folder_path.is_absolute():
        upload_folder_path = BASE_DIR / upload_folder_path
else:
    upload_folder_path = BASE_DIR / "uploads"

app.config["UPLOAD_FOLDER"] = upload_folder_path.resolve()
app.config["UPLOAD_FOLDER"].mkdir(parents=True, exist_ok=True)

ALLOWED = {
    ".png":  ["image/png"],
    ".jpg":  ["image/jpeg"],
    ".jpeg": ["image/jpeg"],
    ".pdf":  ["application/pdf"],
    ".doc":  ["application/msword"],
    ".docx": ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
    ".txt":  ["text/plain"],

}

_redis_url   = os.environ.get("UPSTASH_REDIS_REST_URL")
_redis_token = os.environ.get("UPSTASH_REDIS_REST_TOKEN")

if not _redis_url or not _redis_token:
    raise RuntimeError(
        "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN must be set."
    )

redis_client = Redis(url=_redis_url, token=_redis_token)
log.info("Redis client initialised.")


# ─── CLEANUP — filesystem-based, runs every 60s in background ────────────────

def _cleanup_disk_files():
    """
    Scan uploads/ folder and delete any file older than TRANSFER_TTL_SECONDS.

    """
    upload_folder = app.config["UPLOAD_FOLDER"]
    ttl = app.config["TRANSFER_TTL_SECONDS"]
    now = time.time()
    deleted_count = 0

    try:
        for file_path in upload_folder.iterdir():
            if not file_path.is_file():
                continue
            try:
                # st_mtime = last modification time = when file was written
                file_age_seconds = now - file_path.stat().st_mtime
                if file_age_seconds > ttl:
                    file_path.unlink(missing_ok=True)
                    deleted_count += 1
                    log.info(
                        "Cleanup: deleted %s (age %.0f seconds)",
                        file_path.name,
                        file_age_seconds,
                    )
            except OSError as e:
                log.error("Cleanup error for file %s: %s", file_path, e)

    except Exception as e:
        log.error("Cleanup scan failed: %s", e)

    if deleted_count:
        log.info("Cleanup cycle complete — deleted %d expired file(s)", deleted_count)


# ─── START SCHEDULER ─────────────────────────────────────────────────────────
# daemon=True means scheduler thread dies with the main process
# Prevent double-start when Flask debug reloader spawns a child process
_is_reloader_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
_is_debug = flask_env == "development"

if not _is_debug or _is_reloader_child:
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        _cleanup_disk_files,
        trigger="interval",
        seconds=60,
        id="disk_cleanup",
        max_instances=1,        # never run two cleanup cycles at once
        coalesce=True,          # if missed a run, just run once (not multiple)
    )
    scheduler.start()
    log.info("Background disk cleanup scheduler started — runs every 60 seconds.")







# ─── Helpers ─────────────────────────────────────────────────────────────────

def _validate_file(file_storage):
    filename = file_storage.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED:
        return f"File type '{ext or 'unknown'}' is not supported. Allowed: PNG, JPEG, PDF, Word, TXT."
    reported_mime = (file_storage.mimetype or "").split(";")[0].strip().lower()
    if reported_mime and reported_mime not in ALLOWED[ext]:
        return f"File content does not match its extension."
    return None


def _generate_unique_code():
    attempts = 0
    while attempts < 10:
        code = str(random.randint(1000000, 9999999))
        if not redis_client.get(code):
            return code
        attempts += 1
        log.warning("Code collision on attempt %d, retrying...", attempts)
    raise RuntimeError("Could not generate a unique code after 10 attempts.")


def _store_metadata(code, payload: dict, ttl: int):
    redis_client.set(code, json.dumps(payload), ex=ttl)
    log.info("Stored transfer code=%s type=%s ttl=%ds", code, payload.get("type"), ttl)


def _get_metadata(code) -> dict | None:
    raw = redis_client.get(code)
    if not raw:
        return None
    return json.loads(raw)


def _delete_transfer(code, metadata: dict):
    """Delete Redis key AND disk file."""
    redis_client.delete(code)
    if metadata.get("type") == "file" and metadata.get("file_path"):
        file_path = Path(metadata["file_path"])
        try:
            file_path.unlink(missing_ok=True)
            log.info("Deleted file from disk: %s", file_path)
        except OSError as e:
            log.error("Failed to delete file %s: %s", file_path, e)
    log.info("Deleted transfer code=%s", code)
def generate_qr_base64(url: str) -> str:
    """
    Generates a QR code PNG entirely in RAM.
    No file written to disk. No Redis. No Upstash tokens.
    Returns a base64 string (~1.3KB) that goes straight into
    the JSON response. JavaScript displays it as a plain <img> tag.
    Total CPU cost: ~2ms per call.
    """
    qr = qrcode.QRCode(
        version=None,                               # auto-size
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,                                 # pixels per module
        border=3,                                   # quiet zone
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(
        fill_color="#0A0D1A",   # FileDrop navy — the dark squares
        back_color="white",     # white — required for scanning
    )

    buf = io.BytesIO()          # in-memory buffer, nothing on disk
    img.save(buf, format="PNG")
    buf.seek(0)

    return base64.b64encode(buf.getvalue()).decode("utf-8")

def get_client_ip():
    if request.headers.get("X-Forwarded-For"):
        ip = request.headers["X-Forwarded-For"].split(",")[0].strip()

    elif request.headers.get("X-Real-IP"):
        ip = request.headers["X-Real-IP"]

    else:
        ip = request.remote_addr

    return ip

#ajax checking if code exists
def is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"

# ─── Routes ──────────────────────────────────────────────────────────────────
# NOTE: @app.before_request cleanup hook is REMOVED.
# Cleanup now runs on a 60-second background schedule instead.

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", created_transfer=None, retrieved_transfer=None)


@app.route("/send", methods=["POST"])
def send_transfer():
    mode = request.form.get("mode", "text")
    ttl = app.config["TRANSFER_TTL_SECONDS"]
    expires_at = int(time.time()) + ttl
 
    # ── FILE TRANSFER ──────────────────────────────────────────────────
    if mode == "file":
        uploaded_file = request.files.get("file")
 
        if not uploaded_file or not uploaded_file.filename:
            # NEW: AJAX gets JSON error, browser fallback gets flash
            if is_ajax():
                return jsonify({"ok": False, "error": "Select a file before creating a transfer."}), 400
            flash("Select a file before creating a transfer.", "error")
            return render_template("index.html", created_transfer=None, retrieved_transfer=None)
 
        err = _validate_file(uploaded_file)
        if err:
            # NEW: same pattern for validation error
            if is_ajax():
                return jsonify({"ok": False, "error": err}), 400
            flash(err, "error")
            return render_template("index.html", created_transfer=None, retrieved_transfer=None)
 
        raw_bytes = uploaded_file.read()
        file_size = len(raw_bytes)
 
        safe_original_name = secure_filename(uploaded_file.filename) or f"upload_{uuid.uuid4().hex[:8]}"
        ext = Path(safe_original_name).suffix.lower()
        disk_filename = f"{uuid.uuid4().hex}{ext}"
        file_path = app.config["UPLOAD_FOLDER"] / disk_filename
 
        with open(file_path, "wb") as f:
            f.write(raw_bytes)
        log.info("Saved file to disk: %s (%d bytes)", file_path, file_size)
 
        code = _generate_unique_code()
        metadata = {
            "code": code,
            "type": "file",
            "filename": safe_original_name,
            "file_path": str(file_path),
            "mimetype": uploaded_file.mimetype or "application/octet-stream",
            "size": file_size,
            "expires_at": expires_at,
            "downloaded": False,
        }
        _store_metadata(code, metadata, ttl)
 
        # NEW: AJAX gets JSON with the code, browser gets full page
        
        if is_ajax():
            # X-Forwarded-Proto is set by Railway proxy → gives correct https://
            proto  = request.headers.get("X-Forwarded-Proto", request.scheme)
            qr_url = f"{proto}://{request.host}/r/{code}"

            return jsonify({
                "ok":         True,
                "code":       code,
                "expires_at": expires_at,
                "type":       "file",
                "filename":   safe_original_name,
                "qr_base64":  generate_qr_base64(qr_url),  # ← new field
            })
        
 
        flash("Transfer created — share the code below.", "success")
        return render_template("index.html", created_transfer=metadata, retrieved_transfer=None)
 
    # ── TEXT TRANSFER ──────────────────────────────────────────────────
    content = request.form.get("text", "").strip()
 
    if not content:
        # NEW: AJAX error branch
        if is_ajax():
            return jsonify({"ok": False, "error": "Type or paste some text before creating a transfer."}), 400
        flash("Type or paste some text before creating a transfer.", "error")
        return render_template("index.html", created_transfer=None, retrieved_transfer=None)
 
    if len(content) > 5000:
        # NEW: AJAX error branch
        if is_ajax():
            return jsonify({"ok": False, "error": "Text is too long. Maximum is 5,000 characters."}), 400
        flash("Text is too long. Maximum is 5,000 characters.", "error")
        return render_template("index.html", created_transfer=None, retrieved_transfer=None)
 
    code = _generate_unique_code()
    metadata = {
        "code": code,
        "type": "text",
        "content": content,
        "expires_at": expires_at,
    }
    _store_metadata(code, metadata, ttl)
 
    # NEW: AJAX success branch for text
    # Add this block in the TEXT success branch
    if is_ajax():
        proto  = request.headers.get("X-Forwarded-Proto", request.scheme)
        qr_url = f"{proto}://{request.host}/r/{code}"

        return jsonify({
            "ok":         True,
            "code":       code,
            "expires_at": expires_at,
            "type":       "text",
            "qr_base64":  generate_qr_base64(qr_url),  # ← new field
        })
 
    flash("Transfer created — share the code below.", "success")
    return render_template("index.html", created_transfer=metadata, retrieved_transfer=None)
 
 
# ══════════════════════════════════════════════════════════════
# CHANGE 4 — Your /retrieve route with JSON branches added
# ══════════════════════════════════════════════════════════════
 
@app.route("/retrieve", methods=["POST"])
def retrieve_transfer():
    code = request.form.get("code", "").strip()
 
    if not code or not code.isdigit() or len(code) != 7:
        # NEW: AJAX error branch
        if is_ajax():
            return jsonify({"ok": False, "error": "Enter a valid 7-digit numeric code."}), 400
        flash("Enter a valid 7-digit numeric code.", "error")
        return render_template("index.html", created_transfer=None, retrieved_transfer=None)
 
    metadata = _get_metadata(code)
 
    if not metadata:
        # NEW: AJAX error branch — code not found
        if is_ajax():
            return jsonify({"ok": False, "error": "No transfer found for that code. It may have expired or been mistyped."}), 404
        flash("No transfer found for that code. It may have expired or been mistyped.", "error")
        return render_template("index.html", created_transfer=None, retrieved_transfer=None)
 
    if time.time() > metadata.get("expires_at", 0):
        _delete_transfer(code, metadata)
        # NEW: AJAX error branch — expired
        if is_ajax():
            return jsonify({"ok": False, "error": "That transfer has expired and been deleted."}), 410
        flash("That transfer has expired and been deleted.", "error")
        return render_template("index.html", created_transfer=None, retrieved_transfer=None)
 
    if metadata["type"] == "text":
        _delete_transfer(code, metadata)
        log.info("Text transfer consumed: code=%s", code)
 
    # NEW: AJAX success branches — one for text, one for file
    if is_ajax():
        if metadata["type"] == "text":
            return jsonify({
                "ok": True,
                "type": "text",
                "content": metadata["content"],
            })
        else:
            return jsonify({
                "ok": True,
                "type": "file",
                "filename": metadata["filename"],
                "size": metadata["size"],
                "code": code,  # JavaScript needs this to build the download URL
            })
 
    flash("Transfer retrieved successfully.", "success")
    return render_template("index.html", created_transfer=None, retrieved_transfer=metadata)
 
 #qr code route
@app.route("/r/<code>")
def quick_retrieve(code):
    """
    QR scan landing route.
    Phone camera opens this URL after scanning.

    File  → redirect to /download/<code>
            Flask streams the file → phone downloads it directly
    Text  → burn the code → render quick_text.html with content
    """
    if not code or not code.isdigit() or len(code) != 7:
        flash("Invalid transfer code.", "error")
        return redirect(url_for("index"))

    metadata = _get_metadata(code)

    if not metadata:
        flash("Transfer not found. It may have expired.", "error")
        return redirect(url_for("index"))

    if time.time() > metadata.get("expires_at", 0):
        _delete_transfer(code, metadata)
        flash("This transfer has expired and been deleted.", "error")
        return redirect(url_for("index"))

    if metadata["type"] == "file":
        # Hand off to your existing download route.
        # download_transfer() already handles: read → burn → stream.
        log.info("QR scan → file: code=%s", code)
        return redirect(url_for("download_transfer", code=code))

    # Text — burn and show
    content = metadata["content"]
    _delete_transfer(code, metadata)
    log.info("QR scan → text consumed: code=%s", code)
    return render_template("quick_text.html", content=content)






#about route
@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/contact", methods=["GET","POST"])
def contact():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()

        ip = get_client_ip()
        # Here you would typically send the feedback via email or save it to a database

        sheet_service.contact_append_row(name,email,message,ip)
        

        flash(f"Thank you, {name}, for your message!", "success")
    return render_template("contact.html")

@app.route("/feedback", methods=["GET","POST"])
def feedback():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        message = request.form.get("message", "").strip()
        # Here you would typically send the feedback via email or save it to a database
        ip = get_client_ip()
                
        
        sheet_service.feedback_append_row(name,email,message,ip)
        

        flash(f"Thank you, {name}, for your feedback!", "success")
    return render_template("feedback.html")


@app.route("/robots.txt")
def robots():
    content = """User-agent: *
Allow: /
Disallow: /download/
Disallow: /send
Disallow: /retrieve
Sitemap: https://localdrop-production-a980.up.railway.app/sitemap.xml
"""
    return Response(content, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    today = datetime.date.today().isoformat()
    content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://localdrop-production-a980.up.railway.app/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://localdrop-production-a980.up.railway.app/about</loc>
    <lastmod>{today}</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
  </url>
</urlset>"""
    return Response(content, mimetype="application/xml")

@app.route("/download/<code>")
def download_transfer(code):
    if not code or not code.isdigit() or len(code) != 7:
        flash("Invalid transfer code.", "error")
        return redirect(url_for("index"))

    metadata = _get_metadata(code)
    if not metadata or metadata.get("type") != "file":
        flash("That file transfer is no longer available.", "error")
        return redirect(url_for("index"))

    if time.time() > metadata.get("expires_at", 0):
        _delete_transfer(code, metadata)
        flash("That file has expired and been deleted.", "error")
        return redirect(url_for("index"))

    file_path = Path(metadata["file_path"])
    if not file_path.exists():
        redis_client.delete(code)
        log.error("File missing from disk for code=%s path=%s", code, file_path)
        flash("File could not be found. It may have already been downloaded.", "error")
        return redirect(url_for("index"))

    # Read → Delete → Send (order matters for privacy guarantee)
    file_bytes = file_path.read_bytes()
    _delete_transfer(code, metadata)
    log.info("File downloaded and deleted: code=%s filename=%s", code, metadata["filename"])

    return send_file(
        io.BytesIO(file_bytes),
        as_attachment=True,
        download_name=metadata["filename"],
        mimetype=metadata["mimetype"],
    )


# ─── Error handlers ───────────────────────────────────────────────────────────

@app.errorhandler(413)
def file_too_large(e):
    flash("File is too large. Maximum size is 10 MB.", "error")
    return render_template("index.html", created_transfer=None, retrieved_transfer=None), 413


@app.errorhandler(404)
def not_found(e):
    return render_template("index.html", created_transfer=None, retrieved_transfer=None), 404


@app.errorhandler(500)
def server_error(e):
    log.error("500 error: %s", e)
    flash("Something went wrong on our end. Please try again.", "error")
    return render_template("index.html", created_transfer=None, retrieved_transfer=None), 500


if __name__== "__main__":
        app.run(debug=flask_env == "development")