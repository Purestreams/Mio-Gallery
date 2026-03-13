from flask import Flask, request, jsonify, redirect, send_file, send_from_directory, session
from flask_cors import CORS
from PIL import Image
from pillow_heif import register_heif_opener
import os
import html
from PIL import ImageOps
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
import json
import tarfile
import zipfile
import threading
import shutil
import math
from werkzeug.utils import secure_filename
from PIL import ExifTags
from io import BytesIO
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from concurrent.futures import ThreadPoolExecutor

try:
    import pillow_avif  # noqa: F401
except Exception:
    pillow_avif = None

try:
    import rawpy  # Optional RAW decoder
except Exception:
    rawpy = None

# Register HEIF opener for iPhone photos
register_heif_opener()

debug_mode = False

app = Flask(__name__)
CORS(app)
app.secret_key = os.environ.get("MIO_GALLERY_SECRET", "dev-secret-change-me")

# Configuration
BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent

PHOTO_DIR = REPO_DIR / "photo"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
META_PATH = PHOTO_DIR / ".meta.json"
DESCRIPTION_DIR = PHOTO_DIR / "description"
DESCRIPTION_DIR.mkdir(parents=True, exist_ok=True)
THUMB_DIR = PHOTO_DIR / "thumb"
THUMB_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR = PHOTO_DIR / "download"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
PAGE_DIR = BASE_DIR / "page"

# Logging setup
LOG_DIR = REPO_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def _init_logging():
    handler = RotatingFileHandler(LOG_DIR / "server.log", maxBytes=1_000_000, backupCount=3)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handler.setFormatter(fmt)
    # Ensure we don't add duplicate handlers if reloaded
    existing = [h for h in app.logger.handlers if isinstance(h, RotatingFileHandler)]
    if not existing:
        app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)

_init_logging()

ADMIN_PASSWORD = os.environ.get("MIO_GALLERY_PASSWORD", "Admin123")
RAW_EXTENSIONS = {"cr2", "cr3", "nef", "arw", "orf", "raf", "rw2", "srw", "dng", "pef"}
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tiff', 'webp', 'heic', 'heif', *RAW_EXTENSIONS}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
THUMB_MAX_BYTES = 30 * 1024  # 30KB
MAX_ARCHIVE_SIZE = 500 * 1024 * 1024  # 500MB
ARCHIVE_EXTENSIONS = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
TRANSCODE_WORKERS = max(1, int(os.environ.get("MIO_GALLERY_TRANSCODE_WORKERS", "2")))
ARCHIVE_PROCESS_WORKERS = max(1, int(os.environ.get("MIO_GALLERY_ARCHIVE_WORKERS", "1")))
SIMILARITY_GRID_SIZE = max(8, int(os.environ.get("MIO_GALLERY_SIM_GRID", "12")))
SIMILARITY_DUP_THRESHOLD = min(0.9999, max(0.90, float(os.environ.get("MIO_GALLERY_DUP_THRESHOLD", "0.993"))))

UPLOAD_TMP_DIR = PHOTO_DIR / ".upload_tmp"
UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_CHUNK_DIR = UPLOAD_TMP_DIR / "archive_chunks"
ARCHIVE_CHUNK_DIR.mkdir(parents=True, exist_ok=True)
ZIP_CHUNK_MAX_SIZE = 95 * 1024 * 1024

_TRANSCODE_POOL = ThreadPoolExecutor(max_workers=TRANSCODE_WORKERS, thread_name_prefix="transcode")
_ARCHIVE_POOL = ThreadPoolExecutor(max_workers=ARCHIVE_PROCESS_WORKERS, thread_name_prefix="archive")
_UPLOAD_META_LOCK = threading.Lock()


class DuplicateImageError(ValueError):
    def __init__(self, duplicate_of: str, similarity: float):
        super().__init__(f"Duplicate image (similarity={similarity:.4f})")
        self.duplicate_of = duplicate_of
        self.similarity = float(similarity)


def _is_admin() -> bool:
    return bool(session.get("is_admin"))


def _require_admin():
    if not _is_admin():
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def _meta_get_albums(meta: dict) -> dict:
    albums = meta.get("albums") if isinstance(meta, dict) else None
    return albums if isinstance(albums, dict) else {}


def _meta_get_image_album(meta: dict) -> dict:
    m = meta.get("image_album") if isinstance(meta, dict) else None
    return m if isinstance(m, dict) else {}


def _normalize_album_id(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if s.lower() in {"public", "none", "null"}:
        return None
    return s


def _get_image_album_id(image_id: str) -> str | None:
    meta = _load_meta()
    mapping = _meta_get_image_album(meta)
    album_id = mapping.get(image_id)
    if not album_id:
        return None
    album_id = str(album_id)
    if not album_id:
        return None
    # Treat unknown album ids as public (e.g. album deleted).
    albums = _meta_get_albums(meta)
    return album_id if album_id in albums else None


def _get_album_name(album_id: str | None) -> str | None:
    if not album_id:
        return None
    meta = _load_meta()
    albums = _meta_get_albums(meta)
    a = albums.get(album_id) if isinstance(albums, dict) else None
    if not isinstance(a, dict):
        return None
    name = a.get("name")
    return str(name) if name else None


def _unlocked_album_ids() -> set[str]:
    raw = session.get("unlocked_albums")
    if not raw:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(x) for x in raw if x}
    return set()


def _can_access_album(album_id: str | None) -> bool:
    if _is_admin():
        return True
    if not album_id:
        return True
    return album_id in _unlocked_album_ids()


def _can_access_image(image_id: str) -> bool:
    album_id = _get_image_album_id(image_id)
    return _can_access_album(album_id)


def _require_image_access_or_404(image_id: str):
    if _can_access_image(image_id):
        return None
    # 404 by default to avoid leaking existence.
    return jsonify({'error': 'Image not found'}), 404


@app.route('/', methods=['GET'])
def serve_index_page():
        return send_from_directory(PAGE_DIR, 'index.html')


@app.route('/manage', methods=['GET'])
def serve_manage_page():
        if not _is_admin():
                return redirect('/manage-login')
        return send_from_directory(PAGE_DIR, 'manage.html')


@app.route('/photo/<image_id>', methods=['GET'])
def serve_photo_page(image_id):
    if not _can_access_image(image_id):
        return "Not Found", 404

    payload = _build_image_payload(image_id)
    if not payload:
        return jsonify({'error': 'Image not found'}), 404

    page_url = f"{request.url_root.rstrip('/')}/photo/{image_id}"
    img_url = None
    if payload.get("thumb"):
        img_url = f"{request.url_root.rstrip('/')}{payload['thumb']}"
    if not img_url:
        if payload.get("avif"):
            img_url = f"{request.url_root.rstrip('/')}{payload['avif']}"
        elif payload.get("webp"):
            img_url = f"{request.url_root.rstrip('/')}{payload['webp']}"

    title = payload.get("datetime") or payload.get("date") or payload.get("id") or "Mio Gallery"
    desc = payload.get("description") or "Photo from Mio Gallery"

    meta_block = f"""
  <meta property=\"og:type\" content=\"article\" />
  <meta property=\"og:title\" content=\"{html.escape(title)}\" />
  <meta property=\"og:description\" content=\"{html.escape(desc[:280])}\" />
  {f'<meta property="og:image" content="{html.escape(img_url)}" />' if img_url else ''}
  <meta property=\"og:url\" content=\"{html.escape(page_url)}\" />
  <meta name=\"twitter:card\" content=\"summary_large_image\" />
  <meta name=\"twitter:title\" content=\"{html.escape(title)}\" />
  <meta name=\"twitter:description\" content=\"{html.escape(desc[:280])}\" />
  {f'<meta name="twitter:image" content="{html.escape(img_url)}" />' if img_url else ''}
  <link rel=\"canonical\" href=\"{html.escape(page_url)}\" />
"""

    try:
        html_content = (PAGE_DIR / "photo.html").read_text(encoding="utf-8")
        html_content = html_content.replace("<head>", "<head>\n" + meta_block, 1)
    except Exception:
        return jsonify({'error': 'Page unavailable'}), 500

    return html_content


