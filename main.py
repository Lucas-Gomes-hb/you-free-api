import asyncio
import base64
import json
import time
import os
import re
import urllib.parse
import urllib.request
import logging
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
import yt_dlp

import innertube

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="YouFree API", version="1.0.0")

_COOKIES_FILE = os.path.expanduser('~/youfree_cookies.txt')

# Which experience the app is running as. 'music' keeps the music-only filter
# (60s-12m, no podcast/tutorial titles); 'video' drops it entirely so a 1:1
# YouTube is reachable. Carried per request in X-YouFree-Mode.
#
# A ContextVar, not a module global: this server is async, so two clients in
# different modes would otherwise clobber each other mid-flight.
_APP_MODE: ContextVar[str] = ContextVar('youfree_app_mode', default='music')


@app.middleware('http')
async def _bind_app_mode(request: Request, call_next):
    mode = request.headers.get('x-youfree-mode', 'music')
    token = _APP_MODE.set(mode if mode in ('music', 'video') else 'music')
    try:
        return await call_next(request)
    finally:
        _APP_MODE.reset(token)

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


class VideoRequest(BaseModel):
    video_id: str


class CommentsRequest(BaseModel):
    video_id: str
    continuation: str | None = None
    sort: str = "top"


class RefinedSearchRequest(BaseModel):
    query: str
    params: str | None = None


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
    page_size = 20
    count = page_size + offset
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
        for entry in results['entries'][offset:offset + page_size]:
            if not entry or not _keep(entry):
                continue
            video_id = entry.get('id')
            if not video_id or len(video_id) != 11:
                continue
            videos.append({
                'id': video_id,
                'title': entry.get('title'),
                'thumbnail': _best_thumb(entry.get('thumbnail'), video_id),
                # Flat extraction leaves duration as a float and never fills
                # view_count; normalising here keeps the payload shape identical
                # to the InnerTube path so the client needs no special case.
                'duration': innertube._duration(entry.get('duration')),
                'uploader': entry.get('uploader') or entry.get('channel'),
                'url': f"https://www.youtube.com/watch?v={video_id}",
                'view_count': innertube.as_count(entry.get('view_count')),
                'published_text': None,
                'channel_thumbnail': None,
                'badges': [],
            })
    return {"results": videos, "count": len(videos)}


def _label_for_height(height: int | None) -> str:
    return f"{height}p" if height else 'auto'


def _codec_family(vcodec: str | None) -> str:
    """Normalises a codec string to the family a DASH AdaptationSet is keyed on.

    YouTube labels the same codec several ways across formats: `vp9` in webm,
    `vp09.00.51.08` in mp4, `avc1.64002A` and `avc1.4d4020` for the two AVC
    profiles. Those are all one family, and the level/constraint suffix is what
    makes the device reject a rung — not the family.
    """
    if not vcodec:
        return 'avc1'
    head = vcodec.strip().lower().split('.')[0]
    if head.startswith('avc'):
        return 'avc1'
    if head.startswith('vp9'):
        return 'vp09'
    if head.startswith('av01'):
        return 'av01'
    if head.startswith('hev') or head.startswith('hvc'):
        return 'hvc1'
    return head


