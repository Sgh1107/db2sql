#!/usr/bin/env python3
"""Convert common MySQL 5 dump incompatibilities for MySQL 8."""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

TYPE_RE = re.compile(r"\bTYPE\s*=", re.I)
SQL_MODE_RE = re.compile(r"(\b(?:SESSION\s+|GLOBAL\s+)?SQL_MODE\s*=\s*)'([^']*)'", re.I)
ZERO_DATE_RE = re.compile(r"\s+DEFAULT\s+(?:'0000-00-00(?:[ T]00:00:00(?:\.0+)?)?'|0)", re.I)
DEFAULT_NULL_RE = re.compile(r"\s+DEFAULT\s+NULL\b", re.I)


@dataclass
class Report:
    input_file: str
    output_file: str
    statements: int = 0
    primary_key_columns_fixed: list[str] = field(default_factory=list)
    zero_date_defaults_replaced: list[str] = field(default_factory=list)
    zero_date_defaults_removed: list[str] = field(default_factory=list)
    sql_mode_tokens_removed: int = 0
    type_clauses_converted: int = 0
    warnings: list[str] = field(default_factory=list)


def split_sql(text: str, separator: str = ";", keep_separator: bool = False) -> list[str]:
    """Split SQL while respecting quoted strings and parenthesized expressions."""
    items, start, depth, quote = [], 0, 0, None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                if quote != "`" and index + 1 < len(text) and text[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == separator and depth == 0:
            items.append(text[start:index + (1 if keep_separator else 0)])
            start = index + 1
        index += 1
    if start < len(text):
        items.append(text[start:])
    return items


def outer_parentheses(statement: str) -> tuple[int, int] | None:
    opening = statement.find("(")
    if opening < 0:
        return None
    depth, quote = 0, None
    for index in range(opening, len(statement)):
        char = statement[index]
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return opening, index
    return None


def identifier(text: str) -> str | None:
    match = re.match(r"\s*(?:`((?:``|[^`])+?)`|([A-Za-z_][A-Za-z0-9_$]*))", text)
    return ((match.group(1) or match.group(2)).replace("``", "`").lower() if match else None)


def primary_columns(definition: str) -> list[str]:
    match = re.match(r"\s*(?:CONSTRAINT\s+(?:`[^`]+`|\w+)\s+)?PRIMARY\s+KEY(?:\s+USING\s+\w+)?\s*\((.*)\)\s*$", definition, re.I | re.S)
    if not match:
        return []
    return [name for name in (identifier(item) for item in split_sql(match.group(1), ",")) if name]


def make_not_null(definition: str) -> tuple[str, bool]:
    match = re.match(r"(\s*(?:`(?:``|[^`])+?`|[A-Za-z_][A-Za-z0-9_$]*))(.*)", definition, re.S)
    if not match:
        return definition, False
    name, tail = match.groups()
    modifier = re.search(r"\b(?:DEFAULT|AUTO_INCREMENT|COMMENT|COLLATE|CHARACTER\s+SET|ON\s+UPDATE|GENERATED)\b", tail, re.I)
    position = modifier.start() if modifier else len(tail)
    column_type = tail[:position]
    suffix = tail[position:]
    null = re.search(r"\bNOT\s+NULL\b|\bNULL\b", column_type, re.I)
    if null and null.group(0).upper() == "NOT NULL":
        return definition, False
    if null:
        column_type = column_type[:null.start()] + "NOT NULL" + column_type[null.end():]
    else:
        column_type = column_type.rstrip() + " NOT NULL "
    result = DEFAULT_NULL_RE.sub("", column_type + suffix, count=1)
    return name + result, True


def convert_create_table(statement: str, report: Report) -> str:
    bounds = outer_parentheses(statement)
    if not bounds:
        report.warnings.append("Skipped an unparseable CREATE TABLE statement.")
        return statement
    opening, closing = bounds
    definitions = split_sql(statement[opening + 1:closing], ",")
    key_columns = {column for definition in definitions for column in primary_columns(definition)}
    table = re.search(r"CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)", statement, re.I)
    table_name = table.group(1) if table else "<unknown table>"
    output = []
    for definition in definitions:
        name = identifier(definition)
        if name and name in key_columns:
            definition, changed = make_not_null(definition)
            if changed:
                report.primary_key_columns_fixed.append("{}.{}".format(table_name, name))
        definition = replace_zero_dates(definition, report)
        output.append(definition)
    return statement[:opening + 1] + ",".join(output) + statement[closing:]


def replace_zero_dates(statement: str, report: Report) -> str:
    if not re.search(r"\b(?:DATE|DATETIME|TIMESTAMP)\b", statement, re.I):
        return statement
    column = identifier(statement) or "<unknown column>"
    changed = ZERO_DATE_RE.search(statement)
    if changed:
        if re.search(r"\bNOT\s+NULL\b", statement, re.I):
            report.zero_date_defaults_removed.append(column)
            return ZERO_DATE_RE.sub("", statement)
        report.zero_date_defaults_replaced.append(column)
        return ZERO_DATE_RE.sub(" DEFAULT NULL", statement)
    return statement


def convert_statement(statement: str, report: Report) -> str:
    report.statements += 1
    converted, count = TYPE_RE.subn("ENGINE=", statement)
    report.type_clauses_converted += count

    def sql_mode(match: re.Match) -> str:
        modes = [item for item in match.group(2).split(",") if item.upper() != "NO_AUTO_CREATE_USER"]
        removed = len(match.group(2).split(",")) - len(modes)
        report.sql_mode_tokens_removed += removed
        return match.group(1) + "'" + ",".join(modes) + "'"

    converted = SQL_MODE_RE.sub(sql_mode, converted)
    if re.match(r"\s*CREATE\s+(?:TEMPORARY\s+)?TABLE\b", converted, re.I):
        converted = convert_create_table(converted, report)
    if re.match(r"\s*(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_$]*)\s+", converted):
        converted = replace_zero_dates(converted, report)
    return converted


def warnings_for(text: str, report: Report) -> None:
    checks = {
        r"\bOLD_PASSWORD\s*\(": "OLD_PASSWORD() is removed in MySQL 8.",
        r"\bIDENTIFIED\s+BY\s+PASSWORD\b": "IDENTIFIED BY PASSWORD may require manual account migration.",
        r"\bZEROFILL\b": "ZEROFILL is deprecated in MySQL 8; review column display behavior.",
    }
    for pattern, message in checks.items():
        if re.search(pattern, text, re.I):
            report.warnings.append(message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert a MySQL 5 SQL dump for MySQL 8 import.")
    parser.add_argument("input", type=Path, help="MySQL 5 SQL dump")
    parser.add_argument("-o", "--output", type=Path, help="Converted SQL file")
    parser.add_argument("--report", type=Path, help="JSON conversion report")
    args = parser.parse_args()

    source = args.input.read_text(encoding="utf-8-sig")
    output = args.output or args.input.with_name(args.input.stem + ".mysql8" + args.input.suffix)
    report = Report(str(args.input), str(output))
    converted = "".join(convert_statement(statement, report) for statement in split_sql(source, keep_separator=True))
    warnings_for(source, report)
    output.write_text(converted, encoding="utf-8", newline="\n")
    report_path = args.report or output.with_suffix(output.suffix + ".report.json")
    report_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Converted SQL: {}".format(output))
    print("Conversion report: {}".format(report_path))


if __name__ == "__main__":
    main()
