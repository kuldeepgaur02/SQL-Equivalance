import pytest

from cexgen.errors import InputError
from cexgen.input import Case

SCHEMA = "CREATE TABLE t (x int);"


def make(q1="SELECT x FROM t", q2="SELECT x FROM t WHERE x > 0", schema=SCHEMA, **kw):
    return Case("c", schema, q1, q2, **kw)


def test_trailing_semicolon_and_whitespace_removed():
    case = make(q1="  SELECT x FROM t ;  \n")
    assert case.q1 == "SELECT x FROM t"


def test_semicolon_inside_string_is_kept():
    assert make(q1="SELECT ';' FROM t;").q1 == "SELECT ';' FROM t"


@pytest.mark.parametrize("q", ["", "   \n", "-- only a comment", "/* c */ ;"])
def test_empty_or_comment_only_query(q):
    with pytest.raises(InputError, match="Q1"):
        make(q1=q)


def test_two_statements_rejected_with_line():
    with pytest.raises(InputError, match="one statement, found 2.*line 2"):
        make(q1="SELECT 1;\nDELETE FROM t")


def test_positional_parameters_rejected():
    with pytest.raises(InputError, match=r"\$1"):
        make(q1="SELECT x FROM t WHERE x = $1")


def test_psql_meta_command_rejected():
    with pytest.raises(InputError, match="psql meta-commands"):
        make(schema="\\connect shop\n" + SCHEMA)


def test_unterminated_string_points_at_it():
    with pytest.raises(InputError, match="unterminated string literal.*line 1, column 27"):
        make(q1="SELECT x FROM t WHERE x = 'oops")


def test_nul_byte_rejected():
    with pytest.raises(InputError, match="NUL"):
        make(q2="SELECT x\x00 FROM t")


def test_schema_with_only_comments_rejected():
    with pytest.raises(InputError, match="schema has no SQL"):
        make(schema="-- nothing yet")


def test_name_must_be_printable():
    with pytest.raises(InputError):
        Case("  ", SCHEMA, "SELECT 1", "SELECT 2")
    with pytest.raises(InputError):
        Case("a\nb", SCHEMA, "SELECT 1", "SELECT 2")


def test_non_text_rejected():
    with pytest.raises(InputError, match="must be text"):
        make(q1=None)


def test_meta_is_frozen_copy_and_must_be_json():
    src = {"expected": "different", "tags": ["a"]}
    case = make(meta=src)
    src["tags"].append("b")
    assert case.meta["tags"] == ["a"]
    with pytest.raises(TypeError):
        case.meta["x"] = 1
    with pytest.raises(InputError, match="JSON"):
        make(meta={"bad": object()})


def test_identical_queries_detected():
    assert make(q1="select x from t -- c", q2="SELECT  x\nFROM t;").identical_queries
    assert not make().identical_queries


def test_unicode_identifiers_and_strings_fine():
    case = make(schema='CREATE TABLE "Bücher" ("Titel" text);', q1="SELECT 'café' FROM \"Bücher\"")
    assert "café" in case.q1
