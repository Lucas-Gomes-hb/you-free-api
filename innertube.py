"""InnerTube access for the endpoints yt-dlp does not cover.

yt-dlp resolves streams, search and playlists well, but it does not expose the
watch page's "up next" rail, its comments, or the search refinement chips.
Those live in the `next` and `search` responses of YouTube's private API, so
they are requested directly here.

The renderer walk mirrors `lib/data/services/youtube/innertube_parser.dart` on
the Flutter side: YouTube ships several shapes for the same entity, so every
lookup searches the tree by key rather than following a fixed path.
"""

import json
import logging
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_WEB_UA = (
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/131.0.0.0 Safari/537.36'
)
_CONTEXT = {
    'client': {
        'clientName': 'WEB',
        'clientVersion': '2.20250101.00.00',
        'hl': 'pt-BR',
        'gl': 'BR',
    }
}

# The numeric suffixes differ per locale: pt-BR writes "1,2 mi" and "345 mil"
# where en writes "1.2M" and "345K". Resolving the longest prefix first is the
# only ordering that reads both correctly.
_SUFFIXES = (
    ('mil', 1000),
    ('mi', 10**6),
    ('m', 10**6),
    ('bil', 10**9),
    ('b', 10**9),
    ('k', 1000),
    ('', 1),
)
_NUMBER_RE = re.compile(r'([\d.,]+)\s*([a-zA-ZçÇ]{0,6})')
_DURATION_RE = re.compile(r'^\d{1,2}(:\d{2}){1,2}$')


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _post(endpoint: str, body: dict, *, referer: str = 'https://www.youtube.com/') -> dict:
    payload = dict(body)
    payload.setdefault('context', _CONTEXT)
    request = urllib.request.Request(
        # No key: InnerTube answers the WEB client without one.
        f'https://www.youtube.com/youtubei/v1/{endpoint}?prettyPrint=false',
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'User-Agent': _WEB_UA,
            'X-YouTube-Client-Name': '1',
            'X-YouTube-Client-Version': '2.20250101.00.00',
            'Origin': 'https://www.youtube.com',
            'Referer': referer,
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode('utf-8'))


