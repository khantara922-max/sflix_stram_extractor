"""
SFlix Stream Extractor API — Render-deployable Flask app
=========================================================
Fetches stream URLs from sflix.film using TMDB/IMDB ID lookup
against a remote JSON catalog.
"""

import os
import json
import threading
import time
import urllib.parse
from urllib.parse import urlparse, parse_qs

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
DATA_URL = (
    "https://raw.githubusercontent.com/khantara922-max/"
    "slfix_extract_data/refs/heads/main/data/all_data_part_1.json"
)

H5_API = "https://h5-api.aoneroom.com"
SITE   = "https://sflix.film"

DEFAULT_LANG_CHAIN = [
    ("en", True),
    ("en", False),
    ("hi", True),
    ("hi", False),
]

BASE_HEADERS = {
    "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/150.0.0.0 Safari/537.36",
    "Accept":        "application/json",
    "Content-Type":  "application/json",
    "x-client-info": '{"timezone":"Asia/Dhaka"}',
    "x-source":      "",
}

# ─────────────────────────────────────────────
# Catalog cache (refreshed every 6 hours)
# ─────────────────────────────────────────────
_catalog: list = []
_catalog_lock  = threading.Lock()
_catalog_ts    = 0
CATALOG_TTL    = 6 * 3600


def _load_catalog(force: bool = False) -> list:
    global _catalog, _catalog_ts
    with _catalog_lock:
        if not force and _catalog and (time.time() - _catalog_ts < CATALOG_TTL):
            return _catalog
        try:
            r = requests.get(DATA_URL, timeout=30)
            r.raise_for_status()
            data = r.json()
            _catalog = data if isinstance(data, list) else []
            _catalog_ts = time.time()
            print(f"[catalog] Loaded {len(_catalog)} entries")
        except Exception as e:
            print(f"[catalog] Load failed: {e}")
        return _catalog


def _find_entry(query: str) -> dict | None:
    """
    Match an IMDB id (tt…) or TMDB id (numeric) against catalog entries.
    The catalog field 'imdb_id/tmdb_id' has format 'tt…/12345'.
    """
    q = query.strip().lower()
    catalog = _load_catalog()
    for entry in catalog:
        field = entry.get("imdb_id/tmdb_id", "") or ""
        parts = field.split("/")
        imdb_id = parts[0].strip().lower() if len(parts) > 0 else ""
        tmdb_id = parts[1].strip().lower() if len(parts) > 1 else ""
        if q == imdb_id or q == tmdb_id:
            return entry
    return None


