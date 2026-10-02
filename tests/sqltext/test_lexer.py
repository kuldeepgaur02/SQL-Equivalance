import pytest

from cexgen.sqltext.lexer import (COMMENT, DOLLAR, IDENT, STRING, LexError, find_backslash, find_positional_params,
                                  fingerprint, line_col, split_statements, tokenize)


def kinds(sql):
    return [(t.kind, t.text) for t in tokenize(sql) if t.kind != "code"]


@pytest.mark.parametrize("sql, expected", [
    ("SELECT 'a;b'", [(STRING, "'a;b'")]),
    ("SELECT 'it''s'", [(STRING, "'it''s'")]),
    ("SELECT E'a\\'b'", [(STRING, "E'a\\'b'")]),                 # backslash escape in E''
    ("SELECT 'a\\'", [(STRING, "'a\\'")]),                       # standard string: backslash is literal
    ('SELECT "we;ird""col"', [(IDENT, '"we;ird""col"')]),
    ("SELECT $$a;b$$", [(DOLLAR, "$$a;b$$")]),
    ("SELECT $fn$ x $$ y $fn$", [(DOLLAR, "$fn$ x $$ y $fn$")]),
    ("SELECT 1 -- a;b\n", [(COMMENT, "-- a;b")]),
    ("SELECT /* a /* nested; */ b */ 1", [(COMMENT, "/* a /* nested; */ b */")]),
])
def test_tokens(sql, expected):
    assert kinds(sql) == expected


def test_e_inside_a_word_is_not_an_escape_prefix():
    # "type'..." : the E of "type" must not turn the string into an E'' string
    assert kinds("SELECT type'a\\'") == [(STRING, "'a\\'")]


def test_identifier_with_dollar_is_not_a_dollar_quote():
    assert kinds("SELECT a$b$ FROM t") == []


def test_positional_parameter_is_not_a_dollar_quote():
    assert kinds("SELECT $1, $2") == []
    assert len(find_positional_params("SELECT $1 WHERE x = $2 AND y = '$3'")) == 2


@pytest.mark.parametrize("sql", ["SELECT 'abc", 'SELECT "abc', "SELECT $$abc", "SELECT /* abc", "SELECT E'ab\\'"])
def test_unterminated_raise_with_offset(sql):
    with pytest.raises(LexError) as e:
        tokenize(sql)
    assert e.value.offset == 7


def test_split_statements_respects_strings_comments_and_bodies():
    sql = """
    -- header comment
    CREATE TABLE t (x text DEFAULT 'a;b');
    /* only a comment */ ;
    CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1; $$;
    SELECT 1"""
    stmts = split_statements(sql)
    assert len(stmts) == 3                    # the comment-only statement is dropped
    assert stmts[0].text.startswith("-- header comment")
    assert stmts[1].text.startswith("CREATE FUNCTION") and stmts[1].text.endswith("$$")
    assert stmts[2].text == "SELECT 1"


def test_comment_only_input_has_no_statements():
    assert split_statements("-- nothing\n/* here */ ;;  ") == []


def test_backslash_outside_strings_is_found():
    assert find_backslash("SELECT '\\n' FROM t") is None
    assert find_backslash("\\connect other\nSELECT 1") == 0


def test_fingerprint_ignores_comments_whitespace_case_but_not_strings():
    assert fingerprint("select  id\nFROM t -- x\n;") == fingerprint("SELECT id FROM t")
    assert fingerprint("SELECT 'a  b'") != fingerprint("SELECT 'a b'")
    assert fingerprint('SELECT "Id" FROM t') != fingerprint("SELECT id FROM t")


def test_line_col():
    assert line_col("ab\ncd", 0) == (1, 1)
    assert line_col("ab\ncd", 4) == (2, 2)
