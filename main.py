import asyncio
import json
import time
import os
import re
import urllib.parse
import urllib.request
import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="YouFree API", version="1.0.0")

_COOKIES_FILE = os.path.expanduser('~/youfree_cookies.txt')

app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dedicated executor for background prefetch — doesn't compete with live requests
_prefetch_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="prefetch")


class SearchQuery(BaseModel):
    query: str
    offset: int = 0


class StreamRequest(BaseModel):
    video_id: str
    format: str = "audio"


class SuggestionsRequest(BaseModel):
    video_id: str
    title: str = ""
    uploader: str = ""


class PlaylistRequest(BaseModel):
    url: str


class ChannelRequest(BaseModel):
    url: str


class CookiesRequest(BaseModel):
    content: str


class PrefetchRequest(BaseModel):
    video_ids: list[str]


def _clean_lyrics_title(title: str, artist: str) -> tuple[str, str]:
    t = title
    # Remove content in brackets with production/feature/video keywords
    t = re.sub(
        r'\s*[\(\[][^\)\]]*'
        r'(?:prod\.?|ft\.?|feat\.?|official|video|audio|lyric|clipe|mv|hq|4k|hd|remaster|tradução|legendado|live)'
        r'[^\)\]]*[\)\]]\s*',
        ' ', t, flags=re.IGNORECASE,
    )
    # Remove standalone ft./feat.
    t = re.sub(r'\s*ft\.?\s+.+$', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*feat\.?\s+.+$', '', t, flags=re.IGNORECASE)
    # Remove emojis (crude but effective)
    t = re.sub(r'[^\x00-\x7FÀ-ɏ-ÿ]', '', t)
    t = re.sub(r'\s+', ' ', t).strip(' -')

    # Clean artist
    a = re.sub(r'\s*-\s*Topic$', '', artist, flags=re.IGNORECASE).strip()

    # If "Artist - Song" pattern, extract both
    if ' - ' in t:
        idx = t.index(' - ')
        return t[idx + 3:].strip(), t[:idx].strip()
    return t, a


def _sync_lyrics(title: str, artist: str) -> dict:
    clean_title, clean_artist = _clean_lyrics_title(title, artist)

    def _empty():
        return {'found': False, 'plain_lyrics': None, 'synced_lyrics': None, 'has_sync': False}

    def _query(track: str, art: str) -> dict | None:
        params = urllib.parse.urlencode({'track_name': track, 'artist_name': art})
        url = f"https://lrclib.net/api/get?{params}"
        try:
            req = urllib.request.Request(url, headers={'Lrclib-Client': 'YouFree/1.0'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 404:
                    return None
                data = json.loads(resp.read().decode('utf-8'))
            synced = (data.get('syncedLyrics') or '').strip()
            plain = (data.get('plainLyrics') or '').strip()
            if plain or synced:
                return {
                    'found': True,
                    'plain_lyrics': plain or None,
                    'synced_lyrics': synced or None,
                    'has_sync': bool(synced),
                }
        except Exception as e:
            logger.warning(f"Lyrics fetch error: {e}")
        return None

    # Try with clean title + artist
    result = _query(clean_title, clean_artist)
    # Retry with title only if not found
    if not result and clean_artist:
        result = _query(clean_title, '')
    return result or _empty()


# Cache Firefox profile path — avoid repeated filesystem scans per request
_firefox_profile_cache: str | None | bool = False  # False = not yet resolved

def _firefox_profile() -> str | None:
    global _firefox_profile_cache
    if _firefox_profile_cache is not False:
        return _firefox_profile_cache  # type: ignore[return-value]
    base = os.path.expanduser('~/.config/mozilla/firefox')
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.endswith('.default-release') or name.endswith('.default'):
                _firefox_profile_cache = os.path.join(base, name)
                return _firefox_profile_cache
    _firefox_profile_cache = None
    return None


# Cache _base_opts result — invalidated only when cookies are added or removed
_base_opts_cache: dict | None = None

def _invalidate_opts_cache() -> None:
    global _base_opts_cache
    _base_opts_cache = None

def _base_opts() -> dict:
    global _base_opts_cache
    if _base_opts_cache is not None:
        return dict(_base_opts_cache)
    opts: dict = {
        'quiet': True,
        'no_warnings': True,
        'no_color': True,
        'socket_timeout': 15,
        'retries': 2,
        'fragment_retries': 2,
        'js_runtimes': {'node': {}},
        'remote_components': ['ejs:github'],
    }
    if os.path.isfile(_COOKIES_FILE):
        opts['cookiefile'] = _COOKIES_FILE
    else:
        profile = _firefox_profile()
        if profile:
            opts['cookiesfrombrowser'] = ('firefox', profile)
    _base_opts_cache = dict(opts)
    return opts


# ---------------------------------------------------------------------------
# Stream URL cache — YouTube CDN URLs valid ~6 h; cache 4 h
# ---------------------------------------------------------------------------
_stream_cache: dict = {}
_STREAM_TTL = 4 * 3600


def _cache_get(video_id: str, fmt: str = 'audio') -> dict | None:
    key = f"{video_id}:{fmt}"
    entry = _stream_cache.get(key)
    if entry and (time.monotonic() - entry['ts']) < _STREAM_TTL:
        return entry['data']
    _stream_cache.pop(key, None)
    return None


def _cache_set(video_id: str, data: dict, fmt: str = 'audio') -> None:
    key = f"{video_id}:{fmt}"
    _stream_cache[key] = {'data': data, 'ts': time.monotonic()}
    if len(_stream_cache) > 500:
        oldest = min(_stream_cache, key=lambda k: _stream_cache[k]['ts'])
        del _stream_cache[oldest]


# ---------------------------------------------------------------------------
# Background prefetch — silently warms stream cache after search/suggestions
# ---------------------------------------------------------------------------

def _prefetch_one(video_id: str) -> None:
    if _cache_get(video_id, 'audio'):
        return
    try:
        _cache_set(video_id, _sync_stream(video_id, "audio"), 'audio')
        logger.debug(f"Prefetched stream: {video_id}")
    except Exception:
        pass


def _schedule_prefetch(video_ids: list[str]) -> None:
    for vid in video_ids:
        if vid and not _cache_get(vid, 'audio'):
            _prefetch_executor.submit(_prefetch_one, vid)


# ---------------------------------------------------------------------------
# Sync helpers — run inside asyncio.to_thread() to avoid blocking the loop
# ---------------------------------------------------------------------------

def _sync_search(query: str, offset: int = 0) -> dict:
    count = 10 + offset
    ydl_opts = {
        **_base_opts(),
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        results = ydl.extract_info(f"ytsearch{count}:{query}", download=False)

    videos = []
    if results and 'entries' in results:
        for entry in results['entries'][offset:]:
            if not entry:
                continue
            video_id = entry.get('id')
            if not video_id:
                continue
            videos.append({
                'id': video_id,
                'title': entry.get('title'),
                'thumbnail': _best_thumb(entry.get('thumbnail'), video_id),
                'duration': entry.get('duration'),
                'uploader': entry.get('uploader') or entry.get('channel'),
                'url': f"https://www.youtube.com/watch?v={video_id}",
            })
    return {"results": videos, "count": len(videos)}


def _sync_stream(video_id: str, fmt_req: str) -> dict:
    if fmt_req == "video":
        ydl_opts = {
            **_base_opts(),
            'format': 'best[ext=mp4][height<=720]/best[ext=mp4]/best[height<=720]/best',
            'extractor_args': {'youtube': {'player_client': ['web']}},
        }
    else:
        ydl_opts = {
            **_base_opts(),
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
        }

    if not video_id:
        raise ValueError("video_id is required")

    url = f"https://www.youtube.com/watch?v={video_id}"

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError(f"No video info found for {video_id}")

    base = {
        'title': info.get('title'),
        'thumbnail': info.get('thumbnail'),
        'duration': info.get('duration'),
        'uploader': info.get('uploader'),
    }

    if fmt_req == "video":
        # For merged DASH (bestvideo+bestaudio), the URL is in requested_formats
        video_url = info.get('url')
        if not video_url:
            req_fmts = info.get('requested_formats') or []
            for f in req_fmts:
                if f.get('url') and f.get('vcodec') not in (None, 'none'):
                    video_url = f['url']
                    break
        if not video_url:
            # Last resort: search all formats for any combined mp4
            all_fmt = info.get('formats', [])
            candidates = sorted(
                [
                    f for f in all_fmt
                    if f.get('url')
                    and f.get('vcodec') not in (None, 'none')
                    and f.get('acodec') not in (None, 'none')
                    and (f.get('height') or 9999) <= 720
                ],
                key=lambda f: f.get('height') or 0,
                reverse=True,
            )
            if not candidates:
                candidates = sorted(
                    [f for f in all_fmt if f.get('url') and f.get('vcodec') not in (None, 'none') and f.get('acodec') not in (None, 'none')],
                    key=lambda f: f.get('height') or 0,
                    reverse=True,
                )
            if candidates:
                video_url = candidates[0]['url']
        logger.info(f"Video URL resolved for {video_id}: {'OK' if video_url else 'NONE'}")
        return {**base, 'formats': [], 'video_url': video_url}

    all_formats = info.get('formats', [])
    audio_formats = [
        f for f in all_formats
        if f.get('url') and f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none')
    ]
    if not audio_formats:
        audio_formats = [
            f for f in all_formats
            if f.get('url') and f.get('ext') in {'m4a', 'mp3', 'opus', 'webm', 'ogg'}
        ]
    if not audio_formats:
        # Combined video+audio formats (e.g. mp4 format 18) — skip storyboards/thumbnails
        audio_formats = [
            f for f in all_formats
            if f.get('url') and f.get('acodec') not in (None, 'none') and f.get('ext') != 'mhtml'
        ]
    if not audio_formats:
        audio_formats = [f for f in all_formats if f.get('url') and f.get('ext') != 'mhtml']

    formats = [
        {
            'format_id': f.get('format_id'),
            'url': f.get('url'),
            'ext': f.get('ext'),
            'quality': f.get('format_note') or f.get('resolution'),
            'filesize': f.get('filesize'),
            'is_audio_only': f.get('vcodec') in (None, 'none'),
        }
        for f in audio_formats
    ]
    return {**base, 'formats': formats}


_NON_MUSIC_KEYWORDS = (
    'podcast', 'interview', 'entrevista', 'full movie', 'filme completo',
    'trailer', 'episode', 'episódio', 'talk show', 'documentary',
    'documentário', 'reaction', 'reação', 'unboxing', 'gameplay',
    'tutorial', 'review', 'análise', 'vlog',
)

_GENERIC_CHANNEL_WORDS = (
    'music', 'lyrics', 'vevo', 'official', 'records', 'channel',
    'entertainment', 'media', 'label', 'publishing',
)


def _is_music_entry(entry: dict) -> bool:
    duration = entry.get('duration')
    if duration is not None and (duration < 60 or duration > 720):
        return False
    title_lower = (entry.get('title') or '').lower()
    if any(kw in title_lower for kw in _NON_MUSIC_KEYWORDS):
        return False
    return True


def _extract_clean_artist(uploader: str) -> str | None:
    if not uploader:
        return None
    name = re.sub(r'\s*-\s*Topic$', '', uploader, flags=re.IGNORECASE).strip()
    if len(name) > 40 or any(kw in name.lower() for kw in _GENERIC_CHANNEL_WORDS):
        return None
    return name or None


def _sync_suggestions_radio(video_id: str) -> dict:
    radio_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    ydl_opts = {
        **_base_opts(),
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,
        'playlistend': 25,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(radio_url, download=False)

    videos: list = []
    for entry in ((info or {}).get('entries') or []):
        if not entry:
            continue
        vid = entry.get('id')
        if not vid or vid == video_id:
            continue
        videos.append(_build_video_entry(entry))
        if len(videos) >= 20:
            break
    return {"results": videos, "count": len(videos)}


def _sync_suggestions_text(video_id: str, title: str, uploader: str) -> dict:
    title_query = _clean_suggestion_query(title, uploader) or 'music'
    clean_artist = _extract_clean_artist(uploader)
    ydl_opts = {
        **_base_opts(),
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,
    }
    seen_ids: set = {video_id}
    videos: list = []

    def _collect(entries: list, limit: int) -> None:
        for entry in entries:
            if not entry:
                continue
            vid = entry.get('id')
            if not vid or vid in seen_ids:
                continue
            if not _is_music_entry(entry):
                continue
            seen_ids.add(vid)
            videos.append(_build_video_entry(entry))
            if len(videos) >= limit:
                return

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        if clean_artist:
            r1 = ydl.extract_info(f"ytsearch15:{clean_artist}", download=False)
            _collect((r1 or {}).get('entries') or [], 8)
        remaining = max(15 - len(videos), 5)
        r2 = ydl.extract_info(f"ytsearch{remaining + 5}:{title_query}", download=False)
        _collect((r2 or {}).get('entries') or [], 15)

    return {"results": videos, "count": len(videos)}


# ---------------------------------------------------------------------------
# Home feed cache — YouTube recommended videos (personalised via cookies)
# ---------------------------------------------------------------------------
_home_feed_cache: dict | None = None
_home_feed_ts: float = 0
_HOME_FEED_TTL = 2 * 3600


def _sync_home_feed() -> dict:
    global _home_feed_cache, _home_feed_ts

    now = time.monotonic()
    if _home_feed_cache is not None and (now - _home_feed_ts) < _HOME_FEED_TTL:
        return _home_feed_cache

    ydl_opts = {
        **_base_opts(),
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,
        'playlistend': 30,
    }

    videos: list = []

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info('https://www.youtube.com/playlist?list=RDMM', download=False)
            for entry in ((info or {}).get('entries') or []):
                if not entry:
                    continue
                videos.append(_build_video_entry(entry))
        except Exception as e:
            logger.warning(f"RDMM home feed failed: {e}")

    if not videos:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(':ytreccommended', download=False)
                for entry in ((info or {}).get('entries') or []):
                    if not entry or not _is_music_entry(entry):
                        continue
                    videos.append(_build_video_entry(entry))
            except Exception:
                pass

    if not videos:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            r = ydl.extract_info('ytsearch20:popular music hits', download=False)
            for entry in ((r or {}).get('entries') or []):
                if entry:
                    videos.append(_build_video_entry(entry))

    result = {"results": videos[:20], "count": min(len(videos), 20)}
    _home_feed_cache = result
    _home_feed_ts = now
    return result


def _sync_genre(hashtag: str) -> dict:
    url = f"https://www.youtube.com/hashtag/{hashtag}"
    ydl_opts = {
        **_base_opts(),
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,
        'playlistend': 20,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    videos: list = []
    for entry in ((info or {}).get('entries') or []):
        if not entry or not _is_music_entry(entry):
            continue
        videos.append(_build_video_entry(entry))
    return {"results": videos, "count": len(videos)}


def _sync_playlist(url: str) -> dict:
    ydl_opts = {
        **_base_opts(),
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("Playlist não encontrada")

    playlist_type = 'album' if 'music.youtube.com' in url else 'playlist'
    uploader = info.get('uploader') or info.get('channel')
    tracks = [
        _build_video_entry(e, uploader)
        for e in (info.get('entries') or [])
        if e and e.get('id')
    ]
    thumbnails = info.get('thumbnails') or []
    cover = thumbnails[-1].get('url') if thumbnails else (tracks[0]['thumbnail'] if tracks else None)
    return {
        'id': info.get('id') or '',
        'title': info.get('title') or 'Playlist',
        'thumbnail': cover or info.get('thumbnail'),
        'uploader': uploader,
        'item_count': len(tracks),
        'tracks': tracks,
        'type': playlist_type,
        'url': url,
    }


def _sync_channel(url: str) -> dict:
    if url.startswith('@') and not url.startswith('http'):
        url = f"https://www.youtube.com/{url}"
    elif not url.startswith('http'):
        url = f"https://www.youtube.com/@{url}"
    if 'youtube.com/@' in url and '/videos' not in url and '/playlists' not in url:
        url = url.rstrip('/') + '/videos'

    ydl_opts = {
        **_base_opts(),
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,
        'playlistend': 30,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise ValueError("Canal não encontrado")

    uploader = info.get('uploader') or info.get('channel') or info.get('title')
    videos = [
        _build_video_entry(e, uploader)
        for e in (info.get('entries') or [])
        if e and e.get('id')
    ]
    thumbnails = info.get('thumbnails') or []
    avatar = thumbnails[-1].get('url') if thumbnails else None
    return {
        'id': info.get('channel_id') or info.get('id') or '',
        'title': uploader or 'Canal',
        'thumbnail': avatar,
        'uploader': uploader,
        'item_count': len(videos),
        'tracks': videos,
        'type': 'channel',
        'url': url,
    }


def _sync_search_channels(query: str) -> dict:
    ydl_opts = {
        **_base_opts(),
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        results = ydl.extract_info(f"ytsearch20:{query}", download=False)

    seen: dict = {}
    for entry in (results.get('entries') or []):
        if not entry:
            continue
        channel_id = entry.get('channel_id') or entry.get('uploader_id')
        name = entry.get('uploader') or entry.get('channel')
        channel_url = entry.get('uploader_url') or entry.get('channel_url')
        if channel_id and channel_id not in seen and name:
            video_id = entry.get('id')
            seen[channel_id] = {
                'id': channel_id,
                'name': name,
                'thumbnail': _best_thumb(None, video_id),
                'url': channel_url or f"https://www.youtube.com/channel/{channel_id}",
            }
    channels = list(seen.values())[:6]
    return {"channels": channels, "count": len(channels)}


def _sync_search_playlists(query: str) -> dict:
    encoded = urllib.parse.quote_plus(query)
    search_url = f"https://www.youtube.com/results?search_query={encoded}&sp=EgIQAw%3D%3D"
    ydl_opts = {
        **_base_opts(),
        'extract_flat': True,
        'skip_download': True,
        'ignoreerrors': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(search_url, download=False)

    playlists = []
    for entry in (info.get('entries') or [])[:10]:
        if not entry:
            continue
        playlist_id = entry.get('id')
        if not playlist_id:
            continue
        url = (
            entry.get('url')
            or entry.get('webpage_url')
            or f"https://www.youtube.com/playlist?list={playlist_id}"
        )
        playlists.append({
            'id': playlist_id,
            'title': entry.get('title') or 'Playlist',
            'thumbnail': entry.get('thumbnail'),
            'uploader': entry.get('uploader') or entry.get('channel'),
            'item_count': entry.get('playlist_count'),
            'url': url,
            'type': 'playlist',
        })
    return {"playlists": playlists, "count": len(playlists)}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _clean_suggestion_query(title: str, uploader: str) -> str:
    clean = re.sub(
        r'\s*[\(\[](lyrics?|official(?: music| audio| video)?|hq|4k|hd|remaster(?:ed)?'
        r'|full album|video clipe|clipe oficial|tradução|legendado)[^\)\]]*[\)\]]\s*',
        ' ', title, flags=re.IGNORECASE,
    ).strip()
    clean = re.sub(r'\s+', ' ', clean).strip(' -')
    if ' - ' in clean:
        return clean
    if uploader and len(uploader) <= 40 and not any(
        kw in uploader.lower() for kw in _GENERIC_CHANNEL_WORDS
    ):
        return f"{uploader} {clean}".strip()
    return clean or 'music'


def _best_thumb(url: str | None, video_id: str | None) -> str | None:
    if video_id and not url:
        return f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    if url and 'i.ytimg.com/vi/' in url:
        return re.sub(r'(sq|mq|sd|hq)?default\.jpg', 'maxresdefault.jpg', url)
    return url


def _build_video_entry(entry: dict, fallback_uploader: str | None = None) -> dict:
    video_id = entry.get('id')
    return {
        'id': video_id,
        'title': entry.get('title'),
        'thumbnail': _best_thumb(entry.get('thumbnail'), video_id),
        'duration': entry.get('duration'),
        'uploader': entry.get('uploader') or entry.get('channel') or fallback_uploader,
        'url': f"https://www.youtube.com/watch?v={video_id}",
    }


# ---------------------------------------------------------------------------
# Startup — preload home feed in background so first request is instant
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup():
    asyncio.get_event_loop().run_in_executor(None, _sync_home_feed)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"status": "online", "service": "YouFree API"}


@app.post("/search")
async def search(query: SearchQuery):
    try:
        result = await asyncio.to_thread(_sync_search, query.query, query.offset)
        # Prefetch stream URLs for top results — when user clicks play it'll be instant
        if query.offset == 0:
            ids = [v['id'] for v in result.get('results', [])[:5] if v.get('id')]
            if ids:
                _schedule_prefetch(ids)
        return result
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/stream")
async def get_stream_url(request: StreamRequest):
    cached = _cache_get(request.video_id, request.format)
    if cached:
        logger.info(f"Stream cache hit: {request.video_id}")
        return cached
    try:
        result = await asyncio.to_thread(_sync_stream, request.video_id, request.format)
        _cache_set(request.video_id, result, request.format)
        return result
    except Exception as e:
        logger.error(f"Stream error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/suggestions")
async def get_suggestions(request: SuggestionsRequest):
    try:
        # Run radio and text-search truly concurrently via asyncio — no nested thread pools
        radio_task = asyncio.create_task(
            asyncio.to_thread(_sync_suggestions_radio, request.video_id)
        )
        text_task = asyncio.create_task(
            asyncio.to_thread(_sync_suggestions_text, request.video_id, request.title, request.uploader)
        )
        results = await asyncio.gather(radio_task, text_task, return_exceptions=True)
        radio, text = results

        if not isinstance(radio, Exception) and radio.get('count', 0) >= 5:
            result = radio
        elif not isinstance(text, Exception) and text.get('count', 0) > 0:
            result = text
        elif not isinstance(radio, Exception):
            result = radio
        else:
            result = {"results": [], "count": 0}

        ids = [v['id'] for v in result.get('results', [])[:5] if v.get('id')]
        if ids:
            _schedule_prefetch(ids)
        return result
    except Exception as e:
        logger.error(f"Suggestions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/prefetch")
async def prefetch(request: PrefetchRequest):
    """Client-initiated prefetch — warm stream cache before user clicks play."""
    ids = [v for v in request.video_ids[:50] if v]
    _schedule_prefetch(ids)
    return {"queued": len(ids)}


@app.post("/playlist")
async def get_playlist(request: PlaylistRequest):
    try:
        result = await asyncio.to_thread(_sync_playlist, request.url)
        ids = [t['id'] for t in result.get('tracks', [])[:5] if t.get('id')]
        if ids:
            _schedule_prefetch(ids)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Playlist error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/channel")
async def get_channel(request: ChannelRequest):
    try:
        result = await asyncio.to_thread(_sync_channel, request.url)
        ids = [t['id'] for t in result.get('tracks', [])[:5] if t.get('id')]
        if ids:
            _schedule_prefetch(ids)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Channel error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search_channels")
async def search_channels(query: SearchQuery):
    try:
        return await asyncio.to_thread(_sync_search_channels, query.query)
    except Exception as e:
        logger.error(f"Search channels error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search_playlists")
async def search_playlists(query: SearchQuery):
    try:
        return await asyncio.to_thread(_sync_search_playlists, query.query)
    except Exception as e:
        logger.error(f"Search playlists error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/home_feed")
async def home_feed():
    try:
        return await asyncio.to_thread(_sync_home_feed)
    except Exception as e:
        logger.error(f"Home feed error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status")
async def status():
    has_cookies_file = os.path.isfile(_COOKIES_FILE)
    has_firefox = _firefox_profile() is not None
    if has_cookies_file:
        source = 'cookies_file'
    elif has_firefox:
        source = 'firefox'
    else:
        source = 'none'
    return {'has_cookies_file': has_cookies_file, 'has_firefox': has_firefox, 'source': source}


@app.post("/cookies")
async def upload_cookies(request: CookiesRequest):
    try:
        with open(_COOKIES_FILE, 'w') as f:
            f.write(request.content)
        _invalidate_opts_cache()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/cookies")
async def delete_cookies():
    try:
        if os.path.isfile(_COOKIES_FILE):
            os.remove(_COOKIES_FILE)
        _invalidate_opts_cache()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/lyrics")
async def get_lyrics(title: str, artist: str = ""):
    try:
        return await asyncio.to_thread(_sync_lyrics, title, artist)
    except Exception as e:
        logger.error(f"Lyrics error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/genre/{hashtag}")
async def genre(hashtag: str):
    try:
        return await asyncio.to_thread(_sync_genre, hashtag)
    except Exception as e:
        logger.error(f"Genre error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def _sync_suggest(query: str) -> dict:
    url = (
        "https://suggestqueries.google.com/complete/search"
        f"?client=firefox&ds=yt&hl=pt&q={urllib.parse.quote(query)}"
    )
    with urllib.request.urlopen(url, timeout=5) as resp:
        charset = resp.headers.get_content_charset() or 'utf-8'
        data = json.loads(resp.read().decode(charset, errors='replace'))
    suggestions = data[1] if len(data) > 1 else []
    return {"suggestions": [s for s in suggestions if isinstance(s, str)][:8]}


@app.get("/suggest")
async def suggest(q: str = ""):
    if not q.strip():
        return {"suggestions": []}
    try:
        return await asyncio.to_thread(_sync_suggest, q)
    except Exception:
        return {"suggestions": []}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
