**expdp（Export Data Pump）是 Oracle 自带的数据泵导出工具**，从 Oracle 10g 开始就随数据库一起安装，不需要额外部署。

`expdp` 已经在 `$ORACLE_HOME/bin` 目录下了。

---

## expdp 和 exp 的区别

| | expdp（数据泵） | exp（传统导出） |
|------|------|------|
| 运行位置 | **服务端**，导出文件存在服务器上 | 客户端也可用 |
| 性能 | 快很多（并行、直接路径） | 较慢 |
| 导出文件 | 二进制 `.dmp`，不可读 | 二进制 `.dmp` |
| 推荐 | ✅ 10g 后用这个 | ❌ 逐步淘汰 |

**重要**：expdp 是服务端工具，导出文件只能存在数据库服务器本地，不能直接写到客户端。

---

## 基本用法

### 1. 导出整个数据库
```bash
expdp 用户名/密码@连接名 full=y directory=目录名 dumpfile=备份名.dmp
```

### 2. 导出指定用户的所有表
```bash
expdp 用户名/密码@连接名 schemas=用户名 directory=目录名 dumpfile=用户名.dmp
```

### 3. 导出指定表
```bash
expdp 用户名/密码@连接名 tables=表1,表2 directory=目录名 dumpfile=表.dmp
```

### 4. 只导出表结构（不要数据）
```bash
expdp 用户名/密码@连接名 schemas=用户名 content=metadata_only directory=目录名 dumpfile=结构.dmp
```

### 5. 只导出数据
```bash
expdp 用户名/密码@连接名 schemas=用户名 content=data_only directory=目录名 dumpfile=数据.dmp
```

---

## 你的环境实操步骤

根据之前的信息，你在 `orasrv` 用户下操作，`ORACLE_HOME` 是 `/appdata/orasrv/app/orasrv/product/11.2.0/dbhome_1`。

### 第一步：创建导出目录（只需要做一次）

```sql
-- 用 sys 或 system 登录
sqlplus / as sysdba

-- 创建目录对象，指向服务器上的实际路径
CREATE OR REPLACE DIRECTORY exp_dir AS '/appdata/orasrv/backup';

-- 授权给导出用户
GRANT READ, WRITE ON DIRECTORY exp_dir TO dbuser;
```

在服务器上创建对应的物理目录：
```bash
mkdir -p /appdata/orasrv/backup
```

### 第二步：执行导出

```bash
# 先确保环境变量
export ORACLE_HOME=/appdata/orasrv/app/orasrv/product/11.2.0/dbhome_1
export PATH=$ORACLE_HOME/bin:$PATH

# 导出 dbuser 用户的所有表
expdp dbuser/dbuser@BXQRDB2 schemas=dbuser directory=exp_dir dumpfile=dbuser_$(date +%Y%m%d).dmp logfile=export.log
```

---

## 常用参数

| 参数 | 说明 |
|------|------|
| `full=y` | 导出整个数据库 |
| `schemas=用户1,用户2` | 导出指定用户 |
| `tables=表1,表2` | 导出指定表 |
| `directory=目录名` | 必填，指定导出文件存放的逻辑目录 |
| `dumpfile=文件名.dmp` | 导出文件名 |
| `logfile=日志.log` | 日志文件名 |
| `content=all/metadata_only/data_only` | 导出内容 |
| `parallel=4` | 并行度，加速导出 |
| `compression=all` | 压缩（11g+） |

---

## 对应的导入工具 impdp

```bash
# 导入整个用户
impdp dbuser/dbuser@BXQRDB2 schemas=dbuser directory=exp_dir dumpfile=dbuser_20260731.dmp

# 导入到另一个用户（表空间迁移）
impdp system/密码@BXQRDB2 remap_schema=dbuser:newuser directory=exp_dir dumpfile=dbuser.dmp

# 只导入表结构
impdp dbuser/dbuser@BXQRDB2 schemas=dbuser content=metadata_only directory=exp_dir dumpfile=dbuser.dmp
```

---

## 和 mysqldump 的关键区别

| | mysqldump | expdp |
|------|------|------|
| 输出格式 | 可读的 SQL 文本 | 二进制 `.dmp` |
| 输出位置 | 客户端本地 | 数据库服务器 |
| 可以用编辑器打开修改 | ✅ 可以 | ❌ 不行 |
| 需要建 DIRECTORY | ❌ | ✅ 必须 |