def _dash_mpd(duration: int, video: list, audio: list) -> str:
    """Builds a DASH MPD out of YouTube's split adaptive streams.

    YouTube serves video and audio as separate SegmentTemplate-less mp4 files,
    so every Representation is a single BaseURL covering the whole asset. The
    player needs a real manifest because that is the only way to reach 1080p+
    *and* keep an audio track at the same time — a progressive mp4 stops at
    720p, and a bare video-only stream plays silent.
    """
    total = max(1, int(duration or 1))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="urn:mpeg:dash:profile:isoff-live:2011"'
        ' type="static"'
        f' mediaPresentationDuration="PT{total}S" minBufferTime="PT2S">',
        '  <Period duration="PT%dS">' % total,
    ]

    # One AdaptationSet per (container, codec family) pair.
    #
    # A DASH AdaptationSet is one codec family by definition, and ExoPlayer
    # builds a single video renderer from it. Folding the whole ladder into one
    # set looks harmless but leaves the track selector holding rungs it cannot
    # mix, and the result on device is no video renderer at all — the player
    # keeps playing audio while the picture never appears. YouTube makes this
    # unavoidable: 4K arrives as VP9/AV1 while 720p and below stay AVC, and both
    # even live in the same mp4 container, so the container alone is not enough
    # to separate them.
    mime_for_ext = {
        'mp4': 'video/mp4', 'm4v': 'video/mp4', 'mov': 'video/mp4',
        'webm': 'video/webm', 'mkv': 'video/x-matroska',
    }
    video_groups: dict[tuple, list] = {}
    for f in video:
        key = (f.get('ext') or 'mp4', _codec_family(f.get('vcodec')))
        video_groups.setdefault(key, []).append(f)


    # Best family first, so the highest rung leads and the track selector's
    # initial pick is the one the ladder is ordered around.
    ordered_groups = sorted(
        video_groups.items(),
        key=lambda item: max(int(f.get('tbr') or 0) for f in item[1]),
        reverse=True,
    )

    for group_index, ((ext, _family), group) in enumerate(ordered_groups):
        mime = mime_for_ext.get(ext, 'video/mp4')
        lines.append(
            f'    <AdaptationSet id="{group_index}" mimeType="{mime}"'
            ' segmentAlignment="true" startWithSAP="1">'
        )
        for i, f in enumerate(group):
            h = f.get('height') or 0
            w = f.get('width') or (h * 16 // 9 if h else 0)
            bandwidth = int(f.get('tbr') or 0) * 1000 or 2000000
            lines.append(
                f'      <Representation id="v{group_index}_{i}"'
                f' codecs="{f.get("vcodec") or "avc1.640028"}"'
                f' width="{w}" height="{h}" frameRate="{int(f.get("fps") or 30)}"'
                f' bandwidth="{bandwidth}" scalable="1">'
            )
            lines.append(f'        <BaseURL>{_xml_escape(f["url"])}</BaseURL>')
            lines.extend(_single_segment(f['url'], total))
            lines.append('      </Representation>')
        lines.append('    </AdaptationSet>')

    if audio:
        audio_mime = 'audio/webm' if (audio[0].get('ext') == 'webm') else 'audio/mp4'
        # AdaptationSet@id is xs:unsignedInt in the DASH schema, so it may not be
        # a letter: ExoPlayer parses it with Long.parseLong and aborts the whole
        # manifest on a non-numeric value.
        lines.append(
            f'    <AdaptationSet id="{len(video_groups)}" mimeType="{audio_mime}"'
            ' segmentAlignment="true" startWithSAP="1">'
        )
        for i, f in enumerate(audio):
            bandwidth = int(f.get('tbr') or 0) * 1000 or 128000
            lines.append(
                f'      <Representation id="a{i}" codecs="{f.get("acodec") or "mp4a.40.2"}"'
                f' audioSamplingRate="{int(f.get("asr") or 44100)}"'
                f' bandwidth="{bandwidth}" scalable="1">'
            )
            lines.append(f'        <BaseURL>{_xml_escape(f["url"])}</BaseURL>')
            lines.extend(_single_segment(f['url'], total))
            lines.append('      </Representation>')
        lines.append('    </AdaptationSet>')

    lines += ['  </Period>', '</MPD>', '']
    return '\n'.join(lines)


def _single_segment(url: str, total: int) -> list[str]:
    """Describe one whole-file Representation as a single SegmentList entry.

    Left as a bare <BaseURL>, the player has to guess how many bytes the
    representation is from its bitrate and cuts the last moof/mdat short, which
    surfaces as an EOFException out of the fragmented mp4 extractor: the audio
    keeps playing, the video renderer never gets a sample. Naming the segment
    explicitly hands the extractor an unbounded segment, so it reads the asset
    to the end instead of to a computed length.
    """
    return [
        f'        <SegmentList timescale="1" duration="{total}" startNumber="1">',
        f'          <SegmentURL media="{_xml_escape(url)}" />',
        '        </SegmentList>',
    ]


def _xml_escape(text: str) -> str:
    return (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))


