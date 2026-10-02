"""The SQL that reads Postgres's system catalog (Codd's rule 4: the database
describes itself in tables). Kept in one place so each query can be reviewed.

All queries take parameters, so a literal % is written %%.
"""

# Objects in these schemas, or created by an extension, are not the user's.
USER_SCHEMA = """
    n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND n.nspname NOT LIKE 'pg\\_toast%%'
    AND n.nspname NOT LIKE 'pg\\_temp\\_%%'
"""
NOT_EXTENSION_MEMBER = """
    NOT EXISTS (SELECT 1 FROM pg_depend dep
                WHERE dep.objid = c.oid AND dep.classid = 'pg_class'::regclass AND dep.deptype = 'e')
"""

SEARCH_PATH = "SELECT current_schemas(false)"

# Tables, partitioned tables, views, materialized views, foreign tables.
RELATIONS = f"""
SELECT c.oid, n.nspname, c.relname, c.relkind, c.relispartition, c.relpersistence = 'u',
       c.relrowsecurity, c.relforcerowsecurity
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE {USER_SCHEMA} AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND {NOT_EXTENSION_MEMBER}
ORDER BY n.nspname, c.relname
"""

# Live columns with type oid + modifier, default / generation expression, identity, collation.
COLUMNS = """
SELECT a.attrelid, a.attnum, a.attname, a.atttypid, a.atttypmod, a.attndims, a.attnotnull,
       a.attidentity, a.attgenerated, pg_get_expr(d.adbin, d.adrelid),
       CASE WHEN a.attcollation <> 0 AND co.collname <> 'default' AND a.attcollation <> t.typcollation
            THEN co.collname END,
       a.attinhcount > 0
FROM pg_attribute a
JOIN pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
LEFT JOIN pg_collation co ON co.oid = a.attcollation
WHERE a.attrelid = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attrelid, a.attnum
"""

# One type. The extension column names the extension that owns it, if any.
TYPE = """
SELECT n.nspname, t.typname, t.typtype, t.typcategory, t.typelem, t.typbasetype, t.typtypmod,
       t.typnotnull, t.typndims, t.typrelid,
       (SELECT e.extname FROM pg_depend dep JOIN pg_extension e ON e.oid = dep.refobjid
        WHERE dep.objid = t.oid AND dep.classid = 'pg_type'::regclass AND dep.deptype = 'e' LIMIT 1)
FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE t.oid = %s
"""

FORMAT_TYPE = "SELECT format_type(%s, %s)"

DOMAIN_CHECKS = """
SELECT conname, pg_get_constraintdef(oid, true), convalidated
FROM pg_constraint WHERE contypid = %s AND contype = 'c' ORDER BY conname
"""

ENUM_LABELS = "SELECT enumlabel FROM pg_enum WHERE enumtypid = %s ORDER BY enumsortorder"

RANGE_SUBTYPE = "SELECT rngsubtype FROM pg_range WHERE rngtypid = %s"
MULTIRANGE_SUBTYPE = "SELECT rngsubtype FROM pg_range WHERE rngmultitypid = %s"   # PG 14+

COMPOSITE_FIELDS = """
SELECT attname, atttypid, atttypmod FROM pg_attribute
WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped ORDER BY attnum
"""

# PRIMARY KEY, UNIQUE, FOREIGN KEY, CHECK, EXCLUDE. (NOT NULL is read from the columns.)
CONSTRAINTS = """
SELECT con.oid, con.conname, con.contype, con.conrelid, con.conkey, con.confrelid, con.confkey,
       con.condeferrable, con.condeferred, con.convalidated, con.confmatchtype, con.confupdtype,
       con.confdeltype, con.connoinherit, con.conindid, pg_get_constraintdef(con.oid, true)
FROM pg_constraint con
WHERE con.conrelid = ANY(%s) AND con.contype IN ('p', 'u', 'f', 'c', 'x')
ORDER BY con.conrelid, con.contype, con.conname
"""

EXCLUSION_OPERATORS = """
SELECT con.oid, array_agg(o.oprname ORDER BY x.ord)
FROM pg_constraint con
CROSS JOIN LATERAL unnest(con.conexclop) WITH ORDINALITY AS x(op, ord)
JOIN pg_operator o ON o.oid = x.op
WHERE con.contype = 'x' AND con.conrelid = ANY(%s)
GROUP BY con.oid
"""

# Every index on our tables. indnkeyatts excludes INCLUDE columns, which are not part of the key.
# {nulls_not_distinct} is indnullsnotdistinct on PG 15+, else false.
INDEXES = """
SELECT i.indexrelid, i.indrelid, ic.relname, i.indisunique, i.indisprimary, i.indisvalid,
       i.indnkeyatts, i.indkey::int2[], pg_get_expr(i.indpred, i.indrelid), {nulls_not_distinct},
       am.amname,
       EXISTS (SELECT 1 FROM pg_constraint con WHERE con.conindid = i.indexrelid AND con.contype IN ('p', 'u', 'x'))
FROM pg_index i
JOIN pg_class ic ON ic.oid = i.indexrelid
JOIN pg_am am ON am.oid = ic.relam
WHERE i.indrelid = ANY(%s)
ORDER BY i.indrelid, ic.relname
"""

# The k-th key element of an index, as Postgres prints it (column name or expression).
INDEX_ELEMENTS = "SELECT k, pg_get_indexdef(%s, k, true) FROM generate_series(1, %s) AS k ORDER BY k"

PARTITIONED = """
SELECT p.partrelid, p.partstrat, p.partattrs::int2[], pg_get_partkeydef(p.partrelid)
FROM pg_partitioned_table p WHERE p.partrelid = ANY(%s)
"""

PARTITIONS = """
SELECT i.inhparent, i.inhrelid, pg_get_expr(c.relpartbound, c.oid)
FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
WHERE c.relispartition AND i.inhparent = ANY(%s)
ORDER BY i.inhparent, c.relname
"""

# INHERITS (classic inheritance, not partitioning).
INHERITANCE = """
SELECT i.inhrelid, i.inhparent
FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
WHERE NOT c.relispartition AND i.inhrelid = ANY(%s)
ORDER BY i.inhrelid, i.inhseqno
"""

# Relations a view / materialized view reads.
VIEW_READS = """
SELECT DISTINCT r.ev_class, d.refobjid
FROM pg_rewrite r
JOIN pg_depend d ON d.objid = r.oid AND d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass
WHERE r.ev_class = ANY(%s) AND d.refobjid <> r.ev_class
"""

TRIGGERS = """
SELECT tgrelid, tgname, tgtype, tgenabled FROM pg_trigger
WHERE NOT tgisinternal AND tgrelid = ANY(%s) ORDER BY tgrelid, tgname
"""

RULES = """
SELECT ev_class, rulename FROM pg_rewrite
WHERE ev_class = ANY(%s) AND rulename <> '_RETURN' ORDER BY ev_class, rulename
"""
