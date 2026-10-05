"""Regression tests for the DASH manifest the API builds for ExoPlayer.

Run with the API venv: `api/venv/bin/python -m unittest api.test_dash_mpd`.

The manifest is the only path to 1080p+ with audio, so a malformed one is not a
cosmetic problem: ExoPlayer rejects the whole source and the Watch page falls
back to a lower quality. The container split in particular has to stay correct,
because YouTube offers 1080p+ as VP9/AV1 in webm while 720p and below are mp4.
"""

import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402

NS = {'m': 'urn:mpeg:dash:schema:mpd:2011'}

DEFAULT_AUDIO = {
    'ext': 'm4a',
    'acodec': 'mp4a.40.2',
    'url': 'https://cdn/audio.m4a',
    'tbr': 128,
    'asr': 44100,
}


def adaptation_sets(mpd: str):
    root = ET.fromstring(mpd)
    return root.findall('.//m:AdaptationSet', NS)


def mpd_of(result: dict) -> str:
    """The manifest the `/dash` endpoint serves for a resolved video.

    The endpoint builds it per request so a rung can be narrowed, so the tests
    go through the same call instead of reading a prebuilt string.
    """
    return main._dash_mpd(
        result.get('duration') or 0, result['dash_video'], result['dash_audio'],
    )


