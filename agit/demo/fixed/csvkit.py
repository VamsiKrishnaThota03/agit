"""Tiny CSV helpers used by the nightly reports job.

Deliberately hand-rolled: the demo is about what the agent does to this file,
and about being able to ask, later, why it did it.
"""


def parse_row(line):
    """Split one CSV line into its fields.

    Commas inside a quoted field are literal, and a doubled quote inside a
    quoted field means one quote character. Unquoted fields are trimmed;
    quoted fields are kept byte for byte, because the quotes are what the
    writer used to say "this whitespace matters".
    """
    fields = []
    buffer = []
    in_quotes = False
    was_quoted = False
    i = 0
    while i < len(line):
        char = line[i]
        if in_quotes:
            if char == '"':
                if line[i + 1:i + 2] == '"':
                    buffer.append('"')
                    i += 2
                    continue
                in_quotes = False
            else:
                buffer.append(char)
        elif char == '"':
            in_quotes = True
            was_quoted = True
        elif char == ",":
            fields.append(_finish(buffer, was_quoted))
            buffer = []
            was_quoted = False
        else:
            buffer.append(char)
        i += 1
    fields.append(_finish(buffer, was_quoted))
    return fields


def _finish(buffer, was_quoted):
    text = "".join(buffer)
    return text if was_quoted else text.strip()


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