@app.route('/manage-login', methods=['GET', 'POST'])
def manage_login():
        error = ""
        if request.method == 'POST':
                pw = (request.form.get('password') or '').strip()
                if pw == ADMIN_PASSWORD:
                        session['is_admin'] = True
                        return redirect('/manage')
                error = "Invalid password"

        return (
                """
<!DOCTYPE html>
<html lang=\"en\" data-rw-theme=\"light\">
    <head>
        <meta charset=\"UTF-8\" />
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
        <title>Manage Login</title>
        <link rel=\"stylesheet\" href=\"https://unpkg.com/@rewind-ui/core/dist/rewind-ui.min.css\" />
        <style>
            body{margin:0;font-family:Inter,system-ui,-apple-system,sans-serif;background:#f8fafc;color:#374151}
            .wrap{max-width:520px;margin:0 auto;padding:48px 20px}
            .card{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:18px;box-shadow:0 10px 30px rgba(15,23,42,.12)}
            .title{font-size:22px;font-weight:800;letter-spacing:-.02em;margin-bottom:6px}
            .muted{color:#6b7280;font-size:14px;margin-bottom:14px}
            .row{display:flex;gap:10px;align-items:center}
            .input{flex:1;height:42px;padding:0 12px;border-radius:10px;border:1px solid #e5e7eb;background:#fff;font-size:14px;outline:none}
            .btn{height:42px;padding:0 14px;border-radius:10px;border:1px solid #e5e7eb;background:#fff;font-weight:800;cursor:pointer}
            .btn.primary{background:var(--rw-color-primary-600,#2563eb);border-color:var(--rw-color-primary-600,#2563eb);color:#fff}
            .err{color:#b91c1c;font-size:14px;margin-top:10px}
        </style>
    </head>
    <body>
        <div class=\"wrap\">
            <div class=\"card\">
                <div class=\"title\">Manage Gallery</div>
                <div class=\"muted\">Enter password to continue.</div>
                <form method=\"post\">
                    <div class=\"row\">
                        <input class=\"input\" name=\"password\" type=\"password\" autofocus />
                        <button class=\"btn primary\" type=\"submit\">Enter</button>
                    </div>
                    """
                + (f"<div class=\\\"err\\\">{error}</div>" if error else "")
                + """
                </form>
            </div>
        </div>
    </body>
</html>
"""
        )


def _load_meta():
    if not META_PATH.exists():
        return {}
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_meta(meta: dict):
    tmp = META_PATH.with_suffix(META_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(META_PATH)


def _find_files_by_id(image_id: str):
    matches = []
    if not PHOTO_DIR.exists():
        return matches
    for p in PHOTO_DIR.rglob("*"):
        if p.is_file() and p.stem == image_id:
            matches.append(p)
    return matches


def _description_path(image_id: str) -> Path:
    # image_id comes from filename stem we generated; keep it simple and local.
    safe = Path(image_id).name
    return DESCRIPTION_DIR / f"{safe}.txt"


def _load_description(image_id: str) -> str:
    p = _description_path(image_id)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _save_description(image_id: str, text: str) -> None:
    p = _description_path(image_id)
    text = (text or "").strip()
    if not text:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
        return

    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def _thumb_path(image_id: str) -> Path:
    safe = Path(image_id).name
    return THUMB_DIR / f"{safe}.webp"


def _download_jpg_path(image_id: str) -> Path:
    safe = Path(image_id).name
    return DOWNLOAD_DIR / f"{safe}.jpg"


def _pick_source_file(image_id: str) -> Path | None:
    files = _find_files_by_id(image_id)
    if not files:
        return None
    # Prefer AVIF if readable, else WebP.
    avif = next((p for p in files if p.suffix.lower() == ".avif"), None)
    webp = next((p for p in files if p.suffix.lower() == ".webp"), None)
    return avif or webp or files[0]


def _apply_exif_orientation(img: Image.Image) -> Image.Image:
    """Apply EXIF orientation to image, returning rotated/mirrored image if needed."""
    try:
        return ImageOps.exif_transpose(img)
    except Exception:
        # exif_transpose fails gracefully on images without EXIF; just return original
        return img


def _open_image_any(path: Path) -> Image.Image:
    """Open standard images or RAW files (if rawpy is installed)."""
    try:
        img = Image.open(path)
        img.load()
        return img
    except Exception:
        if rawpy is None:
            raise
        raw = rawpy.imread(str(path))
        try:
            rgb = raw.postprocess(output_bps=8, use_camera_wb=True, no_auto_bright=True)
        finally:
            raw.close()
        return Image.fromarray(rgb)


def _ensure_thumbnail(image_id: str) -> Path | None:
    """Create WebP thumbnail capped at ~50KB (best effort)."""
    out_path = _thumb_path(image_id)
    if out_path.exists():
        return out_path

    src_path = _pick_source_file(image_id)
    if not src_path or not src_path.exists():
        return None

    try:
        with Image.open(src_path) as img:
            img = _apply_exif_orientation(img)
            if img.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                img = bg
            elif img.mode != "RGB":
                img = img.convert("RGB")

            max_side = 640
            min_side = 240
            quality = 76

            while True:
                trial = img.copy()
                trial.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

                buf = BytesIO()
                trial.save(buf, format="WEBP", quality=quality, method=6)
                size = buf.tell()

                if size <= THUMB_MAX_BYTES:
                    out_path.write_bytes(buf.getvalue())
                    return out_path

                if quality > 40:
                    quality -= 8
                    continue

                if max_side <= min_side:
                    # Give up and write the best we have.
                    out_path.write_bytes(buf.getvalue())
                    return out_path

                # Reduce dimensions and try again.
                max_side = max(min_side, int(max_side * 0.85))
                quality = 76

    except Exception:
        return None


@app.route('/api/thumb/<image_id>.webp', methods=['GET'])
def serve_thumbnail(image_id):
    """Serve (and lazily generate) a small WebP thumbnail for grid previews."""
    guard = _require_image_access_or_404(image_id)
    if guard:
        return guard

    files = _find_files_by_id(image_id)
    if not files:
        return jsonify({'error': 'Image not found'}), 404

    p = _ensure_thumbnail(image_id)
    if not p or not p.exists():
        return jsonify({'error': 'Thumbnail not available'}), 404

    from flask import send_from_directory
    return send_from_directory(THUMB_DIR, p.name)

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_image_date(image_path):
    """Extract photo shot date from EXIF data"""
    try:
        with Image.open(image_path) as img:
            exif = None
            try:
                exif = img.getexif()
            except Exception:
                exif = None

            if exif:
                # Try to get DateTimeOriginal (when photo was taken)
                for tag_id in [36867, 36868, 306]:  # DateTimeOriginal, DateTimeDigitized, DateTime
                    try:
                        if tag_id not in exif:
                            continue
                        date_str = exif.get(tag_id)
                        if not date_str:
                            continue
                        if isinstance(date_str, bytes):
                            date_str = date_str.decode("utf-8", errors="ignore")
                        date_str = str(date_str).strip()
                        # Parse EXIF date format: "YYYY:MM:DD HH:MM:SS"
                        return datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                    except Exception:
                        continue

            # Some images may have EXIF bytes; try to parse known tags via mapping (best-effort)
            try:
                raw = getattr(img, "_getexif", None)
                exif_data = raw() if callable(raw) else None
                if exif_data:
                    for tag_id in [36867, 36868, 306]:
                        if tag_id in exif_data:
                            date_str = exif_data[tag_id]
                            if isinstance(date_str, bytes):
                                date_str = date_str.decode("utf-8", errors="ignore")
                            date_str = str(date_str).strip()
                            return datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
            except Exception:
                pass
    except Exception as e:
        print(f"Error reading EXIF data: {e}")
    
    # Fallback to file modification time
    return datetime.fromtimestamp(os.path.getmtime(image_path))


def _get_exif_datetime(image_path) -> datetime | None:
    """Return EXIF DateTimeOriginal/DateTimeDigitized/DateTime if present, else None."""
    try:
        with Image.open(image_path) as img:
            exif = None
            try:
                exif = img.getexif()
            except Exception:
                exif = None

            if exif:
                for tag_id in [36867, 36868, 306]:
                    val = exif.get(tag_id)
                    if not val:
                        continue
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="ignore")
                    s = str(val).strip()
                    try:
                        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
                    except Exception:
                        continue

            # fallback: _getexif()
            raw = getattr(img, "_getexif", None)
            exif_data = raw() if callable(raw) else None
            if exif_data:
                for tag_id in [36867, 36868, 306]:
                    if tag_id not in exif_data:
                        continue
                    val = exif_data.get(tag_id)
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="ignore")
                    s = str(val).strip()
                    try:
                        return datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
                    except Exception:
                        continue
    except Exception:
        return None
    return None


