import unittest

from cre_brain.adapters.ffmpeg_v1 import _caption_filters
from cre_format3 import _regroup_captions


class Format3CaptionControlTests(unittest.TestCase):
    def render(self):
        words = [
            {"text": "one", "start_frame": 10, "end_frame": 16, "emphasis": False},
            {"text": "two", "start_frame": 17, "end_frame": 23, "emphasis": False},
            {"text": "three", "start_frame": 24, "end_frame": 31, "emphasis": False},
        ]
        return {"segments": [{"id": "segment_1", "source_in_frame": 100,
                               "source_out_frame": 200, "output_start_frame": 0}],
                "captions": [{"id": "caption_1", "start_frame": 10,
                              "end_frame": 31, "words": words}]}

    def test_one_word_mode_creates_one_word_per_caption(self):
        render = self.render()
        _regroup_captions(render, 1, [])
        self.assertEqual([1, 1, 1], [len(cue["words"]) for cue in render["captions"]])

    def test_two_word_mode_never_crosses_speaker_change(self):
        render = self.render()
        _regroup_captions(render, 2, [117])
        self.assertEqual([1, 2], [len(cue["words"]) for cue in render["captions"]])

    def test_caption_off_bypasses_all_drawtext_filters(self):
        filters, label = _caption_filters({}, [], "unused.ttf", None,
                                          {"render_enabled": False}, "video_in")
        self.assertEqual([], filters)
        self.assertEqual("video_in", label)


if __name__ == "__main__":
    unittest.main()
