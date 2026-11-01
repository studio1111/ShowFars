import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from showfars.telegram import TelegramPublisher


class TelegramResumeTests(unittest.TestCase):
    def test_resumes_after_recorded_1080p_without_duplicate_lead(self):
        with tempfile.TemporaryDirectory() as directory:
            outputs = {}
            for quality in ("1080p", "720p", "480p"):
                path = Path(directory) / f"{quality}.mp4"
                path.write_bytes(b"fake-video")
                outputs[quality] = path

            publisher = TelegramPublisher("fake-token", "@channel")
            calls = []

            def fake_post(method, data, files=None):
                calls.append((method, data.get("reply_to_message_id")))
                return {"message_id": 20 if method == "sendVideo" else 10}

            publisher._post = fake_post
            ids = publisher.publish_movie(
                {"title": "Test"}, "caption", None, outputs,
                existing_ids={"lead": 5, "1080p": 6},
            )
            self.assertEqual(ids["lead"], 5)
            self.assertEqual(ids["1080p"], 6)
            self.assertEqual([call[0] for call in calls], ["sendVideo", "sendVideo"])
            self.assertEqual([call[1] for call in calls], [5, 5])


if __name__ == "__main__":
    unittest.main()