def _extract_datetime_from_id(image_id: str):
    """Best-effort parse of YYYYMMDD_HHMMSS from generated ids."""
    try:
        parts = (image_id or "").split("_")
        if len(parts) < 2:
            return None
        return datetime.strptime(f"{parts[0]}_{parts[1]}", "%Y%m%d_%H%M%S")
    except Exception:
        return None


def _build_image_payload(image_id: str) -> dict | None:
    """Best-effort metadata for a single image id."""
    files = _find_files_by_id(image_id)
    if not files:
        return None

    meta = _load_meta()
    pinned_map = meta.get("pinned", {}) if isinstance(meta, dict) else {}
    datetime_map = meta.get("datetime", {}) if isinstance(meta, dict) else {}

    dt_str = datetime_map.get(image_id) if isinstance(datetime_map, dict) else None
    dt_obj = None
    if dt_str:
        try:
            dt_obj = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        except Exception:
            dt_obj = None
    if not dt_obj:
        dt_obj = _extract_datetime_from_id(image_id)
    if dt_obj and not dt_str:
        dt_str = dt_obj.strftime("%Y-%m-%d %H:%M:%S")

    src = _pick_source_file(image_id)
    date_str = dt_obj.strftime("%Y-%m-%d") if dt_obj else None
    if not date_str and src and src.exists():
        date_str = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y-%m-%d")

    def _rel(p: Path | None) -> str | None:
        if not p:
            return None
        try:
            return f"/api/images/{p.relative_to(PHOTO_DIR)}"
        except Exception:
            return None

    webp = next((p for p in files if p.suffix.lower() == ".webp"), None)
    avif = next((p for p in files if p.suffix.lower() == ".avif"), None)

    album_id = _get_image_album_id(image_id)
    album_name = _get_album_name(album_id)

    return {
        "id": image_id,
        "date": date_str,
        "datetime": dt_str,
        "thumb": f"/api/thumb/{image_id}.webp",
        "webp": _rel(webp),
        "avif": _rel(avif),
        "pinned": bool(pinned_map.get(image_id, False)),
        "description": _load_description(image_id),
        "album_id": album_id,
        "album_name": album_name,
    }


@app.route('/api/albums/unlocked', methods=['GET'])
def get_unlocked_albums():
    """Return album ids/names unlocked in this session."""
    meta = _load_meta()
    albums = _meta_get_albums(meta)
    unlocked = []
    for aid in sorted(_unlocked_album_ids()):
        a = albums.get(aid)
        if isinstance(a, dict):
            unlocked.append({"id": aid, "name": a.get("name") or aid})
    return jsonify({"unlocked": unlocked}), 200


@app.route('/api/albums/unlock', methods=['POST'])
def unlock_album():
    """Unlock one or more private albums by password for this session."""
    body = request.get_json(silent=True) or {}
    password = (body.get("password") or "").strip()
    if not password:
        return jsonify({"error": "missing_password"}), 400

    meta = _load_meta()
    albums = _meta_get_albums(meta)

    matched = []
    for aid, a in albums.items():
        if not isinstance(a, dict):
            continue
        pw_hash = a.get("password_hash")
        if not pw_hash:
            continue
        try:
            if check_password_hash(str(pw_hash), password):
                matched.append({"id": aid, "name": a.get("name") or aid})
        except Exception:
            continue

    if not matched:
        return jsonify({"error": "invalid_password"}), 401

    unlocked = _unlocked_album_ids()
    for a in matched:
        unlocked.add(a["id"])
    session["unlocked_albums"] = sorted(unlocked)

    return jsonify({"unlocked": matched}), 200


@app.route('/api/albums/lock', methods=['POST'])
def lock_albums():
    """Clear unlocked album access for this session."""
    session.pop("unlocked_albums", None)
    return jsonify({"ok": True}), 200


def _album_id_from_name(name: str, existing: set[str]) -> str:
    base = secure_filename((name or "").strip()).lower() or "album"
    candidate = base
    i = 2
    while candidate in existing:
        candidate = f"{base}-{i}"
        i += 1
    return candidate


