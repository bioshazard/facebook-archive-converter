import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from coalesce import Bundle, match_threads, union_records, publish
from convert import Conversion
from test_convert import fixture, box


class UnionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def bundle(self, label, generation, repeats=1, anchors=True):
        source = fixture(self.root / (label + '-source'), generation)
        f = next((source / 'messages').rglob('*.html'))
        with f.open('a') as stream:
            if anchors:
                for i in range(3):
                    stream.write(box('<div><div></div><div>A distinctive shared context anchor number ' + str(i) +
                                     '</div><div></div><div></div></div>',
                                     f'Jan 2, 2020, 10:0{i+2} AM', 'Other Person'))
            for _ in range(repeats):
                stream.write(box('<div><div></div><div>Repeated legitimate message</div><div></div><div></div></div>',
                                 'Jan 2, 2020, 10:08 AM', 'Other Person'))
        out = self.root / label
        Conversion(source, ZoneInfo('America/New_York')).run().write(out)
        return Bundle(label, out, source)

    def test_exact_content_context_and_max_multiplicity(self):
        a, b = self.bundle('2021', '2021', 3), self.bundle('2022', '2022', 2)
        matches, _ = match_threads(a, b)
        self.assertEqual(len(matches), 1)
        rows, counts = union_records(a, b, matches)
        self.assertEqual(counts['message'], {'common': 7, '2021_only': 1, '2022_only': 0})
        repeated = [r for r in rows if r['object'].get('content') == 'Repeated legitimate message']
        self.assertEqual(len(repeated), 3)
        self.assertEqual(sorted(len(r['observations']) for r in repeated), [1, 2, 2])
        summary = publish(a, b, self.root / 'union')
        self.assertEqual(summary['selected']['messages'], 8)
        self.assertEqual(summary['quarantine'], {})

    def test_names_alone_do_not_match_threads(self):
        a, b = self.bundle('2021', '2021', anchors=False), self.bundle('2022', '2022', anchors=False)
        matches, _ = match_threads(a, b)
        self.assertEqual(matches, {})
        rows, counts = union_records(a, b, matches)
        self.assertEqual(counts['message']['common'], 0)
        self.assertEqual(len([r for r in rows if r['kind'] == 'message']), 6)

    def test_media_byte_equality_ignores_generation_paths(self):
        a, b = self.bundle('2021', '2021'), self.bundle('2022', '2022')
        self.assertEqual([r['fingerprint'] for r in a.records if r['kind'] == 'post'],
                         [r['fingerprint'] for r in b.records if r['kind'] == 'post'])
        out = self.root / 'union'
        summary = publish(a, b, out, target='timelinize')
        self.assertEqual(summary['comparison']['post']['common'], 2)
        self.assertFalse((out / 'INCOMPLETE').exists())
        ledger = json.loads((out / 'union-ledger.json').read_text())
        self.assertTrue(all(len(r['observations']) == 2 for r in ledger))
        self.assertNotIn('"id":', (out / 'archive/posts/your_posts_1.json').read_text())
        self.assertTrue((out / 'archive.zip').is_file())

    def test_extra_ip_reactions_and_standalone_media(self):
        source = fixture(self.root / 'source')
        f = next((source / 'messages').rglob('*.html'))
        f.write_text(box('<div><div></div><div>Kept</div><div></div><div></div><div></div>'
                         '<div>IP Address: 192.0.2.1</div><div><ul class="_tqp"><li>Reaction</li></ul></div></div>',
                         title='Other Person'))
        (source / 'photos_and_videos/your_photos.html').write_text(
            box('<a href="photos_and_videos/p.png"><img src="photos_and_videos/p.png"></a>'
                '<div class="_3-95">Caption</div>', title='Photo title'))
        run = Conversion(source, ZoneInfo('America/New_York')).run()
        self.assertEqual(run.issues, [])
        self.assertEqual(run.counts['message_ip_annotations_provenance_only'], 1)
        self.assertEqual(run.counts['message_reaction_blocks_unconverted'], 1)
        self.assertEqual(run.counts['standalone_media_converted'], 1)
        self.assertEqual(run.files['posts/your_uncategorized_photos.json']['other_photos_v2'][0]['description'], 'Caption')

    def test_missing_later_media_metadata_preserves_richer_observation(self):
        bundles = []
        for year in ['2021', '2022']:
            source = fixture(self.root / year, year)
            (source / 'photos_and_videos/your_photos.html').write_text(box(
                '<a href="photos_and_videos/p.png"><img src="photos_and_videos/p.png"></a>',
                title='Earlier title' if year == '2021' else ''))
            output = self.root / (year + '-out')
            Conversion(source, ZoneInfo('America/New_York')).run().write(output)
            bundles.append(Bundle(year, output, source))
        matches, _ = match_threads(*bundles)
        rows, counts = union_records(*bundles, matches)
        media = [r for r in rows if r['kind'] == 'media']
        self.assertEqual(len(media), 1)
        self.assertEqual(counts['media']['common'], 1)
        self.assertEqual(media[0]['object']['title'], 'Earlier title')
        self.assertEqual([r['object']['title'] for r in media[0]['observations']], ['Earlier title', ''])

    def test_fresh_union_rehashes_changed_canonical_media(self):
        a, b = self.bundle('earlier', '2021'), self.bundle('later', '2022')
        old = self.root / 'old-union'
        publish(a, b, old)
        old_hashes = {r['sha256'] for r in json.loads((old / 'media-manifest.json').read_text()).values()}
        fresh = []
        for bundle in [a, b]:
            media = bundle.source / 'photos_and_videos/p.png'
            media.write_bytes(media.read_bytes() + b'synthetic changed canonical bytes')
            output = self.root / (bundle.label + '-fresh')
            Conversion(bundle.source, ZoneInfo('America/New_York')).run().write(output)
            fresh.append(Bundle(bundle.label, output, bundle.source))
        new = self.root / 'new-union'
        publish(*fresh, new)
        manifest = json.loads((new / 'media-manifest.json').read_text())
        self.assertTrue(old_hashes.isdisjoint({r['sha256'] for r in manifest.values()}))
        from coalesce import file_hash
        for uri, entry in manifest.items():
            self.assertEqual(file_hash(new / 'archive' / uri), file_hash(Path(entry['source'])))

    def test_missing_post_title_requires_primary_payload_and_retains_title(self):
        a, b = self.bundle('2021', '2021'), self.bundle('2022', '2022')
        original = next(r for r in b.records if r['kind'] == 'post')
        original['object']['title'] = ''
        original['semantic']['title'] = ''
        from coalesce import key
        original['fingerprint'] = key(['post', original['semantic']])
        rows, counts = union_records(a, b, match_threads(a, b)[0])
        posts = [r for r in rows if r['kind'] == 'post']
        self.assertEqual(len(posts), 2)
        self.assertEqual(counts['post']['common'], 2)
        self.assertEqual(posts[0]['object']['title'] if posts[0]['object']['data'] else posts[1]['object']['title'],
                         'Example Person posted an update.')
        for bundle in [a, b]:
            r = next(r for r in bundle.records if r['kind'] == 'post')
            r['object']['data'] = []
            r['object']['attachments'] = []
            r['object']['title'] = '' if bundle is b else 'Earlier action title'
            r.pop('union_fingerprint', None)
            r.pop('retained_metadata', None)
            r['semantic'] = bundle.semantic(r['object'])
            r['fingerprint'] = key(['post', r['semantic']])
        rows, counts = union_records(a, b, match_threads(a, b)[0])
        self.assertEqual(counts['post']['common'], 1)
        self.assertEqual(len([r for r in rows if r['kind'] == 'post']), 3)


if __name__ == '__main__':
    unittest.main()