def _hls_rungs(all_fmt: list) -> tuple[str | None, list]:
    """Finds the HLS master manifest plus the ladder the menu advertises.

    YouTube lists every DASH rung twice: once as a progressive https file and
    once as an `m3u8_native` entry pointing at HLS. Only the latter is
    segmented, and a segmented source is the whole reason /hls exists — the
    https files are one giant asset each, so ExoPlayer has no index to seek
    inside and rewinds to byte zero. The master it hands over is already muxed
    (`CODECS="avc1…,mp4a…"`), so one playlist per rung carries sound and
    picture together and nothing has to be assembled here.

    The ladder comes from the format list rather than the manifest: parsing the
    master would mean a second round trip on every /stream, and the heights it
    advertises are the same ones.
    """
    hls = [f for f in all_fmt if f.get('manifest_url') and (f.get('height') or 0) > 0]
    if not hls:
        return None, []

    by_height: dict = {}
    for f in sorted(
        hls,
        key=lambda f: (
            f.get('height') or 0,
            str(f.get('vcodec') or '').startswith('avc1'),
            f.get('fps') or 0,
        ),
        reverse=True,
    ):
        h = f.get('height')
        current = by_height.get(h)
        if current is None or (
            not str(current.get('vcodec') or '').startswith('avc1')
            and str(f.get('vcodec') or '').startswith('avc1')
        ):
            by_height[h] = f

    # Any rung's manifest_url resolves to the same master; the tallest one is
    # the most likely to stay available.
    master = max(hls, key=lambda f: f.get('height') or 0)['manifest_url']
    ladder = [
        {'label': _label_for_height(h), 'height': h, 'vcodec': f.get('vcodec')}
        for h, f in sorted(by_height.items(), key=lambda kv: kv[0], reverse=True)
    ]
    return master, ladder


def _sync_video_streams(all_fmt: list, info: dict) -> dict:
    """Resolves the best playable video source plus the quality ladder.

    Preference order:
      1. DASH/MPD built from the adaptive split streams — only way to get 1080p+
         with audio, and it lets the player switch quality without a re-fetch.
      2. Progressive mp4, when YouTube still offers one.
      3. The HLS master manifest, as a last resort.
    """
    def _has(f, *, video=True, audio=True) -> bool:
        if not f.get('url') or f.get('ext') == 'mhtml':
            return False
        v = f.get('vcodec') not in (None, 'none')
        a = f.get('acodec') not in (None, 'none')
        return (v if video else not v) and (a if audio else not a)

    # Adaptive splits: video-only ladder (mp4 first, so ExoPlayer gets one codec
    # family) and the best audio-only track.
    video_only = [f for f in all_fmt if _has(f, video=True, audio=False)]
    video_only = [f for f in video_only if (f.get('height') or 0) > 0]
    # Only direct media may back a DASH Representation. YouTube also lists every
    # rung as an m3u8_native entry whose "url" is an HLS playlist, not a file:
    # feeding that to the manifest makes the extractor read `#EXTM3U` as if it
    # were an fMP4 atom header, which aborts with EOFException — audio survives
    # and the picture never appears. The ladder is built from the https entries
    # instead, which still carry AVC to 1080p and AV1/VP9 to 2160p.
    video_only = [f for f in video_only if f.get('protocol') != 'm3u8_native']
    # One entry per height. mp4/AVC is the safest family for ExoPlayer, but
    # YouTube only ever offers 1080p+ as VP9/AV1 in webm, so mp4 is preferred
    # per height rather than as a global filter — filtering globally would cap
    # the whole ladder at 720p.
    by_height: dict = {}
    for f in sorted(
        video_only,
        key=lambda f: (f.get('height') or 0, f.get('ext') == 'mp4', f.get('fps') or 0),
        reverse=True,
    ):
        h = f.get('height')
        current = by_height.get(h)
        if current is None or (
            current.get('ext') != 'mp4' and f.get('ext') == 'mp4'
        ):
            by_height[h] = f
    ladder = sorted(by_height.values(), key=lambda f: f.get('height') or 0, reverse=True)

    audio_only = [f for f in all_fmt if _has(f, video=False, audio=True)]
    m4a_audio = [f for f in audio_only if f.get('ext') == 'm4a'] or audio_only
    audio_only = sorted(m4a_audio, key=lambda f: (f.get('abr') or f.get('tbr') or 0),
                        reverse=True)[:1]

    # The whole ladder, so the client can offer 480p/720p/1080p/1440p/2160p.
    resolutions = [
        {
            'label': _label_for_height(f.get('height')),
            'height': f.get('height'),
            'url': f['url'],
            'ext': f.get('ext'),
            'filesize': f.get('filesize') or f.get('filesize_approx'),
            'vcodec': f.get('vcodec'),
            'is_video_only': True,
        }
        for f in ladder
    ]

    result = {
        'formats': [],
        'video_resolutions': resolutions,
        'audio_url': audio_only[0]['url'] if audio_only else None,
    }