@app.route('/api/admin/albums', methods=['GET', 'POST'])
def admin_albums():
    auth = _require_admin()
    if auth:
        return auth

    meta = _load_meta()
    if not isinstance(meta, dict):
        meta = {}
    albums = _meta_get_albums(meta)

    if request.method == 'GET':
        # Include counts (best-effort)
        image_album = _meta_get_image_album(meta)
        counts = {}
        for _, aid in image_album.items():
            if not aid:
                continue
            aid = str(aid)
            counts[aid] = counts.get(aid, 0) + 1

        out = []
        for aid, a in sorted(albums.items(), key=lambda kv: (str((kv[1] or {}).get('name') or kv[0]).lower())):
            if not isinstance(a, dict):
                continue
            out.append({
                "id": aid,
                "name": a.get("name") or aid,
                "count": counts.get(aid, 0),
                "created_at": a.get("created_at"),
            })
        return jsonify({"albums": out}), 200

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    password = (body.get("password") or "").strip()
    if not name:
        return jsonify({"error": "missing_name"}), 400
    if not password:
        return jsonify({"error": "missing_password"}), 400

    existing_ids = set(albums.keys())
    aid = _album_id_from_name(name, existing_ids)
    albums[aid] = {
        "name": name,
        "password_hash": generate_password_hash(password),
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    meta["albums"] = albums
    _save_meta(meta)

    return jsonify({"id": aid, "name": name}), 201


@app.route('/api/admin/albums/<album_id>', methods=['PUT', 'DELETE'])
def admin_album_update(album_id):
    auth = _require_admin()
    if auth:
        return auth

    album_id = str(Path(album_id).name)
    meta = _load_meta()
    if not isinstance(meta, dict):
        meta = {}
    albums = _meta_get_albums(meta)

    if album_id not in albums:
        return jsonify({"error": "not_found"}), 404

    if request.method == 'DELETE':
        albums.pop(album_id, None)
        meta["albums"] = albums
        # Unassign images from this album
        image_album = _meta_get_image_album(meta)
        if isinstance(image_album, dict):
            for img_id, aid in list(image_album.items()):
                if str(aid) == album_id:
                    image_album.pop(img_id, None)
            meta["image_album"] = image_album
        _save_meta(meta)
        return jsonify({"ok": True}), 200

    body = request.get_json(silent=True) or {}
    name = body.get("name")
    password = body.get("password")

    a = albums.get(album_id)
    if not isinstance(a, dict):
        a = {}

    if name is not None:
        name = str(name).strip()
        if not name:
            return jsonify({"error": "missing_name"}), 400
        a["name"] = name
    if password is not None:
        password = str(password).strip()
        if password:
            a["password_hash"] = generate_password_hash(password)

    albums[album_id] = a
    meta["albums"] = albums
    _save_meta(meta)
    return jsonify({"id": album_id, "name": a.get("name") or album_id}), 200


@app.route('/api/admin/images/<image_id>/album', methods=['PUT'])
def admin_set_image_album(image_id):
    auth = _require_admin()
    if auth:
        return auth

    files = _find_files_by_id(image_id)
    if not files:
        return jsonify({'error': 'Image not found'}), 404

    body = request.get_json(silent=True) or {}
    album_id = body.get("album_id")
    if album_id is not None:
        album_id = str(album_id).strip()
    if album_id in (None, "", "public"):
        album_id = None

    meta = _load_meta()
    if not isinstance(meta, dict):
        meta = {}
    albums = _meta_get_albums(meta)
    image_album = _meta_get_image_album(meta)

    if album_id is not None and album_id not in albums:
        return jsonify({"error": "album_not_found"}), 404

    if album_id is None:
        image_album.pop(image_id, None)
    else:
        image_album[image_id] = album_id

    meta["image_album"] = image_album
    _save_meta(meta)

    return jsonify({"id": image_id, "album_id": album_id, "album_name": _get_album_name(album_id)}), 200

def convert_and_save_image(image_path, output_dir, base_name):
    """Convert image (including RAW, if supported) to WebP and AVIF, capped ~1MB each."""
    results = {}
    MAX_OUTPUT_SIZE = 1024 * 1024  # 1MB

    img = _open_image_any(image_path)
    try:
        img = _apply_exif_orientation(img)
        if img.mode in ('RGBA', 'LA', 'P'):
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        webp_path = output_dir / f"{base_name}.webp"
        quality = 70
        while quality >= 50:
            buf = BytesIO()
            img.save(buf, 'WEBP', quality=quality, method=6)
            if buf.tell() <= MAX_OUTPUT_SIZE or quality <= 50:
                webp_path.write_bytes(buf.getvalue())
                break
            quality -= 5
        results['webp'] = str(webp_path.relative_to(PHOTO_DIR))

        try:
            avif_path = output_dir / f"{base_name}.avif"
            quality = 80
            while quality >= 50:
                buf = BytesIO()
                img.save(buf, 'AVIF', quality=quality)
                if buf.tell() <= MAX_OUTPUT_SIZE or quality <= 50:
                    avif_path.write_bytes(buf.getvalue())
                    break
                quality -= 5
            results['avif'] = str(avif_path.relative_to(PHOTO_DIR))
        except Exception as e:
            print(f"AVIF conversion failed: {e}. AVIF support may not be available.")
            results['avif'] = None
    finally:
        try:
            img.close()
        except Exception:
            pass

    return results


def _is_archive_filename(filename: str) -> bool:
    lower = (filename or "").lower()
    return any(lower.endswith(ext) for ext in ARCHIVE_EXTENSIONS)


def _temp_upload_path(suffix: str = "") -> Path:
    suffix = suffix if suffix.startswith(".") or not suffix else f".{suffix}"
    token = f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{os.urandom(4).hex()}"
    return UPLOAD_TMP_DIR / f"upload_{token}{suffix}"


def _safe_upload_id(raw: str) -> str:
    v = secure_filename((raw or "").strip())
    if not v:
        raise ValueError("invalid_upload_id")
    return v


def _parse_int_form(name: str, default: int | None = None) -> int | None:
    v = request.form.get(name)
    if v is None or str(v).strip() == "":
        return default
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _handle_chunked_archive_upload(upload_album_id: str | None):
    upload_id_raw = request.form.get("archive_upload_id")
    if not upload_id_raw:
        return None

    upload_id = _safe_upload_id(upload_id_raw)
    idx = _parse_int_form("archive_chunk_index")
    total = _parse_int_form("archive_chunk_total")
    original_name = (request.form.get("archive_original_name") or "archive.zip").strip()
    ext = Path(original_name).suffix.lower() or ".zip"
    if ext != ".zip":
        return jsonify({"error": "chunked_upload_zip_only", "message": "Chunked upload currently supports .zip only"}), 400

    if idx is None or total is None or idx < 0 or total <= 0 or idx >= total:
        return jsonify({"error": "invalid_chunk_params"}), 400

    if "image" not in request.files:
        return jsonify({"error": "missing_chunk_file"}), 400

    chunk_file = request.files["image"]
    upload_dir = ARCHIVE_CHUNK_DIR / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    chunk_path = upload_dir / f"part_{idx:06d}.bin"
    chunk_file.save(chunk_path)

    chunk_size = chunk_path.stat().st_size
    if chunk_size > ZIP_CHUNK_MAX_SIZE:
        try:
            chunk_path.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"error": "chunk_too_large", "message": "Each zip chunk must be <= 95MB"}), 400

    existing_parts = {p.name for p in upload_dir.glob("part_*.bin") if p.is_file()}
    expected_parts = {f"part_{i:06d}.bin" for i in range(total)}
    missing_count = len(expected_parts - existing_parts)

    if missing_count > 0:
        return jsonify({
            "chunk_received": idx + 1,
            "chunks_total": total,
            "missing_chunks": missing_count,
            "completed": False,
            "message": "Chunk received"
        }), 202

    assembled = _temp_upload_path(ext)
    try:
        with assembled.open("wb") as out:
            for i in range(total):
                part = upload_dir / f"part_{i:06d}.bin"
                with part.open("rb") as inp:
                    shutil.copyfileobj(inp, out, length=1024 * 1024)
    finally:
        try:
            shutil.rmtree(upload_dir, ignore_errors=True)
        except Exception:
            pass

    try:
        _ARCHIVE_POOL.submit(_background_process_archive, assembled, original_name, upload_album_id)
    except Exception:
        try:
            assembled.unlink(missing_ok=True)
        except Exception:
            pass
        return jsonify({"error": "archive_queue_failed"}), 500

    return jsonify({
        "chunk_received": idx + 1,
        "chunks_total": total,
        "missing_chunks": 0,
        "completed": True,
        "archive_processing": True,
        "message": "Archive assembled and queued for background processing"
    }), 202


def _background_process_archive(archive_path: Path, original_name: str, upload_album_id: str | None) -> None:
    queued = 0
    duplicates = 0
    errors = 0
    archive_errors = 0
    seen_signatures: list[tuple[str, dict]] = []
    try:
        extracted_files, extract_errors = _extract_archive_to_temp_files(archive_path)
        archive_errors = len(extract_errors)
        for extracted_temp, member_name in extracted_files:
            try:
                _enqueue_single_upload(extracted_temp, member_name, upload_album_id, seen_signatures)
                queued += 1
            except DuplicateImageError:
                duplicates += 1
                try:
                    extracted_temp.unlink(missing_ok=True)
                except Exception:
                    pass
            except Exception:
                errors += 1
                try:
                    extracted_temp.unlink(missing_ok=True)
                except Exception:
                    pass

        app.logger.info(
            "Archive processed: %s queued=%s duplicates=%s errors=%s extract_errors=%s",
            original_name,
            queued,
            duplicates,
            errors,
            archive_errors,
        )
    except Exception:
        app.logger.exception("Archive processing failed for %s", original_name)
    finally:
        try:
            archive_path.unlink(missing_ok=True)
        except Exception:
            pass