# ─────────────────────────────────────────────
# SFlix extractor (stateless per-request)
# ─────────────────────────────────────────────
class SflixExtractor:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        self._token = None

    @staticmethod
    def parse_page_url(page_url: str):
        parsed     = urlparse(page_url)
        qs         = parse_qs(parsed.query)
        subject_id = qs.get("id", [None])[0]
        detail_path = [s for s in parsed.path.split("/") if s][-1]
        return subject_id, detail_path

    def get_detail(self, detail_path: str, referer_detail_path: str = None):
        ref_slug = referer_detail_path or detail_path
        try:
            r = self.session.get(
                f"{H5_API}/wefeed-h5api-bff/detail",
                params={"detailPath": detail_path},
                headers={
                    "Origin":  SITE,
                    "Referer": f"{SITE}/spa/videoPlayPage/movies/{ref_slug}",
                },
                timeout=20,
            )
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            print(f"[get_detail] request/parse failed: {e}")
            return None
        if body.get("code") != 0:
            print(f"[get_detail] non-zero code: {body.get('code')} msg={body.get('msg')}")
            return None
        # Token may come as a cookie OR inside the response body data
        self._token = (
            self.session.cookies.get("token")
            or (body.get("data") or {}).get("token")
            or body.get("token")
        )
        if not self._token:
            print("[get_detail] no token found in cookies or body")
            # Don't hard-fail — some endpoints don't require a token
        return body["data"]

    def _play_request(self, subject_id: str, detail_path: str, se: int, ep: int):
        # Build auth headers; omit if no token (let server decide)
        extra_headers: dict = {
            "Origin":  SITE,
            "Referer": (
                f"{SITE}/spa/videoPlayPage/movies/{detail_path}"
                f"?id={subject_id}&type=/movie/detail&lang=en"
            ),
            "x-source": "",
        }
        if self._token:
            mb_token_val = urllib.parse.quote(f'"{self._token}"')
            extra_headers["Authorization"] = f"Bearer {self._token}"
            extra_headers["Cookie"]        = f"mb_token={mb_token_val}"

        try:
            # BUG FIX: play endpoint lives on H5_API, not SITE
            r = self.session.get(
                f"{H5_API}/wefeed-h5api-bff/subject/play",
                params={
                    "subjectId":      subject_id,
                    "se":             se,
                    "ep":             ep,
                    "detailPath":     detail_path,
                    "streamSignType": 1,
                },
                headers=extra_headers,
                timeout=20,
            )
            r.raise_for_status()
            body = r.json()
        except Exception as e:
            print(f"[_play_request] request/parse failed sid={subject_id} se={se} ep={ep}: {e}")
            return None

        if body.get("code") != 0:
            print(f"[_play_request] non-zero code: {body.get('code')} msg={body.get('msg')}")
            return None
        data = body.get("data") or {}
        has_streams = (
            len(data.get("streams") or []) > 0
            or len(data.get("dash")    or []) > 0
            or len(data.get("hls")     or []) > 0
        )
        return data if has_streams else None

    def get_streams(self, subject_id: str, detail_path: str,
                    season: int = 1, episode: int = 1,
                    force_movie: bool | None = None):
        if not self._token:
            return None, None

        if force_movie is True:
            data = self._play_request(subject_id, detail_path, 0, 0)
            return (data, True) if data else (None, None)

        if force_movie is False:
            data = self._play_request(subject_id, detail_path, season, episode)
            return (data, False) if data else (None, None)

        # Auto-detect
        data = self._play_request(subject_id, detail_path, 0, 0)
        if data:
            return data, True
        data = self._play_request(subject_id, detail_path, season, episode)
        if data:
            return data, False

        return None, None

    @staticmethod
    def _resolve_lang(dubs: list, lang_chain: list) -> dict | None:
        for lang_code, require_dub in lang_chain:
            for d in dubs:
                if d.get("lanCode") != lang_code:
                    continue
                if require_dub and d.get("type") != 0:
                    continue
                return d
        return None

    def _setup(self, page_url: str, prefer_lang: str | None, lang_chain: list | None):
        subject_id, detail_path = self.parse_page_url(page_url)
        detail = self.get_detail(detail_path)
        if not detail:
            return None, None, None

        subject = detail.get("subject", {})
        chain = (
            [(prefer_lang, True), (prefer_lang, False)] if prefer_lang
            else (lang_chain or DEFAULT_LANG_CHAIN)
        )

        dubs  = subject.get("dubs", [])
        match = self._resolve_lang(dubs, chain)
        if match:
            old_path    = detail_path
            subject_id  = match["subjectId"]
            detail_path = match["detailPath"]
            detail = self.get_detail(detail_path, referer_detail_path=old_path)
            if not detail:
                return None, None, None

        return subject_id, detail_path, detail

    @staticmethod
    def _pick_best(streams: list, prefer_quality: str = "best") -> dict | None:
        if not streams:
            return None
        unlocked = [s for s in streams if not s.get("vipLocked")]
        pool = unlocked or streams
        if prefer_quality == "best":
            return max(pool, key=lambda s: int(s.get("resolutions", 0) or 0))
        if prefer_quality == "lowest":
            return min(pool, key=lambda s: int(s.get("resolutions", 0) or 0))
        match = next((s for s in pool if str(s.get("resolutions")) == str(prefer_quality)), None)
        return match or max(pool, key=lambda s: int(s.get("resolutions", 0) or 0))

    def extract_single(self, page_url: str,
                       season: int = 1,
                       episode: int = 1,
                       prefer_lang: str | None = None,
                       prefer_quality: str = "best",
                       force_movie: bool | None = None) -> dict:
        subject_id, detail_path, detail = self._setup(page_url, prefer_lang, None)
        if subject_id is None:
            return {"error": "detail fetch failed or no token"}

        play, is_movie = self.get_streams(
            subject_id, detail_path,
            season=season, episode=episode,
            force_movie=force_movie,
        )
        if play is None:
            return {"error": "no streams returned"}

        mp4_streams  = play.get("streams", [])
        dash_streams = play.get("dash",    [])
        hls_streams  = play.get("hls",     [])
        best         = self._pick_best(mp4_streams, prefer_quality)

        def _clean_mp4(s):
            return {
                "resolution": s.get("resolutions"),
                "codec":      s.get("codecName"),
                "vip_locked": bool(s.get("vipLocked")),
                "url":        s.get("url"),
            }

        def _clean_dash(s):
            return {
                "resolution":     s.get("resolutions"),
                "vip_locked":     bool(s.get("vipLocked")),
                "url":            s.get("url"),
                "sign_cookie":    s.get("signCookie"),
                "sign_header_key":s.get("signHeaderKey"),
            }

        def _clean_hls(s):
            return {"resolution": s.get("resolutions"), "url": s.get("url")}

        return {
            "is_movie": is_movie,
            "season":   None if is_movie else season,
            "episode":  None if is_movie else episode,
            "best_mp4": {
                "resolution": best.get("resolutions"),
                "url":        best["url"],
            } if best else None,
            "mp4":  [_clean_mp4(s) for s in mp4_streams],
            "dash": [_clean_dash(s) for s in dash_streams],
            "hls":  [_clean_hls(s)  for s in hls_streams],
        }

    def batch_episodes(self, page_url: str,
                       season: int,
                       episodes: list[int],
                       prefer_lang: str | None = None,
                       prefer_quality: str = "best") -> dict:
        subject_id, detail_path, _ = self._setup(page_url, prefer_lang, None)
        if subject_id is None:
            return {"error": "detail fetch failed"}

        results = {}
        for ep in episodes:
            play, _ = self.get_streams(
                subject_id, detail_path,
                season=season, episode=ep,
                force_movie=False,
            )
            if play:
                best = self._pick_best(play.get("streams", []), prefer_quality)
                results[ep] = {
                    "best_mp4": {"resolution": best.get("resolutions"), "url": best["url"]} if best else None,
                    "mp4":  [{"resolution": s.get("resolutions"), "url": s.get("url"),
                              "vip_locked": bool(s.get("vipLocked"))} for s in play.get("streams", [])],
                    "dash": [{"resolution": s.get("resolutions"), "url": s.get("url")} for s in play.get("dash", [])],
                    "hls":  [{"resolution": s.get("resolutions"), "url": s.get("url")} for s in play.get("hls", [])],
                }
            else:
                results[ep] = {"error": "no streams"}

        return results

    def list_dubs(self, page_url: str) -> list:
        _, detail_path = self.parse_page_url(page_url)
        detail = self.get_detail(detail_path)
        if not detail:
            return []
        dubs = detail.get("subject", {}).get("dubs", [])
        return [
            {
                "lang_code":   d.get("lanCode"),
                "lang_name":   d.get("lanName"),
                "type":        "dub" if d.get("type") == 0 else "sub",
                "subject_id":  d.get("subjectId"),
                "detail_path": d.get("detailPath"),
            }
            for d in dubs
        ]


