"""
数据库自动备份：PostgreSQL → Cloudflare R2（backups/ 目录）。

流程
----
1. 优先用 ``pg_dump`` 导出（保真度最高）；
   若 pg_dump 不可用或版本不匹配，回退到基于 SQLAlchemy 的「逻辑导出」
   （反射所有表 → CREATE TABLE + INSERT），保证任何环境都能产出可用备份。
2. 备份文件名 ``backup_YYYYMMDD_HHMMSS.sql``，经存储层上传到 R2 ``backups/``。
3. 上传由 ``STORAGE.save_file`` 完成（会自动删掉本地临时文件）。
4. 清理：删除超过 ``BACKUP_RETENTION_DAYS`` 天的旧备份（按文件名时间戳判断）。

对外函数：
- ``run_backup()``        执行一次备份 + 清理，返回结果 dict
- ``list_backups()``      列出 R2 上的备份（管理后台用）
- ``cleanup_old_backups()`` 单独清理旧备份
"""
import datetime
import logging
import os
import re
import shutil
import subprocess

from .database import DATABASE_URL, engine
from .storage import STORAGE

logger = logging.getLogger("videohub.backup")

BACKUP_PREFIX = "backups/"
NAME_RE = re.compile(r"backup_(\d{8}_\d{6})\.sql$")


def _retention_days() -> int:
    try:
        return max(1, int(os.getenv("BACKUP_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


def _ts() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")


def is_postgres() -> bool:
    return DATABASE_URL.startswith(("postgresql://", "postgresql+"))


# ── 导出实现 ──────────────────────────────────────────────────────────────────
def _dump_with_pg_dump(out_path: str) -> bool:
    """用 pg_dump 导出纯 SQL；成功返回 True。"""
    pg_dump = shutil.which("pg_dump")
    if not pg_dump or not is_postgres():
        return False
    try:
        with open(out_path, "wb") as f:
            res = subprocess.run(
                [pg_dump, "--no-owner", "--no-privileges", "--clean",
                 "--if-exists", DATABASE_URL],
                stdout=f, stderr=subprocess.PIPE, timeout=900,
            )
        if res.returncode == 0 and os.path.getsize(out_path) > 0:
            logger.info("pg_dump 导出成功")
            return True
        logger.warning("pg_dump 失败 rc=%s: %s", res.returncode,
                       res.stderr.decode("utf-8", "replace")[:500])
    except Exception as exc:
        logger.warning("pg_dump 异常: %s", exc)
    return False


def _sql_literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return "'" + v.isoformat() + "'"
    if isinstance(v, (bytes, bytearray)):
        return "'\\x" + v.hex() + "'"
    return "'" + str(v).replace("'", "''") + "'"


def _dump_with_sqlalchemy(out_path: str) -> bool:
    """逻辑导出：按 ORM 模型的表 → DROP/CREATE + INSERT。pg_dump 不可用时的兜底。

    用 Base.metadata（应用真实模型）而非数据库反射，避免把迁移残留的临时表
    （如 _videos_bak）等也导出来，结果更干净、可预测。
    """
    from sqlalchemy import select
    from sqlalchemy.schema import CreateTable
    from . import models  # noqa: F401  确保模型注册到 Base.metadata
    from .database import Base

    tables = list(Base.metadata.sorted_tables)
    if not tables:
        return False
    with open(out_path, "w", encoding="utf-8") as f, engine.connect() as conn:
        f.write(f"-- VideoHub 逻辑备份 {_ts()} UTC（SQLAlchemy fallback）\n")
        for table in tables:
            f.write(f"\nDROP TABLE IF EXISTS {table.name} CASCADE;\n")
            f.write(str(CreateTable(table).compile(engine)).strip() + ";\n")
            cols = [c.name for c in table.columns]
            for row in conn.execute(select(table)):
                vals = ", ".join(_sql_literal(v) for v in row)
                f.write(f"INSERT INTO {table.name} ({', '.join(cols)}) "
                        f"VALUES ({vals});\n")
    return os.path.getsize(out_path) > 0


# ── 对外 API ──────────────────────────────────────────────────────────────────
def run_backup() -> dict:
    """执行一次备份并清理旧备份。返回 {ok, file, size, url, deleted_old} 或 {ok:False, error}。"""
    if not is_postgres():
        msg = "DATABASE_URL 不是 PostgreSQL，跳过备份（本地 SQLite 无需备份）"
        logger.info(msg)
        return {"ok": False, "error": msg}

    name = f"backup_{_ts()}.sql"
    os.makedirs(STORAGE.temp_dir, exist_ok=True)
    tmp = os.path.join(STORAGE.temp_dir, name)
    try:
        if not _dump_with_pg_dump(tmp) and not _dump_with_sqlalchemy(tmp):
            return {"ok": False, "error": "pg_dump 与逻辑导出均失败，未生成备份"}
        size = os.path.getsize(tmp)
        # save_file 会“消费”tmp（上传后删除本地临时文件）
        url = STORAGE.save_file(tmp, BACKUP_PREFIX + name, "application/sql")
        deleted = cleanup_old_backups()
        logger.info("备份完成 %s（%d bytes）→ %s，清理旧备份 %d 个", name, size, url, deleted)
        return {"ok": True, "file": name, "size": size, "url": url, "deleted_old": deleted}
    except Exception as exc:
        logger.exception("备份失败: %s", exc)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return {"ok": False, "error": str(exc)}


def cleanup_old_backups() -> int:
    """删除超过保留天数的备份；按文件名里的时间戳判断（不依赖存储的 mtime）。返回删除数量。"""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=_retention_days())
    deleted = 0
    for obj in STORAGE.list_objects(BACKUP_PREFIX):
        m = NAME_RE.search(obj["key"])
        if not m:
            continue
        try:
            ts = datetime.datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            continue
        if ts < cutoff:
            STORAGE.delete_key(obj["key"])
            deleted += 1
            logger.info("已删除过期备份 %s", obj["key"])
    return deleted


def list_backups() -> list:
    """列出全部备份（管理后台用），按时间倒序。返回 [{file, key, size, url, created_at}]。"""
    items = []
    for obj in STORAGE.list_objects(BACKUP_PREFIX):
        m = NAME_RE.search(obj["key"])
        if not m:
            continue
        created = None
        try:
            created = datetime.datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
        except ValueError:
            pass
        items.append({
            "file": obj["key"].split("/")[-1],
            "key": obj["key"],
            "size": obj["size"],
            "url": STORAGE.url_for(obj["key"]),
            "created_at": created.strftime("%Y-%m-%d %H:%M:%S") if created else None,
        })
    items.sort(key=lambda x: x["file"], reverse=True)
    return items