def _meta_get_similarity_signatures(meta: dict) -> dict:
    m = meta.get("similarity_signatures") if isinstance(meta, dict) else None
    return m if isinstance(m, dict) else {}


def _is_in_subdir(path: Path, subdir: Path) -> bool:
    try:
        path.relative_to(subdir)
        return True
    except Exception:
        return False


def _pick_canonical_source_file(image_id: str) -> Path | None:
    files = _find_files_by_id(image_id)
    if not files:
        return None
    blocked_roots = {THUMB_DIR, DOWNLOAD_DIR, DESCRIPTION_DIR, UPLOAD_TMP_DIR}
    filtered = []
    for p in files:
        if any(_is_in_subdir(p, root) for root in blocked_roots):
            continue
        filtered.append(p)
    if not filtered:
        return None
    avif = next((p for p in filtered if p.suffix.lower() == ".avif"), None)
    webp = next((p for p in filtered if p.suffix.lower() == ".webp"), None)
    return avif or webp or filtered[0]


def _build_similarity_signature(path: Path) -> dict:
    img = _open_image_any(path)
    try:
        img = _apply_exif_orientation(img)
        if img.mode == "P":
            img = img.convert("RGB")
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        w, h = img.size
        small = img.convert("L").resize((SIMILARITY_GRID_SIZE, SIMILARITY_GRID_SIZE), Image.Resampling.LANCZOS)
        vec = [int(x) for x in small.getdata()]
        return {
            "w": int(w),
            "h": int(h),
            "grid": int(SIMILARITY_GRID_SIZE),
            "v": vec,
        }
    finally:
        try:
            img.close()
        except Exception:
            pass


def _signature_similarity(a: dict, b: dict) -> float:
    try:
        va = a.get("v")
        vb = b.get("v")
        if not isinstance(va, list) or not isinstance(vb, list) or not va or len(va) != len(vb):
            return 0.0

        wa = float(a.get("w") or 1.0)
        ha = float(a.get("h") or 1.0)
        wb = float(b.get("w") or 1.0)
        hb = float(b.get("h") or 1.0)
        ratio_a = wa / max(1.0, ha)
        ratio_b = wb / max(1.0, hb)
        ratio_gap = abs(math.log(max(1e-6, ratio_a / max(1e-6, ratio_b))))
        if ratio_gap > 0.08:
            return 0.0

        diff = 0.0
        for i in range(len(va)):
            diff += abs(float(va[i]) - float(vb[i]))
        pixel_score = 1.0 - (diff / (len(va) * 255.0))
        ratio_score = max(0.0, 1.0 - (ratio_gap * 5.0))
        return max(0.0, min(1.0, pixel_score * ratio_score))
    except Exception:
        return 0.0


def _collect_existing_image_ids(meta: dict) -> set[str]:
    ids = set()
    dt_map = meta.get("datetime") if isinstance(meta, dict) else None
    if isinstance(dt_map, dict):
        ids.update(str(k) for k in dt_map.keys() if k)

    for p in PHOTO_DIR.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in {".webp", ".avif"}:
            continue
        if _is_in_subdir(p, THUMB_DIR) or _is_in_subdir(p, DOWNLOAD_DIR) or _is_in_subdir(p, UPLOAD_TMP_DIR):
            continue
        ids.add(p.stem)
    return ids


def _get_or_build_signature(image_id: str, signature_map: dict) -> dict | None:
    existing = signature_map.get(image_id)
    if isinstance(existing, dict) and isinstance(existing.get("v"), list) and existing.get("v"):
        return existing

    src = _pick_canonical_source_file(image_id)
    if not src or not src.exists():
        return None
    try:
        sig = _build_similarity_signature(src)
        signature_map[image_id] = sig
        return sig
    except Exception:
        return None


def _find_duplicate_match(
    new_signature: dict,
    signature_map: dict,
    seen_signatures: list[tuple[str, dict]],
    existing_ids: set[str],
) -> tuple[str, float] | None:
    best_id = None
    best_score = 0.0

    for seen_id, seen_sig in seen_signatures:
        score = _signature_similarity(new_signature, seen_sig)
        if score > best_score:
            best_score = score
            best_id = seen_id

    for image_id in existing_ids:
        ref = _get_or_build_signature(image_id, signature_map)
        if not ref:
            continue
        score = _signature_similarity(new_signature, ref)
        if score > best_score:
            best_score = score
            best_id = image_id

    if best_id and best_score >= SIMILARITY_DUP_THRESHOLD:
        return best_id, best_score
    return None


def _reserve_unique_image_id(meta: dict, year_month_dir: Path, photo_date: datetime) -> str:
    base_prefix = photo_date.strftime('%Y%m%d_%H%M%S')
    dt_map = meta.get("datetime")
    if not isinstance(dt_map, dict):
        dt_map = {}

    while True:
        candidate = f"{base_prefix}_{os.urandom(6).hex()}"
        if (
            candidate not in dt_map
            and not (year_month_dir / f"{candidate}.webp").exists()
            and not (year_month_dir / f"{candidate}.avif").exists()
            and not (UPLOAD_TMP_DIR / f"{candidate}.webp").exists()
            and not (UPLOAD_TMP_DIR / f"{candidate}.avif").exists()
        ):
            return candidate


def _cleanup_meta_for_image(image_id: str) -> None:
    with _UPLOAD_META_LOCK:
        meta = _load_meta()
        if not isinstance(meta, dict):
            return

        changed = False
        dt_map = meta.get("datetime")
        if isinstance(dt_map, dict) and image_id in dt_map:
            dt_map.pop(image_id, None)
            meta["datetime"] = dt_map
            changed = True

        image_album = meta.get("image_album")
        if isinstance(image_album, dict) and image_id in image_album:
            image_album.pop(image_id, None)
            meta["image_album"] = image_album
            changed = True

        similarity = meta.get("similarity_signatures")
        if isinstance(similarity, dict) and image_id in similarity:
            similarity.pop(image_id, None)
            meta["similarity_signatures"] = similarity
            changed = True

        if changed:
            _save_meta(meta)