def _get(url: str) -> str:
    request = urllib.request.Request(
        url, headers={'User-Agent': _WEB_UA, 'Accept-Language': 'pt-BR,pt;q=0.9'}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode('utf-8', errors='replace')


# ── Tree walking ──────────────────────────────────────────────────────────────

def _walk(node, key: str, want_map: bool = True) -> list:
    """Every map (or list) stored under `key`, at any depth."""
    out: list = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            value = current.get(key)
            if isinstance(value, (dict if want_map else list)):
                out.append(value)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return out


def _first(node, key: str):
    found = _walk(node, key)
    return found[0] if found else None


_INVISIBLE_RE = re.compile(
    '[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]'
)


def _clean(value: str | None) -> str | None:
    """Drops the zero-width and bidi control characters YouTube leaves inside
    labels, which would otherwise render as stray glyphs or shuffle the text
    direction of a whole label."""
    if value is None:
        return None
    return _INVISIBLE_RE.sub('', value)


def _text(node) -> str | None:
    """YouTube's interchangeable text shapes, plus the accessibility label."""
    if node is None:
        return None
    if isinstance(node, str):
        return _clean(node) or None
    if not isinstance(node, dict):
        return None

    simple = node.get('simpleText')
    if isinstance(simple, str) and simple:
        return _clean(simple)
    content = node.get('content')
    if isinstance(content, str) and content:
        return _clean(content)
    runs = node.get('runs')
    if isinstance(runs, list) and runs:
        joined = ''.join(
            r.get('text', '') for r in runs if isinstance(r, dict) and isinstance(r.get('text'), str)
        )
        if joined:
            return _clean(joined)
    # Counters such as the like button carry their number only here.
    for holder in (node.get('accessibility'), node.get('accessibilityData')):
        if isinstance(holder, str) and holder:
            return _clean(holder)
        if not isinstance(holder, dict):
            continue
        nested = holder.get('accessibilityData')
        for label in (holder.get('label'), nested.get('label') if isinstance(nested, dict) else None):
            if isinstance(label, str) and label:
                return _clean(label)
    return None


def _localized_number(raw: str) -> float | None:
    """Reads a number written in either locale."""
    dots, commas = raw.count('.'), raw.count(',')
    if dots and commas:
        decimal = '.' if raw.rfind('.') > raw.rfind(',') else ','
        thousands = ',' if decimal == '.' else '.'
        whole, _, fraction = raw.rpartition(decimal)
        try:
            return float(f'{whole.replace(thousands, "")}.{fraction}')
        except ValueError:
            return None
    if dots or commas:
        separator = '.' if dots else ','
        groups = raw.split(separator)
        text = ''.join(groups) if (len(groups) > 2 or all(len(g) == 3 for g in groups[1:])) \
            else '.'.join(groups)
        try:
            return float(text)
        except ValueError:
            return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_view_count(text: str | None) -> int | None:
    if not text:
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    base = _localized_number(match.group(1))
    if base is None:
        return None
    suffix = (match.group(2) or '').lower()
    for prefix, multiplier in _SUFFIXES:
        if not prefix or suffix.startswith(prefix):
            return int(round(base * multiplier))
    return int(round(base))


def _duration(text) -> int | None:
    """Accepts the `3:33` / `1:02:03` text labels and, for convenience, the raw
    seconds integers that the player endpoint hands back."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(text) if text >= 0 else None
    if not text:
        return None
    value = text.strip()
    if not _DURATION_RE.match(value):
        return None
    parts = [int(p) for p in value.split(':')]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return None


def _best_thumb(url: str | None, video_id: str) -> str:
    if not url:
        return f'https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg'
    if 'i.ytimg.com/vi/' in url:
        return re.sub(r'(sq|mq|sd|hq)?default\.jpg', 'maxresdefault.jpg', url)
    return url


def _largest_image(node) -> str | None:
    best, best_width = None, -1
    for key in ('thumbnails', 'sources', 'thumbnailsWithBg'):
        for group in _walk(node, key, want_map=False):
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, dict):
                    continue
                url = item.get('url')
                if not isinstance(url, str) or not url:
                    continue
                width = item.get('width') or 0
                if width > best_width:
                    best_width, best = width, url
    return best


def _badges(renderer: dict) -> list[str]:
    badges: list[str] = []
    for key in ('thumbnailOverlayTimeStatusRenderer', 'badges'):
        for node in _walk(renderer, key):
            if not isinstance(node, dict):
                continue
            label = _text(node.get('text')) or _text(node.get('label'))
            if label:
                badges.append(label)
            if (_text(node.get('style')) or '').upper() == 'BADGE_STYLE_TYPE_LIVE_NOW':
                badges.append('AO VIVO')
    return list(dict.fromkeys(badges))


def _published_text(renderer: dict) -> str | None:
    for key in ('publishedTimeText', 'publishedText'):
        value = renderer.get(key)
        text = value if isinstance(value, str) else _text(value)
        if text:
            return text
    for part in _walk(renderer, 'text'):
        content = part.get('content') if isinstance(part, dict) else None
        if isinstance(content, str) and content.lower().startswith('há '):
            return content
    return None


def _video(renderer: dict, seen: set[str] | None = None) -> dict | None:
    video_id = renderer.get('videoId')
    if not (isinstance(video_id, str) and len(video_id) == 11):
        endpoint = _first(renderer.get('navigationEndpoint'), 'watchEndpoint')
        video_id = endpoint.get('videoId') if isinstance(endpoint, dict) else None
    if not (isinstance(video_id, str) and len(video_id) == 11):
        return None
    if seen is not None:
        if video_id in seen:
            return None
        seen.add(video_id)

    title = _text(renderer.get('title'))
    if not title:
        return None

    uploader = (
        _text(renderer.get('ownerText'))
        or _text(renderer.get('longBylineText'))
        or _text(renderer.get('shortBylineText'))
        or _text(renderer.get('author'))
    )
    duration = _duration(_text(renderer.get('lengthText')))
    if duration is None and renderer.get('lengthSeconds'):
        try:
            duration = int(renderer['lengthSeconds'])
        except (TypeError, ValueError):
            duration = None

    view_count = parse_view_count(
        _text(renderer.get('viewCountText')) or _text(renderer.get('shortViewCountText'))
    )
    channel = _first(renderer, 'channelThumbnailSupportedRenderers')

    return {
        'id': video_id,
        'title': title,
        'thumbnail': _best_thumb(_largest_image(renderer.get('thumbnail')), video_id),
        'duration': duration,
        'uploader': uploader,
        'url': f'https://www.youtube.com/watch?v={video_id}',
        'view_count': view_count,
        'published_text': _published_text(renderer),
        'channel_thumbnail': _largest_image(channel) if channel else None,
        'badges': _badges(renderer),
    }


_VIDEO_RENDERER_KEYS = (
    'videoRenderer',
    'playlistVideoRenderer',
    'playlistPanelVideoRenderer',
    'compactVideoRenderer',
    'gridVideoRenderer',
)

# Depth guard for the recursive walk below. InnerTube payloads nest far deeper
# than this in a handful of places (engagement panels, entity bundles), so the
# cap is generous but keeps a malformed response from blowing the stack.
_MAX_DEPTH = 40


def _iter_video_entries(node, depth: int = 0):
    """Yields `(key, renderer)` for every video entry, in document order.

    A single list can mix shapes: YouTube serves the older `*Renderer` items
    next to `lockupViewModel` ones, so walking once per key would scramble the
    order the user actually sees.
    """
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, list):
        for item in node:
            yield from _iter_video_entries(item, depth + 1)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key in _VIDEO_RENDERER_KEYS or key == 'lockupViewModel':
            yield key, value
        elif isinstance(value, (dict, list)):
            yield from _iter_video_entries(value, depth + 1)


def _video_from_lockup(node: dict, seen: set[str] | None = None) -> dict | None:
    """YouTube's newer list item. Every field name ends in `ViewModel` instead
    of `Renderer`, the duration hides inside a thumbnail badge, and the counts
    arrive as one flat run of text parts."""
    if not isinstance(node, dict):
        return None
    if node.get('contentType') not in (None, 'LOCKUP_CONTENT_TYPE_VIDEO'):
        return None

    video_id = node.get('contentId')
    if not (isinstance(video_id, str) and len(video_id) == 11):
        return None
    if seen is not None:
        if video_id in seen:
            return None
        seen.add(video_id)

    metadata = _first(node, 'lockupMetadataViewModel') or {}
    title = _clean((metadata.get('title') or {}).get('content') or '').strip()
    if not title:
        return None

    image = _first(node, 'contentImage') or {}
    duration = None
    for badge in _walk(image, 'thumbnailBadgeViewModel'):
        parsed = _duration(str(badge.get('text') or '').strip())
        if parsed is not None:
            duration = parsed
            break

    # Channel, views and age are interleaved as plain text parts. The first is
    # always the channel, and the views are the first later part that parses as
    # a count; the age follows it.
    parts: list[str] = []
    for row in (_first(metadata, 'contentMetadataViewModel') or {}).get('metadataRows') or []:
        for part in row.get('metadataParts') or []:
            text = _clean((part.get('text') or {}).get('content') or '').strip()
            if text:
                parts.append(text)

    view_count = None
    published = None
    for index, text in enumerate(parts[1:], start=1):
        parsed = parse_view_count(text)
        if parsed is not None:
            view_count = parsed
            if index + 1 < len(parts):
                published = parts[index + 1]
            break

    return {
        'id': video_id,
        'title': title,
        'thumbnail': _best_thumb(_largest_image(image), video_id),
        'duration': duration,
        'uploader': parts[0] if parts else None,
        'url': f'https://www.youtube.com/watch?v={video_id}',
        'view_count': view_count,
        'published_text': published,
        'channel_thumbnail': _largest_image(_first(metadata, 'avatarViewModel')),
        'badges': _badges(metadata),
    }


def videos_from(node, *, seen: set[str] | None = None, limit: int | None = None) -> list[dict]:
    ids = seen if seen is not None else set()
    out: list[dict] = []
    for key, entry in _iter_video_entries(node):
        if key == 'lockupViewModel':
            video = _video_from_lockup(entry, ids)
        else:
            video = _video(entry, ids)
        if video:
            out.append(video)
            if limit and len(out) >= limit:
                return out
    return out


# ── Watch page ────────────────────────────────────────────────────────────────

def _watch_data(video_id: str) -> dict:
    """`next` returns the whole watch-next payload in one call."""
    return _post('next', {'videoId': video_id})


def _as_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def as_count(value) -> int | None:
    """Normalises a count that may already be numeric (yt-dlp) or be a
    localised string (InnerTube)."""
    exact = _as_int(value)
    return exact if exact is not None else parse_view_count(value)


def video_details(video_id: str) -> dict | None:
    """Merges the two endpoints that between them own the whole watch header.

    `next` carries the watch-page renderers (channel, published date, like
    button) while `player` carries the factual numbers (title, exact duration,
    view count, full description). Neither alone is enough, and asking both in
    parallel keeps it to a single round-trip latency.
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        next_future = pool.submit(_post, 'next', {'videoId': video_id})
        player_future = pool.submit(_post, 'player', {'videoId': video_id})
        payload = next_future.result()
        player = player_future.result()

    facts = player.get('videoDetails') or {}
    primary = _first(payload, 'videoPrimaryInfoRenderer') or {}
    secondary = _first(payload, 'videoSecondaryInfoRenderer') or {}
    owner = _first(secondary, 'videoOwnerRenderer') or {}

    title = facts.get('title') or _text(primary.get('title'))
    if not title:
        return None

    description = (
        _text(secondary.get('attributedDescription'))
        or _text(secondary.get('description'))
        or facts.get('shortDescription')
    )

    channel_url = None
    browse = _first(owner, 'browseEndpoint') or {}
    if browse.get('canonicalBaseUrl'):
        channel_url = f"https://www.youtube.com{browse['canonicalBaseUrl']}"
    elif browse.get('browseId'):
        channel_url = f"https://www.youtube.com/channel/{browse['browseId']}"

    return {
        'id': video_id,
        'title': re.sub(r'\s+', ' ', title).strip(),
        'description': description,
        'channel_id': (owner.get('navigationEndpoint') or {})
                        .get('browseEndpoint', {}).get('browseId'),
        'channel_name': _text(owner.get('title')),
        'channel_thumbnail': _largest_image(owner) or None,
        'channel_url': channel_url,
        'subscriber_count_text': _text(owner.get('subscriberCountText')),
        'view_count': _as_int(facts.get('viewCount')) or _view_count_from_primary(primary),
        'like_count': _like_count(payload),
        'duration': _as_int(facts.get('lengthSeconds')),
        'published_text': _text(primary.get('dateText')),
        'badges': _badges(primary),
        'thumbnail': _best_thumb(
            _largest_image((_first(payload, 'videoRenderer') or {}).get('thumbnail')),
            video_id,
        ),
    }


def _view_count_from_primary(primary: dict) -> int | None:
    """The count is nested one level deeper than the field name suggests."""
    node = primary.get('viewCount')
    renderer = node.get('videoViewCountRenderer') if isinstance(node, dict) else None
    if isinstance(renderer, dict):
        for key in ('viewCount', 'shortViewCount'):
            parsed = parse_view_count(_text(renderer.get(key)))
            if parsed:
                return parsed
        exact = _as_int(renderer.get('originalViewCount'))
        if exact:
            return exact
    return parse_view_count(_text(node))


def _like_count(payload) -> int | None:
    """The like button is a stack of view models keyed by `iconName`, with the
    count sitting in its `title`. The older toggleButtonRenderer shape is kept
    as a fallback for clients that still answer with it."""
    for node in _walk(payload, 'buttonViewModel'):
        if not isinstance(node, dict):
            continue
        if str(node.get('iconName', '')).upper() != 'LIKE':
            continue
        title = node.get('title')
        parsed = parse_view_count(title if isinstance(title, str) else _text(title))
        if parsed:
            return parsed

    for holder in (_first(payload, 'videoPrimaryInfoRenderer'),
                   _first(payload, 'videoSecondaryInfoRenderer')):
        if not holder:
            continue
        for row in _walk(holder, 'topLevelButtons', want_map=False):
            if not isinstance(row, list):
                continue
            for button in _walk(row, 'toggleButtonRenderer'):
                label = _text(button.get('defaultText')) or _text(button)
                parsed = parse_view_count(label)
                if parsed is not None:
                    return parsed
    return None


def related(video_id: str, limit: int = 20) -> list[dict]:
    """The rail lives under `secondaryResults`, wrapped one extra level deep."""
    payload = _watch_data(video_id)
    return videos_from(payload, seen={video_id}, limit=limit)


# ── Comments ──────────────────────────────────────────────────────────────────

def _comment_from_thread(thread: dict) -> dict | None:
    comments = thread.get('comments') or thread
    renderer = _first(comments, 'commentRenderer') or _first(thread, 'commentRenderer')
    if not isinstance(renderer, dict):
        return None
    text = _text(renderer.get('contentText'))
    if not text:
        return None

    author_endpoint = renderer.get('authorEndpoint') or {}
    browse = author_endpoint.get('browseEndpoint') or {}
    author_id = renderer.get('authorId') or browse.get('browseId')
    comment_id = renderer.get('commentId')

    flat_replies = len(_walk(thread, 'commentReplyRenderer'))
    nested = 0
    for container in _walk(thread, 'commentRepliesRenderer'):
        nested += len(_walk(container, 'commentThreadRenderer'))
    replies = max(flat_replies, nested)

    return {
        'id': comment_id or f"cmt_{author_id or ''}_{abs(hash(text))}",
        'author_id': author_id or '',
        'author_name': _text(renderer.get('authorText')),
        'author_thumbnail': _largest_image(_first(renderer, 'authorThumbnail')),
        'text': re.sub(r'\s+', ' ', text).strip(),
        'like_count': parse_view_count(
            _text(_first(renderer, 'voteCount')) or _text(_first(renderer, 'likeCountNotliked'))
        ) or 0,
        'published_text': _text(renderer.get('publishedTimeText')),
        'reply_count': replies,
        'is_creator_hearted': bool(_first(renderer, 'creatorHeart')),
        'is_pinned': bool(_first(thread, 'pinnedCommentBadge')) or bool(_text(_first(thread, 'pinnedText'))),
    }


def _comment_from_entity(entity: dict, *, hearted: bool = False,
                         pinned: bool = False) -> dict | None:
    """Builds a comment from the `commentEntityPayload` shape.

    Content, author and counts all live in this one payload; the sibling
    `engagementToolbarStateEntityPayload` holds the heart state, and whether a
    comment is pinned comes from its `commentViewModel` reference.
    """
    if not isinstance(entity, dict):
        return None
    properties = entity.get('properties') or {}
    text = _clean((properties.get('content') or {}).get('content') or '').strip()
    if not text:
        return None

    author = entity.get('author') or {}
    toolbar = entity.get('toolbar') or {}
    return {
        'id': properties.get('commentId') or entity.get('key') or '',
        'author_id': author.get('channelId') or '',
        'author_name': _clean(str(author.get('displayName') or '').lstrip('@')),
        'author_thumbnail': author.get('avatarThumbnailUrl'),
        'text': re.sub(r'\s+', ' ', text).strip(),
        'like_count': as_count(
            toolbar.get('likeCountNotliked') or toolbar.get('likeCountLiked')
        ) or 0,
        'published_text': _clean(properties.get('publishedTime')) or '',
        'reply_count': as_count(toolbar.get('replyCount')) or 0,
        'is_creator_hearted': hearted,
        'is_pinned': pinned,
    }


def _comments_from_entities(payload) -> list[dict]:
    """Reads the entity-batch protocol YouTube moved comment paging to.

    A continuation now answers with `continuationItems` holding
    `commentViewModel` *references* (order, pinning, thread structure) plus a
    flat `frameworkUpdates` batch carrying the actual `commentEntityPayload`
    bodies keyed by the same opaque key. Walking the references in order keeps
    the user's sort, which the batch itself has lost.
    """
    bodies: dict[str, dict] = {}
    for entity in _walk(payload, 'commentEntityPayload'):
        if isinstance(entity, dict) and isinstance(entity.get('key'), str):
            bodies[entity['key']] = entity

    hearted: set[str] = set()
    for state in _walk(payload, 'engagementToolbarStateEntityPayload'):
        if not isinstance(state, dict):
            continue
        # `endswith` rather than a substring test: the unhearted value is
        # `..._UNHEARTED`, which contains "HEARTED" but is the opposite meaning.
        if str(state.get('heartState', '')).endswith('_HEARTED') and isinstance(state.get('key'), str):
            hearted.add(state['key'])

    out: list[dict] = []
    seen: set[str] = set()

    def unwrap(reference) -> dict | None:
        """The key is self-nesting (`commentViewModel.commentViewModel`), so the
        body sits one level below whatever the first lookup returns."""
        while isinstance(reference, dict) and 'commentKey' not in reference:
            nested = _first(reference, 'commentViewModel')
            if not isinstance(nested, dict):
                return None
            reference = nested
        return reference if isinstance(reference, dict) else None

    # The pinned comment and the ranking order both come from the ordered item
    # list, so it has to be walked as a list rather than through a key search.
    references = []
    command = _first(payload, 'reloadContinuationItemsCommand') or {}
    for entry in command.get('continuationItems') or []:
        if not isinstance(entry, dict):
            continue
        thread = _first(entry, 'commentThreadRenderer')
        reference = unwrap(_first(thread, 'commentViewModel')) if isinstance(thread, dict) else None
        if reference:
            references.append((thread, reference))

    for thread, reference in references:
        body = bodies.get(reference.get('commentKey'))
        if not body:
            continue
        comment = _comment_from_entity(
            body,
            hearted=reference.get('toolbarStateKey') in hearted,
            pinned=bool(reference.get('pinnedText')),
        )
        if not comment or comment['id'] in seen:
            continue
        replies = _first(thread, 'inlineRepliesEntityPayload') or _first(thread, 'replies')
        if replies and not comment['reply_count']:
            comment['reply_count'] = len(_walk(replies, 'commentEntityPayload')) or comment['reply_count']
        seen.add(comment['id'])
        out.append(comment)

    if not out:
        # Some responses answer with the batch only, so fall back to its order.
        for body in bodies.values():
            comment = _comment_from_entity(body, hearted=body.get('key') in hearted)
            if comment and comment['id'] not in seen:
                seen.add(comment['id'])
                out.append(comment)
    return out


def _comments_from(node) -> list[dict]:
    """Top-level comments only: a reply is itself a nested thread renderer."""
    out: list[dict] = []
    seen: set[str] = set()

    def top_level(key: str) -> list:
        found: list = []
        stack = [node]
        skip = ('commentThreadRenderer', 'commentRenderer')
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key_name, value in current.items():
                    if key_name == key and isinstance(value, dict):
                        found.append(value)
                        continue
                    if key_name in skip:
                        continue
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return found

    for thread in top_level('commentThreadRenderer'):
        comment = _comment_from_thread(thread)
        if comment and comment['id'] not in seen:
            seen.add(comment['id'])
            out.append(comment)
    for renderer in top_level('commentRenderer'):
        text = _text(renderer.get('contentText'))
        if not text:
            continue
        author = renderer.get('authorId') or (
            (renderer.get('authorEndpoint') or {}).get('browseEndpoint') or {}
        ).get('browseId')
        identifier = renderer.get('commentId') or f"cmt_{author or ''}_{abs(hash(text))}"
        if identifier in seen:
            continue
        seen.add(identifier)
        out.append({
            'id': identifier,
            'author_id': author or '',
            'author_name': _text(renderer.get('authorText')),
            'author_thumbnail': _largest_image(_first(renderer, 'authorThumbnail')),
            'text': re.sub(r'\s+', ' ', text).strip(),
            'like_count': parse_view_count(_text(_first(renderer, 'voteCount'))) or 0,
            'published_text': _text(renderer.get('publishedTimeText')),
            'reply_count': 0,
            'is_creator_hearted': bool(_first(renderer, 'creatorHeart')),
            'is_pinned': False,
        })
    return out


def _comments_anywhere(payload) -> list[dict]:
    """Tries the entity protocol first, then the legacy renderers."""
    found = _comments_from_entities(payload)
    if found:
        return found
    node = payload.get('onResponseReceivedActions') or payload.get('contents') or payload
    return _comments_from(node)


def _continuation(node) -> str | None:
    for command in _walk(node, 'continuationCommand'):
        token = command.get('token')
        if isinstance(token, str) and token:
            return token
    return None


def _comment_sort_token(payload: dict, sort: str) -> str | None:
    """Continuation token for one ordering of the comment section.

    The ordering is not a request parameter: each option in the section's sort
    menu carries its own continuation, so switching order means booting the
    section with the other token. The menu is always inlined in the watch
    response, which is why this costs nothing extra.
    """
    if sort != 'newest':
        return None
    for panel in payload.get('engagementPanels') or []:
        renderer = panel.get('engagementPanelSectionListRenderer') or {}
        header = renderer.get('header') or {}
        title = header.get('engagementPanelTitleHeaderRenderer') or {}
        menu = title.get('menu') or {}
        sub = menu.get('sortFilterSubMenuRenderer') or {}
        for item in sub.get('subMenuItems') or []:
            if (item.get('title') or '').lower() != 'mais recentes':
                continue
            endpoint = item.get('serviceEndpoint') or {}
            token = (endpoint.get('continuationCommand') or {}).get('token')
            if token:
                return token
    return None


def comments(video_id: str, continuation: str | None = None, sort: str = 'top') -> dict:
    watch_url = f'https://www.youtube.com/watch?v={video_id}'

    # The first page never inlines comments: the watch response only carries the
    # continuation token that boots the comment section, so it always costs two
    # requests.
    if continuation:
        payload = _post('next', {'continuation': continuation, 'currentUrl': watch_url},
                        referer=watch_url)
        return {
            'comments': _comments_anywhere(payload),
            'continuation': _continuation(payload),
        }

    payload = _watch_data(video_id)
    token = _comment_sort_token(payload, sort) or _continuation(payload)
    if not token:
        found = _comments_anywhere(payload)
        return {'comments': found, 'continuation': None}

    payload = _post('next', {'continuation': token, 'currentUrl': watch_url},
                    referer=watch_url)
    return {
        'comments': _comments_anywhere(payload),
        'continuation': _continuation(payload),
    }


# ── Search refinements ────────────────────────────────────────────────────────

def search_filters(query: str, params: str | None = None) -> list[dict]:
    """Refinement chips valid for this exact query *and* current params.

    The chips are context-sensitive, so they have to be re-read after every
    refinement; each one carries the full opaque `params` for the request it
    would issue, which is what the UI replays instead of composing filters.
    """
    body = {'query': query}
    if params:
        body['params'] = params
    payload = _post('search', body)

    # The chips ride in `header`; `contents` only holds results. Searching the
    # whole payload also covers layouts that nest them elsewhere.
    options: list[dict] = []
    seen: set[str] = set()
    for key in ('searchFilterRenderer', 'filterChipRenderer'):
        for chip in _walk(payload, key):
            if not isinstance(chip, dict):
                continue
            value = _filter_params(chip)
            if not value or value in seen:
                continue
            label = _text(chip.get('label')) or _text(_first(chip, 'text'))
            if not label:
                continue
            seen.add(value)
            options.append({
                'value': value,
                'label': label,
                'iconHint': _text(_first(chip, 'icon').get('iconType')) if _first(chip, 'icon') else None,
                'selected': value == params,
            })
    return options


def _filter_params(chip: dict) -> str | None:
    for key in ('searchEndpoint', 'navigationEndpoint', 'serviceEndpoint'):
        for endpoint in _walk(chip, key):
            params = endpoint.get('params')
            if isinstance(params, str) and params:
                return params
            nested = endpoint.get('continuationCommand', {}).get('request')
            if isinstance(nested, dict) and isinstance(nested.get('params'), str):
                return nested['params']
    return None


def search_videos(query: str, params: str | None = None, limit: int = 30) -> list[dict]:
    """Refined video search.

    Without refinements this keeps using yt-dlp, which resolves entries more
    completely; once a chip is picked the opaque params can only be replayed
    against InnerTube, so the search switches over.
    """
    if not params:
        raise NotImplementedError('use yt-dlp for unfiltered search')
    body = {'query': query, 'params': params}
    payload = _post('search', body)
    return videos_from(
        payload.get('contents') or payload.get('onResponseReceivedCommands'),
        limit=limit,
    )
