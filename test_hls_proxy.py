"""Regression tests for the HLS proxy that serves YouTube's master playlist.

Run with the API venv: `api/venv/bin/python -m unittest api.test_hls_proxy`.

The proxy exists because ExoPlayer cannot seek inside a whole-file DASH rung:
the playlist from YouTube is already segmented and already muxed, so the only
job here is to relink it at this server and pass the bytes through. Two things
can break that badly — a rewritten playlist that keeps a signed googlevideo URL
(the client then bypasses the API and the request fails on a fresh resolve) and
a playlist that links a non-YouTube host (an open relay).
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402

SEGMENT = 'https://rr3---sn-x.googlevideo.com/videoplayback?expire=1&seg=1'


class HostAllowlist(unittest.TestCase):
    def test_accepts_google_video_hosts(self):
        for url in (
            'https://rr1---sn-abc.googlevideo.com/videoplayback',
            'https://manifest.googlevideo.com/api/manifest/dash/1',
            'https://googlevideo.com/videoplayback',
        ):
            self.assertTrue(main._proxy_host_ok(url), url)

    def test_rejects_everything_else(self):
        for url in (
            'https://evil.example/videoplayback',
            'https://googlevideo.com.evil.example/v',
            'http://169.254.169.254/latest/meta-data',
            'not a url',
        ):
            self.assertFalse(main._proxy_host_ok(url), url)


class TokenRoundTrip(unittest.TestCase):
    def test_survives_a_signed_query_string(self):
        url = SEGMENT
        self.assertEqual(main._untoken(main._token(url)), url)

    def test_token_is_url_safe(self):
        # base64 std would emit '+' and '/', which do not survive a query string.
        token = main._token('https://x.googlevideo.com/v?a=1&b=2/3+4')
        self.assertNotIn('+', token)
        self.assertNotIn('/', token)
        self.assertNotIn('=', token)


class UriRewriting(unittest.TestCase):
    def master(self) -> str:
        return (
            '#EXTM3U\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS="avc1.640028,mp4a.40.2"\n'
            f'{SEGMENT}\n'
            f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="pt",URI="{SEGMENT}"\n'
        )

    def test_rewrites_the_playlist_uri(self):
        out = main._rewrite_uris(self.master(), '/hls/pl', 'vid1')
        self.assertIn('/hls/pl/vid1?u=', out)
        self.assertNotIn('googlevideo.com', out)

    def test_keeps_the_tag_lines(self):
        out = main._rewrite_uris(self.master(), '/hls/pl', 'vid1')
        self.assertIn('#EXT-X-STREAM-INF:BANDWIDTH=1', out)
        self.assertIn('CODECS="avc1.640028,mp4a.40.2"', out)
        self.assertIn('#EXTM3U', out)

    def test_master_keeps_the_audio_groups_proxied(self):
        # The variants are video-only: dropping the audio groups leaves CODECS
        # promising mp4a with no rendition, which ExoPlayer refuses to play.
        out = main._rewrite_uris(self.master(), '/hls/pl', 'vid1')
        media = [l for l in out.splitlines() if l.startswith('#EXT-X-MEDIA')]
        self.assertEqual(len(media), 1)
        self.assertIn('URI="/hls/pl/vid1?u=', media[0])
        self.assertIn('GROUP-ID="a"', media[0])

    def test_drops_a_tag_pointing_at_a_foreign_host(self):
        out = main._rewrite_uris(
            '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,URI="https://evil.example/a.m3u8"\n',
            '/hls/pl', 'vid1')
        self.assertNotIn('evil.example', out)

    def test_media_playlist_does_not_drop_segments(self):
        out = main._rewrite_uris('#EXTM3U\n#EXTINF:9.0,\n' + SEGMENT, '/hls/seg', 'vid1')
        self.assertIn('#EXTINF:9.0,', out)
        self.assertIn('/hls/seg/vid1?u=', out)

    def test_refuses_a_foreign_host(self):
        out = main._rewrite_uris(
            '#EXTM3U\n#EXTINF:9.0,\nhttps://evil.example/seg.ts\n', '/hls/seg', 'vid1')
        self.assertNotIn('evil.example', out)

    def test_is_idempotent(self):
        once = main._rewrite_uris(self.master(), '/hls/pl', 'vid1')
        twice = main._rewrite_uris(once, '/hls/pl', 'vid1')
        self.assertEqual(once, twice)

    def test_always_ends_with_a_newline(self):
        out = main._rewrite_uris('#EXTM3U', '/hls/pl', 'vid1')
        self.assertTrue(out.endswith('\n'))


class HlsRungs(unittest.TestCase):
    def test_picks_the_tallest_video_manifest_and_its_ladder(self):
        formats = [
            {'format_id': '140', 'height': None, 'vcodec': 'none', 'acodec': 'mp4a'},
            {'format_id': '137', 'height': 1080, 'vcodec': 'avc1', 'acodec': 'none',
             'manifest_url': 'https://manifest.googlevideo.com/m/master.m3u8'},
            {'format_id': '136', 'height': 720, 'vcodec': 'avc1', 'acodec': 'none',
             'manifest_url': 'https://manifest.googlevideo.com/m/720.m3u8'},
            {'format_id': '251', 'height': 144, 'vcodec': 'vp9', 'acodec': 'none',
             'manifest_url': 'https://manifest.googlevideo.com/m/144.m3u8'},
        ]
        master, ladder = main._hls_rungs(formats)
        self.assertEqual(master, 'https://manifest.googlevideo.com/m/master.m3u8')
        self.assertEqual([r['height'] for r in ladder], [1080, 720, 144])

    def test_no_video_returns_nothing_to_serve(self):
        master, ladder = main._hls_rungs([
            {'format_id': '140', 'height': None, 'vcodec': 'none', 'acodec': 'mp4a'},
        ])
        self.assertIsNone(master)
        self.assertEqual(ladder, [])


class PublicPayload(unittest.TestCase):
    def test_internal_keys_never_leave_the_server(self):
        # `hls_master` is a signed CDN URL and `mpd` a whole manifest: both stay
        # in the cache for the proxy endpoints to read.
        out = main._public_stream({
            'video_url': '/dash/abc.mpd',
            'hls_url': '/hls/abc.m3u8',
            'mpd': '<MPD/>',
            'hls_master': 'https://manifest.googlevideo.com/m',
            'hls_ladder': [{'height': 720}],
        })
        self.assertEqual(set(out), {'video_url', 'hls_url'})
        self.assertEqual(out['hls_url'], '/hls/abc.m3u8')


if __name__ == '__main__':
    unittest.main()