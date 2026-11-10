import unittest

from csvkit import parse, parse_row


class TestQuotedFields(unittest.TestCase):
    def test_comma_inside_quotes_stays_in_one_field(self):
        self.assertEqual(
            parse_row('Mumbai,"Powai, Hiranandani",400076'),
            ["Mumbai", "Powai, Hiranandani", "400076"],
        )

    def test_surrounding_quotes_are_removed(self):
        self.assertEqual(parse_row('"a","b"'), ["a", "b"])

    def test_doubled_quote_is_one_literal_quote(self):
        self.assertEqual(parse_row('"say ""hi""",x'), ['say "hi"', "x"])

    def test_document_with_quoted_commas(self):
        text = 'name,address\nAsha,"12, MG Road"\n'
        self.assertEqual(parse(text), [["name", "address"], ["Asha", "12, MG Road"]])


if __name__ == "__main__":
    unittest.main()
