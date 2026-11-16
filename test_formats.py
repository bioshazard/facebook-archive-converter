"""Public synthetic fixtures exercise interchange and consumer boundaries."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from zoneinfo import ZoneInfo

from convert import Conversion
from formats import compatibility, modern_files, normalized
from test_convert import fixture, box


class FormatTests(unittest.TestCase):
    def test_unicode_round_trip_and_explicit_encoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = fixture(Path(tmp) / "source")
            # All repaired fields and all intentionally untouched fields differ.
            (source / "profile_information/profile_information.html").write_text(
                '<table><tr><th>Name</th><td>Café 🦊</td></tr>'
                '<tr><th>Username</th><td>example.test</td></tr></table>')
            (source / "photos_and_videos/your_photos.html").write_text(box(
                '<a href="photos_and_videos/p.png"><img src="photos_and_videos/p.png"></a>'
                '<div class="_3-95">Caption 🦊</div>', title='Title 漢字'))
            run = Conversion(source, ZoneInfo("America/New_York")).run()
            document = normalized(run.files, run.provenance)
            self.assertEqual(document["profile"]["name"], "Café 🦊")
            self.assertEqual(document["threads"][0]["messages"][0]["text"], "Hi 🐈\nline two")
            self.assertEqual(modern_files(document), run.files)
            self.assertEqual(compatibility(run.files, "meta-json"), run.files)
            encoded = compatibility(run.files, "timelinize")
            self.assertEqual(encoded["profile_information/profile_information.json"]["profile_v2"]["name"]["full_name"].encode("latin1").decode(), "Café 🦊")
            for path, obj in encoded.items():
                if path.startswith("posts/your_posts_"):
                    self.assertEqual(obj[0]["title"], run.files[path][0]["title"])
                if path.startswith("posts/your_uncategorized"):
                    self.assertEqual(obj["other_photos_v2"][0]["title"].encode("latin1").decode(), "Title 漢字")
            # Adapter cannot mutate the normalized parser representation.
            self.assertEqual(modern_files(document), run.files)

    def test_normalized_portable_archive_and_cli_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = fixture(root / "source")
            out = root / "out"
            proc = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("convert.py")), str(source),
                "--source-timezone", "America/New_York", "--media", "copy", "--output", str(out)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            report = json.loads((out / "report.json").read_text())
            self.assertEqual(report["target"], "normalized")
            self.assertFalse((out / "archive/posts").exists())
            document = json.loads((out / "normalized.json").read_text())
            uri = document["posts"][1]["attachments"][0]["uri"]
            self.assertTrue(uri.startswith("media/"))
            with zipfile.ZipFile(out / "archive.zip") as archive:
                self.assertEqual(json.loads(archive.read("normalized.json")), document)
                self.assertEqual(archive.read(uri), (source / "photos_and_videos/p.png").read_bytes())
            self.assertFalse((out / "INCOMPLETE").exists())

    def test_compatibility_outputs_keep_unicode_document_and_resolvable_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = Conversion(fixture(root / "source"), ZoneInfo("America/New_York")).run()
            for target in ["meta-json", "timelinize"]:
                out = root / target
                run.write(out, "copy", target=target)
                doc = json.loads((out / "normalized.json").read_text())
                self.assertEqual(doc["posts"][0]["text"][0], "Café 🦊 漢字\nsecond & third")
                uri = doc["posts"][1]["attachments"][0]["uri"]
                self.assertTrue((out / "archive" / uri).is_file())
                self.assertEqual(json.loads((out / "source-manifest.json").read_text())["media_sources"][uri], "photos_and_videos/p.png")


if __name__ == "__main__":
    unittest.main()