class DashMpdTests(unittest.TestCase):
    def test_each_adaptation_set_holds_one_codec_family(self):
        # The shape that left the device with no video renderer at all: 4K in
        # VP9, the lower rungs in AVC, both inside mp4.
        video = [
            {'height': 2160, 'width': 3840, 'ext': 'mp4',
             'vcodec': 'vp09.00.51.08', 'url': 'https://cdn/2160',
             'tbr': 27994, 'fps': 60},
            {'height': 1440, 'width': 2560, 'ext': 'mp4',
             'vcodec': 'vp09.00.50.08', 'url': 'https://cdn/1440',
             'tbr': 14095, 'fps': 60},
            {'height': 1080, 'width': 1920, 'ext': 'mp4',
             'vcodec': 'avc1.64002A', 'url': 'https://cdn/1080',
             'tbr': 6236, 'fps': 60},
            {'height': 720, 'width': 1280, 'ext': 'mp4',
             'vcodec': 'avc1.4D4020', 'url': 'https://cdn/720',
             'tbr': 3821, 'fps': 60},
        ]
        sets = adaptation_sets(main._dash_mpd(100, video, [DEFAULT_AUDIO]))
        video_sets = [a for a in sets if a.get('mimeType', '').startswith('video/')]
        self.assertEqual(len(video_sets), 2)

        families = []
        for a in video_sets:
            reps = a.findall('m:Representation', NS)
            fams = {main._codec_family(r.get('codecs')) for r in reps}
            self.assertEqual(len(fams), 1, 'an AdaptationSet must not mix codecs')
            families.append((fams.pop(),
                             [r.get('height') for r in reps]))

        # Best family leads the manifest.
        self.assertEqual(families[0][0], 'vp09')
        self.assertEqual(families[0][1], ['2160', '1440'])
        self.assertEqual(families[1][0], 'avc1')
        self.assertEqual(families[1][1], ['1080', '720'])

    def test_mixed_container_ladder_is_split_per_container(self):
        video = [
            {'height': 1080, 'width': 1920, 'ext': 'webm',
             'vcodec': 'vp09.00.41.08', 'url': 'https://cdn/v1080.webm',
             'tbr': 4000, 'fps': 30},
            {'height': 720, 'width': 1280, 'ext': 'mp4',
             'vcodec': 'avc1.64001F', 'url': 'https://cdn/v720.mp4',
             'tbr': 2500, 'fps': 30},
        ]
        by_mime = {a.get('mimeType') for a in
                   adaptation_sets(main._dash_mpd(213, video, [DEFAULT_AUDIO]))}
        self.assertEqual(by_mime, {'video/webm', 'video/mp4', 'audio/mp4'})

    def test_codec_family_normalises_every_spelling_youtube_uses(self):
        cases = {
            'vp9': 'vp09',
            'vp09.00.51.08': 'vp09',
            'avc1.64002A': 'avc1',
            'avc1.4d4020': 'avc1',
            'av01.0.13M.08': 'av01',
            'hev1.1.6.L120.90': 'hvc1',
            None: 'avc1',
        }
        for codec, expected in cases.items():
            self.assertEqual(main._codec_family(codec), expected, codec)

    def test_audio_mime_follows_the_container(self):
        audio = {'ext': 'webm', 'acodec': 'opus', 'url': 'https://cdn/a.webm',
                 'tbr': 128, 'asr': 48000}
        sets = adaptation_sets(main._dash_mpd(60, [], [audio]))
        self.assertEqual([a.get('mimeType') for a in sets], ['audio/webm'])

    def test_signed_urls_are_xml_escaped(self):
        video = [{'height': 1080, 'width': 1920, 'ext': 'mp4',
                  'vcodec': 'avc1.640028',
                  'url': 'https://cdn/v?a=1&b=2<c>"q"', 'tbr': 5000, 'fps': 30}]
        mpd = main._dash_mpd(10, video, [DEFAULT_AUDIO])
        self.assertIn('&amp;', mpd)
        self.assertIn('&lt;c&gt;', mpd)
        self.assertIn('&quot;q&quot;', mpd)
        # And the escaped document has to still parse.
        base = ET.fromstring(mpd).find('.//m:BaseURL', NS)
        self.assertEqual(base.text, 'https://cdn/v?a=1&b=2<c>"q"')

    def test_missing_fps_and_bandwidth_get_defaults(self):
        video = [{'height': 480, 'width': 854, 'ext': 'mp4',
                  'vcodec': 'avc1.4d401e', 'url': 'https://cdn/480.mp4'}]
        rep = adaptation_sets(main._dash_mpd(5, video, [DEFAULT_AUDIO]))[0] \
            .find('m:Representation', NS)
        self.assertEqual(rep.get('frameRate'), '30')
        self.assertEqual(rep.get('bandwidth'), '2000000')

    def test_zero_duration_never_emits_an_empty_period(self):
        mpd = main._dash_mpd(0, [], [DEFAULT_AUDIO])
        period = ET.fromstring(mpd).find('.//m:Period', NS)
        self.assertEqual(period.get('duration'), 'PT1S')

    def test_adaptation_set_ids_are_numeric(self):
        # ExoPlayer runs AdaptationSet@id through Long.parseLong; a single
        # non-numeric id makes it reject the entire manifest.
        video = [
            {'height': 2160, 'width': 3840, 'ext': 'webm', 'vcodec': 'vp09',
             'url': 'https://cdn/v2160.webm', 'tbr': 18000, 'fps': 60},
            {'height': 720, 'width': 1280, 'ext': 'mp4', 'vcodec': 'avc1',
             'url': 'https://cdn/v720.mp4', 'tbr': 2500, 'fps': 30},
        ]
        for audio in ([DEFAULT_AUDIO],
                      [{'ext': 'webm', 'acodec': 'opus', 'url': 'https://cdn/a.webm',
                        'tbr': 128, 'asr': 48000}],
                      []):
            sets = adaptation_sets(main._dash_mpd(60, video, audio))
            ids = [a.get('id') for a in sets]
            for value in ids:
                self.assertIsNotNone(value)
                self.assertTrue(value.isdigit(), f'non-numeric AdaptationSet@id: {value!r}')
            self.assertEqual(len(ids), len(set(ids)), 'ids must be unique')

    def test_adaptation_set_ids_are_numeric_without_video(self):
        sets = adaptation_sets(main._dash_mpd(60, [], [DEFAULT_AUDIO]))
        self.assertEqual(sets[0].get('id'), '0')

    def test_ladder_skips_hls_playlists(self):
        """A Representation must point at a file, never at an HLS playlist.

        YouTube lists the same rungs twice: once as direct https media and once
        as m3u8_native, where the "url" is a playlist. The playlist entries are
        the attractive ones (they carry AVC up to 1080p) but they are not media,
        and the extractor dies with EOFException reading one.
        """
        fmt = [
            {'height': 2160, 'ext': 'mp4', 'vcodec': 'vp09.00.51.08', 'acodec': 'none',
             'protocol': 'm3u8_native', 'url': 'https://cdn/hls2160', 'tbr': 27994},
            {'height': 2160, 'ext': 'mp4', 'vcodec': 'av01.0.13M.08', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/v2160.mp4', 'tbr': 20000},
            {'height': 1080, 'ext': 'mp4', 'vcodec': 'avc1.64002A', 'acodec': 'none',
             'protocol': 'm3u8_native', 'url': 'https://cdn/hls1080', 'tbr': 4500},
            {'height': 1080, 'ext': 'mp4', 'vcodec': 'avc1.64002a', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/v1080.mp4', 'tbr': 4400},
            {'height': 720, 'ext': 'mp4', 'vcodec': 'avc1.4d4020', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/v720.mp4', 'tbr': 2500},
            DEFAULT_AUDIO,
        ]
        info = {'id': 'vid', 'duration': 60}
        result = main._sync_video_streams(fmt, info)
        self.assertEqual(result['video_format'], 'dash')
        urls = [r['url'] for r in result['video_resolutions']]
        self.assertTrue(urls, 'ladder should not be empty')
        for url in urls:
            self.assertNotIn('hls', url, f'ladder rung points at a playlist: {url}')
        self.assertIn('https://cdn/v1080.mp4', urls)
        for aset in adaptation_sets(mpd_of(result)):
            if not (aset.get('mimeType') or '').startswith('video/'):
                continue
            for rep in aset.findall('m:Representation', NS):
                base = rep.find('m:BaseURL', NS)
                self.assertIsNotNone(base, rep.get('id'))
                self.assertTrue(base.text.startswith('https://cdn/v'), base.text)

    def test_ladder_prefers_mp4_at_the_same_height(self):
        """Same height, both families: the mp4 rung is the decodable one."""
        fmt = [
            {'height': 1080, 'ext': 'webm', 'vcodec': 'vp9', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/vp9.webm', 'tbr': 4400},
            {'height': 1080, 'ext': 'mp4', 'vcodec': 'avc1.64002a', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/avc.mp4', 'tbr': 4400},
            DEFAULT_AUDIO,
        ]
        result = main._sync_video_streams(fmt, {'id': 'vid', 'duration': 60})
        rungs = result['video_resolutions']
        self.assertEqual(len(rungs), 1)
        self.assertEqual(rungs[0]['url'], 'https://cdn/avc.mp4')

    def test_height_filter_narrows_the_manifest_to_one_rung(self):
        """The whole point of ?height=: the engine has nothing to switch to.

        Every rendition is a whole fragmented file, so ExoPlayer cannot switch
        between them mid-playback without re-reading the asset from byte zero.
        Serving one rung is what makes the quality menu honest.
        """
        fmt = [
            {'height': 2160, 'ext': 'mp4', 'vcodec': 'av01.0.13M.08', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/v2160.mp4', 'tbr': 20000},
            {'height': 1080, 'ext': 'mp4', 'vcodec': 'avc1.64002a', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/v1080.mp4', 'tbr': 4400},
            {'height': 720, 'ext': 'mp4', 'vcodec': 'avc1.4d4020', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/v720.mp4', 'tbr': 2500},
            DEFAULT_AUDIO,
        ]
        result = main._sync_video_streams(fmt, {'id': 'vid', 'duration': 60})
        self.assertEqual(len(result['video_resolutions']), 3)

        wanted = [f for f in result['dash_video'] if f['height'] == 720]
        mpd = main._dash_mpd(60, wanted, result['dash_audio'])
        heights = sorted(
            int(rep.get('height'))
            for aset in adaptation_sets(mpd)
            if (aset.get('mimeType') or '').startswith('video/')
            for rep in aset.findall('m:Representation', NS)
        )
        self.assertEqual(heights, [720])
        # The audio track has to survive the filter, or the video plays silent.
        self.assertTrue(any(
            (aset.get('mimeType') or '').startswith('audio/')
            for aset in adaptation_sets(mpd)
        ))

    def test_ladder_exposes_the_codec_so_the_app_can_pick_a_safe_rung(self):
        """Auto needs to know which rung is AVC: 4K only arrives as AV1/VP9."""
        fmt = [
            {'height': 2160, 'ext': 'mp4', 'vcodec': 'av01.0.13M.08', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/v2160.mp4', 'tbr': 20000},
            {'height': 1080, 'ext': 'mp4', 'vcodec': 'avc1.64002a', 'acodec': 'none',
             'protocol': 'https', 'url': 'https://cdn/v1080.mp4', 'tbr': 4400},
            DEFAULT_AUDIO,
        ]
        result = main._sync_video_streams(fmt, {'id': 'vid', 'duration': 60})
        rungs = {r['height']: r['vcodec'] for r in result['video_resolutions']}
        self.assertTrue(rungs[1080].lower().startswith('avc'))
        self.assertTrue(rungs[2160].lower().startswith('av01'))

    def test_label_for_height(self):
        self.assertEqual(main._label_for_height(1080), '1080p')
        self.assertEqual(main._label_for_height(None), 'auto')


if __name__ == '__main__':
    unittest.main()