def _background_transcode(temp_path: Path, year_month_dir: Path, image_id: str) -> None:
    try:
        convert_and_save_image(temp_path, year_month_dir, image_id)
    except Exception:
        app.logger.exception("Transcoding failed for %s", image_id)
        try:
            (year_month_dir / f"{image_id}.webp").unlink(missing_ok=True)
        except Exception:
            pass
        try:
            (year_month_dir / f"{image_id}.avif").unlink(missing_ok=True)
        except Exception:
            pass
        _cleanup_meta_for_image(image_id)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _enqueue_single_upload(
    temp_path: Path,
    original_filename: str,
    upload_album_id: str | None,
    seen_signatures: list[tuple[str, dict]],
) -> dict:
    ext = (Path(original_filename).suffix or "").lower().lstrip(".")
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("File type not allowed")
    if ext in RAW_EXTENSIONS and rawpy is None:
        raise ValueError("RAW support requires rawpy to be installed on the server")

    if temp_path.stat().st_size > MAX_FILE_SIZE:
        raise ValueError("File too large (max 50MB)")

    photo_date = _get_exif_datetime(temp_path) or datetime.now()
    year_month_dir = PHOTO_DIR / photo_date.strftime("%Y") / photo_date.strftime("%m")
    year_month_dir.mkdir(parents=True, exist_ok=True)
    new_signature = _build_similarity_signature(temp_path)

    image_id = None
    with _UPLOAD_META_LOCK:
        meta = _load_meta()
        if not isinstance(meta, dict):
            meta = {}

        signature_map = _meta_get_similarity_signatures(meta)
        existing_ids = _collect_existing_image_ids(meta)

        duplicate = _find_duplicate_match(new_signature, signature_map, seen_signatures, existing_ids)
        if duplicate:
            dup_id, score = duplicate
            raise DuplicateImageError(dup_id, score)

        image_id = _reserve_unique_image_id(meta, year_month_dir, photo_date)

        dt_map = meta.get("datetime")
        if not isinstance(dt_map, dict):
            dt_map = {}
        dt_map[image_id] = photo_date.strftime("%Y-%m-%d %H:%M:%S")
        meta["datetime"] = dt_map

        image_album = _meta_get_image_album(meta)
        if upload_album_id is None:
            image_album.pop(image_id, None)
        else:
            image_album[image_id] = upload_album_id
        meta["image_album"] = image_album

        signature_map[image_id] = new_signature
        meta["similarity_signatures"] = signature_map

        _save_meta(meta)

    seen_signatures.append((image_id, new_signature))

    try:
        queued_src = _temp_upload_path(Path(original_filename).suffix.lower() or ".img")
        temp_path.replace(queued_src)
        _TRANSCODE_POOL.submit(_background_transcode, queued_src, year_month_dir, image_id)
    except Exception:
        if image_id:
            _cleanup_meta_for_image(image_id)
        raise

    rel_dir = year_month_dir.relative_to(PHOTO_DIR)
    return {
        "id": image_id,
        "original_filename": original_filename,
        "date": photo_date.strftime("%Y-%m-%d"),
        "datetime": photo_date.strftime("%Y-%m-%d %H:%M:%S"),
        "queued": True,
        "thumb": f"/api/thumb/{image_id}.webp",
        "webp": f"/api/images/{rel_dir}/{image_id}.webp",
        "avif": f"/api/images/{rel_dir}/{image_id}.avif",
    }


def _extract_archive_to_temp_files(archive_path: Path) -> tuple[list[tuple[Path, str]], list[dict]]:
    extracted: list[tuple[Path, str]] = []
    errors: list[dict] = []
    archive_name = archive_path.name

    if archive_path.stat().st_size > MAX_ARCHIVE_SIZE:
        return extracted, [{"filename": archive_name, "error": "Archive too large (max 500MB)"}]

    lower = archive_name.lower()

    def _add_member(stream, member_name: str, declared_size: int | None):
        name = Path(member_name or "").name
        if not name:
            return
        if not allowed_file(name):
            errors.append({"filename": f"{archive_name}:{member_name}", "error": "File type not allowed"})
            return
        if declared_size is not None and declared_size > MAX_FILE_SIZE:
            errors.append({"filename": f"{archive_name}:{member_name}", "error": "File too large (max 50MB)"})
            return

        tmp = _temp_upload_path(Path(name).suffix.lower() or ".img")
        try:
            with tmp.open("wb") as out:
                shutil.copyfileobj(stream, out, length=1024 * 1024)
            if tmp.stat().st_size > MAX_FILE_SIZE:
                tmp.unlink(missing_ok=True)
                errors.append({"filename": f"{archive_name}:{member_name}", "error": "File too large (max 50MB)"})
                return
            extracted.append((tmp, name))
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            errors.append({"filename": f"{archive_name}:{member_name}", "error": str(exc)})

    try:
        if lower.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    with zf.open(info, "r") as src:
                        _add_member(src, info.filename, info.file_size)
        else:
            with tarfile.open(archive_path, mode="r:*") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with src:
                        _add_member(src, member.name, int(member.size) if member.size is not None else None)
    except Exception as exc:
        errors.append({"filename": archive_name, "error": f"Archive read failed: {exc}"})

    return extracted, errors

@app.route('/api/upload', methods=['POST'])
def upload_image():
    """
    POST API to upload images
    Accepts: multipart/form-data with 'image' or 'images' field
    Returns: JSON after files are accepted and queued for background transcoding
    """
    auth = _require_admin()
    if auth:
        return auth

    # Optional: assign new uploads to a private album (default recommended by UI).
    meta_for_upload = _load_meta()
    albums_for_upload = _meta_get_albums(meta_for_upload)
    public_flag = (request.form.get("public") or "").strip().lower() in {"1", "true", "yes", "on"}
    upload_album_id = _normalize_album_id(request.form.get("album_id"))

    if upload_album_id is not None and upload_album_id not in albums_for_upload:
        return jsonify({"error": "album_not_found"}), 404

    if not public_flag and upload_album_id is None and len(albums_for_upload) > 0:
        # UI defaults to private; enforce explicit choice when albums exist.
        return jsonify({"error": "missing_album", "message": "Select an album or mark as public"}), 400

    chunk_resp = _handle_chunked_archive_upload(upload_album_id)
    if chunk_resp is not None:
        return chunk_resp

    if 'image' not in request.files and 'images' not in request.files:
        return jsonify({'error': 'No image file provided'}), 400
    
    # Handle both single and multiple file uploads
    files = request.files.getlist('images') if 'images' in request.files else [request.files['image']]
    
    results = []
    errors = []
    duplicates = []
    seen_signatures: list[tuple[str, dict]] = []

    for file in files:
        filename = (file.filename or "").strip()
        if not filename:
            errors.append({'filename': 'empty', 'error': 'No selected file'})
            continue

        try:
            if _is_archive_filename(filename):
                archive_temp = _temp_upload_path(Path(filename).suffix.lower() or ".archive")
                file.save(archive_temp)

                try:
                    _ARCHIVE_POOL.submit(_background_process_archive, archive_temp, filename, upload_album_id)
                    results.append({
                        'original_filename': filename,
                        'queued': True,
                        'archive_processing': True,
                    })
                except Exception:
                    try:
                        archive_temp.unlink(missing_ok=True)
                    except Exception:
                        pass
                    errors.append({'filename': filename, 'error': 'archive_queue_failed'})
                continue

            temp_path = _temp_upload_path(Path(filename).suffix.lower() or ".img")
            file.save(temp_path)
            results.append(_enqueue_single_upload(temp_path, filename, upload_album_id, seen_signatures))
        except DuplicateImageError as dup:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
            duplicates.append({
                'filename': filename,
                'duplicate_of': dup.duplicate_of,
                'similarity': round(float(dup.similarity), 4),
            })
        except Exception as e:
            errors.append({'filename': filename, 'error': str(e)})

    response = {
        'uploaded': results,
        'queued': len(results),
        'removed_duplicates': len(duplicates),
        'message': 'Files accepted and queued for background transcoding' if results else 'No files queued'
    }
    if errors:
        response['errors'] = errors
    if duplicates:
        response['duplicates'] = duplicates

    status_code = 202 if (results or duplicates) else 400
    return jsonify(response), status_code

