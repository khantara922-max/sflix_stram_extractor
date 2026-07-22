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
        if body.get("code") != 0:
            return None
        self._token = self.session.cookies.get("token")
        if not self._token:
            return None
        return body["data"]

    def _play_request(self, subject_id: str, detail_path: str, se: int, ep: int):
        if not self._token:
            return None
        mb_token_val = urllib.parse.quote(f'"{self._token}"')
        r = self.session.get(
            f"{SITE}/wefeed-h5api-bff/subject/play",
            params={
                "subjectId":      subject_id,
                "se":             se,
                "ep":             ep,
                "detailPath":     detail_path,
                "streamSignType": 1,
            },
            headers={
                "Authorization": f"Bearer {self._token}",
                "Cookie":        f"mb_token={mb_token_val}",
                "Origin":        SITE,
                "Referer": (
                    f"{SITE}/spa/videoPlayPage/movies/{detail_path}"
                    f"?id={subject_id}&type=/movie/detail&lang=en"
                ),
                "x-source": "",
            },
            timeout=20,
        )
        body = r.json()
        if body.get("code") != 0:
            return None
        data = body["data"]
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


# ── GET / — API docs ─────────────────────────
@app.get("/")
def index():
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
