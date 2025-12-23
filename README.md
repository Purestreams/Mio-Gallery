![main page](image.png)

Demo: [https://img.amiya.eu.org/](https://img.amiya.eu.org/) (no upload access)

# Mio-Gallery

A small self-hosted photo gallery (Flask + static HTML) with:
- Grid gallery + lightbox viewer
- Admin “Manage” page for upload, pin/unpin, description edits, and delete
- Automatic WebP + AVIF conversion on upload
- Lazy-generated WebP thumbnails (~50KB target)
- Optional downloads as AVIF or JPG

## Repo layout

- `api/main.py` — Flask app + JSON APIs
- `api/page/index.html` — gallery UI (`/`)
- `api/page/manage.html` — admin UI (`/manage`)
- `api/requirements.txt` — Python dependencies
- `photo/` — stored images + metadata
  - `photo/YYYY/MM/` — images stored as `*.webp` and (if available) `*.avif`
  - `photo/thumb/` — generated thumbnails (`*.webp`)
  - `photo/download/` — cached JPG conversions (`*.jpg`)
  - `photo/description/` — per-image descriptions (`<id>.txt`)
  - `photo/.meta.json` — pinned state + captured datetimes

Note: this repo’s `.gitignore` ignores `photo/` by default.

## Requirements

- Python 3.x
- macOS/Linux/Windows supported (uses Pillow for image processing)

## Run locally

```zsh
cd api
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Then open:
- Gallery: `http://localhost:5088/`
- Admin: `http://localhost:5088/manage` (redirects to login)

## Admin / security

The admin page uses a simple session cookie.

Environment variables:
- `MIO_GALLERY_PASSWORD` — manage password (default: `zhu123`)
- `MIO_GALLERY_SECRET` — Flask session secret (default: `dev-secret-change-me`)

Example:

```zsh
export MIO_GALLERY_PASSWORD='change-me'
export MIO_GALLERY_SECRET='a-long-random-secret'
python api/main.py
```

## How uploads are stored

On upload, files are:
- Temporarily written under `photo/` and validated (type + size)
- Converted to `WebP` and (when supported) `AVIF`
- Saved under `photo/YYYY/MM/` using an id like `YYYYMMDD_HHMMSS_<hash>`
- Datetime saved to `photo/.meta.json` (EXIF preferred; upload time fallback)

## HTTP routes

Pages:
- `GET /` — gallery UI
- `GET /manage` — manage UI (requires admin session)
- `GET|POST /manage-login` — login form

APIs:
- `GET /api/health`
- `GET /api/images?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`
- `POST /api/upload` — `multipart/form-data` with `image` or `images`
- `PUT /api/images/<id>/pin` — JSON `{ "pinned": true|false }` (or toggle if omitted)
- `GET|PUT /api/images/<id>/description` — JSON `{ "description": "..." }`
- `DELETE /api/images/<id>`
- `GET /api/images/<id>/download?format=avif|jpg`
- `GET /api/images/<path:filename>` — serves files from `photo/`
- `GET /api/thumb/<id>.webp` — thumbnail (generated on demand)