# ─────────────────────────────────────────────
# Flask app
# ─────────────────────────────────────────────
app = Flask(__name__)
CORS(app)


# Global error handlers — ensure all errors return JSON, never HTML
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "bad request", "detail": str(e)}), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "internal server error", "detail": str(e)}), 500

@app.errorhandler(Exception)
def unhandled(e):
    import traceback
    traceback.print_exc()
    return jsonify({"error": "unexpected error", "detail": str(e)}), 500


def _err(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _get_entry_or_url(req_data: dict):
    """
    Resolve the target entry from request data.
    Accepts:
      - id: IMDB or TMDB id (looks up catalog)
      - url: direct sflix.film URL (skips catalog)
    Returns (entry_or_None, url_str, error_response_or_None)
    """
    id_query = req_data.get("id", "").strip()
    direct   = req_data.get("url", "").strip()

    if id_query:
        entry = _find_entry(id_query)
        if not entry:
            return None, None, _err(f"No catalog entry found for id='{id_query}'", 404)
        return entry, entry["main_url"], None

    if direct:
        return None, direct, None

    return None, None, _err("Provide 'id' (IMDB/TMDB) or 'url'")


# ── GET /health ──────────────────────────────
@app.get("/health")
def health():
    return jsonify({"status": "ok", "catalog_entries": len(_load_catalog())})


# ── GET /catalog/reload ──────────────────────
@app.get("/catalog/reload")
def catalog_reload():
    data = _load_catalog(force=True)
    return jsonify({"loaded": len(data)})


# ── GET /catalog/search?q=<imdb|tmdb> ────────
@app.get("/catalog/search")
def catalog_search():
    q = request.args.get("q", "").strip()
    if not q:
        return _err("'q' param required")
    entry = _find_entry(q)
    if not entry:
        return _err(f"Not found: {q}", 404)
    return jsonify(entry)


# ── POST /extract ─────────────────────────────
# Body (JSON):
#   id           : IMDB or TMDB id       (mutually exclusive with url)
#   url          : direct sflix.film URL
#   season       : int (default 1, series only)
#   episode      : int (default 1, series only)
#   prefer_lang  : str (e.g. "en", "hi") default auto
#   quality      : "best" | "lowest" | "720" etc.
#   force_movie  : true | false | null (default null = auto-detect)
@app.post("/extract")
def extract():
    body = request.get_json(silent=True) or {}
    entry, url, err = _get_entry_or_url(body)
    if err:
        return err

    season      = int(body.get("season",  1))
    episode     = int(body.get("episode", 1))
    prefer_lang = body.get("prefer_lang") or None
    quality     = body.get("quality", "best")
    fm_raw      = body.get("force_movie")
    force_movie = None if fm_raw is None else bool(fm_raw)

    ex     = SflixExtractor()
    result = ex.extract_single(
        url,
        season=season,
        episode=episode,
        prefer_lang=prefer_lang,
        prefer_quality=quality,
        force_movie=force_movie,
    )

    if "error" in result:
        return jsonify({"error": result["error"], "entry": entry}), 502

    if entry:
        result["catalog"] = {
            "title":       entry.get("title"),
            "imdb_id":     (entry.get("imdb_id/tmdb_id") or "").split("/")[0],
            "tmdb_id":     (entry.get("imdb_id/tmdb_id") or "").split("/")[1] if "/" in (entry.get("imdb_id/tmdb_id") or "") else None,
            "genre":       entry.get("genre"),
            "imdb_rating": entry.get("imdbRatingValue"),
            "poster":      entry.get("url"),
            "main_url":    entry.get("main_url"),
        }

    return jsonify(result)


# ── POST /extract/batch ───────────────────────
# Body (JSON):
#   id or url     : as above
#   season        : int (required)
#   episodes      : list[int]  e.g. [1,2,3]
#   prefer_lang   : optional
#   quality       : optional
@app.post("/extract/batch")
def extract_batch():
    body = request.get_json(silent=True) or {}
    entry, url, err = _get_entry_or_url(body)
    if err:
        return err

    season   = int(body.get("season", 1))
    episodes = body.get("episodes")
    if not episodes or not isinstance(episodes, list):
        return _err("'episodes' must be a non-empty list of integers")
    episodes = sorted(set(int(e) for e in episodes if str(e).isdigit()))
    if not episodes:
        return _err("No valid episode numbers")

    prefer_lang = body.get("prefer_lang") or None
    quality     = body.get("quality", "best")

    ex      = SflixExtractor()
    results = ex.batch_episodes(
        url,
        season=season,
        episodes=episodes,
        prefer_lang=prefer_lang,
        prefer_quality=quality,
    )

    if "error" in results:
        return jsonify(results), 502

    payload = {
        "season":   season,
        "episodes": results,
    }
    if entry:
        payload["catalog"] = {
            "title":    entry.get("title"),
            "main_url": entry.get("main_url"),
        }

    return jsonify(payload)


# ── POST /dubs ────────────────────────────────
# Body: id or url
@app.post("/dubs")
def dubs():
    body  = request.get_json(silent=True) or {}
    entry, url, err = _get_entry_or_url(body)
    if err:
        return err

    ex   = SflixExtractor()
    data = ex.list_dubs(url)
    return jsonify({
        "dubs":  data,
        "title": entry.get("title") if entry else None,
        "url":   url,
    })


# ── GET / — Web UI ───────────────────────────
@app.get("/")
def index():
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SFlix Stream Extractor</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap');

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #0c0d10;
    --surface:   #13151b;
    --border:    #1f2230;
    --accent:    #e8b84b;
    --accent-dim:#7a5e1a;
    --text:      #dde1ec;
    --muted:     #5a6072;
    --green:     #3ecf6a;
    --red:       #e05c5c;
    --blue:      #5b9cf6;
    --radius:    10px;
  }

  html { font-size: 15px; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'DM Sans', system-ui, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 40px 16px 80px;
  }

  /* ── Header ── */
  header {
    text-align: center;
    margin-bottom: 40px;
  }
  .logo {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }
  .logo svg { width: 32px; height: 32px; }
  .logo-text {
    font-size: 1.55rem;
    font-weight: 600;
    letter-spacing: -0.5px;
    color: #fff;
  }
  .logo-text span { color: var(--accent); }
  header p {
    color: var(--muted);
    font-size: 0.88rem;
    letter-spacing: 0.02em;
  }

  /* ── Card ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 28px;
    width: 100%;
    max-width: 620px;
    margin-bottom: 20px;
  }
  .card-title {
    font-size: 0.72rem;
    font-weight: 500;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 18px;
  }

  /* ── Form controls ── */
  .row { display: flex; gap: 10px; flex-wrap: wrap; }
  .field { display: flex; flex-direction: column; gap: 6px; flex: 1; min-width: 140px; }
  .field label { font-size: 0.8rem; color: var(--muted); font-weight: 500; }
  input, select {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    font-family: inherit;
    font-size: 0.9rem;
    padding: 9px 12px;
    outline: none;
    transition: border-color 0.15s;
    width: 100%;
  }
  input:focus, select:focus { border-color: var(--accent); }
  input::placeholder { color: var(--muted); }
  select option { background: var(--surface); }

  /* type toggle */
  .toggle-row {
    display: flex;
    gap: 0;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }
  .toggle-row label {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 8px;
    cursor: pointer;
    font-size: 0.85rem;
    color: var(--muted);
    transition: background 0.15s, color 0.15s;
    user-select: none;
  }
  .toggle-row input[type=radio] { display: none; }
  .toggle-row input[type=radio]:checked + span { color: var(--accent); }
  .toggle-row label:has(input:checked) {
    background: #1a1c25;
    color: var(--accent);
  }

  /* series fields */
  #series-fields {
    display: none;
    margin-top: 12px;
  }
  #series-fields.visible { display: flex; }

  /* submit */
  .btn {
    width: 100%;
    padding: 12px;
    margin-top: 18px;
    background: var(--accent);
    color: #0c0d10;
    border: none;
    border-radius: 6px;
    font-family: inherit;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
    transition: opacity 0.15s, transform 0.1s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
  }
  .btn:hover:not(:disabled) { opacity: 0.9; transform: translateY(-1px); }
  .btn:disabled { opacity: 0.45; cursor: not-allowed; transform: none; }

  /* ── Spinner ── */
  .spinner {
    width: 18px; height: 18px;
    border: 2px solid rgba(0,0,0,0.3);
    border-top-color: #0c0d10;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    display: none;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Results ── */
  #results { width: 100%; max-width: 620px; display: none; }

  .meta-row {
    display: flex;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 20px;
  }
  .poster {
    width: 72px;
    height: 104px;
    object-fit: cover;
    border-radius: 6px;
    border: 1px solid var(--border);
    flex-shrink: 0;
    background: var(--border);
  }
  .meta-info { flex: 1; min-width: 0; }
  .meta-title { font-size: 1.15rem; font-weight: 600; margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .meta-badges { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .badge {
    font-size: 0.72rem;
    padding: 3px 8px;
    border-radius: 4px;
    background: var(--border);
    color: var(--muted);
    font-family: 'DM Mono', monospace;
  }
  .badge.green { background: rgba(62,207,106,0.12); color: var(--green); }
  .badge.gold  { background: rgba(232,184,75,0.12);  color: var(--accent); }
  .badge.blue  { background: rgba(91,156,246,0.12);  color: var(--blue); }

  /* best stream highlight */
  .best-stream {
    background: linear-gradient(135deg, rgba(232,184,75,0.08) 0%, rgba(232,184,75,0.02) 100%);
    border: 1px solid var(--accent-dim);
    border-radius: var(--radius);
    padding: 16px 18px;
    margin-bottom: 14px;
  }
  .best-label {
    font-size: 0.7rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--accent);
    font-weight: 500;
    margin-bottom: 8px;
  }
  .best-res { font-size: 1.5rem; font-weight: 600; color: #fff; margin-bottom: 8px; }
  .stream-url {
    font-family: 'DM Mono', monospace;
    font-size: 0.75rem;
    color: var(--muted);
    word-break: break-all;
    margin-bottom: 10px;
  }
  .copy-btn {
    background: var(--accent);
    color: #0c0d10;
    border: none;
    border-radius: 5px;
    font-family: inherit;
    font-size: 0.8rem;
    font-weight: 600;
    padding: 7px 14px;
    cursor: pointer;
    transition: opacity 0.15s;
    margin-right: 8px;
  }
  .copy-btn:hover { opacity: 0.85; }
  .open-btn {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 5px;
    font-family: inherit;
    font-size: 0.8rem;
    padding: 7px 14px;
    cursor: pointer;
    transition: border-color 0.15s;
    text-decoration: none;
    display: inline-block;
  }
  .open-btn:hover { border-color: var(--accent); color: var(--accent); }

  /* stream list */
  .streams-section { margin-top: 16px; }
  .streams-label {
    font-size: 0.72rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--muted);
    font-weight: 500;
    margin-bottom: 10px;
  }
  .stream-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 7px;
    transition: border-color 0.15s;
  }
  .stream-item:hover { border-color: var(--accent-dim); }
  .stream-res {
    font-family: 'DM Mono', monospace;
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--text);
    min-width: 50px;
  }
  .stream-codec {
    font-size: 0.75rem;
    color: var(--muted);
    flex: 1;
  }
  .stream-actions { display: flex; gap: 6px; }
  .icon-btn {
    background: var(--border);
    border: none;
    border-radius: 4px;
    color: var(--muted);
    cursor: pointer;
    padding: 5px 8px;
    font-size: 0.75rem;
    transition: background 0.15s, color 0.15s;
  }
  .icon-btn:hover { background: var(--accent); color: #0c0d10; }
  .vip-tag {
    font-size: 0.68rem;
    padding: 2px 6px;
    background: rgba(224,92,92,0.12);
    color: var(--red);
    border-radius: 3px;
  }

  /* error */
  .error-box {
    border: 1px solid rgba(224,92,92,0.3);
    background: rgba(224,92,92,0.07);
    border-radius: var(--radius);
    padding: 16px;
    color: var(--red);
    font-size: 0.88rem;
  }

  /* divider */
  hr { border: none; border-top: 1px solid var(--border); margin: 18px 0; }

  /* toast */
  .toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: var(--green);
    color: #0c0d10;
    padding: 10px 18px;
    border-radius: 6px;
    font-size: 0.85rem;
    font-weight: 600;
    opacity: 0;
    transform: translateY(8px);
    transition: opacity 0.2s, transform 0.2s;
    pointer-events: none;
    z-index: 999;
  }
  .toast.show { opacity: 1; transform: translateY(0); }

  @media (max-width: 480px) {
    .meta-row { flex-direction: column; }
    .poster { width: 100%; height: 160px; }
  }
