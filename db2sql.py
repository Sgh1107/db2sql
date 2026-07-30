#!/usr/bin/env python3
"""
高性能多数据库操作工具 v2.0
支持 MySQL (PyMySQL/mysqlclient) 和 Oracle (cx_Oracle)
核心优化：连接池、预编译语句缓存、批量操作、流式查询、智能重连
"""

import logging
import time
import threading
from contextlib import contextmanager
from typing import Any, List, Dict, Optional, Tuple, Union, Generator
from queue import Queue, Empty, Full
import hashlib
import re

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ============================================================
# 连接池实现（线程安全、高性能）
# ============================================================
class ConnectionPool:
    """通用连接池，支持 MySQL 和 Oracle"""
    
    def __init__(
        self,
        creator,
        min_size: int = 2,
        max_size: int = 10,
        idle_timeout: int = 300,
        max_lifetime: int = 3600,
        acquire_timeout: int = 30,
        health_check_interval: int = 60
    ):
        """
        Args:
            creator:        连接工厂函数
            min_size:       最小连接数
            max_size:       最大连接数
            idle_timeout:   空闲超时（秒），超时后回收
            max_lifetime:   连接最大存活时间（秒）
            acquire_timeout: 获取连接超时（秒）
            health_check_interval: 健康检查间隔（秒）
        """
        self.creator = creator
        self.min_size = min_size
        self.max_size = max_size
        self.idle_timeout = idle_timeout
        self.max_lifetime = max_lifetime
        self.acquire_timeout = acquire_timeout
        self.health_check_interval = health_check_interval
        
        self._pool: Queue = Queue(maxsize=max_size)
        self._size: int = 0
        self._lock = threading.Lock()
        self._last_health_check: float = 0
        
        # 启动后台健康检查线程
        self._running = True
        self._health_thread = threading.Thread(target=self._health_checker, daemon=True)
        self._health_thread.start()
        
        # 预创建最小连接数
        self._prefill()
    
    def _prefill(self):
        """预创建最小连接数"""
        for _ in range(self.min_size):
            try:
                conn = self._create_connection()
                self._pool.put(conn, block=False)
            except Exception:
                logger.warning("预填充连接池失败，将在需要时懒加载")
                break
    
    def _create_connection(self):
        """创建新连接并记录创建时间"""
        conn = self.creator()
        conn._created_at = time.time()
        conn._last_used = time.time()
        with self._lock:
            self._size += 1
        return conn
    
    def _destroy_connection(self, conn):
        """销毁连接"""
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            self._size -= 1
    
    @contextmanager
    def get(self):
        """获取连接（上下文管理器）"""
        conn = self._acquire()
        try:
            yield conn
        finally:
            self._release(conn)
    
    def _acquire(self):
        """获取连接，带超时和自动扩容"""
        deadline = time.time() + self.acquire_timeout
        
        while time.time() < deadline:
            # 1. 尝试从池中获取
            try:
                conn = self._pool.get(block=True, timeout=min(1.0, deadline - time.time()))
                # 检查连接是否过期
                if time.time() - conn._created_at > self.max_lifetime:
                    self._destroy_connection(conn)
                    continue
                # 检查连接是否存活
                if not self._ping(conn):
                    self._destroy_connection(conn)
                    continue
                conn._last_used = time.time()
                return conn
            except Empty:
                pass
            
            # 2. 池为空，尝试创建新连接
            with self._lock:
                if self._size < self.max_size:
                    try:
                        conn = self._create_connection()
                        conn._last_used = time.time()
                        return conn
                    except Exception as e:
                        logger.error(f"创建连接失败: {e}")
                        time.sleep(0.5)
                        continue
            
            # 3. 池已满，等待其他线程归还
            try:
                conn = self._pool.get(block=True, timeout=min(1.0, deadline - time.time()))
                conn._last_used = time.time()
                return conn
            except Empty:
                continue
        
        raise TimeoutError(f"获取连接超时（{self.acquire_timeout}秒）")
    
    def _release(self, conn):
        """归还连接到池中"""
        if conn is None:
            return
        try:
            self._pool.put(conn, block=False)
        except Full:
            self._destroy_connection(conn)
    
    def _ping(self, conn) -> bool:
        """检查连接是否存活"""
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM DUAL" if hasattr(conn, 'dsn') else "SELECT 1")
            cursor.fetchone()
            cursor.close()
            return True
        except Exception:
            return False
    
    def _health_checker(self):
        """后台健康检查，清理过期和空闲连接"""
        while self._running:
            time.sleep(self.health_check_interval)
            try:
                # 检查过期连接
                expired = []
                while True:
                    try:
                        conn = self._pool.get(block=False)
                        if time.time() - conn._created_at > self.max_lifetime or \
                           time.time() - conn._last_used > self.idle_timeout:
                            expired.append(conn)
                        else:
                            self._pool.put(conn, block=False)
                            break
                    except Empty:
                        break
                
                for conn in expired:
                    self._destroy_connection(conn)
                
                # 补充到最小连接数
                with self._lock:
                    need = self.min_size - self._size
                for _ in range(need):
                    try:
                        conn = self._create_connection()
                        self._pool.put(conn, block=False)
                    except Exception:
                        break
                        
            except Exception as e:
                logger.error(f"健康检查异常: {e}")
    
    def close(self):
        """关闭连接池"""
        self._running = False
        while True:
            try:
                conn = self._pool.get(block=False)
                self._destroy_connection(conn)
            except Empty:
                break
    
    @property
    def size(self) -> int:
        return self._size