@app.route('/api/images', methods=['GET'])
def get_images():
    """
    GET API to retrieve image list with optional date filtering
    Query params:
        - start_date: YYYY-MM-DD (optional)
        - end_date: YYYY-MM-DD (optional)
    Returns: JSON with list of image URLs
    """
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    album_filter = (request.args.get('album') or '').strip()
    
    # Parse date filters
    start_dt = None
    end_dt = None
    
    try:
        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError as e:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    images = []
    meta = _load_meta()
    pinned_map = meta.get("pinned", {}) if isinstance(meta, dict) else {}
    datetime_map = meta.get("datetime", {}) if isinstance(meta, dict) else {}
    if not isinstance(datetime_map, dict):
        datetime_map = {}

    image_album = _meta_get_image_album(meta)
    albums = _meta_get_albums(meta)

    # Album access rules (IMPORTANT): behaves the same for admins and normal users.
    # Admin login should not automatically reveal private albums on the public gallery page.
    if album_filter and album_filter not in ("all", "public"):
        # requesting a specific private album
        if album_filter not in _unlocked_album_ids():
            return jsonify({"error": "forbidden"}), 403
    
    # Walk through photo directory
    for year_dir in sorted(PHOTO_DIR.iterdir()):
        if not year_dir.is_dir() or year_dir.name.startswith('.'):
            continue
        
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or month_dir.name.startswith('.'):
                continue
            
            # Check if this month is in date range
            try:
                month_date = datetime.strptime(f"{year_dir.name}-{month_dir.name}", "%Y-%m")
                if start_dt and month_date.replace(day=28) < start_dt.replace(day=1):
                    continue
                if end_dt and month_date.replace(day=1) > end_dt.replace(day=28):
                    continue
            except ValueError:
                continue
            
            # Group images by base name
            image_groups = {}
            for img_file in sorted(month_dir.iterdir()):
                if img_file.is_file() and not img_file.name.startswith('.'):
                    base_name = img_file.stem
                    ext = img_file.suffix[1:]  # Remove the dot
                    
                    if base_name not in image_groups:
                        image_groups[base_name] = {'webp': None, 'avif': None, 'date': None}
                    
                    relative_path = img_file.relative_to(PHOTO_DIR)
                    image_groups[base_name][ext] = f"/api/images/{relative_path}"
                    
                    # Extract date from filename
                    if not image_groups[base_name]['date']:
                        try:
                            date_part = base_name.split('_')[0]
                            img_date = datetime.strptime(date_part, "%Y%m%d")
                            image_groups[base_name]['date'] = img_date.strftime("%Y-%m-%d")
                        except:
                            image_groups[base_name]['date'] = f"{year_dir.name}-{month_dir.name}-01"
            
            # Filter by exact date range and add to results
            for base_name, img_data in image_groups.items():
                img_album_id = None
                try:
                    img_album_id = image_album.get(base_name) if isinstance(image_album, dict) else None
                    img_album_id = str(img_album_id).strip() if img_album_id else None
                except Exception:
                    img_album_id = None

                # Normalize: unknown album ids => public
                if img_album_id and img_album_id not in albums:
                    img_album_id = None

                # Enforce access (no admin bypass here)
                if album_filter == "public":
                    if img_album_id is not None:
                        continue
                elif album_filter and album_filter not in ("all", "public"):
                    if img_album_id != album_filter:
                        continue
                else:
                    if img_album_id is not None and img_album_id not in _unlocked_album_ids():
                        continue

                dt_str = datetime_map.get(base_name)
                dt_obj = None
                if dt_str:
                    try:
                        dt_obj = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        dt_obj = None
                if not dt_obj:
                    dt_obj = _extract_datetime_from_id(base_name)

                date_str = img_data.get('date')
                if dt_obj:
                    date_str = dt_obj.strftime("%Y-%m-%d")

                # Apply date range filter against best available date
                try:
                    img_date = datetime.strptime(date_str, "%Y-%m-%d")
                    if start_dt and img_date < start_dt:
                        continue
                    if end_dt and img_date > end_dt:
                        continue
                except Exception:
                    pass

                images.append({
                    'id': base_name,
                    'date': date_str,
                    'datetime': dt_obj.strftime("%Y-%m-%d %H:%M:%S") if dt_obj else dt_str,
                    'thumb': f"/api/thumb/{base_name}.webp",
                    'webp': img_data['webp'],
                    'avif': img_data['avif'],
                    'pinned': bool(pinned_map.get(base_name, False)),
                    'description': _load_description(base_name),
                    'album_id': img_album_id,
                    'album_name': (albums.get(img_album_id) or {}).get('name') if img_album_id and isinstance(albums.get(img_album_id), dict) else None,
                })

    # Pinned first, then newest first (best effort)
    def _sort_key(x):
        try:
            d = datetime.strptime(x.get('date') or "1970-01-01", "%Y-%m-%d")
        except Exception:
            d = datetime(1970, 1, 1)
        return (0 if x.get('pinned') else 1, -int(d.timestamp()), x.get('id') or "")

    images.sort(key=_sort_key)
    
    return jsonify({
        'total': len(images),
        'images': images,
        'filters': {
            'start_date': start_date,
            'end_date': end_date
        }
    })


@app.route('/api/admin/images', methods=['GET'])
def admin_get_images():
    """Admin-only: list all images, including private albums."""
    auth = _require_admin()
    if auth:
        return auth

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    # Parse date filters
    start_dt = None
    end_dt = None

    try:
        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    images = []
    meta = _load_meta()
    pinned_map = meta.get("pinned", {}) if isinstance(meta, dict) else {}
    datetime_map = meta.get("datetime", {}) if isinstance(meta, dict) else {}
    if not isinstance(datetime_map, dict):
        datetime_map = {}

    image_album = _meta_get_image_album(meta)
    albums = _meta_get_albums(meta)

    for year_dir in sorted(PHOTO_DIR.iterdir()):
        if not year_dir.is_dir() or year_dir.name.startswith('.'):
            continue
        for month_dir in sorted(year_dir.iterdir()):
            if not month_dir.is_dir() or month_dir.name.startswith('.'):
                continue

            try:
                month_date = datetime.strptime(f"{year_dir.name}-{month_dir.name}", "%Y-%m")
                if start_dt and month_date.replace(day=28) < start_dt.replace(day=1):
                    continue
                if end_dt and month_date.replace(day=1) > end_dt.replace(day=28):
                    continue
            except ValueError:
                continue

            image_groups = {}
            for img_file in sorted(month_dir.iterdir()):
                if img_file.is_file() and not img_file.name.startswith('.'):
                    base_name = img_file.stem
                    ext = img_file.suffix[1:]
                    if base_name not in image_groups:
                        image_groups[base_name] = {'webp': None, 'avif': None, 'date': None}
                    relative_path = img_file.relative_to(PHOTO_DIR)
                    image_groups[base_name][ext] = f"/api/images/{relative_path}"
                    if not image_groups[base_name]['date']:
                        try:
                            date_part = base_name.split('_')[0]
                            img_date = datetime.strptime(date_part, "%Y%m%d")
                            image_groups[base_name]['date'] = img_date.strftime("%Y-%m-%d")
                        except Exception:
                            image_groups[base_name]['date'] = f"{year_dir.name}-{month_dir.name}-01"

            for base_name, img_data in image_groups.items():
                img_album_id = None
                try:
                    img_album_id = image_album.get(base_name) if isinstance(image_album, dict) else None
                    img_album_id = str(img_album_id).strip() if img_album_id else None
                except Exception:
                    img_album_id = None
                if img_album_id and img_album_id not in albums:
                    img_album_id = None

                dt_str = datetime_map.get(base_name)
                dt_obj = None
                if dt_str:
                    try:
                        dt_obj = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        dt_obj = None
                if not dt_obj:
                    dt_obj = _extract_datetime_from_id(base_name)

                date_str = img_data.get('date')
                if dt_obj:
                    date_str = dt_obj.strftime("%Y-%m-%d")

                try:
                    img_date = datetime.strptime(date_str, "%Y-%m-%d")
                    if start_dt and img_date < start_dt:
                        continue
                    if end_dt and img_date > end_dt:
                        continue
                except Exception:
                    pass

                images.append({
                    'id': base_name,
                    'date': date_str,
                    'datetime': dt_obj.strftime("%Y-%m-%d %H:%M:%S") if dt_obj else dt_str,
                    'thumb': f"/api/thumb/{base_name}.webp",
                    'webp': img_data['webp'],
                    'avif': img_data['avif'],
                    'pinned': bool(pinned_map.get(base_name, False)),
                    'description': _load_description(base_name),
                    'album_id': img_album_id,
                    'album_name': (albums.get(img_album_id) or {}).get('name') if img_album_id and isinstance(albums.get(img_album_id), dict) else None,
                })

    def _sort_key(x):
        try:
            d = datetime.strptime(x.get('date') or "1970-01-01", "%Y-%m-%d")
        except Exception:
            d = datetime(1970, 1, 1)
        return (0 if x.get('pinned') else 1, -int(d.timestamp()), x.get('id') or "")

    images.sort(key=_sort_key)

    return jsonify({
        'total': len(images),
        'images': images,
        'filters': {
            'start_date': start_date,
            'end_date': end_date
        }
    })


