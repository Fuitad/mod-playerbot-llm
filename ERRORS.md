# ERRORS.md

Standing traps in this module's tooling. Not a diary: an entry earns its place only if the
thing that caused the failure is still true now that it is fixed.

## aiomysql pool discards a connection returned mid-transaction (2026-08-02)

What didn't work: acquiring a pooled connection, running a plain SELECT, and returning it.
With `autocommit=False` even a bare SELECT opens a transaction, and `Pool.release` calls
`conn.get_transaction_status()` and, when it is set, closes the connection instead of
returning it to the free list. Nothing errors. The only symptom is that every read silently
destroys a socket and opens a new one, which looks like ordinary connection churn.

What worked: end every acquisition by closing what it opened.

```python
async with pool.acquire() as connection:
    try:
        yield connection
    finally:
        if connection.get_transaction_status():
            await connection.rollback()
```

Note for next time: this is aiomysql's documented release behaviour, not a bug, so it will
keep being true. Checking the status first avoids a round trip when nothing is open, and it
is the same predicate the pool itself uses. A test that asserts `pool.freesize == pool.size`
after a run of reads catches it; nothing else does.

## MySQL notes from CREATE TABLE IF NOT EXISTS reach stderr as Python warnings (2026-08-02)

What didn't work: idempotent schema creation on every start. From the second start onward
MySQL emits one "table already exists" note per table, and the driver surfaces each as a
Python warning on stderr. Seven of them bury any real message, and for a CLI command they
print before the JSON on stdout, so anything reading the combined output sees them first.

What worked: `SET sql_notes = 0` around the DDL, restored in a `finally`. It suppresses the
notes at the server for that connection only, and genuine errors still raise.

Note for next time: this is server behaviour, so it applies to any `IF NOT EXISTS` DDL run
routinely rather than once. Suppressing it in Python's `warnings` module instead would be
global and would also hide warnings worth seeing.

## The MySQL-backed tests need a disposable server, never a running one (2026-08-02)

What didn't work: the `acore` MySQL user lacks `CREATE`, `RELOAD`, and `LOCK TABLES`, so
creating a scratch schema on the development server fails, and `mysqldump --single-transaction`
fails for the same reason.

What worked: `bash sidecar/scripts/run_ledger_mysql_tests.sh`. It starts its own MySQL 8
container on port 33062 under its own name, exports the DSN, and removes it afterwards. It
refuses to run if that container name already exists rather than reusing somebody else's
data.

Note for next time: a bare `uv run pytest` deliberately EXCLUDES the `mysql` marked tests
(`addopts = "-m 'not mysql'"`) rather than skipping them silently, so a green `pytest` is
honest about not having touched a database. Run the script separately before claiming the
ledger is verified.