# ============================================================
# 预编译语句缓存（LRU）
# ============================================================
class PreparedCache:
    """预编译语句缓存，避免重复解析 SQL"""
    
    def __init__(self, max_size: int = 100):
        self._cache: Dict[str, Any] = {}
        self._access_order: List[str] = []
        self.max_size = max_size
        self._lock = threading.Lock()
    
    def _hash_sql(self, sql: str) -> str:
        return hashlib.md5(sql.encode()).hexdigest()
    
    def get(self, sql: str):
        key = self._hash_sql(sql)
        with self._lock:
            if key in self._cache:
                self._access_order.remove(key)
                self._access_order.append(key)
                return self._cache[key]
        return None
    
    def set(self, sql: str, stmt):
        key = self._hash_sql(sql)
        with self._lock:
            if key in self._cache:
                self._access_order.remove(key)
            elif len(self._cache) >= self.max_size:
                old_key = self._access_order.pop(0)
                del self._cache[old_key]
            self._cache[key] = stmt
            self._access_order.append(key)
    
    def clear(self):
        with self._lock:
            self._cache.clear()
            self._access_order.clear()


# ============================================================
# 核心数据库操作类
# ============================================================
class FastDB:
    """高性能数据库操作封装"""
    
    def __init__(self, db_type: str, **kwargs):
        """
        Args:
            db_type: 'mysql' 或 'oracle'
            **kwargs: 连接参数
        """
        self.db_type = db_type.lower()
        self.kwargs = kwargs
        self.pool: Optional[ConnectionPool] = None
        self.prepared_cache = PreparedCache()
        
        # 初始化驱动和连接池
        self._init_pool()
    
    def _init_pool(self):
        """初始化连接池"""
        if self.db_type == 'mysql':
            self._init_mysql_pool()
        elif self.db_type == 'oracle':
            self._init_oracle_pool()
        else:
            raise ValueError(f"不支持的数据库类型: {self.db_type}")
    
    def _init_mysql_pool(self):
        """初始化 MySQL 连接池"""
        try:
            import pymysql
            self.driver = pymysql
        except ImportError:
            try:
                import MySQLdb
                self.driver = MySQLdb
            except ImportError:
                raise ImportError("请安装 PyMySQL 或 mysqlclient: pip install PyMySQL")
        
        # MySQL 连接参数
        self.mysql_params = {
            'host': self.kwargs.get('host', 'localhost'),
            'port': int(self.kwargs.get('port', 3306)),
            'user': self.kwargs['user'],
            'password': self.kwargs['password'],
            'database': self.kwargs.get('database', ''),
            'charset': self.kwargs.get('charset', 'utf8mb4'),
            'autocommit': True,
            'connect_timeout': 10,
            'cursorclass': self.driver.cursors.DictCursor,
        }
        
        def create_mysql_conn():
            return self.driver.connect(**self.mysql_params)
        
        self.pool = ConnectionPool(
            creator=create_mysql_conn,
            min_size=self.kwargs.get('pool_min', 2),
            max_size=self.kwargs.get('pool_max', 10),
            idle_timeout=self.kwargs.get('pool_idle_timeout', 300),
            max_lifetime=self.kwargs.get('pool_max_lifetime', 3600),
            acquire_timeout=self.kwargs.get('pool_acquire_timeout', 30),
        )
    
    def _init_oracle_pool(self):
        """初始化 Oracle 连接池"""
        try:
            import cx_Oracle
            self.driver = cx_Oracle
        except ImportError:
            raise ImportError("请安装 cx_Oracle: pip install cx_Oracle")
        
        # Oracle 连接参数
        dsn = self.kwargs.get('dsn', '')
        if not dsn:
            host = self.kwargs.get('host', 'localhost')
            port = self.kwargs.get('port', '1521')
            service_name = self.kwargs.get('service_name', '')
            sid = self.kwargs.get('sid', '')
            if service_name:
                dsn = f"{host}:{port}/{service_name}"
            elif sid:
                dsn = f"{host}:{port}:{sid}"
            else:
                dsn = f"{host}:{port}"
        
        self.oracle_dsn = dsn
        self.oracle_user = self.kwargs['user']
        self.oracle_password = self.kwargs['password']
        self.oracle_mode = self.kwargs.get('mode', None)
        
        def create_oracle_conn():
            return self.driver.connect(
                user=self.oracle_user,
                password=self.oracle_password,
                dsn=self.oracle_dsn,
                encoding='UTF-8',
                nencoding='UTF-8',
            )
        
        self.pool = ConnectionPool(
            creator=create_oracle_conn,
            min_size=self.kwargs.get('pool_min', 2),
            max_size=self.kwargs.get('pool_max', 10),
            idle_timeout=self.kwargs.get('pool_idle_timeout', 300),
            max_lifetime=self.kwargs.get('pool_max_lifetime', 3600),
            acquire_timeout=self.kwargs.get('pool_acquire_timeout', 30),
        )
    
    # ============================================================
    # 基本查询操作
    # ============================================================
    def query(self, sql: str, params: Union[tuple, dict, None] = None,
              fetch_size: int = 0) -> List[Dict]:
        """执行查询并返回所有结果"""
        start = time.time()
        with self.pool.get() as conn:
            cursor = conn.cursor()
            
            if self.db_type == 'oracle' and fetch_size > 0:
                cursor.arraysize = fetch_size
            
            cursor.execute(sql, params or ())
            
            if self.db_type == 'mysql':
                result = cursor.fetchall()
            else:
                # Oracle: 手动构建字典列表
                columns = [col[0] for col in cursor.description]
                rows = cursor.fetchall()
                result = [dict(zip(columns, row)) for row in rows]
            
            cursor.close()
            
            elapsed = time.time() - start
            if elapsed > 1.0:
                logger.warning(f"慢查询 ({elapsed:.2f}s): {sql[:200]}")
            
            return result
    
    def query_one(self, sql: str, params: Union[tuple, dict, None] = None) -> Optional[Dict]:
        """查询单行结果"""
        with self.pool.get() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or ())
            
            if self.db_type == 'mysql':
                result = cursor.fetchone()
            else:
                row = cursor.fetchone()
                if row:
                    columns = [col[0] for col in cursor.description]
                    result = dict(zip(columns, row))
                else:
                    result = None
            
            cursor.close()
            return result
    
    def query_column(self, sql: str, params: Union[tuple, dict, None] = None,
                     column_index: int = 0) -> List[Any]:
        """查询单列数据"""
        with self.pool.get() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or ())
            result = [row[column_index] for row in cursor.fetchall()]
            cursor.close()
            return result
    
    # ============================================================
    # 流式查询（处理大数据量）
    # ============================================================
    def query_stream(self, sql: str, params: Union[tuple, dict, None] = None,
                     batch_size: int = 1000) -> Generator[List[Dict], None, None]:
        """流式查询，适用于大数据量场景"""
        with self.pool.get() as conn:
            if self.db_type == 'mysql':
                cursor = self.driver.cursors.SSCursor(conn)
            else:
                cursor = conn.cursor()
                cursor.arraysize = batch_size
            
            cursor.execute(sql, params or ())
            
            if self.db_type == 'mysql':
                # MySQL SSCursor 逐行读取
                batch = []
                for row in cursor:
                    batch.append(row)
                    if len(batch) >= batch_size:
                        yield batch
                        batch = []
                if batch:
                    yield batch
            else:
                # Oracle fetchmany
                columns = [col[0] for col in cursor.description]
                while True:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    yield [dict(zip(columns, row)) for row in rows]
            
            cursor.close()
    
    # ============================================================
    # 写入操作
    # ============================================================
    def execute(self, sql: str, params: Union[tuple, dict, None] = None) -> int:
        """执行 DML 语句，返回影响行数"""
        start = time.time()
        with self.pool.get() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, params or ())
            affected = cursor.rowcount
            conn.commit()
            cursor.close()
            
            elapsed = time.time() - start
            if elapsed > 1.0:
                logger.warning(f"慢写入 ({elapsed:.2f}s): {sql[:200]}")
            
            return affected
    
    def execute_many(self, sql: str, params_list: List[Union[tuple, dict]]) -> int:
        """批量执行 DML 语句（高性能批量插入/更新）"""
        if not params_list:
            return 0
        
        start = time.time()
        with self.pool.get() as conn:
            cursor = conn.cursor()
            
            # Oracle 需要特殊处理批量绑定
            if self.db_type == 'oracle' and len(params_list) > 100:
                cursor.prepare(sql)
                cursor.executemany(None, params_list)
            else:
                cursor.executemany(sql, params_list)
            
            affected = cursor.rowcount
            conn.commit()
            cursor.close()
            
            elapsed = time.time() - start
            logger.debug(f"批量写入 {len(params_list)} 条, 耗时 {elapsed:.3f}s")
            
            return affected
    
    def insert_batch(self, table: str, data: List[Dict],
                     replace: bool = False) -> int:
        """高性能批量插入
        
        Args:
            table: 表名
            data: 字典列表，每个字典代表一行
            replace: 是否使用 REPLACE INTO (仅 MySQL)
        """
        if not data:
            return 0
        
        columns = list(data[0].keys())
        placeholders = ', '.join(['%s'] * len(columns))
        column_names = ', '.join(columns)
        
        # 处理 Oracle 占位符差异
        if self.db_type == 'oracle':
            placeholders = ', '.join([f':{i+1}' for i in range(len(columns))])
        
        insert_type = 'REPLACE INTO' if (replace and self.db_type == 'mysql') else 'INSERT INTO'
        sql = f"{insert_type} {table} ({column_names}) VALUES ({placeholders})"
        
        # 转换为元组列表
        params_list = [tuple(row[col] for col in columns) for row in data]
        
        return self.execute_many(sql, params_list)
    
    # ============================================================
    # 事务支持
    # ============================================================
    @contextmanager
    def transaction(self):
        """事务上下文管理器"""
        with self.pool.get() as conn:
            conn.autocommit = False
            cursor = conn.cursor()
            try:
                yield cursor
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.autocommit = True
                cursor.close()
    
    # ============================================================
    # 表操作
    # ============================================================
    def table_exists(self, table: str) -> bool:
        """检查表是否存在"""
        if self.db_type == 'mysql':
            sql = "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s"
            db = self.kwargs.get('database', '')
            result = self.query_one(sql, (db, table))
        else:
            sql = "SELECT 1 FROM user_tables WHERE table_name = UPPER(:1)"
            result = self.query_one(sql, (table.upper(),))
        return result is not None
    
    def get_tables(self) -> List[str]:
        """获取所有表名"""
        if self.db_type == 'mysql':
            db = self.kwargs.get('database', '')
            sql = "SELECT TABLE_NAME FROM information_schema.tables WHERE table_schema = %s"
            return self.query_column(sql, (db,))
        else:
            sql = "SELECT TABLE_NAME FROM user_tables"
            return self.query_column(sql)
    
    def count(self, table: str, where: str = '', params: Union[tuple, dict, None] = None) -> int:
        """统计行数"""
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        result = self.query_one(sql, params)
        return list(result.values())[0] if result else 0
    
    # ============================================================
    # 分页查询
    # ============================================================
    def paginate(self, table: str, page: int = 1, page_size: int = 20,
                 columns: str = '*', where: str = '',
                 order_by: str = '',
                 params: Union[tuple, dict, None] = None) -> Dict:
        """高性能分页查询"""
        total = self.count(table, where, params)
        
        if self.db_type == 'mysql':
            offset = (page - 1) * page_size
            sql = f"SELECT {columns} FROM {table}"
            if where:
                sql += f" WHERE {where}"
            if order_by:
                sql += f" ORDER BY {order_by}"
            sql += f" LIMIT {offset}, {page_size}"
        else:
            # Oracle 分页（12c+ 支持 OFFSET FETCH）
            offset = (page - 1) * page_size + 1
            end = offset + page_size - 1
            sql = f"""
            SELECT * FROM (
                SELECT a.*, ROWNUM rn FROM (
                    SELECT {columns} FROM {table}
                    {f'WHERE {where}' if where else ''}
                    {f'ORDER BY {order_by}' if order_by else ''}
                ) a WHERE ROWNUM <= {end}
            ) WHERE rn >= {offset}
            """
        
        rows = self.query(sql, params)
        
        return {
            'page': page,
            'page_size': page_size,
            'total': total,
            'total_pages': (total + page_size - 1) // page_size,
            'data': rows
        }
    
    # ============================================================
    # 高级功能
    # ============================================================
    def explain(self, sql: str, params: Union[tuple, dict, None] = None) -> List[Dict]:
        """查看执行计划"""
        if self.db_type == 'mysql':
            return self.query(f"EXPLAIN {sql}", params)
        else:
            # Oracle 需要先执行 EXPLAIN PLAN
            self.execute(f"EXPLAIN PLAN FOR {sql}")
            plan = self.query("SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY)")
            return plan
    
    def call_proc(self, proc_name: str, params: Union[tuple, dict, None] = None) -> Any:
        """调用存储过程/函数"""
        with self.pool.get() as conn:
            cursor = conn.cursor()
            if self.db_type == 'oracle':
                # Oracle 存储过程
                result = cursor.callproc(proc_name, params or ())
            else:
                # MySQL 存储过程
                cursor.callproc(proc_name, params or ())
                result = cursor.fetchall()
            cursor.close()
            return result
    
    # ============================================================
    # 资源释放
    # ============================================================
    def close(self):
        """关闭连接池"""
        if self.pool:
            self.pool.close()
            self.pool = None
        self.prepared_cache.clear()
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


