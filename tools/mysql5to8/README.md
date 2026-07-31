# MySQL 5 To MySQL 8 Converter

`mysql5_to_mysql8.py` converts common MySQL 5 dump syntax and DDL definitions that fail under MySQL 8 defaults. The source SQL file is never overwritten.

## Run

```powershell
python .\mysql5_to_mysql8.py .\legacy_dump.sql
```

This writes:

- `legacy_dump.mysql8.sql`: converted SQL dump
- `legacy_dump.mysql8.sql.report.json`: conversion report

Choose explicit output paths when needed:

```powershell
python .\mysql5_to_mysql8.py .\legacy_dump.sql -o .\mysql8_dump.sql --report .\conversion_report.json
```

## Automatic Conversions

- Adds `NOT NULL` to each column in a primary key, including composite primary keys.
- Removes `DEFAULT NULL` from a primary-key column after making it non-null.
- Converts legacy `TYPE=...` table options to `ENGINE=...`.
- Removes `NO_AUTO_CREATE_USER` from `sql_mode` assignments because MySQL 8 removed it.
- Replaces zero-date defaults on nullable `DATE`, `DATETIME`, and `TIMESTAMP` fields with `DEFAULT NULL`.
- Removes zero-date defaults from non-null date/time fields because `DEFAULT NULL` would be invalid there.

## Manual Review

Read the JSON report before importing production data. The tool reports, but deliberately does not rewrite, constructs that need schema or account-specific decisions, including:

- `OLD_PASSWORD()`
- `IDENTIFIED BY PASSWORD`
- `ZEROFILL`

The converter targets conventional `mysqldump`-style SQL. It does not execute the dump or connect to either database.
