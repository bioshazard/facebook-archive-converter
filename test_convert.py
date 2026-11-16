"""Synthetic fixtures shaped like observed 2021/2022 exports; no private data."""
import argparse
import struct
import zlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from convert import Conversion, DOM, Refusal, legacy, parse_offset, timestamp

def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


PNG = (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 16, 8, 2, 0, 0, 0)) +
       png_chunk(b"IDAT", zlib.compress((b"\0" + b"\x40\x80\xc0" * 16) * 16)) + png_chunk(b"IEND", b""))


def box(body, date="Jul 4, 2020, 1:23 PM", title="Example Person posted an update."):
    return ('<div class="pam _3-95 _2pi0 _2lej uiBoxWhite noborder">'
            f'<div class="_3-96 _2pio _2lek _2lel">{title}</div>'
            f'<div class="_3-96 _2let">{body}</div>'
            f'<div class="_3-94 _2lem">{date}</div></div>')


def fixture(root, generation="2021"):
    root.mkdir(mode=0o700)
    (root / "index.html").write_text("<html>synthetic export</html>")
    (root / "profile_information").mkdir()
    (root / "profile_information/profile_information.html").write_text(
        '<table><tr><th>Name</th><td>Example Person</td></tr>'
        '<tr><th>Username</th><td>example.test</td></tr></table>')
    (root / "posts").mkdir()
    date = "Jul 4, 2020, 1:23 PM" if generation == "2021" else "Jul 04, 2020 1:23pm"
    (root / ("posts/your_posts_1.html" if generation == "2021" else "posts/your_posts.html")).write_text(
        '<html><head><base href="../" /></head><body>' +
        box('<div><div class="_2pin"><div>Café 🦊 漢字<br>second &amp; third</div></div></div>', date) +
        box('<div><div class="_2pin"><div><a href="photos_and_videos/p.png"><img src="photos_and_videos/p.png"></a></div></div></div>',
            "2020-07-05T12:00:00+00:00", "Example Person added a photo.") + '</body></html>')
    (root / "photos_and_videos").mkdir()
    (root / "photos_and_videos/p.png").write_bytes(PNG)
    message_dir = root / ("messages/inbox/thread" if generation == "2021" else "messages/thread")
    message_dir.mkdir(parents=True)
    (message_dir / ("message_1.html" if generation == "2021" else "message.html")).write_text(
        box('<div><div></div><div>Hi 🐈<br>line two</div><div></div><div></div></div>',
            "Jan 2, 2020, 10:00 AM", "Example Person") +
        box('<div><div></div><div>Olá 🌍</div><div></div><div></div></div>',
            "Jan 2, 2020, 10:01 AM", "Other Person"))
    (root / "comments").mkdir()
    (root / "comments/comments.html").write_text("<html>omitted category</html>")
    return root


class ConvertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.tmp.name)
        self.source = fixture(self.parent / "source")

    def tearDown(self):
        self.tmp.cleanup()

    def run_conversion(self):
        return Conversion(self.source, ZoneInfo("America/New_York")).run()

    def test_both_layouts_unicode_relationships_and_copy(self):
        for generation in ["2021", "2022"]:
            with self.subTest(generation=generation):
                source = fixture(self.parent / generation, generation)
                run = Conversion(source, ZoneInfo("America/New_York")).run()
                self.assertEqual(run.issues, [])
                out = self.parent / (generation + "-converted")
                run.write(out, "copy", target="timelinize")
                run.unchanged()
                self.assertEqual(run.counts["posts_converted"], 2)
                self.assertEqual(run.counts["messages_converted"], 2)
                posts = json.loads((out / "archive/posts/your_posts_1.json").read_text())
                self.assertEqual(posts[0]["data"][0]["post"].encode("latin1").decode(), "Café 🦊 漢字\nsecond & third")
                self.assertEqual(posts[0]["timestamp"], 1593883380)
                self.assertEqual(len(posts[1]["attachments"][0]["data"]), 1)
                threads = list((out / "archive/messages").rglob("*.json"))
                self.assertEqual(len(json.loads(threads[0].read_text())["participants"]), 2)
                self.assertEqual((out / "archive/your_facebook_activity/posts/media/photos_and_videos/p.png").read_bytes(), PNG)
                self.assertEqual(run.report()["unconverted_html_by_category"], {"comments": 1, "index.html": 1})
                self.assertEqual(os.stat(out).st_mode & 0o777, 0o700)
                self.assertEqual(os.stat(out / "report.json").st_mode & 0o777, 0o600)

    def test_timezone_required_and_private_unresolved_provenance(self):
        run = Conversion(self.source).run()
        self.assertEqual(run.report()["issues_by_reason"], {"timezone_required": 3})
        run.evidence(self.parent / "plan")
        self.assertTrue(any(x.get("timestamp_basis") == "unresolved" for x in run.provenance))
        with self.assertRaisesRegex(Refusal, "unresolved_records"):
            run.write(self.parent / "blocked")
        self.assertFalse((self.parent / "blocked").exists())

    def test_export_timezone_precedes_operator_current_timezone(self):
        (self.source / "location").mkdir()
        (self.source / "location/timezone.html").write_text(box("America/Chicago", date="", title="Timezone"))
        with self.assertRaisesRegex(Refusal, "timezone_conflicts"):
            self.run_conversion()
        run = Conversion(self.source).run()
        self.assertEqual(run.report()["timezone"], "America/Chicago")
        self.assertEqual(run.report()["export_timezone"], "America/Chicago")
        self.assertEqual(run.files["posts/your_posts_1.json"][0]["timestamp"], 1593886980)
        self.assertIn("location/timezone.html", run.reads)

    def test_updated_prose_is_not_a_date_label(self):
        p = self.source / 'posts/your_posts_1.html'
        p.write_text(box('<div><div class="_2pin"><div>Updated my settings 🦊</div>'
                         '<div>Updated Jul 4, 2020, 1:25 PM</div></div></div>'))
        run = self.run_conversion()
        self.assertEqual(run.files['posts/your_posts_1.json'][0]['data'][0]['post'],
                         'Updated my settings 🦊')
        self.assertEqual(run.counts['post_update_labels'], 1)

    def test_dates_fold_gap_offset_epoch_and_precision(self):
        zone = ZoneInfo("America/New_York")
        for date, error in [("Nov 7, 2021, 1:30 AM", "ambiguous"),
                            ("Mar 14, 2021, 2:30 AM", "nonexistent"), ("not a date", "unsupported")]:
            with self.assertRaisesRegex(Refusal, error):
                timestamp(DOM(date).root, zone)
        self.assertEqual(timestamp(DOM("Nov 7, 2021, 1:30 AM").root, offset=parse_offset("-04:00"))[0], 1636263000)
        self.assertEqual(timestamp(DOM('<span data-utime="1600000000">opaque</span>').root)[0], 1600000000)
        self.assertEqual(timestamp(DOM("2020-07-04T17:23:59Z").root)[0], 1593883439)
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_offset("-04:99")

    def test_output_and_input_guards(self):
        run = self.run_conversion()
        for path in [self.source / "output", self.source, self.parent]:
            with self.assertRaises(Refusal):
                run.write(path)
        output = self.parent / "out"
        run.write(output)
        self.assertTrue((output / "archive/media/photos_and_videos/p.png").is_symlink())
        with self.assertRaisesRegex(Refusal, "output_exists"):
            run.write(output)
        (self.source / "index.html").write_text("changed")
        with self.assertRaisesRegex(Refusal, "source_stat_changed"):
            run.write(self.parent / "out2")

    def test_media_path_and_missing_reference_refusal(self):
        run = self.run_conversion()
        for path in ["../private.png", "%2e%2e/private.png", "/etc/passwd", "a\\b.png", "missing.png"]:
            with self.subTest(path=path), self.assertRaises(Refusal):
                run.media_ref(path)
        (self.source / "escape").symlink_to(self.parent)
        with self.assertRaisesRegex(Refusal, "source_symlink"):
            Conversion(self.source)

    def test_existing_json_and_ambiguous_layout_refusal(self):
        (self.source / "posts/your_posts.html").write_text("<html></html>")
        with self.assertRaisesRegex(Refusal, "ambiguous_posts"):
            self.run_conversion()
        (self.source / "existing.json").write_text("{}")
        with self.assertRaisesRegex(Refusal, "existing_json"):
            Conversion(self.source)

    def test_duplicate_records_preserved_determinism_and_no_fake_ids(self):
        p = self.source / "posts/your_posts_1.html"
        p.write_text(p.read_text() * 2)
        a, b = self.run_conversion(), self.run_conversion()
        self.assertEqual(a.files, b.files)
        self.assertEqual(a.provenance, b.provenance)
        self.assertEqual(a.counts["posts_converted"], 4)
        self.assertNotIn('"id"', json.dumps(a.files))

    def test_explicit_partial_quarantines_unresolved_records(self):
        run = Conversion(self.source).run()
        out = self.parent / "partial"
        run.write(out, allow_partial=True, target="timelinize")
        self.assertEqual(len(json.loads((out / "archive/posts/your_posts_1.json").read_text())), 1)
        self.assertEqual(len(json.loads((out / "refused-records.json").read_text())), 3)
        self.assertTrue(json.loads((out / "report.json").read_text())["partial_output_explicitly_allowed"])

    def test_five_part_message_and_missing_media_are_explicit(self):
        file = self.source / "messages/inbox/thread/message_1.html"
        file.write_text(box('<div><div></div><div>Text</div><div></div><div></div>'
                            '<div><a href="photos_and_videos/p.png"><img src="photos_and_videos/p.png"></a></div></div>',
                            title="Other Person"))
        run = self.run_conversion()
        self.assertEqual(run.counts["messages_converted"], 1)
        self.assertEqual(run.issues, [])
        (self.source / "photos_and_videos/p.png").unlink()
        run = self.run_conversion()
        self.assertEqual(run.report()["issues_by_reason"], {"missing_media": 2})

    def test_group_chunks_and_reaction_omission_are_counted(self):
        (self.source / "messages/inbox/thread/message_2.html").write_text(
            box('<div><div></div><div>Hello group</div><div></div><div>👍 Actor</div></div>',
                title="Third Person"))
        run = self.run_conversion()
        thread = next(v for k, v in run.files.items() if k.startswith("messages/"))
        self.assertEqual(len(thread["messages"]), 3)
        self.assertEqual(len(thread["participants"]), 3)
        self.assertEqual(run.counts["message_reaction_blocks_unconverted"], 1)


if __name__ == "__main__":
    unittest.main()
