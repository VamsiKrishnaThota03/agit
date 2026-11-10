"""Tiny CSV helpers used by the nightly reports job.

Deliberately hand-rolled: the demo is about what the agent does to this file,
and about being able to ask, later, why it did it.
"""
import csv


def parse_row(line):
    """Split one CSV line into its fields.

    Quoting is the standard library's problem, not ours: csv.reader already
    implements RFC 4180, including commas inside quotes and "" as an escaped
    quote. Fields are trimmed afterwards to preserve this module's older
    behaviour.
    """
    for fields in csv.reader([line]):
        return [field.strip() for field in fields]
    return [""]


def parse(text):
    """Parse a whole CSV document into a list of rows."""
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        rows.append(parse_row(line))
    return rows


def column(rows, index):
    """Pull one column out of parsed rows."""
    return [row[index] for row in rows if index < len(row)]