</style>
</head>
<body>

<header>
  <div class="logo">
    <svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="32" height="32" rx="8" fill="#e8b84b"/>
      <path d="M8 10.5C8 9.67 8.67 9 9.5 9h4L11 14h3.5L10 23l10-11h-4.5L18 7H9.5C8.67 7 8 7.67 8 8.5v22" fill="none"/>
      <polygon points="10,23 20,12 15.5,12 18,7 9.5,7 9.5,9 13.5,9 11,14 14.5,14" fill="#0c0d10"/>
    </svg>
    <span class="logo-text">S<span>Flix</span> Extractor</span>
  </div>
  <p>Resolve IMDB / TMDB IDs to direct stream URLs</p>
</header>

<!-- Search card -->
<div class="card">
  <div class="card-title">Find streams</div>

  <div class="field" style="margin-bottom:14px">
    <label for="id-input">IMDB ID or TMDB ID</label>
    <input id="id-input" type="text" placeholder="e.g. tt4052886 or 63174" autocomplete="off" spellcheck="false">
  </div>

  <div class="field" style="margin-bottom:14px">
    <label>Content type</label>
    <div class="toggle-row">
      <label><input type="radio" name="ctype" value="auto" checked><span>Auto-detect</span></label>
      <label><input type="radio" name="ctype" value="movie"><span>Movie</span></label>
      <label><input type="radio" name="ctype" value="series"><span>Series</span></label>
    </div>
  </div>

  <div class="row" id="series-fields">
    <div class="field">
      <label for="season-input">Season</label>
      <input id="season-input" type="number" min="1" value="1">
    </div>
    <div class="field">
      <label for="episode-input">Episode</label>
      <input id="episode-input" type="number" min="1" value="1">
    </div>
  </div>

  <div class="row" style="margin-top:14px">
    <div class="field">
      <label for="quality-select">Quality</label>
      <select id="quality-select">
        <option value="best">Best available</option>
        <option value="1080">1080p</option>
        <option value="720">720p</option>
        <option value="480">480p</option>
        <option value="lowest">Lowest</option>
      </select>
    </div>
    <div class="field">
      <label for="lang-input">Language (optional)</label>
      <input id="lang-input" type="text" placeholder="e.g. en, hi, bn">
    </div>
  </div>

  <button class="btn" id="extract-btn" onclick="doExtract()">
    <span id="btn-text">Extract Streams</span>
    <div class="spinner" id="btn-spinner"></div>
  </button>
