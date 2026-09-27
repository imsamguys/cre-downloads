import unittest
from pathlib import Path

from cre_brain.face_placement import (boxes_overlap_with_padding, choose_below_cheek_placement,
                                      choose_caption_placement, choose_plain_caption_placement)


FONT = str(Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Montserrat.ttf")
REGULAR = FONT
HEAVY = FONT


class FacePlacementTests(unittest.TestCase):
    def choose(self, face, heavy_words, heavy_size=58):
        return choose_caption_placement(
            face, "small words", heavy_words, REGULAR, HEAVY, 38, heavy_size, 0.15
        )

    def test_left_face_places_caption_on_right_with_padding(self):
        result = self.choose({"x": 150, "y": 70, "w": 150, "h": 190}, ["bold"])
        self.assertEqual(result.placement["side"], "right")
        self.assertFalse(boxes_overlap_with_padding(result.caption_bbox, result.face_bbox))

    def test_right_face_places_caption_on_left_with_padding(self):
        result = self.choose({"x": 470, "y": 70, "w": 130, "h": 180}, ["bold", "words"])
        self.assertEqual(result.placement["side"], "left")
        self.assertFalse(boxes_overlap_with_padding(result.caption_bbox, result.face_bbox))

    def test_long_caption_falls_below(self):
        result = self.choose({"x": 300, "y": 30, "w": 180, "h": 300},
                             ["international", "relations", "warfare"], heavy_size=72)
        self.assertEqual(result.placement["side"], "below")
        self.assertIn("below", result.fallback)

    def test_plain_caption_wraps_beside_face(self):
        result = choose_plain_caption_placement(
            {"x": 120, "y": 60, "w": 160, "h": 210},
            "everything what happens he starts", HEAVY, 46, 56, 2)
        self.assertEqual(result.placement["side"], "right")
        self.assertFalse(boxes_overlap_with_padding(result.caption_bbox, result.face_bbox))

    def test_plain_caption_falls_below_after_two_lines(self):
        result = choose_plain_caption_placement(
            {"x": 300, "y": 40, "w": 180, "h": 260},
            "one exceptionally long phrase that cannot fit beside the centered face",
            HEAVY, 46, 56, 2)
        self.assertEqual(result.placement["side"], "below")

    def test_below_cheek_is_centered_and_accepts_global_adjustment(self):
        face = {"x": 260, "y": 90, "w": 140, "h": 170}
        result = choose_below_cheek_placement(
            face, "HELLO", HEAVY, 46, 56, x_offset=20, y_offset=-10)
        self.assertEqual(result.placement["side"], "below")
        self.assertEqual(result.placement["anchor_x"], 350)
        self.assertGreater(result.placement["anchor_y"], face["y"] + face["h"] // 2)



if __name__ == "__main__":
    unittest.main()
