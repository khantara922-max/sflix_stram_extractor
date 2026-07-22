# SStream — sflix.film Extractor

Web UI for extracting stream URLs from sflix.film using TMDB or IMDB IDs.

---

## Deploy to Render (free)

1. Push this folder to a GitHub repo
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` — click **Deploy**
5. Your app is live at `https://your-app.onrender.com`

Or manually:
- **Build Command:** `npm install`
- **Start Command:** `npm start`
- **Environment:** Node
- **Plan:** Free

---

## Local development

```bash
npm install
npm start
# Open http://localhost:3000
```

---

## API Endpoints

### `GET /api/search?id=<imdb_or_tmdb_id>`
Looks up a title in the catalogue by IMDB ID (tt…) or TMDB ID (numeric).

### `POST /api/extract`
Extracts stream URLs for a movie or single episode.
```json
{
  "id": "tt4574334",
  "season": 1,
  "episode": 1,
  "quality": "best",
  "preferLang": "en",
  "forceMovie": null
}
```

### `POST /api/batch`
Extracts stream URLs for multiple episodes of a series.
```json
{
  "id": "tt4574334",
  "season": 1,
  "episodes": [1, 2, 3, 4, 5],
  "quality": "best",
  "preferLang": "en"
}
```

### `GET /api/dubs?detailPath=<path>`
Lists all available language dubs/subs for a title.

### `GET /api/stats`
Returns catalogue size.

---

## ID Formats

| Format | Example |
|--------|---------|
| IMDB   | `tt4574334` or `4574334` |
| TMDB   | `66732` |

---

## Quality Options

| Value | Meaning |
|-------|---------|
| `best` | Highest resolution available |
| `1080` | 1080p exactly (falls back to best) |
| `720` | 720p exactly |
| `480` | 480p exactly |
| `lowest` | Lowest resolution |

---

## Notes

- The catalogue is fetched from GitHub and cached for 1 hour per server instance
- Streams may be VIP-locked on some titles; free streams are preferred automatically
- `forceMovie: null` = auto-detect (tries movie mode, falls back to series)
