# SFlix Stream Extractor API

A Render-deployable REST API that resolves TMDB/IMDB IDs to stream URLs from sflix.film.

---

## Deploy to Render

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → **New Web Service**
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` — just click **Deploy**

Or manually:
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60`
- **Python version:** 3.11

---

## Endpoints

### `GET /`
Returns API documentation and usage examples.

### `GET /health`
```json
{ "status": "ok", "catalog_entries": 1234 }
```

### `GET /catalog/reload`
Force-refreshes the catalog from GitHub. Catalog auto-refreshes every 6 hours.

### `GET /catalog/search?q=<id>`
Look up a catalog entry by IMDB or TMDB id.
```
GET /catalog/search?q=tt4052886
GET /catalog/search?q=63174
```

---

### `POST /extract`
Extract stream URLs for a movie or a single series episode.

**Body:**
```json
{
  "id": "tt4052886",
  "season": 1,
  "episode": 1,
  "prefer_lang": "en",
  "quality": "best",
  "force_movie": null
}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | string | — | IMDB id (`tt…`) or TMDB id (numeric) |
| `url` | string | — | Direct sflix.film URL (alternative to `id`) |
| `season` | int | 1 | Season number (series only) |
| `episode` | int | 1 | Episode number (series only) |
| `prefer_lang` | string | auto | Language code e.g. `"en"`, `"hi"`, `"bn"` |
| `quality` | string | `"best"` | `"best"`, `"lowest"`, or exact e.g. `"720"` |
| `force_movie` | bool/null | `null` | `true`=movie, `false`=series, `null`=auto |

**Response:**
```json
{
  "is_movie": true,
  "season": null,
  "episode": null,
  "best_mp4": {
    "resolution": 1080,
    "url": "https://..."
  },
  "mp4": [
    { "resolution": 1080, "codec": "h264", "vip_locked": false, "url": "https://..." },
    { "resolution": 720,  "codec": "h264", "vip_locked": false, "url": "https://..." }
  ],
  "dash": [],
  "hls":  [],
  "catalog": {
    "title": "Lucifer",
    "imdb_id": "tt4052886",
    "tmdb_id": "63174",
    "genre": "Crime,Drama,Fantasy",
    "imdb_rating": "8.0",
    "poster": "https://...",
    "main_url": "https://sflix.film/..."
  }
}
```

---

### `POST /extract/batch`
Extract streams for multiple episodes at once.

**Body:**
```json
{
  "id": "tt13443470",
  "season": 1,
  "episodes": [1, 2, 3, 4, 5],
  "quality": "best"
}
```

**Response:**
```json
{
  "season": 1,
  "catalog": { "title": "Wednesday", "main_url": "https://..." },
  "episodes": {
    "1": { "best_mp4": { "resolution": 1080, "url": "..." }, "mp4": [...] },
    "2": { "best_mp4": { "resolution": 720,  "url": "..." }, "mp4": [...] },
    "3": { "error": "no streams" }
  }
}
```

---

### `POST /dubs`
List all available language variants (dubs + subs) for a title.

**Body:**
```json
{ "id": "tt4574334" }
```

**Response:**
```json
{
  "title": "Stranger Things",
  "url": "https://sflix.film/...",
  "dubs": [
    { "lang_code": "en", "lang_name": "English", "type": "dub", "subject_id": "...", "detail_path": "..." },
    { "lang_code": "hi", "lang_name": "Hindi",   "type": "dub", "subject_id": "...", "detail_path": "..." }
  ]
}
```

---

## ID Format

The catalog `imdb_id/tmdb_id` field uses `"tt…/12345"` format. You can pass either:
- IMDB: `tt4052886`
- TMDB: `63174`

---

## Catalog Source

Loaded from:
```
https://raw.githubusercontent.com/khantara922-max/slfix_extract_data/refs/heads/main/data/all_data_part_1.json
```
Auto-refreshed every 6 hours. Force refresh via `GET /catalog/reload`.
