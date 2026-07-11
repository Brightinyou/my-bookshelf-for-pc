import unittest

from services import pdfcols


class FakeTextPage:
    def __init__(self, text, boxes):
        self.text = text
        self.boxes = boxes

    def count_chars(self):
        return len(self.text)

    def get_text_range(self):
        return self.text

    def get_charbox(self, index, loose=True):
        return self.boxes[index]


class FakePage:
    def __init__(self, text, boxes, size=(200, 100)):
        self.size = size
        self.textpage = FakeTextPage(text, boxes)

    def get_size(self):
        return self.size

    def get_textpage(self):
        return self.textpage


def box(x0, y0, x1, y1, page_height=100):
    """Top-origin test coordinates to pypdfium2's bottom-origin charbox."""
    return x0, page_height - y1, x1, page_height - y0


class GlyphRepairTests(unittest.TestCase):
    def test_repairs_degenerate_line_initial_from_next_glyph(self):
        text = "end\nAuthor"
        boxes = [
            box(10, 10, 15, 20), box(15, 10, 20, 20), box(20, 10, 25, 20),
            (0, 0, 0, 0),
            box(80, 10, 80, 10),
            box(16, 30, 21, 40), box(21, 30, 26, 40), box(26, 30, 31, 40),
            box(31, 30, 36, 40), box(36, 30, 41, 40),
        ]
        page = FakePage(text, boxes)

        _w, _h, glyphs, _mh, _mw = pdfcols._glyphs(page)
        initial = next(g for g in glyphs if g[3] == "A")
        following = next(g for g in glyphs if g[3] == "u")

        self.assertEqual(initial[1], following[1])
        self.assertEqual(initial[0], following[0])
        self.assertTrue(initial[6])
        lines = [line for line in pdfcols._reading_order(page).splitlines() if line]
        self.assertEqual(lines, ["end", "Author"])

    def test_close_float_first_letter_inversion_uses_stream_order(self):
        text = "Author"
        boxes = [
            box(16.000001, 10, 16.000001, 10),
            box(16, 10, 21, 20), box(21, 10, 26, 20), box(26, 10, 31, 20),
            box(31, 10, 36, 20), box(36, 10, 41, 20),
        ]
        page = FakePage(text, boxes)

        _w, _h, glyphs, _mh, _mw = pdfcols._glyphs(page)
        initial = next(g for g in glyphs if g[3] == "A")
        following = next(g for g in glyphs if g[3] == "u")

        self.assertEqual(initial[0], following[0])
        self.assertEqual(pdfcols._reading_order(page).strip(), "Author")

    def test_repairs_partial_normal_outlier_raw_word(self):
        text = "Introduction"
        boxes = [
            box(70, 10, 75, 20), box(75, 10, 80, 20),
            box(80, 10, 85, 20), box(85, 10, 90, 20),
            box(90, 10, 90, 10), box(90, 10, 90, 10),
            box(30, 30, 35, 40), box(35, 30, 40, 40),
            box(40, 30, 45, 40), box(45, 30, 50, 40),
            box(50, 30, 55, 40), box(55, 30, 60, 40),
        ]
        page = FakePage(text, boxes)

        _w, _h, glyphs, _mh, _mw = pdfcols._glyphs(page)
        dominant = next(g for g in glyphs if g[3] == "u")
        repaired = [g for g in glyphs if g[5] < 6]

        self.assertTrue(all(g[6] for g in repaired))
        self.assertTrue(all(g[0] == dominant[0] for g in repaired))
        self.assertTrue(all(g[1] == dominant[1] for g in repaired))
        self.assertFalse(dominant[6])
        self.assertEqual(pdfcols._reading_order(page).strip(), text)

    def test_previous_anchor_uses_previous_glyph_end(self):
        text = "abX"
        boxes = [
            box(10, 10, 15, 20), box(15, 10, 20, 20),
            box(19.999999, 10, 19.999999, 10),
        ]

        _w, _h, glyphs, _mh, _mw = pdfcols._glyphs(FakePage(text, boxes))
        previous = next(g for g in glyphs if g[3] == "b")
        final = next(g for g in glyphs if g[3] == "X")

        self.assertEqual(final[0], previous[2])
        self.assertEqual(pdfcols._text(glyphs, 2), "abX")

    def test_does_not_repair_across_raw_line_break(self):
        text = "A\nb"
        boxes = [box(80, 10, 80, 10), (0, 0, 0, 0), box(10, 30, 15, 40)]

        _w, _h, glyphs, _mh, _mw = pdfcols._glyphs(FakePage(text, boxes))
        initial = next(g for g in glyphs if g[3] == "A")

        self.assertEqual(initial[0], 80)
        self.assertEqual(initial[1], 10)
        self.assertFalse(initial[6])

    def test_repairs_degenerate_space_before_next_glyph(self):
        text = "j u"
        boxes = [
            box(10, 10, 15, 20),
            box(40, 10, 40, 10),  # 뒤쪽 x에 남은 퇴화 space
            box(15, 10, 20, 20),
        ]
        page = FakePage(text, boxes)

        _w, _h, glyphs, _mh, _mw = pdfcols._glyphs(page)

        space = next(glyph for glyph in glyphs if glyph[4])
        following = next(glyph for glyph in glyphs if glyph[3] == "u")

        self.assertTrue(space[6])
        self.assertEqual(space[0], following[0])
        self.assertEqual(space[1], following[1])
        self.assertEqual(pdfcols._reading_order(page).strip(), "j u")

    def test_repairs_normal_space_sorted_behind_next_glyph(self):
        text = "robots are\nsocial sphere"
        boxes = [box(10 + i * 5, 10, 15 + i * 5, 20) for i in range(6)]
        boxes.append(box(40.000001, 10, 42, 20))
        boxes.extend(box(40 + i * 5, 10, 45 + i * 5, 20) for i in range(3))
        boxes.append((0, 0, 0, 0))
        boxes.extend(box(10 + i * 5, 30, 15 + i * 5, 40) for i in range(6))
        boxes.append(box(40.000001, 30, 42, 40))
        boxes.extend(box(40 + i * 5, 30, 45 + i * 5, 40) for i in range(6))
        page = FakePage(text, boxes)

        _w, _h, glyphs, _mh, _mw = pdfcols._glyphs(page)
        spaces = [glyph for glyph in glyphs if glyph[4]]

        self.assertTrue(all(space[6] for space in spaces))
        self.assertEqual(
            [line for line in pdfcols._reading_order(page).splitlines() if line],
            ["robots are", "social sphere"],
        )

    def test_repaired_glyph_suppresses_inferred_spaces(self):
        chars = [
            (0, 10, 5, "A", False, 0, False, False),
            (100, 10, 100, "b", False, 1, True, False),
            (105, 10, 110, "c", False, 2, False, False),
        ]

        self.assertEqual(pdfcols._text(chars, 2), "Abc")

    def test_layout_gap_statistics_ignore_repaired_glyphs(self):
        row = [
            (0, 10, 5, "a", False, 0, False, False),
            (7, 10, 7, "x", False, 1, True, False),
            (10, 10, 15, "b", False, 2, False, False),
        ]

        self.assertEqual(pdfcols._adaptive_space_gap([row], 10), 2.5)

    def test_unmapped_line_end_glyph_keeps_hyphen_behavior(self):
        text = "benefi\ufffe"
        boxes = [
            box(i * 5, 10, i * 5 + 5, 20)
            for i in range(len(text) - 1)
        ]
        boxes.append(box(30, 10, 30, 10))

        _w, _h, glyphs, _mh, _mw = pdfcols._glyphs(FakePage(text, boxes))
        broken = next(g for g in glyphs if g[7])

        self.assertTrue(broken[6])
        self.assertEqual(pdfcols._text(glyphs, 2), "benefi-")

    def test_unmapped_glyph_inside_ascii_word_joins_fragments(self):
        text = "educa\ufffetional"
        boxes = [box(i * 5, 10, i * 5 + 5, 20) for i in range(5)]
        boxes.append(box(25, 10, 25, 10))
        boxes.extend(box(25 + i * 5, 10, 30 + i * 5, 20) for i in range(6))
        page = FakePage(text, boxes)

        _w, _h, glyphs, _mh, _mw = pdfcols._glyphs(page)

        self.assertEqual(pdfcols._reading_order(page).strip(), "educational")

    def test_unmapped_glyph_between_non_ascii_text_keeps_space(self):
        chars = [
            (0, 10, 5, "가", False, 0, False, False),
            (5, 10, 5, "\ufffe", False, 1, True, True),
            (5, 10, 10, "나", False, 2, False, False),
        ]

        self.assertEqual(pdfcols._text(chars, 2), "가 나")


if __name__ == "__main__":
    unittest.main()
