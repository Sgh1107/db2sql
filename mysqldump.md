**mysqldump** 是 MySQL 官方自带的**数据库备份工具**，能把数据库的结构和数据导出成 SQL 文本文件，方便备份、迁移或版本控制。

---

## 基本用法

### 1. 导出整个数据库
```bash
mysqldump -u 用户名 -p 数据库名 > 备份文件.sql
```
例如：
```bash
mysqldump -u root -p mydb > mydb_backup.sql
```
回车后输入密码，就会把 `mydb` 整个库导出到 `mydb_backup.sql`。

---

### 2. 只导出表结构（不导出数据）
```bash
mysqldump -u root -p --no-data mydb > mydb_structure.sql
```

### 3. 只导出数据（不导出表结构）
```bash
mysqldump -u root -p --no-create-info mydb > mydb_data.sql
```

### 4. 导出指定表
```bash
mysqldump -u root -p mydb table1 table2 > tables_backup.sql
```

### 5. 导出所有数据库
```bash
mysqldump -u root -p --all-databases > all_db.sql
```

### 6. 远程数据库导出
```bash
mysqldump -h 192.168.7.12 -P 3306 -u root -p mydb > remote_backup.sql
```

---

## 常用参数

| 参数 | 说明 |
|------|------|
| `--no-data` | 只导结构，不导数据 |
| `--no-create-info` | 只导数据，不导建表语句 |
| `--add-drop-table` | 在 CREATE TABLE 前加 DROP TABLE（默认开启） |
| `--single-transaction` | 使用事务保证 InnoDB 备份一致性，不锁表 |
| `--routines` | 导出存储过程和函数 |
| `--triggers` | 导出触发器 |
| `--events` | 导出定时任务 |
| `--quick` | 一行一行读表，大表备份不爆内存 |

---

## 生产环境推荐用法

```bash
mysqldump -u root -p \
  --single-transaction \
  --routines \
  --triggers \
  --events \
  mydb > mydb_$(date +%Y%m%d).sql
```

-   `--single-transaction`：对 InnoDB 表不锁表，备份期间业务可正常读写
-   `$(date +%Y%m%d)`：自动加日期后缀，方便区分版本

---

## 恢复数据

把 SQL 文件导回去：
```bash
mysql -u root -p mydb < mydb_backup.sql
```

---

## 和你之前 Oracle 环境的对比

| | MySQL | Oracle |
|------|------|------|
| 导出工具 | `mysqldump` | `exp` / `expdp` |
| 导入工具 | `mysql` | `imp` / `impdp` |
| 导出格式 | 纯 SQL 文本 | 二进制 `.dmp` 文件 |
