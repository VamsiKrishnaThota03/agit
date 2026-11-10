import unittest

from csvkit import column, parse, parse_row


class TestCsvKit(unittest.TestCase):
    def test_simple_row(self):
        self.assertEqual(parse_row("a, b ,c"), ["a", "b", "c"])

    def test_parse_skips_blank_lines(self):
        self.assertEqual(parse("a,b\n\nc,d"), [["a", "b"], ["c", "d"]])

    def test_column(self):
        self.assertEqual(column([["a", "b"], ["c", "d"]], 1), ["b", "d"])


if __name__ == "__main__":
    unittest.main()
