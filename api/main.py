from flask import Flask, request, jsonify, redirect, send_file, send_from_directory, session
from flask_cors import CORS
from PIL import Image
from pillow_heif import register_heif_opener
import os
import html
from PIL import ImageOps
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
import hashlib
import json
from werkzeug.utils import secure_filename
from PIL import ExifTags
from io import BytesIO
from werkzeug.exceptions import HTTPException

try:
    import pillow_avif  # noqa: F401
except Exception:
    pillow_avif = None

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
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'tiff', 'webp', 'heic', 'heif'}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
THUMB_MAX_BYTES = 50 * 1024  # 50KB


def _is_admin() -> bool:
        return bool(session.get("is_admin"))


def _require_admin():
        if not _is_admin():
                return jsonify({'error': 'Unauthorized'}), 401
        return None


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

    return {
        "id": image_id,
        "date": date_str,
        "datetime": dt_str,
        "thumb": f"/api/thumb/{image_id}.webp",
        "webp": _rel(webp),
        "avif": _rel(avif),
        "pinned": bool(pinned_map.get(image_id, False)),
        "description": _load_description(image_id),
    }

def convert_and_save_image(image_path, output_dir, base_name):
    """Convert image to WebP and AVIF formats, limiting output to ~1MB"""
    results = {}
    MAX_OUTPUT_SIZE = 1024 * 1024  # 1MB
    
    with Image.open(image_path) as img:
        img = _apply_exif_orientation(img)
        # Convert to RGB if necessary (for transparency handling)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create white background for transparent images
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Save as WebP with size limit
        webp_path = output_dir / f"{base_name}.webp"
        quality = 70
        while quality >= 50:
            buf = BytesIO()
            img.save(buf, 'WEBP', quality=quality, method=6)
            size = buf.tell()
            if size <= MAX_OUTPUT_SIZE or quality <= 50:
                webp_path.write_bytes(buf.getvalue())
                break
            quality -= 5
        results['webp'] = str(webp_path.relative_to(PHOTO_DIR))
        
        # Save as AVIF with size limit
        try:
            avif_path = output_dir / f"{base_name}.avif"
            quality = 80
            while quality >= 50:
                buf = BytesIO()
                img.save(buf, 'AVIF', quality=quality)
                size = buf.tell()
                if size <= MAX_OUTPUT_SIZE or quality <= 50:
                    avif_path.write_bytes(buf.getvalue())
                    break
                quality -= 5
            results['avif'] = str(avif_path.relative_to(PHOTO_DIR))
        except Exception as e:
            print(f"AVIF conversion failed: {e}. AVIF support may not be available.")
            results['avif'] = None
    
    return results

@app.route('/api/upload', methods=['POST'])
def upload_image():
    """
    POST API to upload images
    Accepts: multipart/form-data with 'image' or 'images' field
    Returns: JSON with uploaded image paths
    """
    auth = _require_admin()
    if auth:
        return auth

    if 'image' not in request.files and 'images' not in request.files:
        return jsonify({'error': 'No image file provided'}), 400
    
    # Handle both single and multiple file uploads
    files = request.files.getlist('images') if 'images' in request.files else [request.files['image']]
    
    results = []
    errors = []
    
    for file in files:
        if file.filename == '':
            errors.append({'filename': 'empty', 'error': 'No selected file'})
            continue
        
        if not allowed_file(file.filename):
            errors.append({'filename': file.filename, 'error': 'File type not allowed'})
            continue
        
        try:
            # Save original file temporarily
            temp_path = PHOTO_DIR / f"temp_{secure_filename(file.filename)}"
            file.save(temp_path)
            
            # Check file size
            if os.path.getsize(temp_path) > MAX_FILE_SIZE:
                os.remove(temp_path)
                errors.append({'filename': file.filename, 'error': 'File too large (max 50MB)'})
                continue
            
            upload_dt = datetime.now()
            exif_dt = _get_exif_datetime(temp_path)
            photo_date = exif_dt or upload_dt
            
            # Create directory structure: photo/YYYY/MM/
            year_month_dir = PHOTO_DIR / photo_date.strftime("%Y") / photo_date.strftime("%m")
            year_month_dir.mkdir(parents=True, exist_ok=True)
            
            # Generate unique filename using hash
            with open(temp_path, 'rb') as f:
                file_hash = hashlib.md5(f.read()).hexdigest()[:12]
            
            base_name = f"{photo_date.strftime('%Y%m%d_%H%M%S')}_{file_hash}"
            
            # Convert and save
            converted_paths = convert_and_save_image(temp_path, year_month_dir, base_name)

            # Persist datetime for display (EXIF preferred; upload time fallback)
            meta = _load_meta()
            if not isinstance(meta, dict):
                meta = {}
            dt_map = meta.get("datetime")
            if not isinstance(dt_map, dict):
                dt_map = {}
            dt_map[base_name] = photo_date.strftime("%Y-%m-%d %H:%M:%S")
            meta["datetime"] = dt_map
            _save_meta(meta)
            
            # Remove temp file
            os.remove(temp_path)
            
            results.append({
                'original_filename': file.filename,
                'date': photo_date.strftime("%Y-%m-%d"),
                'datetime': photo_date.strftime("%Y-%m-%d %H:%M:%S"),
                'webp': f"/api/images/{converted_paths['webp']}",
                'avif': f"/api/images/{converted_paths['avif']}" if converted_paths['avif'] else None
            })
            
        except Exception as e:
            if temp_path.exists():
                os.remove(temp_path)
            errors.append({'filename': file.filename, 'error': str(e)})
    
    response = {'uploaded': results}
    if errors:
        response['errors'] = errors
    
    status_code = 200 if results else 400
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
