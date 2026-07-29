"""The restore path's torn-tail check, which had drifted and had no test.

`object_backup_index._drop_torn_tail` carried a docstring saying it
"mirrors object_records._drop_torn_tail". That mirror was upgraded to be
quote-aware; this copy was not, silently, for months — and nothing caught
it because **it had no test at all**.

It was worse than the read-side original had been. The old body returned
`text` unchanged whenever it ended with a newline — including a newline
sitting INSIDE an unclosed quote. So a backup archive holding a row torn
mid-quoted-field was folded as if complete, and a restore could resurrect
a garbled row that a live read of the identical bytes handles correctly.

The fix is not a second correct copy. It CALLS
`object_records.committed_prefix_len`, which is exported for this reason:
two implementations of one invariant drift, one cannot.
"""

import object_backup_index
import object_records


def drop(text):
    return object_backup_index._drop_torn_tail(text)


def test_the_two_modules_now_share_one_implementation():
    """The actual fix. A private copy is what failed here, so the property
    worth pinning is that there is no longer a copy to drift."""
    assert object_records.committed_prefix_len is object_records._committed_prefix_len
    source = (
        __import__("pathlib").Path(object_backup_index.__file__).read_text()
    )
    assert "object_records.committed_prefix_len(text)" in source
    assert 'text.rfind("\\n")' not in source


def test_a_newline_inside_a_quote_is_not_a_row_boundary():
    """THE regression. The old body returned this untouched because the
    file 'ends with a newline' — that newline is inside an unclosed quote,
    so the row is torn and must go."""
    torn = 'id\tnote\n1\t"line one\n'
    assert drop(torn) == "id\tnote\n"


def test_a_complete_multiline_row_survives_intact():
    """The other half: a quoted newline in a CLOSED field is content, and
    dropping it would destroy committed data."""
    whole = 'id\tnote\n1\t"line one\nline two"\n'
    assert drop(whole) == whole


def test_an_ordinary_torn_tail_still_goes():
    assert drop("a\tb\nc\td\nhalf-writ") == "a\tb\nc\td\n"


def test_a_quote_free_file_behaves_exactly_as_before():
    """The cheap common case must be byte-identical to the old rfind, or
    this 'fix' is a behaviour change to every backup ever taken."""
    for text in ("", "a\tb\n", "a\tb\nc\td\n", "a\tb\ntorn", "no newline at all"):
        old = (text if text == "" or text.endswith("\n")
               else (text[: text.rfind("\n") + 1] if "\n" in text else ""))
        assert drop(text) == old, repr(text)


def test_a_torn_backup_folds_without_the_garbled_row(tmp_path):
    """End to end through the parser that actually reads an archive: the
    torn row must not appear among the folded records."""
    header = ["_op", "id", "note"]
    text = (
        "_op\tid\tnote\n"
        "\t1\tfine\n"
        '\t2\t"torn mid-field\n'
    )
    folded = object_backup_index._parse_append_tsv_by_id(text, header)
    assert set(folded) == {"1"}
    assert "2" not in folded
