/**
 * sflix-stream-extractor — Render-deployable Express server
 * Serves the web UI and proxies all API calls to sflix.film / h5-api.aoneroom.com
 */

const express  = require("express");
const fetch    = require("node-fetch");
const cors     = require("cors");
const path     = require("path");
const { URL, URLSearchParams } = require("url");

const app  = express();
const PORT = process.env.PORT || 3000;

// ─── Middleware ────────────────────────────────────────────────────────────────
app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, "../public")));

// ─── Constants ────────────────────────────────────────────────────────────────
const H5_API    = "https://h5-api.aoneroom.com";
const SITE      = "https://sflix.film";
const DATA_URL  = "https://raw.githubusercontent.com/khantara922-max/slfix_extract_data/refs/heads/main/data/all_data_part_1.json";

const BASE_HEADERS = {
  "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
  "Accept":        "application/json",
  "Content-Type":  "application/json",
  "x-client-info": '{"timezone":"Asia/Dhaka"}',
  "x-source":      "",
};

const DEFAULT_LANG_CHAIN = [
  { lang: "en", dub: true  },
  { lang: "en", dub: false },
  { lang: "hi", dub: true  },
  { lang: "hi", dub: false },
];

// ─── In-memory data cache ──────────────────────────────────────────────────────
let cachedData     = null;
let cacheTimestamp = 0;
const CACHE_TTL    = 60 * 60 * 1000; // 1 hour

async function fetchAllData() {
  const now = Date.now();
  if (cachedData && (now - cacheTimestamp) < CACHE_TTL) {
    return cachedData;
  }
  console.log("[data] Fetching catalogue from GitHub...");
  const res  = await fetch(DATA_URL, { headers: { "User-Agent": BASE_HEADERS["User-Agent"] } });
  if (!res.ok) throw new Error(`Failed to fetch data: ${res.status}`);
  cachedData     = await res.json();
  cacheTimestamp = now;
  console.log(`[data] Loaded ${cachedData.length} entries.`);
  return cachedData;
}

// ─── Helper: cookie jar per request ───────────────────────────────────────────
function makeCookieJar() {
  const jar = {};
  return {
    set(cookieStr) {
      // parse "key=val; Path=/; ..." style
      const pairs = cookieStr.split(";");
      const [k, v] = pairs[0].split("=");
      if (k && v !== undefined) jar[k.trim()] = v.trim();
    },
    get(key) { return jar[key] || null; },
    header() {
      return Object.entries(jar).map(([k, v]) => `${k}=${v}`).join("; ");
    },
  };
}

// ─── Helper: pick best stream ──────────────────────────────────────────────────
function pickBest(streams, quality = "best") {
  if (!streams || !streams.length) return null;
  const unlocked = streams.filter(s => !s.vipLocked);
  const pool     = unlocked.length ? unlocked : streams;

  if (quality === "lowest") {
    return pool.reduce((a, b) =>
      (parseInt(a.resolutions) || 0) < (parseInt(b.resolutions) || 0) ? a : b
    );
  }
  if (quality !== "best") {
    const exact = pool.find(s => String(s.resolutions) === String(quality));
    if (exact) return exact;
  }
  return pool.reduce((a, b) =>
    (parseInt(a.resolutions) || 0) > (parseInt(b.resolutions) || 0) ? a : b
  );
}

// ─── Helper: resolve preferred language from dubs list ────────────────────────
function resolveLang(dubs, chain) {
  if (!dubs || !chain) return null;
  for (const { lang, dub } of chain) {
    for (const d of dubs) {
      if (d.lanCode !== lang) continue;
      if (dub && d.type !== 0) continue;
      return d;
    }
  }
  return null;
}

// ─── Core extractor class (server-side, stateless per request) ────────────────
class SflixExtractor {
  constructor() {
    this.token  = null;
    this.cookie = null; // raw cookie header string
  }

  async getDetail(detailPath, refererDetailPath) {
    const refSlug = refererDetailPath || detailPath;
    const url     = `${H5_API}/wefeed-h5api-bff/detail?detailPath=${encodeURIComponent(detailPath)}`;

    const res = await fetch(url, {
      headers: {
        ...BASE_HEADERS,
        "Origin":  SITE,
        "Referer": `${SITE}/spa/videoPlayPage/movies/${refSlug}`,
      },
    });

    // Extract Set-Cookie header
    const setCookieHeader = res.headers.raw()["set-cookie"] || res.headers.raw()["Set-Cookie"] || [];
    let tokenCookie = null;

    for (const c of setCookieHeader) {
      const match = c.match(/(?:^|;\s*)token=([^;]+)/);
      if (match) { tokenCookie = match[1]; break; }
    }

    if (!tokenCookie) {
      // Try to extract from cookie header directly
      const cookieStr = Array.isArray(setCookieHeader)
        ? setCookieHeader.join("; ")
        : String(setCookieHeader);
      const m = cookieStr.match(/token=([^;,\s]+)/);
      if (m) tokenCookie = m[1];
    }

    if (tokenCookie) {
      this.token  = tokenCookie;
      this.cookie = `token=${tokenCookie}`;
    }

    const body = await res.json();
    if (body.code !== 0) throw new Error(`Detail API error: ${JSON.stringify(body)}`);

    return body.data;
  }