# DASH is still the better single source (one request per rung, one codec
    # family), so it stays the default. The HLS master is what makes seeking
    # cheap, because every rendition there is a list of short segments instead
    # of one 2 GB asset; /hls proxies it.
    hls_master, hls_ladder = _hls_rungs(all_fmt)
    result['hls_master'] = hls_master
    result['hls_ladder'] = hls_ladder

    if ladder and audio_only:
        video_id = info.get('id') or ''
        result['video_url'] = f"/dash/{video_id}.mpd"
        result['video_format'] = 'dash'
        # Kept out of the JSON body: the manifest travels over HTTP and the
        # player fetches it anyway. The `/dash` endpoint rebuilds it from these
        # two lists, so one resolution serves every rung the menu offers.
        result['dash_video'] = ladder
        result['dash_audio'] = audio_only
        result['duration'] = int(info.get('duration') or 0)
        result['hls_url'] = f"/hls/{video_id}.m3u8" if hls_master else None
        result['hls_resolutions'] = hls_ladder
        logger.info(
            f"Video URL resolved: dash, {len(resolutions)} qualities "
            f"up to {ladder[0].get('height')}p, "
            f"hls {'ready' if hls_master else 'unavailable'}"
        )
        return result

    # Progressive mp4 (video+audio in a single URL) whenever it is still offered
    def _combined(max_height: int) -> list:
        return sorted(
            [f for f in all_fmt if _has(f) and (f.get('height') or 9999) <= max_height],
            key=lambda f: f.get('height') or 0,
            reverse=True,
        )

    candidates = _combined(1080) or _combined(9999)
    if candidates:
        result['video_url'] = candidates[0]['url']
        result['video_format'] = 'progressive'
        result['video_resolutions'] = [
            {
                'label': _label_for_height(f.get('height')),
                'height': f.get('height'),
                'url': f['url'],
                'ext': f.get('ext'),
                'filesize': f.get('filesize') or f.get('filesize_approx'),
                'is_video_only': False,
            }
            for f in sorted(candidates, key=lambda f: f.get('height') or 0, reverse=True)
        ]
        logger.info(f"Video URL resolved: progressive {candidates[0].get('height')}p")
        return result

    # YouTube now serves adaptive streams only. The HLS master manifest is the one
    # URL that still carries both, so the player gets a single source as-is.
    manifest = next(
        (f.get('manifest_url') for f in all_fmt
         if f.get('manifest_url') and (f.get('height') or 0) > 0),
        None,
    )
    result['video_url'] = manifest
    result['video_format'] = 'hls' if manifest else None
    logger.info(f"Video URL resolved: {'hls' if manifest else 'NONE'}")
    return result


def _sync_stream(video_id: str, fmt_req: str) -> dict:
    if fmt_req == "video":
        # No format selector: YouTube no longer offers progressive mp4 for most
        # videos, and an unmatched selector aborts the whole extraction.
        ydl_opts = _base_opts()
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
        all_fmt = info.get('formats', [])
        return {**base, **_sync_video_streams(all_fmt, info)}

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


def _keep(entry: dict) -> bool:
    """Music-mode gate applied to every catalog response.

    Video mode turns it off (see `app_mode`): a 1:1 YouTube needs long
    lectures, streams and unlisted videos, all of which this filter drops.
    """
    return True if _APP_MODE.get() == 'video' else _is_music_entry(entry)


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
        ve = _build_video_entry(entry)
        if ve:
            videos.append(ve)
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
            if not _keep(entry):
                continue
            seen_ids.add(vid)
            ve = _build_video_entry(entry)
            if ve:
                videos.append(ve)
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
# Keyed by app mode so switching modes never serves the other mode's feed.
_home_feed_cache: dict[str, dict] = {}
_home_feed_ts: dict[str, float] = {}
_HOME_FEED_TTL = 2 * 3600