@app.route('/api/images/<image_id>/pin', methods=['PUT'])
def pin_image(image_id):
    """Pin/unpin an image by id. Body: {"pinned": true|false}. If omitted, toggles."""
    auth = _require_admin()
    if auth:
        return auth

    files = _find_files_by_id(image_id)
    if not files:
        return jsonify({'error': 'Image not found'}), 404

    body = request.get_json(silent=True) or {}
    meta = _load_meta()
    if not isinstance(meta, dict):
        meta = {}
    pinned_map = meta.get("pinned")
    if not isinstance(pinned_map, dict):
        pinned_map = {}

    if "pinned" in body:
        new_state = bool(body.get("pinned"))
    else:
        new_state = not bool(pinned_map.get(image_id, False))

    if new_state:
        pinned_map[image_id] = True
    else:
        pinned_map.pop(image_id, None)

    meta["pinned"] = pinned_map
    _save_meta(meta)

    return jsonify({'id': image_id, 'pinned': new_state}), 200


@app.route('/api/images/<image_id>/description', methods=['GET', 'PUT'])
def image_description(image_id):
    """Get/set image description stored in photo/description/<id>.txt."""
    guard = _require_image_access_or_404(image_id)
    if guard and request.method == 'GET':
        return guard

    files = _find_files_by_id(image_id)
    if not files:
        return jsonify({'error': 'Image not found'}), 404

    if request.method == 'GET':
        return jsonify({'id': image_id, 'description': _load_description(image_id)}), 200

    auth = _require_admin()
    if auth:
        return auth

    body = request.get_json(silent=True) or {}
    desc = body.get('description', '')
    if desc is None:
        desc = ''
    _save_description(image_id, str(desc))
    return jsonify({'id': image_id, 'description': _load_description(image_id)}), 200


@app.route('/api/images/<image_id>', methods=['GET'])
def get_image(image_id):
    guard = _require_image_access_or_404(image_id)
    if guard:
        return guard

    payload = _build_image_payload(image_id)
    if not payload:
        return jsonify({'error': 'Image not found'}), 404
    return jsonify(payload), 200


@app.route('/api/images/<image_id>', methods=['DELETE'])
def delete_image(image_id):
    """Delete an image by id (removes all matching format files)."""
    auth = _require_admin()
    if auth:
        return auth

    files = _find_files_by_id(image_id)
    if not files:
        return jsonify({'error': 'Image not found'}), 404

    deleted = []
    for p in files:
        try:
            deleted.append(str(p.relative_to(PHOTO_DIR)))
            p.unlink()
        except Exception:
            pass

    meta = _load_meta()
    if isinstance(meta, dict):
        pinned_map = meta.get("pinned")
        if isinstance(pinned_map, dict):
            pinned_map.pop(image_id, None)
            meta["pinned"] = pinned_map

        image_album = meta.get("image_album")
        if isinstance(image_album, dict):
            image_album.pop(image_id, None)
            meta["image_album"] = image_album

        similarity = meta.get("similarity_signatures")
        if isinstance(similarity, dict):
            similarity.pop(image_id, None)
            meta["similarity_signatures"] = similarity

        _save_meta(meta)

    try:
        _description_path(image_id).unlink(missing_ok=True)
    except Exception:
        pass

    try:
        _thumb_path(image_id).unlink(missing_ok=True)
    except Exception:
        pass

    try:
        _download_jpg_path(image_id).unlink(missing_ok=True)
    except Exception:
        pass

    return jsonify({'id': image_id, 'deleted': deleted}), 200


@app.route('/api/images/<image_id>/download', methods=['GET'])
def download_image(image_id):
    """Download an image as AVIF or JPG (JPG is converted from AVIF/WebP)."""
    guard = _require_image_access_or_404(image_id)
    if guard:
        return guard

    files = _find_files_by_id(image_id)
    if not files:
        return jsonify({'error': 'Image not found'}), 404

    fmt = (request.args.get('format') or 'avif').lower()
    if fmt not in ('avif', 'jpg'):
        return jsonify({'error': 'Invalid format. Use avif|jpg'}), 400

    if fmt == 'avif':
        avif = next((p for p in files if p.suffix.lower() == '.avif'), None)
        if not avif or not avif.exists():
            return jsonify({'error': 'AVIF not available'}), 404
        return send_file(avif, as_attachment=True, download_name=f"{image_id}.avif")

    out = _download_jpg_path(image_id)
    if out.exists():
        return send_file(out, as_attachment=True, download_name=f"{image_id}.jpg")

    src = _pick_source_file(image_id)
    if not src or not src.exists():
        return jsonify({'error': 'Source not available'}), 404

    try:
        with Image.open(src) as img:
            img = _apply_exif_orientation(img)
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')

            tmp = out.with_suffix(out.suffix + '.tmp')
            img.save(tmp, format='JPEG', quality=92, optimize=True)
            tmp.replace(out)
    except Exception as e:
        return jsonify({'error': f'JPG conversion failed: {e}'}), 500

    return send_file(out, as_attachment=True, download_name=f"{image_id}.jpg")

@app.route('/api/images/<path:filename>', methods=['GET'])
def serve_image(filename):
    """Serve image files from photo directory"""
    from flask import send_from_directory
    file_path = PHOTO_DIR / filename
    if file_path.exists() and file_path.is_file():
        # enforce album access based on image id (filename stem)
        img_id = file_path.stem
        guard = _require_image_access_or_404(img_id)
        if guard:
            return guard
        return send_from_directory(PHOTO_DIR, filename)
    return jsonify({'error': 'Image not found'}), 404

@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'ok', 'message': 'Mio Gallery API is running'})

if __name__ == '__main__':
    app.run(debug=debug_mode, host='0.0.0.0', port=5088)

# Global error handler to log crashes and return consistent responses
@app.errorhandler(Exception)
def _handle_exception(e):
    # HTTP errors: log and return as JSON for API routes; preserve HTML for pages
    if isinstance(e, HTTPException):
        try:
            app.logger.warning("HTTPException %s on %s %s: %s", e.code, request.method, request.path, e.description)
        except Exception:
            pass
        if request.path.startswith('/api/'):
            return jsonify({'error': e.name.lower().replace(' ', '_'), 'message': e.description}), e.code
        return e

    # Unhandled exceptions: log stack trace
    try:
        app.logger.exception("Unhandled exception on %s %s", request.method, request.path)
    except Exception:
        pass

    if request.path.startswith('/api/'):
        return jsonify({'error': 'internal_error', 'message': 'Server error'}), 500
    return "Internal Server Error", 500
