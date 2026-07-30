#!/usr/bin/env python3
''' Standalone MySQL and Oracle table-to-SQL exporter. '''

from __future__ import annotations

import datetime as dt
import math
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


@dataclass
class ConnectionSettings:
    kind: str
    host: str
    port: int
    user: str
    password: str
    database: str = ""
    service: str = ""
    sid: str = ""
    dsn: str = ""


@dataclass
class ExportSettings:
    directory: Path
    tables_per_file: int
    export_workers: int
    rows_per_insert: int
    include_data: bool
    include_drop: bool


class Cancelled(Exception):
    pass


class Adapter:
    def __init__(self, settings: ConnectionSettings):
        self.settings = settings
        self.kind = settings.kind
        if self.kind == "mysql":
            try:
                import pymysql
            except ImportError as exc:
                raise RuntimeError("未安装 PyMySQL。请执行: python -m pip install PyMySQL") from exc
            self.driver = pymysql
        else:
            try:
                import oracledb
            except ImportError as exc:
                raise RuntimeError("未安装 oracledb。请执行: python -m pip install oracledb") from exc
            self.driver = oracledb

    def connect(self):
        s = self.settings
        if self.kind == "mysql":
            return self.driver.connect(host=s.host, port=s.port, user=s.user, password=s.password,
                                       database=s.database, charset="utf8mb4", autocommit=True)
        dsn = s.dsn.strip()
        if not dsn:
            if s.service.strip():
                dsn = self.driver.makedsn(s.host, s.port, service_name=s.service.strip())
            elif s.sid.strip():
                dsn = self.driver.makedsn(s.host, s.port, sid=s.sid.strip())
            else:
                raise ValueError("Oracle 必须填写 DSN、服务名或 SID")
        return self.driver.connect(user=s.user, password=s.password, dsn=dsn, encoding="UTF-8", nencoding="UTF-8")

    def quote(self, identifier: str) -> str:
        if not identifier or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$#" for ch in identifier) or identifier[0].isdigit():
            raise ValueError("不支持的标识符: {}".format(identifier))
        return "`{}`".format(identifier) if self.kind == "mysql" else '"{}"'.format(identifier.upper())

    def list_databases(self) -> list[str]:
        if self.kind != "mysql":
            return []
        connection = self.connect()
        cursor = connection.cursor()
        try:
            cursor.execute("SHOW DATABASES")
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def list_tables(self) -> list[str]:
        connection = self.connect()
        cursor = connection.cursor()
        try:
            if self.kind == "mysql":
                cursor.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
                return sorted(row[0] for row in cursor.fetchall())
            cursor.execute("SELECT table_name FROM user_tables ORDER BY table_name")
            return [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()
            connection.close()

    def ddl(self, connection, table: str) -> str:
        cursor = connection.cursor()
        try:
            if self.kind == "mysql":
                cursor.execute("SHOW CREATE TABLE {}".format(self.quote(table)))
                return cursor.fetchone()[1].rstrip("; \n")
            try:
                cursor.execute("SELECT DBMS_METADATA.GET_DDL('TABLE', :1) FROM DUAL", [table.upper()])
                value = cursor.fetchone()[0]
                if value:
                    return (value.read() if hasattr(value, "read") else str(value)).rstrip("; \n")
            except Exception:
                pass
            cursor.execute("SELECT column_name, data_type, data_length, data_precision, data_scale, nullable "
                           "FROM user_tab_columns WHERE table_name = :1 ORDER BY column_id", [table.upper()])
            columns = []
            for name, data_type, length, precision, scale, nullable in cursor.fetchall():
                definition = data_type
                if data_type in {"VARCHAR2", "CHAR", "NCHAR", "NVARCHAR2", "RAW"} and length:
                    definition += "({})".format(length)
                elif data_type == "NUMBER" and precision:
                    definition += "({}{})".format(precision, ",{}".format(scale) if scale is not None else "")
                columns.append("  {} {}{}".format(self.quote(name), definition, " NOT NULL" if nullable == "N" else ""))
            if not columns:
                raise RuntimeError("未读取到 {} 的字段定义".format(table))
            return "CREATE TABLE {} (\n{}\n)".format(self.quote(table), ",\n".join(columns))
        finally:
            cursor.close()

    def rows(self, connection, table: str, batch_size: int):
        cursor = None
        try:
            if self.kind == "mysql":
                cursor = self.driver.cursors.SSCursor(connection)
            else:
                cursor = connection.cursor()
                cursor.arraysize = batch_size
            cursor.execute("SELECT * FROM {}".format(self.quote(table)))
            columns = [column[0] for column in cursor.description]
            while True:
                values = cursor.fetchmany(batch_size)
                if not values:
                    return
                yield columns, values
        finally:
            if cursor:
                cursor.close()

    def insert_sql(self, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
        names = ", ".join(self.quote(name) for name in columns)
        data = ",\n".join("(" + ", ".join(self.literal(value) for value in row) + ")" for row in rows)
        return "INSERT INTO {} ({}) VALUES\n{};\n".format(self.quote(table), names, data)

    def literal(self, value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                raise ValueError("不能导出 NaN 或 Infinity")
            return repr(value)
        if hasattr(value, "read"):
            return self.literal(value.read())
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value).hex()
            return "X'{}'".format(raw) if self.kind == "mysql" else "HEXTORAW('{}')".format(raw)
        if isinstance(value, dt.datetime):
            if self.kind == "oracle":
                return "TO_TIMESTAMP('{}', 'YYYY-MM-DD HH24:MI:SS.FF')".format(value.strftime("%Y-%m-%d %H:%M:%S.%f"))
            return "'{}'".format(value.strftime("%Y-%m-%d %H:%M:%S.%f"))
        if isinstance(value, dt.date):
            return "TO_DATE('{}', 'YYYY-MM-DD')".format(value.isoformat()) if self.kind == "oracle" else "'{}'".format(value.isoformat())
        text = str(value).replace("'", "''")
        if self.kind == "mysql":
            text = text.replace("\\", "\\\\").replace("\x00", "\\0").replace("\n", "\\n").replace("\r", "\\r")
        return "'{}'".format(text)


class Exporter:
    def __init__(self, connection: ConnectionSettings, settings: ExportSettings,
                 status: Callable[[str], None], cancelled: threading.Event):
        self.connection = connection
        self.settings = settings
        self.status = status
        self.cancelled = cancelled

    def run(self, tables: Sequence[str]) -> list[Path]:
        if not tables:
            raise ValueError("请至少选择一张表")
        self.settings.directory.mkdir(parents=True, exist_ok=True)
        groups = [list(tables[index:index + self.settings.tables_per_file]) for index in range(0, len(tables), self.settings.tables_per_file)]
        workers = min(self.settings.export_workers, len(groups))
        completed: list[Path] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sql-export") as pool:
            futures = [pool.submit(self._export_group, number + 1, group) for number, group in enumerate(groups)]
            for future in as_completed(futures):
                completed.append(future.result())
        completed.sort()
        (self.settings.directory / "export_manifest.txt").write_text("\n".join(item.name for item in completed) + "\n", encoding="utf-8")
        runner = self._write_runner()
        self.status("导出完成: {} 个 SQL 文件，已生成 {}".format(len(completed), runner.name))
        return completed

    def _export_group(self, number: int, tables: Sequence[str]) -> Path:
        adapter = Adapter(self.connection)
        output = self.settings.directory / "group_{:03d}.sql".format(number)
        temporary = output.with_suffix(".sql.part")
        connection = adapter.connect()
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as file:
                file.write("-- Generated by DB to SQL Exporter at {}\n".format(dt.datetime.now().isoformat(timespec="seconds")))
                if adapter.kind == "oracle":
                    file.write("WHENEVER SQLERROR EXIT SQL.SQLCODE\nSET DEFINE OFF\n")
                for table in tables:
                    if self.cancelled.is_set():
                        raise Cancelled()
                    self.status("正在导出 group_{:03d}: {}".format(number, table))
                    file.write("\n-- Table: {}\n".format(table))
                    if self.settings.include_drop:
                        if adapter.kind == "mysql":
                            file.write("DROP TABLE IF EXISTS {};\n".format(adapter.quote(table)))
                        else:
                            file.write("BEGIN EXECUTE IMMEDIATE 'DROP TABLE {}'; EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;\n/\n".format(adapter.quote(table)))
                    file.write(adapter.ddl(connection, table) + ";\n")
                    if self.settings.include_data:
                        for columns, rows in adapter.rows(connection, table, self.settings.rows_per_insert):
                            if self.cancelled.is_set():
                                raise Cancelled()
                            file.write(adapter.insert_sql(table, columns, rows))
            temporary.replace(output)
            return output
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            connection.close()

    def _write_runner(self) -> Path:
        if self.connection.kind == "mysql":
            path = self.settings.directory / "restore_mysql.py"
            script = MYSQL_PYTHON_RUNNER
        else:
            path = self.settings.directory / "restore_oracle.py"
            script = ORACLE_PYTHON_RUNNER
        path.write_text(script, encoding="utf-8", newline="\n")
        return path


MYSQL_SHELL_RUNNER = r'''#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
    echo "Usage: $0 <host> <port> <database> <user>" >&2
    exit 2
fi

HOST="$1"
PORT="$2"
DATABASE="$3"
USER="$4"
MYSQL_BIN="${MYSQL_BIN:-mysql}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
read -rsp "Target MySQL password: " MYSQL_PWD
echo
export MYSQL_PWD
for FILE in "$SCRIPT_DIR"/group_*.sql; do
    [ -e "$FILE" ] || continue
    echo "Importing $(basename "$FILE")"
    "$MYSQL_BIN" --host="$HOST" --port="$PORT" --user="$USER" "$DATABASE" < "$FILE"
done
unset MYSQL_PWD
'''

MYSQL_BATCH_RUNNER = r'''@echo off
setlocal
if "%~4"=="" (
  echo Usage: %~nx0 ^<host^> ^<port^> ^<database^> ^<user^>
  exit /b 2
)
set /p MYSQL_PWD=Target MySQL password: 
for %%F in ("%~dp0group_*.sql") do (
  echo Importing %%~nxF
  mysql -h "%~1" -P "%~2" -u "%~4" "%~3" ^< "%%~fF" || exit /b 1
)
set MYSQL_PWD=
endlocal
'''

ORACLE_SHELL_RUNNER = r'''#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <connect_string> <user>" >&2
    exit 2
fi

CONNECT_STRING="$1"
USER="$2"
SQLPLUS_BIN="${SQLPLUS_BIN:-sqlplus}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
read -rsp "Target Oracle password: " PASSWORD
echo
for FILE in "$SCRIPT_DIR"/group_*.sql; do
    [ -e "$FILE" ] || continue
    echo "Importing $(basename "$FILE")"
    "$SQLPLUS_BIN" -L -S "${USER}/${PASSWORD}@${CONNECT_STRING}" "@${FILE}"
done
unset PASSWORD
'''

ORACLE_BATCH_RUNNER = r'''@echo off
setlocal
if "%~2"=="" (
  echo Usage: %~nx0 ^<connect_string^> ^<user^>
  exit /b 2
)
set /p PASSWORD=Target Oracle password: 
for %%F in ("%~dp0group_*.sql") do (
  echo Importing %%~nxF
  sqlplus -L -S "%~2/%PASSWORD%@%~1" "@%%~fF" || exit /b 1
)
set PASSWORD=
endlocal
'''
MYSQL_PYTHON_RUNNER = r'''#!/usr/bin/env python3
import argparse
import getpass
import os
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description="Sequential MySQL SQL importer")
parser.add_argument("host")
parser.add_argument("port", type=int)
parser.add_argument("database")
parser.add_argument("user")
parser.add_argument("--mysql-bin", default="mysql")
args = parser.parse_args()
password = getpass.getpass("Target MySQL password: ")
environment = os.environ.copy()
environment["MYSQL_PWD"] = password
files = sorted(Path(__file__).parent.glob("group_*.sql"))
if not files:
    raise SystemExit("No group_*.sql files found beside this script.")
for path in files:
    print("Importing {}".format(path.name))
    with path.open("rb") as source:
        subprocess.run([args.mysql_bin, "--host=" + args.host, "--port=" + str(args.port), "--user=" + args.user, args.database], stdin=source, env=environment, check=True)
print("Import complete.")
'''

ORACLE_PYTHON_RUNNER = r'''#!/usr/bin/env python3
import argparse
import getpass
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description="Sequential Oracle SQL importer")
parser.add_argument("connect_string")
parser.add_argument("user")
parser.add_argument("--sqlplus-bin", default="sqlplus")
args = parser.parse_args()
password = getpass.getpass("Target Oracle password: ")
files = sorted(Path(__file__).parent.glob("group_*.sql"))
if not files:
    raise SystemExit("No group_*.sql files found beside this script.")
connection = "{}/{}@{}".format(args.user, password, args.connect_string)
for path in files:
    print("Importing {}".format(path.name))
    subprocess.run([args.sqlplus_bin, "-L", "-S", connection, "@" + str(path)], check=True)
print("Import complete.")
'''


class App(ttk.Frame):
    def __init__(self, root: tk.Tk):
        super().__init__(root, padding=20)
        self.root = root
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop = threading.Event()
        self.tables: list[str] = []
        self.root.title("数据库转 SQL")
        self.root.geometry("1100x780")
        self.root.minsize(920, 680)
        self.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self._build()
        self.after(100, self._events)

    def _build(self):
        ttk.Label(self, text="数据库转 SQL", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(self, text="选择源表，生成可迁移的 SQL 分组文件和目标端恢复脚本", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 16))
        connection = ttk.LabelFrame(self, text="源数据库连接", padding=16, style="Panel.TLabelframe")
        connection.grid(row=2, column=0, sticky="ew")
        for col in (1, 3, 5): connection.columnconfigure(col, weight=1)
        self.kind = tk.StringVar(value="mysql")
        self.fields = {key: tk.StringVar() for key in ("host", "port", "user", "password", "database", "service", "sid", "dsn")}
        self.fields["host"].set("localhost"); self.fields["port"].set("3306")
        self._field(connection, "类型", ttk.Combobox(connection, textvariable=self.kind, values=("mysql", "oracle"), state="readonly"), 0, 0)
        self._field(connection, "主机", ttk.Entry(connection, textvariable=self.fields["host"]), 0, 2)
        self._field(connection, "端口", ttk.Entry(connection, textvariable=self.fields["port"]), 0, 4)
        self._field(connection, "用户名", ttk.Entry(connection, textvariable=self.fields["user"]), 1, 0)
        self._field(connection, "密码", ttk.Entry(connection, textvariable=self.fields["password"], show="*"), 1, 2)
        self.database_combo = ttk.Combobox(connection, textvariable=self.fields["database"], state="disabled")
        self.mysql_widgets = self._field(connection, "数据库", self.database_combo, 1, 4)
        self.oracle_widgets = [self._field(connection, "服务名", ttk.Entry(connection, textvariable=self.fields["service"]), 1, 4), self._field(connection, "SID", ttk.Entry(connection, textvariable=self.fields["sid"]), 2, 0), self._field(connection, "DSN", ttk.Entry(connection, textvariable=self.fields["dsn"]), 2, 2)]
        self.load_databases_button = ttk.Button(connection, text="读取数据库", command=self.load_databases)
        self.load_databases_button.grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Button(connection, text="读取表清单", command=self.load_tables, style="Primary.TButton").grid(row=3, column=2, columnspan=2, sticky="w", pady=(12, 0))
        self.kind.trace_add("write", self.toggle_kind); self.toggle_kind()

        tables = ttk.LabelFrame(self, text="选择导出表", padding=14, style="Panel.TLabelframe")
        tables.grid(row=3, column=0, sticky="nsew", pady=(14, 0))
        tables.columnconfigure(1, weight=1)
        tables.rowconfigure(1, weight=1)
        self.filter = tk.StringVar(); self.filter.trace_add("write", lambda *_: self.show_tables())
        ttk.Label(tables, text="筛选表名", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        ttk.Entry(tables, textvariable=self.filter).grid(row=0, column=1, sticky="ew", pady=(0, 6))
        ttk.Button(tables, text="全选结果", command=lambda: self.listbox.selection_set(0, tk.END)).grid(row=0, column=2, padx=(10, 6), pady=(0, 6))
        ttk.Button(tables, text="清除选择", command=lambda: self.listbox.selection_clear(0, tk.END)).grid(row=0, column=3, pady=(0, 6))
        list_area = ttk.Frame(tables, style="Panel.TFrame")
        list_area.grid(row=1, column=0, columnspan=4, sticky="nsew")
        list_area.columnconfigure(0, weight=1)
        list_area.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(list_area, selectmode=tk.EXTENDED, exportselection=False, background="#ffffff", foreground="#172033", selectbackground="#2563eb", selectforeground="#ffffff", borderwidth=0, highlightthickness=1, highlightbackground="#d7dde7", highlightcolor="#2563eb", activestyle="none")
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_area, orient="vertical", command=self.listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scrollbar.set)

        options = ttk.LabelFrame(self, text="导出设置", padding=16, style="Panel.TLabelframe")
        options.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        for col in (1, 3, 5):
            options.columnconfigure(col, weight=1)
        self.output = tk.StringVar(value=str(Path.cwd() / "sql_export")); self.per_file = tk.StringVar(value="30"); self.workers = tk.StringVar(value="4"); self.batch = tk.StringVar(value="1500"); self.data = tk.BooleanVar(value=True); self.drop = tk.BooleanVar(value=True)
        self._field(options, "输出目录", ttk.Entry(options, textvariable=self.output), 0, 0, 3)
        ttk.Button(options, text="选择目录", command=self.choose_dir).grid(row=0, column=5, padx=(0, 8), sticky="e")
        self._field(options, "每组表数", ttk.Spinbox(options, from_=1, to=1000, textvariable=self.per_file, width=8), 1, 0)
        self._field(options, "导出线程", ttk.Spinbox(options, from_=1, to=32, textvariable=self.workers, width=8), 1, 2)
        self._field(options, "每批行数", ttk.Spinbox(options, from_=1, to=10000, textvariable=self.batch, width=8), 1, 4)
        ttk.Checkbutton(options, text="导出数据", variable=self.data).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Checkbutton(options, text="生成 DROP TABLE", variable=self.drop).grid(row=2, column=2, columnspan=2, sticky="w", pady=(10, 0))
        self.start = ttk.Button(options, text="开始导出", command=self.export, style="Primary.TButton")
        self.start.grid(row=2, column=4, sticky="e", pady=(10, 0))
        ttk.Button(options, text="取消", command=self.stop.set).grid(row=2, column=5, padx=(8, 0), pady=(10, 0), sticky="w")
        self.status = tk.StringVar(value="填写连接信息后读取表。")
        ttk.Label(self, textvariable=self.status, style="Status.TLabel", anchor="w").grid(row=5, column=0, sticky="ew", pady=(12, 0))

    def _field(self, parent, label, widget, row, col, span=1):
        left = ttk.Label(parent, text=label); left.grid(row=row, column=col, sticky="w", padx=(0, 5), pady=3)
        widget.grid(row=row, column=col + 1, columnspan=span, sticky="ew", padx=(0, 10), pady=3)
        return left, widget

    def toggle_kind(self, *_):
        mysql = self.kind.get() == "mysql"; self.fields["port"].set("3306" if mysql else "1521")
        self.load_databases_button.configure(state="normal" if mysql else "disabled")
        for item in self.mysql_widgets: (item.grid if mysql else item.grid_remove)()
        for label, widget in self.oracle_widgets:
            (label.grid if not mysql else label.grid_remove)(); (widget.grid if not mysql else widget.grid_remove)()

    def connection(self) -> ConnectionSettings:
        try: port = int(self.fields["port"].get())
        except ValueError as exc: raise ValueError("端口必须是数字") from exc
        if not self.fields["user"].get().strip(): raise ValueError("必须填写用户名")
        return ConnectionSettings(self.kind.get(), self.fields["host"].get().strip(), port, self.fields["user"].get().strip(), self.fields["password"].get(), self.fields["database"].get().strip(), self.fields["service"].get().strip(), self.fields["sid"].get().strip(), self.fields["dsn"].get().strip())

    def load_databases(self):
        try:
            settings = self.connection()
        except ValueError as exc:
            messagebox.showerror("连接配置", str(exc))
            return
        self.status.set("正在读取数据库...")
        threading.Thread(target=self._database_thread, args=(settings,), daemon=True).start()

    def _database_thread(self, settings):
        try:
            self.events.put(("databases", Adapter(settings).list_databases()))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def load_tables(self):
        try:
            settings = self.connection()
            if settings.kind == "mysql" and not settings.database:
                raise ValueError("请先读取并选择数据库")
        except ValueError as exc:
            messagebox.showerror("连接配置", str(exc))
            return
        self.status.set("正在读取表...")
        threading.Thread(target=self._load_thread, args=(settings,), daemon=True).start()

    def _load_thread(self, settings):
        try: self.events.put(("tables", Adapter(settings).list_tables()))
        except Exception as exc: self.events.put(("error", str(exc)))

    def show_tables(self):
        selected = {self.listbox.get(i) for i in self.listbox.curselection()}; query = self.filter.get().lower()
        self.listbox.delete(0, tk.END)
        for index, table in enumerate(table for table in self.tables if query in table.lower()):
            self.listbox.insert(tk.END, table)
            if table in selected: self.listbox.selection_set(index)

    def choose_dir(self):
        directory = filedialog.askdirectory(initialdir=self.output.get())
        if directory: self.output.set(directory)

    def export(self):
        selected = [self.listbox.get(i) for i in self.listbox.curselection()]
        try:
            connection = self.connection(); settings = ExportSettings(Path(self.output.get()), int(self.per_file.get()), int(self.workers.get()), int(self.batch.get()), self.data.get(), self.drop.get())
            if not selected or min(settings.tables_per_file, settings.export_workers, settings.rows_per_insert) < 1: raise ValueError("请选择表，并填写大于零的导出参数")
        except ValueError as exc: messagebox.showerror("导出配置", str(exc)); return
        self.stop.clear(); self.start.configure(state="disabled"); threading.Thread(target=self._export_thread, args=(connection, settings, selected), daemon=True).start()

    def _export_thread(self, connection, settings, tables):
        try:
            Exporter(connection, settings, lambda text: self.events.put(("status", text)), self.stop).run(tables)
            self.events.put(("done", "导出完成。输出目录中包含 group SQL 文件和恢复脚本。"))
        except Cancelled: self.events.put(("done", "导出已取消，已完成的分组文件会保留。"))
        except Exception: self.events.put(("error", traceback.format_exc(limit=2)))

    def _events(self):
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "status": self.status.set(payload)
                elif event == "databases":
                    self.database_combo.configure(values=payload, state="readonly")
                    if payload and self.fields["database"].get() not in payload:
                        self.fields["database"].set(payload[0])
                    self.status.set("已读取 {} 个数据库，请选择后读取表清单。".format(len(payload)))
                elif event == "tables": self.tables = payload; self.show_tables(); self.status.set("已读取 {} 张表。".format(len(payload)))
                elif event == "done": self.start.configure(state="normal"); self.status.set(payload); messagebox.showinfo("数据库转 SQL", payload)
                elif event == "error": self.start.configure(state="normal"); self.status.set("操作失败"); messagebox.showerror("数据库转 SQL", payload)
        except queue.Empty: pass
        self.after(100, self._events)


def main():
    root = tk.Tk(); style = ttk.Style(root)
    if "clam" in style.theme_names(): style.theme_use("clam")
    style.configure("TFrame", background="#f3f5f8")
    style.configure("Panel.TFrame", background="#ffffff")
    style.configure("TLabel", background="#f3f5f8", foreground="#172033", font=("Segoe UI", 10))
    style.configure("Panel.TLabel", background="#ffffff", foreground="#42526a", font=("Segoe UI", 10))
    style.configure("Title.TLabel", background="#f3f5f8", font=("Segoe UI", 21, "bold"), foreground="#10213f")
    style.configure("Hint.TLabel", background="#f3f5f8", foreground="#64748b")
    style.configure("TLabelframe", background="#f3f5f8", bordercolor="#d7dde7", relief="solid")
    style.configure("TLabelframe.Label", background="#f3f5f8", foreground="#1e293b", font=("Segoe UI", 10, "bold"))
    style.configure("Panel.TLabelframe", background="#f3f5f8", bordercolor="#d7dde7", relief="solid")
    style.configure("Panel.TLabelframe.Label", background="#f3f5f8", foreground="#1e293b", font=("Segoe UI", 10, "bold"))
    style.configure("TEntry", fieldbackground="#ffffff", padding=6)
    style.configure("TCombobox", padding=5)
    style.configure("TButton", padding=(10, 6), font=("Segoe UI", 10))
    style.configure("Primary.TButton", background="#2563eb", foreground="white", font=("Segoe UI", 10, "bold"), padding=(13, 7))
    style.map("Primary.TButton", background=[("active", "#1d4ed8"), ("disabled", "#9eb8ee")])
    style.configure("Status.TLabel", background="#e7eef9", foreground="#334155", padding=(12, 8))
    App(root); root.mainloop()


if __name__ == "__main__":
    main()