  async playRequest(subjectId, detailPath, se, ep) {
    if (!this.token) throw new Error("No token — call getDetail() first");

    const mbTokenVal = encodeURIComponent(`"${this.token}"`);
    const params     = new URLSearchParams({
      subjectId,
      se,
      ep,
      detailPath,
      streamSignType: 1,
    });

    const res = await fetch(`${SITE}/wefeed-h5api-bff/subject/play?${params}`, {
      headers: {
        ...BASE_HEADERS,
        "Authorization": `Bearer ${this.token}`,
        "Cookie":        `mb_token=${mbTokenVal}`,
        "Origin":        SITE,
        "Referer":       `${SITE}/spa/videoPlayPage/movies/${detailPath}?id=${subjectId}&type=/movie/detail&lang=en`,
      },
    });

    const body = await res.json();
    if (body.code !== 0) {
      return { error: true, code: body.code, message: body.message, data: body.data };
    }

    const data       = body.data || {};
    const hasStreams  =
      (data.streams && data.streams.length > 0) ||
      (data.dash    && data.dash.length    > 0) ||
      (data.hls     && data.hls.length     > 0);

    if (!hasStreams) {
      return { error: true, code: "NO_STREAMS", message: "No streams in response", data };
    }

    return { error: false, data };
  }

  async getStreams(subjectId, detailPath, season = 1, episode = 1, forceMovie = null) {
    // Try movie mode first (or forced)
    if (forceMovie === true || forceMovie === null) {
      const result = await this.playRequest(subjectId, detailPath, 0, 0);
      if (!result.error) return { ...result.data, isMovie: true };
      if (forceMovie === true) return null;
    }

    // Try series mode
    const result = await this.playRequest(subjectId, detailPath, season, episode);
    if (!result.error) return { ...result.data, isMovie: false };

    // Check if limited
    if (result.data && result.data.limited) {
      return { limited: true };
    }

    return null;
  }

  async extract(entry, season = 1, episode = 1, preferLang = null, quality = "best", forceMovie = null) {
    const { main_url, subjectId, detailPath } = entry;

    // Build language chain
    const langChain = preferLang
      ? [{ lang: preferLang, dub: true }, { lang: preferLang, dub: false }, ...DEFAULT_LANG_CHAIN]
      : DEFAULT_LANG_CHAIN;

    // Fetch detail
    let detail = await this.getDetail(detailPath);
    const subject = detail.subject || {};

    let finalSubjectId  = subjectId  || detail.subject?.subjectId;
    let finalDetailPath = detailPath;

    // Language switch
    const dubs  = subject.dubs || [];
    const match = resolveLang(dubs, langChain);

    if (match) {
      const oldPath   = finalDetailPath;
      finalSubjectId  = match.subjectId;
      finalDetailPath = match.detailPath;
      detail          = await this.getDetail(finalDetailPath, oldPath);
    }

    // Get streams
    const play = await this.getStreams(finalSubjectId, finalDetailPath, season, episode, forceMovie);
    if (!play) return { success: false, error: "No streams found" };
    if (play.limited) return { success: false, error: "Daily free limit reached for this content" };

    const mp4Streams  = play.streams || [];
    const dashStreams  = play.dash    || [];
    const hlsStreams   = play.hls     || [];
    const bestMp4     = pickBest(mp4Streams, quality);

    return {
      success: true,
      title:        subject.title   || entry.title,
      isMovie:      play.isMovie,
      season, episode,
      quality,
      lang:         match ? { code: match.lanCode, name: match.lanName, type: match.type === 0 ? "dub" : "sub" } : { code: "original" },
      mp4:          mp4Streams,
      dash:         dashStreams,
      hls:          hlsStreams,
      bestMp4,
      dubs:         dubs.map(d => ({
        lanCode:    d.lanCode,
        lanName:    d.lanName,
        type:       d.type === 0 ? "dub" : "sub",
        subjectId:  d.subjectId,
        detailPath: d.detailPath,
      })),
    };
  }
}

// ─── Routes ───────────────────────────────────────────────────────────────────

// Search catalogue by IMDB or TMDB ID
app.get("/api/search", async (req, res) => {
  const { id } = req.query;
  if (!id) return res.status(400).json({ error: "Missing 'id' param" });

  try {
    const data   = await fetchAllData();
    const query  = id.trim().toLowerCase();
    const entry  = data.find(item => {
      const ids = (item["imdb_id/tmdb_id"] || "").toLowerCase().split("/");
      return ids.some(x => x === query || x === query.replace(/^tt/, "") || x === `tt${query}`);
    });

    if (!entry) {
      return res.status(404).json({ error: `No entry found for ID: ${id}` });
    }

    return res.json({ success: true, entry });
  } catch (err) {
    console.error("[search] Error:", err.message);
    return res.status(500).json({ error: err.message });
  }
});

