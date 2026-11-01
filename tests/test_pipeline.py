import json
import unittest
from pathlib import Path

from showfars.pipeline import valid_https, build_caption


class PipelineTests(unittest.TestCase):
    def test_allowlisted_https_host(self):
        self.assertTrue(valid_https("https://media.example.org/a.mp4", ["example.org"]))
        self.assertTrue(valid_https("https://cdn.example.org/a.mp4", ["example.org"]))
        self.assertFalse(valid_https("http://media.example.org/a.mp4", ["example.org"]))
        self.assertFalse(valid_https("https://example.org.evil.test/a.mp4", ["example.org"]))
        self.assertFalse(valid_https("https://unknown.test/a.mp4", ["example.org"]))

    def test_caption_html_escaped(self):
        caption = build_caption({"title": "<script>", "year": 2025, "description": "A & B"})
        self.assertNotIn("<script>", caption)
        self.assertIn("&lt;script&gt;", caption)
        self.assertIn("A &amp; B", caption)

    def test_movies_manifest_is_json_list(self):
        path = Path(__file__).parents[1] / "data" / "movies.json"
        self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), list)


if __name__ == "__main__":
    unittest.main()
