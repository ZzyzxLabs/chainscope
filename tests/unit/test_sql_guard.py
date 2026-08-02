"""The read-only SQL guard, and the bypass that made it decorative.

``assert_read_only_sql`` calls itself a guard rail rather than a security
boundary, and that is the right framing --- the connection is opened with
``enable_external_access`` off, and *that* is the control. But one of the things
the rail is supposed to catch, DuckDB itself will not: chained statements.
``con.execute()`` runs every statement it is given, and the store has no
transaction to roll back to.

The bypass was an ordering mistake. Comments were stripped before string
literals, so a ``--`` *inside a string* swallowed the rest of the line for the
check while DuckDB saw the original text. Measured against a real view:

    select '--' ; drop table transfers

passed the guard, executed both statements, and the table was gone --- the next
query answered "Table with name transfers does not exist". Reachable from the
MCP agent's SQL tool, so from anything composing a query on a user's behalf.

Blanking strings first is the fix, and the direction matters: a string can
contain a comment marker, and a comment can contain a quote.
"""

from __future__ import annotations

import pytest

from chainscope.store.analytics import UnsafeQuery, assert_read_only_sql


def refused(query: str) -> bool:
    try:
        assert_read_only_sql(query)
        return False
    except UnsafeQuery:
        return True


class TestChainedStatements:
    """Only the first statement is ever checked, so anything that lets a second
    one through defeats the whole guard."""

    def test_the_plain_form_is_refused(self):
        assert refused("select 1; drop table transfers")

    def test_a_comment_marker_inside_a_string_cannot_hide_the_semicolon(self):
        """The measured bypass. It destroyed a real table."""
        assert refused("select '--' ; drop table transfers")

    @pytest.mark.parametrize(
        "query",
        [
            "select '--' ; drop table transfers",
            "select 'x--y' ; delete from transfers",
            "select '/*' ; drop table transfers",
            "select '--', '--' ; drop table transfers",
            "select 'it''s --' ; drop table transfers",
            "SELECT '--' ; DROP TABLE transfers",
        ],
    )
    def test_every_quoting_shape_of_it(self, query):
        assert refused(query)

    def test_a_comment_before_the_semicolon_is_still_refused(self):
        assert refused("select 1 --\n; drop table transfers")

    def test_a_trailing_semicolon_is_fine(self):
        """Refusing this would make the guard reject ordinary pasted SQL."""
        assert_read_only_sql("select 1;")


class TestStringsAreNotStatements:
    """The other direction has to keep holding: searching for the literal text
    'drop' is an ordinary thing to do in a forensics tool."""

    def test_a_forbidden_word_inside_a_string_is_allowed(self):
        assert_read_only_sql("select * from attributions where label = 'drop'")

    def test_a_forbidden_word_inside_a_comment_is_allowed(self):
        assert_read_only_sql("select 1 -- drop table transfers")

    def test_an_escaped_quote_does_not_end_the_string(self):
        assert_read_only_sql("select * from attributions where label = 'it''s drop'")

    def test_a_quote_inside_a_comment_does_not_open_a_string(self):
        """If the comment's apostrophe were treated as opening a literal, the
        rest of the query would be blanked and a real statement could hide in
        it."""
        assert refused("select 1 /* don't */ ; drop table transfers")


class TestPragma:
    """DuckDB's PRAGMA is both an inspection verb and a configuration verb, and
    the guard admitted the whole family. `enable_external_access` is refused by
    DuckDB once data is loaded, but that is DuckDB's decision to make, not a
    property of the guard."""

    def test_a_read_only_pragma_still_works(self):
        assert_read_only_sql("pragma version")
        assert_read_only_sql("pragma show_tables")

    @pytest.mark.parametrize(
        "query",
        [
            "pragma enable_external_access=true",
            "pragma memory_limit='10GB'",
            "pragma threads=64",
            "PRAGMA enable_external_access = true",
        ],
    )
    def test_an_assigning_pragma_is_refused(self, query):
        assert refused(query)


class TestTheOrdinaryCases:
    def test_a_select_passes(self):
        assert_read_only_sql("select * from transfers limit 10")

    def test_a_cte_passes(self):
        assert_read_only_sql("with t as (select 1) select * from t")

    def test_an_empty_query_is_refused(self):
        assert refused("   ")

    def test_a_write_is_refused(self):
        assert refused("delete from transfers")

    def test_a_file_read_is_refused(self):
        assert refused("select * from read_csv('/etc/passwd')")
