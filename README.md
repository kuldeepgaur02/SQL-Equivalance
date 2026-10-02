# cexgen

Finds a database instance on which two SQL queries (Q1, Q2) return different
results. Input is the schema and the two queries; there is no data.

Built one step at a time, following the pipeline diagram up to the mutation stage.

| Step | Box in the diagram | Status |
|---|---|---|
| 1 | Input (schema + Q1 + Q2) + Postgres sandbox | done |
| – | Runner: database per schema, reset, attempt log, schema memory | done |
| 2 | Parse schema — Postgres | done |
| 3 | Parse queries — sqlglot | done |
| 4 | Table order (FK graph) | done |
| 5 | Base data — one row per table | done |
| 6 | INSERT into Postgres + repair loop | done |
| 7 | Run Q1 and Q2 — compare | next |
| 8 | LLM: rebuild base | |
| 9 | Hand-off to the mutation stage | |

## Setup

```bash
docker compose up -d          # Postgres 16 on localhost:5433 (user/password/db: cex)
pip install -e ".[dev]"
python -m pytest              # tests needing Postgres are skipped if it is not running
```

To use a different Postgres, set `CEX_DSN` (for example `CEX_DSN="dbname=postgres"`).
The user needs permission to create databases.

## Usage

```bash
cexgen check examples/shop              # one case folder
cexgen check examples/shop_batch.json   # many query pairs, one schema
cexgen check examples                   # every case under a folder
cexgen check examples/shop --keep-db    # keep the sandbox database to inspect it
cexgen check examples --fresh           # ignore saved schema memory (clean evaluation run)
cexgen schema examples/shop             # show the schema model (add --json for the full model)
cexgen queries examples --rules         # show what Step 3 found in Q1 / Q2 and the schema's rules
cexgen plan examples/cycle              # show the insert plan (Step 4)
cexgen base examples/shop --no-llm      # show the base data (Step 5); drop --no-llm to use the LLM
cexgen load examples/cycle --no-llm     # insert it with the repair loop (Step 6)
cexgen cleanup                          # drop sandbox databases left by killed runs
```

## Input formats

**Case folder:** `schema.sql`, `q1.sql`, `q2.sql`, plus an optional `meta.json`
(free-form, for example `{"expected": "different"}`).

**Batch file (`.json`):** one schema, many named query pairs:

```json
{
  "name": "shop",
  "schema_file": "shop/schema.sql",
  "meta": {"suite": "shop"},
  "pairs": [
    {"name": "gt_vs_gte", "q1": "SELECT ...", "q2": "SELECT ...", "meta": {"expected": "different"}},
    {"name": "from_files", "q1_file": "q/a.sql", "q2_file": "q/b.sql"}
  ]
}
```

**Suite:** a folder holding any mix of the above, searched recursively.
Names starting with `.` or `_` are ignored.

**Python:** `Case(name, schema_sql, q1, q2)`.

To add a new input format, add a class with `matches(path)` and
`load(path, settings)` to `SOURCES` in `src/cexgen/input/sources.py`.

## Layout

