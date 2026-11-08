"""测试公共配置：在导入后端之前，把数据库指向临时目录里的独立 SQLite 文件。

backend/database.py 会用 load_dotenv 读取 .env，但 load_dotenv 不覆盖已有环境变量，
所以这里先设置的 DATABASE_URL 一定生效，测试永远不会连到 .env 或生产里的数据库。
"""
import atexit
import os
import shutil
import sys
import tempfile

_TMP_DIR = tempfile.mkdtemp(prefix="my-project-tests-")
atexit.register(shutil.rmtree, _TMP_DIR, ignore_errors=True)
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP_DIR, "test.db")
os.environ["BACKUP_ENABLED"] = "false"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