// List dubs for a given entry (by detailPath)
app.get("/api/dubs", async (req, res) => {
  const { detailPath } = req.query;
  if (!detailPath) return res.status(400).json({ error: "Missing 'detailPath'" });

  try {
    const ext    = new SflixExtractor();
    const detail = await ext.getDetail(detailPath);
    const dubs   = (detail.subject?.dubs || []).map(d => ({
      lanCode:    d.lanCode,
      lanName:    d.lanName,
      type:       d.type === 0 ? "dub" : "sub",
      subjectId:  d.subjectId,
      detailPath: d.detailPath,
    }));
    return res.json({ success: true, dubs, title: detail.subject?.title });
  } catch (err) {
    return res.status(500).json({ error: err.message });
  }
});

// Extract streams — main endpoint
app.post("/api/extract", async (req, res) => {
  const {
    id,
    season     = 1,
    episode    = 1,
    preferLang = null,
    quality    = "best",
    forceMovie = null,
  } = req.body;

  if (!id) return res.status(400).json({ error: "Missing 'id'" });

  try {
    const data  = await fetchAllData();
    const query = id.trim().toLowerCase();
    const entry = data.find(item => {
      const ids = (item["imdb_id/tmdb_id"] || "").toLowerCase().split("/");
      return ids.some(x => x === query || x === query.replace(/^tt/, "") || x === `tt${query}`);
    });

    if (!entry) {
      return res.status(404).json({ error: `No entry found for ID: ${id}` });
    }

    const ext    = new SflixExtractor();
    const result = await ext.extract(entry, parseInt(season), parseInt(episode), preferLang, quality, forceMovie);

    return res.json(result);
  } catch (err) {
    console.error("[extract] Error:", err.message);
    return res.status(500).json({ error: err.message });
  }
});

// Batch episodes for series
app.post("/api/batch", async (req, res) => {
  const {
    id,
    season     = 1,
    episodes   = [1],
    preferLang = null,
    quality    = "best",
  } = req.body;

  if (!id) return res.status(400).json({ error: "Missing 'id'" });

  try {
    const data  = await fetchAllData();
    const query = id.trim().toLowerCase();
    const entry = data.find(item => {
      const ids = (item["imdb_id/tmdb_id"] || "").toLowerCase().split("/");
      return ids.some(x => x === query || x === query.replace(/^tt/, "") || x === `tt${query}`);
    });

    if (!entry) {
      return res.status(404).json({ error: `No entry found for ID: ${id}` });
    }

    // Reuse one extractor instance so we only fetch detail once
    const ext      = new SflixExtractor();
    const langChain = preferLang
      ? [{ lang: preferLang, dub: true }, { lang: preferLang, dub: false }, ...DEFAULT_LANG_CHAIN]
      : DEFAULT_LANG_CHAIN;

    let detail     = await ext.getDetail(entry.detailPath);
    const subject  = detail.subject || {};
    const dubs     = subject.dubs   || [];
    const match    = resolveLang(dubs, langChain);

    let finalSubjectId  = entry.subjectId;
    let finalDetailPath = entry.detailPath;

    if (match) {
      const oldPath   = finalDetailPath;
      finalSubjectId  = match.subjectId;
      finalDetailPath = match.detailPath;
      detail          = await ext.getDetail(finalDetailPath, oldPath);
    }

    const results = {};
    for (const ep of episodes) {
      const play = await ext.getStreams(finalSubjectId, finalDetailPath, parseInt(season), parseInt(ep), false);
      if (play && !play.limited) {
        const best = pickBest(play.streams || [], quality);
        results[ep] = {
          success:    !!best,
          mp4:        play.streams || [],
          dash:       play.dash    || [],
          hls:        play.hls     || [],
          bestMp4:    best         || null,
        };
      } else {
        results[ep] = { success: false, error: play?.limited ? "Rate limited" : "No streams" };
      }
    }

    return res.json({
      success: true,
      title:   subject.title || entry.title,
      season,
      results,
    });
  } catch (err) {
    console.error("[batch] Error:", err.message);
    return res.status(500).json({ error: err.message });
  }
});

// Catalogue stats
app.get("/api/stats", async (req, res) => {
  try {
    const data = await fetchAllData();
    return res.json({ count: data.length, cached: cachedData !== null });
  } catch (err) {
    return res.status(500).json({ error: err.message });
  }
});

// Serve the frontend for all other routes
app.get("*", (req, res) => {
  res.sendFile(path.join(__dirname, "../public/index.html"));
});

// ─── Start ────────────────────────────────────────────────────────────────────
app.listen(PORT, () => {
  console.log(`🎬 sflix-extractor running on port ${PORT}`);
  // Pre-warm cache
  fetchAllData().catch(e => console.error("[boot] Cache pre-warm failed:", e.message));
});