```
src/cexgen/
├── config.py            settings (from environment variables, or built in code)
├── errors.py            error types shared by all steps; each has its own CLI exit code
├── cli.py               `cexgen` command; each step adds a subcommand
├── sqltext/lexer.py     PostgreSQL-aware scanning: strings, $$ bodies, nested comments
├── input/               Step 1: Input
│   ├── case.py          Case: validated schema + Q1 + Q2
│   ├── files.py         safe file reading (size limit, UTF-8 / BOM)
│   └── sources.py       folder / batch / suite sources, load_cases()
├── db/                  Step 1: Postgres
│   ├── connection.py    connections: timeouts, pinned session settings, clear errors
│   ├── sandbox.py       one temporary database (create, run schema SQL, drop)
│   ├── workspace.py     one database per schema, reset() between cases
│   ├── seed.py          rows + sequence counters the schema SQL left behind: save / restore
│   ├── inventory.py     what the schema SQL created, with warnings
│   └── diagnostics.py   Postgres errors → line, column, excerpt, hint
├── schema/              Step 2: Parse schema — Postgres
│   ├── model.py         SchemaModel / Table / Column, rules grouped by Codd's integrity categories
│   ├── types.py         type families and limits; domains unwrapped
│   ├── partitions.py    partition bounds (range / list / hash / default)
│   ├── catalog.py       the pg_catalog queries, in one reviewable file
│   ├── reader.py        read_schema(): catalog -> SchemaModel, one consistent snapshot
│   ├── describe.py      readable summary and JSON form
│   └── step.py          pipeline step: model read once per schema, cached
├── queries/             Step 3: Parse queries — sqlglot
│   ├── model.py         QueryInfo / Predicate / Term / Comparison / Join
│   ├── validate.py      Postgres checks the query: valid, read-only, result columns, volatile functions
│   ├── resolver.py      sqlglot parse + qualify; columns traced to base tables through CTEs / subqueries
│   ├── expressions.py   the shared analyser: condition -> predicates (queries AND schema rules)
│   ├── analyzer.py      analyze_query(): tables, filters, joins, constants, JSON paths, features
│   ├── rules.py         Step 2's CHECK / index / exclusion rules -> predicates, "understood" or not
│   ├── describe.py      readable summaries
│   └── step.py          pipeline step (rules once per schema, queries per case)
├── ordering/            Step 4: Table order (FK graph)
│   ├── model.py         InsertPlan / Step / FkPlan (how each FK is satisfied)
│   ├── graph.py         FK graph, cycles (Tarjan), stable topological order
│   ├── planner.py       plan_inserts(): which tables, which order, which FK strategy
│   ├── describe.py      readable plan
│   └── step.py          pipeline step
├── basedata/            Step 5: Base data — one row per table
│   ├── defaults.py      hardcoded default per type family, within the column's limits
│   ├── values.py        compare / step / fit values the way Postgres does
│   ├── solver.py        satisfy the schema's understood rules and partition bounds
│   ├── json_array.py    JSON / ARRAY values: LLM (checked) or rule table
│   ├── builder.py       build_base(): rows, then FKs as the insert plan says
│   ├── model.py         BaseData / Cell (value + where it came from)
│   └── describe.py, step.py
├── insertion/           Step 6: INSERT into Postgres + repair loop
│   ├── sql.py           INSERT / UPDATE without casts (no silent truncation); array / record literals
│   ├── classify.py      SQLSTATE -> repair branch
│   ├── fix_code.py      PK / UNIQUE, FK, NOT NULL: fixed by code, using what is in the database
│   ├── fix_rules.py     CHECK / unknown in --no-llm mode; candidates checked by Postgres itself
│   ├── llm_repair.py    CHECK / unknown: the LLM, with every earlier attempt; answer validated
│   ├── loader.py        the loop: savepoints, cycles, FK re-sync, 3 repairs then skip, UPDATEs, read-back
│   └── model.py, describe.py, step.py
├── llm/                 the LLM, behind a provider interface
│   ├── base.py          LLMProvider: complete_json(system, prompt, schema)
│   ├── registry.py      which providers exist; add another API here
│   ├── providers/claude.py   Claude via the Anthropic SDK (first provider)
│   ├── prompts.py       every prompt, in one place
│   └── oracle.py        LLM or rule table; logs every call; caches answers in schema memory
├── baseline/rules.py    the rule table (--no-llm baseline, and fallback for a failed call)
├── journal/             attempt log: every action of a case and its exact result
├── memory/              schema memory: what is known about a schema, kept across runs
└── runner/batch.py      outer loop: group cases by schema, run the pipeline steps per case
```

## Runs, attempt logs, schema memory

```
runs/
├── <case>-<hash>/<UTC time>.jsonl        one line per action: step, what, status, error, timing
└── _schemas/<fingerprint>-pg<major>.json what is known about a schema (reused next time)
```

- **One database per schema.** Cases on the same schema share it. Before each case,
  `reset()` empties every table, restores the rows and sequence counters the
  schema SQL left behind (exactly, via COPY, without firing triggers), and
  refreshes materialized views in dependency order.
- **Attempt log.** Written line by line as actions happen, so a crash loses
  nothing that already happened.
- **Schema memory.** Keyed by the schema's fingerprint (comments, whitespace
  and keyword case ignored) plus the Postgres major version, so a changed
  schema never reuses old knowledge. Concurrent runs merge rather than
  overwrite. A damaged file is set aside. `--fresh` neither reads nor writes it.
- **Failures.** A step failing fails only its own case; a broken schema fails
  only the cases that use it. Both are recorded in the case's log.

## Step 1: what is handled

**Input**
- Empty, whitespace-only and comment-only schemas or queries are rejected.
- A query must be exactly one statement. Trailing `;` is removed. A `;` inside a
  string, quoted identifier, comment or `$$` body is not a statement break.
- Unterminated strings, identifiers, comments and dollar quotes are reported
  with their line and column.
- psql meta-commands (`\connect`, `\i`) and `$1` parameters are rejected.
- Files: a size limit, UTF-8 with or without a BOM, and invalid UTF-8 or NUL
  bytes are reported with their location.
- Batch files are strict: unknown keys (typos), duplicate names, and an
  ambiguous `schema` + `schema_file` are all errors.
- Pairs whose queries are the same text are flagged.

