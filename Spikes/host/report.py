"""The one report format every spike speaks.

A spike is a firmware build that measures itself and prints its findings. There
are seven of them across two runtimes, and without one format that is seven
parsers, six of which are written in a hurry at the end.

The format is deliberately dull:

    RESULT case=trigger_to_output block=32 min=41 mean=43 max=68
    CASE case=flash_erase state=HUNG
    DONE cases=6

One line, one record. A leading keyword, then `key=value` pairs separated by
spaces. Values are bare - no quoting, no escaping, no spaces inside a value -
because a spike printing this is a C program with a 24-bit cycle counter and no
business owning a serialiser.

Anything that does not parse is kept as noise rather than dropped. A spike that
crashes prints a traceback, and the traceback is usually the finding.
"""


def parse_line(line):
    """One line as (keyword, {key: value}), or None if it is not a record.

    Values that look like integers or floats come back as numbers, because
    every consumer of this wants arithmetic and none of them want to remember
    which fields were which.
    """
    line = line.strip()
    if not line:
        return None
    parts = line.split()
    keyword = parts[0]
    if not keyword.isupper() or not keyword.isalpha():
        # Not a record. Serial from a badge carries banners, ANSI title
        # sequences and tracebacks, and none of them are ours.
        return None
    fields = {}
    for part in parts[1:]:
        if "=" not in part:
            return None
        key, _, value = part.partition("=")
        if not key:
            return None
        fields[key] = _coerce(value)
    return keyword, fields


def _coerce(value):
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def parse(text):
    """Every record in a capture, plus the lines that were not records.

    Returns (records, noise). Records are (keyword, fields) in the order they
    arrived; noise is every other non-empty line, which is where a traceback
    ends up.
    """
    records = []
    noise = []
    for line in text.splitlines():
        parsed = parse_line(line)
        if parsed is None:
            stripped = line.strip()
            if stripped:
                noise.append(stripped)
        else:
            records.append(parsed)
    return records, noise


def select(records, keyword):
    """Just the fields of every record with this keyword."""
    return [fields for word, fields in records if word == keyword]


def cases(records):
    """What happened to each case, as {name: state}.

    A spike announces a case before running it and reports it afterwards, so a
    case that appears without a result is one the badge died inside. That is
    the whole point of the format: the campaign has to be able to say which
    case killed it, having been nowhere near it at the time.
    """
    state = {}
    for word, fields in records:
        name = fields.get("case")
        if name is None:
            continue
        if word == "CASE":
            state[name] = fields.get("state", "STARTED")
        elif word == "RESULT":
            state[name] = "OK"
    return state
