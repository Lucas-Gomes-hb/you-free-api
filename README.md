# YouFree API

> **Educational Project — Non-Commercial Use Only**
>
> This project was built exclusively for learning and study purposes. It must not be used for any commercial purpose, monetization, or profit of any kind. See the [License & Usage Terms](#license--usage-terms) section for details.

---

YouFree API is a self-hosted backend built with **FastAPI** and **yt-dlp** that powers the YouFree Flutter app. It handles YouTube search, stream URL resolution, content discovery, lyrics fetching, and more — all served over a simple HTTP API.

**Repository:** [github.com/Lucas-Gomes-hb/you-free-api](https://github.com/Lucas-Gomes-hb/you-free-api)

**Flutter app:** [github.com/Lucas-Gomes-hb/you-free-app](https://github.com/Lucas-Gomes-hb/you-free-app)

---

## Table of Contents

- [Features](#features)
- [Endpoints](#endpoints)
  - [Health & Status](#health--status)
  - [Search](#search)
  - [Stream Resolution](#stream-resolution)
  - [Suggestions & Autoplay](#suggestions--autoplay)
  - [Collections](#collections)
  - [Home Feed & Genres](#home-feed--genres)
  - [Lyrics](#lyrics)
  - [Autocomplete](#autocomplete)
  - [Cookies](#cookies)
  - [Cache Warm-up](#cache-warm-up)
- [Caching](#caching)
- [Format Selection](#format-selection)
- [Cookies & Authentication](#cookies--authentication)
- [Tech Stack](#tech-stack)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Installation](#installation)
  - [Running the Server](#running-the-server)
- [Configuration](#configuration)
- [License & Usage Terms](#license--usage-terms)
- [Disclaimer](#disclaimer)

---

## Features

- **YouTube search** — full-text search for videos, channels, and playlists with pagination support.
- **Stream URL resolution** — extracts direct CDN URLs for both audio-only and video streams with intelligent format fallback.
- **In-memory stream cache** — resolved URLs are cached for 4 hours (up to 500 entries), avoiding repeated yt-dlp calls for the same video.
- **Autoplay suggestions** — generates a radio-like queue by combining YouTube's recommendation playlist (RD) and text-based search concurrently.
- **Collection browsing** — full metadata and track listing for YouTube playlists, albums (music.youtube.com), and channels.
- **Home feed** — personalized content feed loaded on startup and refreshed every 2 hours.
- **Genre/hashtag feed** — browse videos by YouTube hashtag with music-only filtering.
- **Lyrics** — plain and synchronized (LRC) lyrics via lrclib.net, with automatic title/artist cleaning.
- **Autocomplete** — real-time search suggestions from Google's YouTube suggestion API.
- **Background prefetching** — top results are pre-resolved in a background thread pool to minimize playback latency.
- **Cookie support** — works with a cookies file or auto-detected Firefox profile for accessing restricted content.

---

## Endpoints

All endpoints return JSON. All `POST` endpoints accept `application/json`.

### Health & Status

#### `GET /`
Basic health check.

**Response:**
```json
{
  "status": "online",
  "service": "YouFree API"
}
```

---

#### `GET /status`
Returns information about the current authentication source (cookies).

**Response:**
```json
{
  "has_cookies_file": true,
  "has_firefox": false,
  "source": "cookies_file"
}
```

| Field | Description |
|---|---|
| `has_cookies_file` | Whether `~/youfree_cookies.txt` is present |
| `has_firefox` | Whether a Firefox default profile was auto-detected |
| `source` | `"cookies_file"`, `"firefox"`, or `"none"` |

---

### Search

#### `POST /search`
Searches YouTube for videos.

**Request body:**
```json
{
  "query": "string",
  "offset": 0
}
```

| Field | Type | Description |
|---|---|---|
| `query` | `string` | Search term |
| `offset` | `int` | Pagination offset (0 = first page). On offset 0, the first 5 results are auto-prefetched in the background. |

**Response:**
```json
{
  "results": [
    {
      "id": "video_id",
      "title": "Track Title",
      "thumbnail": "https://i.ytimg.com/vi/.../maxresdefault.jpg",
      "duration": 213,
      "uploader": "Artist Name",
      "url": "https://www.youtube.com/watch?v=..."
    }
  ],
  "count": 10
}
```

---

#### `POST /search_channels`
Searches YouTube for channels.

**Request body:**
```json
{
  "query": "string",
  "offset": 0
}
```

**Response:**
```json
{
  "channels": [
    {
      "id": "channel_id",
      "title": "Channel Name",
      "thumbnail": "...",
      "uploader": "Channel Name",
      "url": "https://www.youtube.com/@channelname",
      "subscriber_count": 1200000
    }
  ],
  "count": 6
}
```

Returns up to 6 results, deduplicated by channel ID.

---

#### `POST /search_playlists`
Searches YouTube for playlists.

**Request body:**
```json
{
  "query": "string",
  "offset": 0
}
```

**Response:**
```json
{
  "playlists": [
    {
      "id": "playlist_id",
      "title": "Playlist Title",
      "thumbnail": "...",
      "uploader": "Channel Name",
      "url": "https://www.youtube.com/playlist?list=...",
      "item_count": 32
    }
  ],
  "count": 10
}
```

Returns up to 10 results.

---

### Stream Resolution

#### `POST /stream`
Resolves the direct CDN stream URL for a given video. This is the most critical endpoint — it wraps yt-dlp's format extraction with caching, fallback logic, and format-specific selection.

**Request body:**
```json
{
  "video_id": "dQw4w9WgXcQ",
  "format": "audio"
}
```

| Field | Type | Description |
|---|---|---|
| `video_id` | `string` | YouTube video ID |
| `format` | `"audio"` or `"video"` | Whether to resolve an audio-only or video stream |

**Response (format = "audio"):**
```json
{
  "title": "Track Title",
  "thumbnail": "https://i.ytimg.com/vi/.../maxresdefault.jpg",
  "duration": 213,
  "uploader": "Artist Name",
  "formats": [
    {
      "format_id": "140",
      "url": "https://cdn.googlevideo.com/...",
      "ext": "m4a",
      "quality": 128,
      "filesize": 3456789,
      "is_audio_only": true
    }
  ]
}
```

**Response (format = "video"):**
```json
{
  "title": "Track Title",
  "thumbnail": "https://i.ytimg.com/vi/.../maxresdefault.jpg",
  "duration": 213,
  "uploader": "Artist Name",
  "video_url": "https://cdn.googlevideo.com/..."
}
```

Stream URLs are cached in memory for **4 hours** using the key `{video_id}:{format}`.

---

### Suggestions & Autoplay

#### `POST /suggestions`
Generates a list of related video suggestions for autoplay (Radio Mode).

**Request body:**
```json
{
  "video_id": "dQw4w9WgXcQ",
  "title": "Never Gonna Give You Up",
  "uploader": "Rick Astley"
}
```

**Response:**
```json
{
  "results": [
    {
      "id": "...",
      "title": "...",
      "thumbnail": "...",
      "duration": 240,
      "uploader": "...",
      "url": "..."
    }
  ],
  "count": 12
}
```

Internally runs two yt-dlp searches **concurrently**:
1. YouTube Radio playlist (`RD{video_id}`) — YouTube's own recommendation engine
2. Text search based on the artist/title

Results from radio mode are used if there are 5 or more results; otherwise the text search results are used. Duplicates are filtered, and all results pass the music filter (60s–12m duration, no podcast/tutorial keywords).

---

### Collections

#### `POST /playlist`
Fetches the full metadata and track listing for a YouTube playlist or album.

**Request body:**
```json
{
  "url": "https://www.youtube.com/playlist?list=PLxxxx"
}
```

Also accepts `music.youtube.com` URLs, which are detected as type `"album"` rather than `"playlist"`.

**Response:**
```json
{
  "id": "PLxxxx",
  "title": "Playlist Title",
  "thumbnail": "...",
  "uploader": "Channel Name",
  "item_count": 18,
  "type": "playlist",
  "url": "https://www.youtube.com/playlist?list=PLxxxx",
  "tracks": [
    {
      "id": "...",
      "title": "...",
      "thumbnail": "...",
      "duration": 213,
      "uploader": "...",
      "url": "..."
    }
  ]
}
```

The first 5 tracks are automatically prefetched in the background after the response is returned.

---

#### `POST /channel`
Fetches the metadata and up to 30 latest videos from a YouTube channel.

**Request body:**
```json
{
  "url": "https://www.youtube.com/@channelname"
}
```

Also accepts `@channelname` shorthand — the API automatically expands it to the full URL and appends `/videos` if needed.

**Response:**
```json
{
  "id": "UCxxxx",
  "title": "Channel Name",
  "thumbnail": "...",
  "uploader": "Channel Name",
  "item_count": 30,
  "type": "channel",
  "url": "https://www.youtube.com/@channelname/videos",
  "tracks": [...]
}
```

The first 5 tracks are automatically prefetched in the background.

---

### Home Feed & Genres

#### `GET /home_feed`
Returns a curated feed of music videos.

**Response:**
```json
{
  "results": [...],
  "count": 25
}
```

The feed is generated by trying, in order:
1. YouTube's RDMM (music mix) playlist
2. `:ytrecommended` (YouTube recommended)
3. A fallback search for popular music

Results are filtered to music only (60s–12m, no podcast/tutorial titles). The feed is cached for **2 hours** and preloaded at server startup in a background thread.

---

#### `GET /genre/{hashtag}`
Returns videos tagged with a specific YouTube hashtag.

**Example:** `GET /genre/lofi`

**Response:**
```json
{
  "results": [...],
  "count": 20
}
```

Results are filtered to music only and cached per hashtag.

---

### Lyrics

#### `GET /lyrics?title=...&artist=...`
Fetches lyrics for a track from lrclib.net.

**Query parameters:**

| Param | Type | Description |
|---|---|---|
| `title` | `string` | Track title (automatically cleaned: removes feat., brackets, emojis) |
| `artist` | `string` | Artist name (removes "Topic" suffix from YouTube auto-uploads) |

**Response:**
```json
{
  "found": true,
  "plain_lyrics": "Never gonna give you up\nNever gonna let you down\n...",
  "synced_lyrics": "[00:18.06] Never gonna give you up\n[00:20.21] Never gonna let you down\n...",
  "has_sync": true
}
```

If lyrics are not found:
```json
{
  "found": false,
  "plain_lyrics": null,
  "synced_lyrics": null,
  "has_sync": false
}
```

The API cleans the title before querying: strips content in parentheses/brackets (e.g., `(Official Video)`, `(feat. Artist)`), removes `ft.`/`feat.` suffixes, and strips emojis.

---

### Autocomplete

#### `GET /suggest?q=...`
Returns real-time search suggestions from Google's YouTube autocomplete service.

**Example:** `GET /suggest?q=rick+astley`

**Response:**
```json
{
  "suggestions": [
    "rick astley never gonna give you up",
    "rick astley together forever",
    "rick astley - whenever you need somebody"
  ]
}
```

Returns up to 8 suggestions. Uses Google's Firefox/YouTube suggestion API (`suggestqueries.google.com`).

---

### Cookies

#### `POST /cookies`
Uploads a cookies file to be used by yt-dlp for accessing restricted content.

**Request body:**
```json
{
  "content": "# Netscape HTTP Cookie File\n..."
}
```

The content is saved to `~/youfree_cookies.txt`. The internal options cache is invalidated so the new cookies are used immediately.

**Response:**
```json
{ "ok": true }
```

---

#### `DELETE /cookies`
Deletes the cookies file and reverts to Firefox profile detection (or no auth).

**Response:**
```json
{ "ok": true }
```

---

### Cache Warm-up

#### `POST /prefetch`
Manually triggers background stream URL pre-resolution for a list of video IDs. Useful for warming up the cache before the user plays a track.

**Request body:**
```json
{
  "video_ids": ["dQw4w9WgXcQ", "oHg5SJYRHA0", "..."]
}
```

Maximum 50 IDs per request. Each ID is prefetched as audio format in the background (non-blocking).

**Response:**
```json
{ "queued": 3 }
```

---

## Caching

| Cache | TTL | Max size | Key |
|---|---|---|---|
| Stream URLs | 4 hours | 500 entries | `{video_id}:{format}` |
| Home feed | 2 hours | 1 entry | — |
| Genre feeds | 2 hours | per hashtag | `{hashtag}` |
| Firefox profile path | Runtime | 1 entry | — |

Stream cache eviction is FIFO: when the limit is reached, the oldest entries are removed first.

---

## Format Selection

yt-dlp is configured to find the best available format depending on what the client requests.

**Audio:**

Priority order:
1. Best pure audio — `bestaudio[ext=m4a]/bestaudio`
2. m4a, mp3, opus, webm, ogg fallback
3. Combined video+audio formats (mhtml storyboards are excluded)
4. Any format with a URL

**Video:**

Priority order:
1. Best ≤720p mp4 — `best[ext=mp4][height<=720]`
2. Any mp4
3. Best ≤720p any codec
4. Merged DASH (bestvideo+bestaudio)
5. Any combined format

The video endpoint forces the `web` player client in yt-dlp to avoid formats that require sign-in.

---

## Cookies & Authentication

By default, the API works without any authentication. For age-restricted or otherwise protected content, you can provide cookies in two ways:

**Option 1 — Cookies file (recommended)**
Upload a Netscape-format cookies file via `POST /cookies`. The file is saved to `~/youfree_cookies.txt` and used automatically by all yt-dlp calls. You can export this file from a browser extension such as "Get cookies.txt LOCALLY".

**Option 2 — Firefox profile auto-detection**
If no cookies file is present, the API scans `~/.config/mozilla/firefox/` for a `.default-release` or `.default` profile and uses it directly. No manual configuration needed.

Check `GET /status` to see which source is currently active.

---

## Tech Stack

| Package | Version | Purpose |
|---|---|---|
| Python | 3.10+ | Language |
| FastAPI | 0.115.0 | Web framework |
| Uvicorn | 0.32.0 | ASGI server |
| yt-dlp | latest | YouTube metadata & stream extraction |
| python-multipart | 0.0.12 | Form data support |

---

## Getting Started

### Prerequisites

- Python 3.10 or later
- `pip`
- Network access to YouTube from the host machine

### Installation

```bash
# Clone the repository
git clone git@github.com:Lucas-Gomes-hb/you-free-api.git
cd you-free-api

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Running the Server

```bash
# Option 1 — run directly
python main.py

# Option 2 — run via uvicorn (useful for development with auto-reload)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at `http://localhost:8000`.

Interactive documentation (Swagger UI) is available at `http://localhost:8000/docs` while the server is running.

---

## Configuration

There are no configuration files or environment variables. All settings are hardcoded in `main.py`:

| Setting | Value | Description |
|---|---|---|
| Host | `0.0.0.0` | Listens on all network interfaces |
| Port | `8000` | Default HTTP port |
| Stream cache TTL | 4 hours | How long resolved CDN URLs are kept in memory |
| Home feed cache TTL | 2 hours | How long the home feed result is kept |
| Max stream cache entries | 500 | Oldest entries evicted first |
| Background prefetch workers | 8 threads | `ThreadPoolExecutor` size |
| Socket timeout | 15 seconds | yt-dlp socket timeout per request |
| Retries | 2 | yt-dlp retry attempts |
| Cookies file path | `~/youfree_cookies.txt` | Where to read/write the cookies file |

To change the port, edit the `uvicorn.run()` call at the bottom of `main.py`.

---

## License & Usage Terms

This project is released under the **Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)** license.

**You are free to:**
- Use, study, copy, modify, and distribute this project and its source code.
- Build upon it and create derivative works.
- Share it with others freely.

**Under the following conditions:**
- **Attribution** — You must give appropriate credit to the original author(s) and include a link back to this repository.
- **NonCommercial** — You may **not** use this project, its source code, or any derivative of it for commercial purposes, monetization, profit, paid services, or any activity that generates revenue.

Full license text: [creativecommons.org/licenses/by-nc/4.0](https://creativecommons.org/licenses/by-nc/4.0/)

---

## Disclaimer

> **This project exists solely for educational and study purposes.**
>
> YouFree API is not affiliated with, endorsed by, or in any way connected to YouTube, Google LLC, or any of their subsidiaries or partners. YouTube and all related names, logos, and trademarks are the property of their respective owners.
>
> Using yt-dlp to extract content from YouTube may be subject to YouTube's [Terms of Service](https://www.youtube.com/t/terms). By running this software, you acknowledge and agree that:
>
> - You will only use it for personal, non-commercial, and educational purposes.
> - You will not use it to infringe on the intellectual property rights of any content creator.
> - The authors of this project bear no responsibility for how the software is used by others.
>
> **This project must not be used for commercial purposes, for building products or services, or for any activity that generates revenue of any kind.**