# ============================================================
# 使用示例
# ============================================================
def _legacy_example():
    """Retained as a reference; interactive exports start the GUI instead."""
    # ---- MySQL 示例 ----
    mysql_config = {
        'host': 'localhost',
        'port': 3306,
        'user': 'dbuser',
        'password': 'dbuser',
        'database': 'test',
        'charset': 'utf8mb4',
        'pool_min': 2,
        'pool_max': 20,
    }
    
    # ---- Oracle 示例 ----
    oracle_config = {
        'host': '192.168.7.12',
        'port': 1521,
        'service_name': 'oradb',  # 或使用 'sid': 'oradb'
        'user': 'dbuser',
        'password': 'dbuser',
        'pool_min': 2,
        'pool_max': 20,
    }
    
    # 选择一种数据库
    # db = FastDB('mysql', **mysql_config)
    db = FastDB('oracle', **oracle_config)
    
    try:
        # 1. 简单查询
        users = db.query("SELECT * FROM users WHERE status = :1", ('active',))
        print(f"查询到 {len(users)} 条记录")
        
        # 2. 单行查询
        user = db.query_one("SELECT * FROM users WHERE id = :1", (1,))
        print(user)
        
        # 3. 单列查询
        ids = db.query_column("SELECT id FROM users")
        print(ids)
        
        # 4. 批量插入（高性能）
        data = [
            {'name': f'user_{i}', 'email': f'user{i}@test.com'}
            for i in range(1000)
        ]
        affected = db.insert_batch('users', data)
        print(f"批量插入 {affected} 条")
        
        # 5. 流式查询（处理千万级数据）
        for batch in db.query_stream("SELECT * FROM huge_table", batch_size=5000):
            for row in batch:
                # 处理每一行
                pass
            print(f"处理批次: {len(batch)} 条")
        
        # 6. 事务
        with db.transaction() as cur:
            cur.execute("UPDATE accounts SET balance = balance - 100 WHERE id = :1", (1,))
            cur.execute("UPDATE accounts SET balance = balance + 100 WHERE id = :1", (2,))
        
        # 7. 分页
        page_data = db.paginate('users', page=1, page_size=20, order_by='id DESC')
        print(f"第1页，共{page_data['total_pages']}页，总{page_data['total']}条")
        
        # 8. 存储过程
        # result = db.call_proc('sp_process_data', (param1, param2))
        
        # 9. 查看执行计划
        plan = db.explain("SELECT * FROM users WHERE status = :1", ('active',))
        for row in plan:
            print(row)
            
    finally:
        db.close()


if __name__ == '__main__':
    from db2sql_app import main
    main()