def _sync_home_feed() -> dict:
    mode = _APP_MODE.get()
    now = time.monotonic()
    cached = _home_feed_cache.get(mode)
    if cached is not None and (now - _home_feed_ts.get(mode, 0.0)) < _HOME_FEED_TTL:
        return cached

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
                ve = _build_video_entry(entry)
                if ve:
                    videos.append(ve)
        except Exception as e:
            logger.warning(f"RDMM home feed failed: {e}")

    if not videos:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(':ytreccommended', download=False)
                for entry in ((info or {}).get('entries') or []):
                    if not entry or not _keep(entry):
                        continue
                    ve = _build_video_entry(entry)
                    if ve:
                        videos.append(ve)
            except Exception:
                pass

    if not videos:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            r = ydl.extract_info('ytsearch20:popular music hits', download=False)
            for entry in ((r or {}).get('entries') or []):
                if entry:
                    ve = _build_video_entry(entry)
                    if ve:
                        videos.append(ve)

    result = {"results": videos[:20], "count": min(len(videos), 20)}
    _home_feed_cache[mode] = result
    _home_feed_ts[mode] = now
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
        if not entry or not _keep(entry):
            continue
        ve = _build_video_entry(entry)
        if ve:
            videos.append(ve)
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
    tracks = []
    for e in (info.get('entries') or []):
        if e and e.get('id') and len(e['id']) == 11:
            ve = _build_video_entry(e, uploader)
            if ve:
                tracks.append(ve)
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
    videos = []
    for e in (info.get('entries') or []):
        if e and e.get('id') and len(e['id']) == 11:
            ve = _build_video_entry(e, uploader)
            if ve:
                videos.append(ve)
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


def _build_video_entry(entry: dict, fallback_uploader: str | None = None) -> dict | None:
    video_id = entry.get('id')
    if not video_id or len(video_id) != 11:
        return None
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


# Kept in the cache for `/dash` and the HLS proxy to read, never sent to a
# client: `mpd` is a whole manifest as a string, and `hls_master` is a signed
# googlevideo URL the client has no use for once `/hls/{id}.m3u8` exists.
_INTERNAL_STREAM_KEYS = ('mpd', 'hls_master', 'hls_ladder')


def _public_stream(result: dict) -> dict:
    return {k: v for k, v in result.items() if k not in _INTERNAL_STREAM_KEYS}


@app.post("/stream")
async def get_stream_url(request: StreamRequest):
    cached = _cache_get(request.video_id, request.format)
    if cached:
        logger.info(f"Stream cache hit: {request.video_id}")
        return _public_stream(cached)
    try:
        result = await asyncio.to_thread(_sync_stream, request.video_id, request.format)
        _cache_set(request.video_id, result, request.format)
        return _public_stream(result)
    except Exception as e:
        logger.error(f"Stream error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dash/{video_id}.mpd")