</div>

<!-- Results -->
<div id="results">
  <div class="card" id="result-card"></div>
</div>

<div class="toast" id="toast">Copied!</div>

<script>
  // Toggle series fields
  document.querySelectorAll('input[name=ctype]').forEach(r => {
    r.addEventListener('change', () => {
      const sf = document.getElementById('series-fields');
      sf.classList.toggle('visible', r.value === 'series');
    });
  });

  // Enter key submits
  document.getElementById('id-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') doExtract();
  });

  function setLoading(on) {
    const btn = document.getElementById('extract-btn');
    const txt = document.getElementById('btn-text');
    const spin = document.getElementById('btn-spinner');
    btn.disabled = on;
    txt.textContent = on ? 'Extracting…' : 'Extract Streams';
    spin.style.display = on ? 'block' : 'none';
  }

  async function doExtract() {
    const id = document.getElementById('id-input').value.trim();
    if (!id) {
      document.getElementById('id-input').focus();
      return;
    }

    const ctype = document.querySelector('input[name=ctype]:checked').value;
    const quality = document.getElementById('quality-select').value;
    const lang = document.getElementById('lang-input').value.trim();
    const season = parseInt(document.getElementById('season-input').value) || 1;
    const episode = parseInt(document.getElementById('episode-input').value) || 1;

    const body = { id, quality };
    if (lang) body.prefer_lang = lang;
    if (ctype === 'movie')  body.force_movie = true;
    if (ctype === 'series') { body.force_movie = false; body.season = season; body.episode = episode; }
    if (ctype === 'auto' && document.getElementById('series-fields').classList.contains('visible')) {
      body.season = season; body.episode = episode;
    }

    setLoading(true);
    const resultsEl = document.getElementById('results');
    resultsEl.style.display = 'none';

    try {
      const resp = await fetch('/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      renderResult(data, resp.ok);
    } catch(e) {
      renderError('Network error: ' + e.message);
    } finally {
      setLoading(false);
    }
  }

  function renderResult(data, ok) {
    const card = document.getElementById('result-card');
    const results = document.getElementById('results');

    if (!ok || data.error) {
      card.innerHTML = `<div class="error-box">
        <strong>Error:</strong> ${escHtml(data.error || 'Unknown error')}
      </div>`;
      results.style.display = 'block';
      return;
    }

    const cat = data.catalog || {};
    const isMovie = data.is_movie;
    const mp4 = data.mp4 || [];
    const best = data.best_mp4;

    let html = '';

    // Meta row
    if (cat.title || cat.poster) {
      html += `<div class="meta-row">`;
      if (cat.poster) {
        html += `<img class="poster" src="${escHtml(cat.poster)}" alt="poster" onerror="this.style.display='none'">`;
      }
      html += `<div class="meta-info">`;
      if (cat.title) html += `<div class="meta-title">${escHtml(cat.title)}</div>`;
      html += `<div class="meta-badges">`;
      html += `<span class="badge ${isMovie ? 'blue' : 'green'}">${isMovie ? 'Movie' : 'Series'}</span>`;
      if (!isMovie && data.season != null) html += `<span class="badge">S${data.season} E${data.episode}</span>`;
      if (cat.imdb_rating) html += `<span class="badge gold">★ ${escHtml(cat.imdb_rating)}</span>`;
      if (cat.imdb_id)  html += `<span class="badge">${escHtml(cat.imdb_id)}</span>`;
      if (cat.genre) {
        cat.genre.split(',').slice(0,3).forEach(g => {
          html += `<span class="badge">${escHtml(g.trim())}</span>`;
        });
      }
      html += `</div></div></div>`;
    }

    // Best stream
    if (best) {
      html += `<div class="best-stream">
        <div class="best-label">Best stream</div>
        <div class="best-res">${best.resolution ? best.resolution + 'p' : 'Unknown'}</div>
        <div class="stream-url">${escHtml(best.url)}</div>
        <button class="copy-btn" onclick="copyUrl('${escAttr(best.url)}', this)">Copy URL</button>
        <a class="open-btn" href="${escHtml(best.url)}" target="_blank" rel="noopener">Open link ↗</a>
      </div>`;
    } else {
      html += `<div class="error-box">No streams found for this title.</div>`;
    }

    // All mp4 streams
    if (mp4.length > 1) {
      html += `<div class="streams-section">
        <div class="streams-label">All MP4 streams (${mp4.length})</div>`;
      mp4.forEach(s => {
        html += `<div class="stream-item">
          <span class="stream-res">${s.resolution ? s.resolution + 'p' : '—'}</span>
          <span class="stream-codec">${escHtml(s.codec || 'mp4')}</span>
          ${s.vip_locked ? '<span class="vip-tag">VIP</span>' : ''}
          <div class="stream-actions">
            <button class="icon-btn" onclick="copyUrl('${escAttr(s.url)}', this)" title="Copy URL">⎘ Copy</button>
            <a class="icon-btn" href="${escHtml(s.url)}" target="_blank" rel="noopener" title="Open">↗</a>
          </div>
        </div>`;
      });
      html += `</div>`;
    }

    // HLS / DASH
    if ((data.hls || []).length > 0) {
      html += `<hr><div class="streams-section"><div class="streams-label">HLS streams</div>`;
      (data.hls || []).forEach(s => {
        html += `<div class="stream-item">
          <span class="stream-res">${s.resolution ? s.resolution + 'p' : 'HLS'}</span>
          <span class="stream-codec">m3u8</span>
          <div class="stream-actions">
            <button class="icon-btn" onclick="copyUrl('${escAttr(s.url)}', this)">⎘ Copy</button>
            <a class="icon-btn" href="${escHtml(s.url)}" target="_blank" rel="noopener">↗</a>
          </div>
        </div>`;
      });
      html += `</div>`;
    }

    if ((data.dash || []).length > 0) {
      html += `<hr><div class="streams-section"><div class="streams-label">DASH streams</div>`;
      (data.dash || []).forEach(s => {
        html += `<div class="stream-item">
          <span class="stream-res">${s.resolution ? s.resolution + 'p' : 'DASH'}</span>
          <span class="stream-codec">mpd</span>
          <div class="stream-actions">
            <button class="icon-btn" onclick="copyUrl('${escAttr(s.url)}', this)">⎘ Copy</button>
            <a class="icon-btn" href="${escHtml(s.url)}" target="_blank" rel="noopener">↗</a>
          </div>
        </div>`;
      });
      html += `</div>`;
    }

    card.innerHTML = html;
    results.style.display = 'block';
  }

  function renderError(msg) {
    const card = document.getElementById('result-card');
    card.innerHTML = `<div class="error-box"><strong>Error:</strong> ${escHtml(msg)}</div>`;
    document.getElementById('results').style.display = 'block';
  }

  function copyUrl(url, el) {
    navigator.clipboard.writeText(url).then(() => {
      const t = document.getElementById('toast');
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 1800);
    });
  }

  function escHtml(s) {
    return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function escAttr(s) {
    return String(s || '').replace(/'/g, "\\'");
  }
</script>
</body>
</html>"""
    from flask import Response
    return Response(html, mimetype='text/html')


# ── GET /api-docs — JSON API documentation ───
@app.get("/api-docs")
def api_docs():
    return jsonify({
        "name":    "SFlix Stream Extractor API",
        "version": "1.0.0",
        "endpoints": {
            "GET  /health":            "Health check + catalog entry count",
            "GET  /catalog/reload":    "Force reload the remote JSON catalog",
            "GET  /catalog/search?q=": "Lookup catalog entry by IMDB or TMDB id",
            "POST /extract":           "Extract streams for a movie or single episode",
            "POST /extract/batch":     "Extract streams for multiple episodes",
            "POST /dubs":              "List available language dubs/subs",
        },
        "body_params": {
            "id":          "IMDB id (tt…) or TMDB id (numeric) — matches catalog",
            "url":         "Direct sflix.film URL (alternative to id)",
            "season":      "Season number (default 1)",
            "episode":     "Episode number (default 1)",
            "episodes":    "List of episode numbers for /extract/batch",
            "prefer_lang": "Language code e.g. 'en', 'hi' (default: auto)",
            "quality":     "'best', 'lowest', or resolution e.g. '720' (default: best)",
            "force_movie": "true=force movie, false=force series, null=auto-detect",
        },
        "examples": {
            "movie_by_imdb":    {"id": "tt4052886"},
            "movie_by_tmdb":    {"id": "63174"},
            "series_episode":   {"id": "tt4574334", "season": 1, "episode": 3},
            "batch_episodes":   {"id": "tt13443470", "season": 1, "episodes": [1, 2, 3, 4, 5]},
            "direct_url":       {"url": "https://sflix.film/spa/videoPlayPage/movies/lucifer-UQASHYbVPB2"},
        },
    })


# ─────────────────────────────────────────────
# Startup: pre-load catalog in background
# ─────────────────────────────────────────────
threading.Thread(target=_load_catalog, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