**Sandbox**
- Each run gets its own database, so `public.x`, `SET search_path = ''`,
  extensions and extra schemas from `pg_dump` files all stay isolated.
- The database is created with UTF8 and C collation. The session is pinned to
  UTC, ISO dates, exact float output and timeouts, so results are the same on
  every machine.
- The schema SQL runs as one transaction: it applies fully or not at all.
  Session changes it makes are reset afterwards.
- Statements that would change the whole server (roles, databases,
  tablespaces, `ALTER SYSTEM`) are refused before anything runs.
- Errors show the line, column, an excerpt, and a hint (for example, use
  `pg_dump --no-owner` for a missing role).
- The database is dropped on close, an error or Ctrl-C. Databases left by a
  killed run are named with their creation time and removed by `cexgen cleanup`.
- The inventory reports things later steps must handle: rows the schema
  inserted itself, triggers, rules, foreign tables, forced row-level security,
  temporary tables, partitions, views, enums, domains and extensions.

## Step 2: the schema model

Postgres is the parser. After the schema SQL runs, `read_schema()` reads the
catalog in one consistent snapshot and builds a read-only model. Rules are
grouped by Codd's integrity categories:

| Category | Model | Covers |
|---|---|---|
| Domain integrity | `Column`, `TypeInfo` | type family and limits (int range, `varchar(n)`, `numeric(p,s)`, time precision, enum labels, array element type, range subtype, composite fields), NOT NULL, defaults, identity, generated columns, collation; nested domains unwrapped with their NOT NULL and CHECKs |
| Entity integrity | `PrimaryKey`, `UniqueKey` | composite PK; UNIQUE; unique indexes that are partial, on expressions, or have INCLUDE columns (excluded from the key); `NULLS NOT DISTINCT`; invalid indexes |
| Referential integrity | `ForeignKey` | composite, to UNIQUE (not PK) columns, cross-schema, self-referencing, `MATCH FULL/SIMPLE`, `ON DELETE/UPDATE`, deferrable, `NOT VALID` |
| General integrity | `CheckConstraint`, `ExclusionConstraint`, `Partitioning` | table and domain CHECKs (`VALUE` replaced by the column), exclusion constraints with operators and `WHERE`, range/list/hash partition bounds including `MINVALUE/MAXVALUE`, `DEFAULT` and nested partitions |

The model also records partitions (rows go in through the parent),
inheritance, views and materialized views (with the tables they read),
foreign tables, triggers, rules, row-level security and seed rows. Stored
expressions are normalised (redundant outer parentheses removed), so every rule
has one form. Warnings flag what later steps must handle: partitioned tables
with no partitions, `NOT VALID` constraints, enums without labels, types with
no value rules, and inheritance.

## Step 3: query analysis

Postgres is the authority and sqlglot is the analyser:

1. **Postgres** checks each query in a READ ONLY transaction. `EXPLAIN` confirms
   it is valid for this schema. Wrapping it as `SELECT * FROM (q) LIMIT 0`
   confirms it is a pure query (a write, a data-modifying `WITH`, or
   `SELECT INTO` cannot be wrapped) and gives the result columns. `pg_proc`
   lists the volatile functions it calls (`random()`, `clock_timestamp()`),
   whose results change from run to run.
2. **sqlglot** parses and qualifies the query, then the shared expression
   analyser turns every WHERE / ON / HAVING into predicates
   (`term op constants`). Each predicate records its clause, its scope (main,
   CTE, subquery, union branch) and whether it is **required** (a top-level
   AND, not inside OR or `NOT (... AND ...)`). An outer join's ON condition is
   never treated as a filter on the result.
3. If sqlglot cannot parse a query that Postgres accepted, the case continues;
   its filters are reported as unknown.

The same analyser reads Step 2's CHECK, partial-index and exclusion rules. A
rule is "understood" when every part became a required predicate, so data
generation can satisfy it directly; otherwise it is left to the repair loop.

Pair notes flag what decides the comparison early: a different number of
result columns, different column types, identical query text, volatile
functions.

## Step 4: the insert plan

Decided before any data exists:

- **Which tables get rows:** the tables Q1 and Q2 read (views expanded,
  partitions mapped to their partitioned parent), plus every FK parent they
  need, transitively. Other tables are logged as "not needed". If a query
  could not be parsed, every table gets a row.
- **How each foreign key is satisfied:**

| Strategy | When |
|---|---|
| `parent` | the parent row is inserted in an earlier step |
| `null_then_update` | a nullable self-reference, or the nullable FK chosen to break a cycle: insert NULL, UPDATE after all rows exist |
| `self` | a NOT NULL self-reference: the row points at itself (Postgres checks the FK after the row is written); the table is skipped only if a CHECK forbids it |
| `deferred` | a cycle in which every FK is NOT NULL: the tables go in together and the FKs are checked at the end of the transaction (made DEFERRABLE in the sandbox if needed) |
| `null` | the parent table cannot get rows; the FK is left NULL (MATCH SIMPLE: one nullable column is enough; MATCH FULL: all columns must be nullable) |

  A NOT NULL FK into a table that gets no rows skips the child, and that skip
  cascades to its own children.