async def get_dash_manifest(video_id: str, height: int = 0):
    """Serves the DASH manifest ExoPlayer needs to play 1080p+ with audio.

    The manifest has to come from a real URL: ExoPlayer fetches the MPD and then
    the segment ranges itself, so a string embedded in a JSON body is not an
    option. Signed googlevideo URLs are embedded as BaseURL, so the player talks
    to Google directly and this server stays out of the media path.

    `height` narrows the manifest to a single video rung. Every rendition is a
    whole fragmented file, so a DASH AdaptationSet has no per-segment timeline
    to switch on: ExoPlayer re-reads the asset from byte zero when it changes
    rungs, which on a two hour video is a stall long enough to look like a hang,
    and its own initial pick is the lowest rung because the bandwidth estimate
    starts at 200 kbps. Handing it exactly the rung that was asked for removes
    the switch entirely, and the label the UI shows is then the truth.
    """
    cached = _cache_get(video_id, 'video')
    if not (cached and cached.get('dash_video')):
        try:
            cached = await asyncio.to_thread(_sync_stream, video_id, 'video')
        except Exception as e:
            logger.error(f"DASH error for {video_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        if not cached.get('dash_video'):
            raise HTTPException(status_code=404, detail="No DASH manifest for this video")
        _cache_set(video_id, cached, 'video')

    video = cached['dash_video']
    if height:
        wanted = [f for f in video if (f.get('height') or 0) == height]
        if not wanted:
            raise HTTPException(status_code=404, detail=f"No {height}p rendition")
        video = wanted
    mpd = _dash_mpd(cached.get('duration') or 0, video, cached['dash_audio'])
    return Response(content=mpd, media_type='application/dash+xml')


def _proxy_host_ok(url: str) -> bool:
    """The proxy is an open relay unless it only speaks to YouTube."""
    try:
        host = (urllib.parse.urlparse(url).hostname or '').lower()
    except ValueError:
        return False
    return host == 'googlevideo.com' or host.endswith('.googlevideo.com')


def _token(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode().rstrip('=')


def _untoken(token: str) -> str:
    padding = '=' * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + padding).decode()


_URI_ATTR = re.compile(r'URI="([^"]*)"')


def _rewrite_uris(text: str, path: str, video_id: str) -> str:
    """Repoints every URI at this server, leaving the rest of the tags untouched.

    Covers both URI lines and `URI="…"` attributes. The attribute form matters
    in the master: YouTube's variants are video-only and their audio lives in
    `#EXT-X-MEDIA` groups (`AUDIO="233"`), so a master without those entries
    advertises `mp4a` in CODECS with no audio rendition behind it and ExoPlayer
    fails with "Unable to bind a sample queue to TrackGroup audio/mp4a-latm".

    Only googlevideo hosts are relinked; anything else is dropped instead of
    becoming a relay for whatever a playlist happens to name.
    """
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('#'):
            match = _URI_ATTR.search(line)
            if match:
                uri = match.group(1)
                if uri.startswith('/hls/'):
                    out.append(line)
                elif _proxy_host_ok(uri):
                    proxied = f"{path}/{video_id}?u={_token(uri)}"
                    out.append(line[:match.start(1)] + proxied + line[match.end(1):])
                else:
                    logger.warning(f"HLS: refusing to proxy {uri[:80]}")
                continue
            out.append(line)
            continue
        if stripped.startswith('/hls/'):
            out.append(line)
            continue
        if _proxy_host_ok(stripped):
            out.append(f"{path}/{video_id}?u={_token(stripped)}")
        else:
            logger.warning(f"HLS: refusing to proxy {stripped[:80]}")
    return '\n'.join(out) + '\n'


async def _fetch_text(url: str) -> str:
    def _get() -> str:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'YouFree/1.0',
            'Accept': '*/*',
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode('utf-8', 'replace')

    return await asyncio.to_thread(_get)


async def _hls_cache(video_id: str) -> dict:
    """Resolves (and remembers) the HLS master for [video_id]."""
    cached = _cache_get(video_id, 'video')
    if not cached:
        try:
            cached = await asyncio.to_thread(_sync_stream, video_id, 'video')
        except Exception as e:
            logger.error(f"HLS error for {video_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))
        _cache_set(video_id, cached, 'video')
    if not cached.get('hls_master'):
        raise HTTPException(status_code=404, detail="No HLS manifest for this video")
    return cached


@app.get("/hls/{video_id}.m3u8")
async def get_hls_master(video_id: str):
    """Proxies YouTube's HLS master so every rendition stays addressable.

    This is the source that makes seeking usable. The DASH rungs are single
    assets — 1080p on a long video runs past 2 GB with an empty `sidx`, so
    ExoPlayer has no index and rewinds to byte zero on every jump, which reads
    as a freeze. Here each rendition is a list of short segments, so a seek
    fetches the one segment it lands on. The variants are video-only; their
    audio is the `#EXT-X-MEDIA` groups, which are proxied like the variants.
    """
    cached = await _hls_cache(video_id)
    text = await _fetch_text(cached['hls_master'])
    return Response(
        content=_rewrite_uris(text, '/hls/pl', video_id),
        media_type='application/vnd.apple.mpegurl',
    )


@app.get("/hls/pl/{video_id}")
async def get_hls_media_playlist(video_id: str, u: str):
    """One rendition's playlist, with its segments repointed at /hls/seg."""
    try:
        url = _untoken(u)
    except Exception:
        raise HTTPException(status_code=400, detail="Bad playlist token")
    if not _proxy_host_ok(url):
        raise HTTPException(status_code=403, detail="Playlist host not allowed")
    text = await _fetch_text(url)
    return Response(
        content=_rewrite_uris(text, '/hls/seg', video_id),
        media_type='application/vnd.apple.mpegurl',
    )


@app.get("/hls/seg/{video_id}")
async def get_hls_segment(video_id: str, u: str):
    """Streams one media segment.

    A segment is a bounded range request, the one shape the tokenless CDN always
    serves, so it is relayed as bytes.
    """
    try:
        url = _untoken(u)
    except Exception:
        raise HTTPException(status_code=400, detail="Bad segment token")
    if not _proxy_host_ok(url):
        raise HTTPException(status_code=403, detail="Segment host not allowed")

    def _get() -> tuple[bytes, str]:
        req = urllib.request.Request(url, headers={
            'User-Agent': 'YouFree/1.0',
            'Accept': '*/*',
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read(), resp.headers.get('Content-Type') or 'video/mp2t'

    try:
        body, content_type = await asyncio.to_thread(_get)
    except Exception as e:
        logger.warning(f"HLS segment failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
    return Response(content=body, media_type=content_type)


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


# ── Video mode ────────────────────────────────────────────────────────────────
# yt-dlp does not expose the watch page's "up next" rail, its comments, or the
# search refinement chips, so those come from InnerTube directly. Every call is
# blocking HTTP, hence `asyncio.to_thread`, and the watch payloads are cached
# because the watch page and its comments barely change.

_watch_cache: dict[str, tuple[float, dict]] = {}
_WATCH_TTL = 3600
_comments_cache: dict[str, tuple[float, list]] = {}
_COMMENTS_TTL = 900
_related_cache: dict[str, tuple[float, list]] = {}
_RELATED_TTL = 3600


def _cached(store: dict, key: str, ttl: float):
    entry = store.get(key)
    if entry and (time.monotonic() - entry[0]) < ttl:
        return entry[1]
    return None


def _store(store: dict, key: str, value) -> None:
    store[key] = (time.monotonic(), value)
    if len(store) > 200:
        oldest = min(store.items(), key=lambda item: item[1][0])[0]
        store.pop(oldest, None)


@app.post("/video_details")
async def video_details(request: VideoRequest):
    video_id = request.video_id
    if not (isinstance(video_id, str) and len(video_id) == 11):
        raise HTTPException(status_code=400, detail="video_id inválido")
    hit = _cached(_watch_cache, video_id, _WATCH_TTL)
    if hit is not None:
        return hit
    try:
        details = await asyncio.to_thread(innertube.video_details, video_id)
    except Exception as e:
        logger.error(f"video_details error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    if not details:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    _store(_watch_cache, video_id, details)
    return details


@app.post("/related")
async def related(request: VideoRequest):
    video_id = request.video_id
    if not (isinstance(video_id, str) and len(video_id) == 11):
        raise HTTPException(status_code=400, detail="video_id inválido")
    hit = _cached(_related_cache, video_id, _RELATED_TTL)
    if hit is not None:
        return {"results": hit, "count": len(hit)}
    try:
        items = await asyncio.to_thread(innertube.related, video_id)
    except Exception as e:
        logger.error(f"related error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    _store(_related_cache, video_id, items)
    return {"results": items, "count": len(items)}


@app.post("/comments")
async def comments(request: CommentsRequest):
    video_id = request.video_id
    if not (isinstance(video_id, str) and len(video_id) == 11):
        raise HTTPException(status_code=400, detail="video_id inválido")

    # Only the first, uncontinued page is cacheable: continuation tokens are
    # single-use and tied to the exact paging position.
    if not request.continuation:
        hit = _cached(_comments_cache, f'{video_id}:{request.sort}', _COMMENTS_TTL)
        if hit is not None:
            return {"comments": hit, "continuation": None}
    try:
        page = await asyncio.to_thread(
            innertube.comments, video_id, request.continuation, request.sort
        )
    except Exception as e:
        logger.error(f"comments error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    if not request.continuation:
        _store(_comments_cache, f'{video_id}:{request.sort}', page.get('comments', []))
    return page


@app.post("/search_videos")
async def search_videos(request: RefinedSearchRequest):
    try:
        if not request.params:
            # Unrefined search stays on yt-dlp, which resolves entries fully.
            result = await asyncio.to_thread(_sync_search, request.query, 0)
            return result
        items = await asyncio.to_thread(
            innertube.search_videos, request.query, request.params
        )
        return {"results": items, "count": len(items)}
    except Exception as e:
        logger.error(f"search_videos error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/search_filters")
async def search_filters(request: RefinedSearchRequest):
    try:
        options = await asyncio.to_thread(
            innertube.search_filters, request.query, request.params
        )
        return {"filters": options, "count": len(options)}
    except Exception as e:
        logger.error(f"search_filters error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