- **Order:** parents first, ties broken by name, so the same schema always
  gives the same plan.

These choices deliberately differ from the diagram in two places, as agreed:
a NOT NULL self-reference gets a self-pointing row instead of being skipped,
and a cycle is broken at a nullable FK before deferral is used.

## Step 5: base data

One row per table in the insert plan, following the diagram:

| Column | Value |
|---|---|
| scalar | a hardcoded default for its type (ignores the queries), within the column's limits |
| ENUM | the first label |
| JSON / ARRAY | the LLM (one call per case for all such columns), built from the JSON paths the queries read; the rule table with `--no-llm` |
| FK | as the insert plan says: copied from the parent row or the row itself, or NULL (and set by UPDATE after all rows exist) |

The row is then made to satisfy every schema rule Step 3 understood (CHECKs,
domain CHECKs, column comparisons, partition bounds), so inserts do not fail
on rules that could have been known. Rules it cannot satisfy are listed for
Step 6's repair. Every value records where it came from (`default`, `enum`,
`rule`, `fk`, `fk-null`, `llm`, `llm-memory`, `json-rule`, `memory`).

An LLM answer is checked before use: valid JSON, every queried path present
(missing ones are added), array elements valid for their type. An invalid
answer, or a failed call, falls back to the rule table for that column and is
recorded in the attempt log. Answers that worked are cached in schema memory.

## The LLM

- **Credentials:** set `ANTHROPIC_API_KEY` (or run `ant auth login`). Model:
  `CEX_LLM_MODEL` (default `claude-opus-5-5`), effort: `CEX_LLM_EFFORT`.
- **No credentials:** a case that needs the LLM stops with a clear message,
  instead of quietly using the rule table, so an "LLM" result is never secretly
  a baseline result. Cases that need no LLM decision run normally.
- **Baseline:** `--no-llm` runs the same pipeline with the rule table.
- **Another API:** write a class with `name` and
  `complete_json(system, prompt, schema, purpose) -> LLMResult` (see
  `llm/base.py` and `llm/providers/claude.py`), register it with
  `register_provider("my_api", factory)` in `llm/registry.py`, and select it
  with `CEX_LLM_PROVIDER=my_api`. Nothing else changes.

Note: `cexgen check` now runs Steps 1–5, so a case with JSON / ARRAY columns
needs either credentials or `--no-llm`.

## Step 6: INSERT and the repair loop

As in the diagram: INSERT, and on failure repair and retry, at most 3 times,
then skip.

| Failure (SQLSTATE) | Repair |
|---|---|
| PK / UNIQUE (23505) | code: a new value of the same type not yet in the column (max + 1, a suffix, next label) |
| FK (23503) | code: point at an existing parent row (seed rows count); else NULL if allowed |
| NOT NULL (23502) | code: the type's default, or an existing parent key for an FK column |
| CHECK (23514), no partition for row, exclusion (23P01), bad data (22xxx), trigger error, anything else | LLM: sees the table, the row, the exact error and constraint, every earlier attempt on the row with its result, and values that failed this constraint in earlier runs. With `--no-llm` (or when the call fails), the rule table: the solver, then a bounded search over candidate values, each checked by Postgres against every CHECK of the table before the INSERT is retried |

- **Transactions:** one transaction for the whole load, a savepoint per row (a
  failure undoes only that row), committed at the end. A cycle's tables go in
  under one savepoint with their FKs deferred; the deferred check runs inside
  it, so the error names the row to repair.
- **FKs:** before every attempt, FK values are copied from the parent rows as
  Postgres stored them, so a repaired parent key carries over. If a parent
  ended up without a row, a nullable FK becomes NULL and a NOT NULL FK skips
  the child. Then the planned UPDATEs run.
- **Values are sent without casts.** `'abcdef'::varchar(3)` would silently
  truncate; untyped values make Postgres raise "value too long" instead.
- **The database is the truth:** each row is read back with
  `RETURNING *` (defaults, generated columns and triggers may change it), and
  a snapshot of every table follows the load, including seed rows and rows
  triggers added. Materialized views are refreshed so Q1 / Q2 see the data.
- **Memory:** rows that inserted are kept (`base_rows`, Step 5 starts from
  them next time); values that failed a constraint are kept (`failed_fixes`,
  shown to the LLM).
- Every attempt, error, repair and LLM call is in the case's attempt log.
