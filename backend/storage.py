"""
统一存储接口：本地磁盘 / Cloudflare R2，通过环境变量 STORAGE_BACKEND 切换。

设计要点
--------
上传的视频还需要本地文件给 ffmpeg 抽封面，所以接口区分两类写入：

- ``save_file(src, key)``  —— **消费** src：本地存储把临时文件 move 到最终位置，
  R2 把它上传后删掉本地。封面 JPG、Banner 图都走这个。
- ``persist(src, key)``    —— **保留** src 供后续读取（抽封面），返回
  ``(url, local_path)``，调用方用完后调 ``release(local_path)``。视频走这个。

读取已存储对象（补封面场景）用 ``fetch_local(url) -> (path, is_temp)``：
本地直接给路径，R2 下载到临时文件并标记 is_temp=True，用完自行删除。

URL 约定
--------
- 本地：返回根相对路径 ``/static/uploads/<key>``（前端用 mediaUrl 拼 API_BASE）
- R2：  返回绝对地址 ``<R2_PUBLIC_URL>/<key>``（天然跨域可访问）
"""
import logging
import mimetypes
import os
import shutil
import tempfile
from typing import Optional, Tuple

logger = logging.getLogger("videohub.storage")


class BaseStorage:
    """存储后端抽象基类。"""

    temp_dir: str = tempfile.gettempdir()

    def url_for(self, key: str) -> str:
        raise NotImplementedError

    def save_file(self, src_path: str, key: str, content_type: Optional[str] = None) -> str:
        """把本地文件落库（消费 src，调用后 src 不再存在），返回公开 URL。"""
        raise NotImplementedError

    def persist(self, src_path: str, key: str,
                content_type: Optional[str] = None) -> Tuple[str, str]:
        """落库但保留一个本地可读副本，返回 (公开 URL, 本地路径)。配对 release()。"""
        raise NotImplementedError

    def release(self, local_path: Optional[str]) -> None:
        """persist() 返回的本地路径用完后调用。本地存储是 no-op；R2 删临时文件。"""

    def delete(self, url: str) -> None:
        """按公开 URL 删除已存储的对象（不存在时静默）。"""
        raise NotImplementedError

    def fetch_local(self, url: str) -> Optional[Tuple[str, bool]]:
        """拿到某存储对象的本地可读路径，返回 (path, is_temp)；拿不到返回 None。"""
        raise NotImplementedError

    def key_from_url(self, url: str) -> Optional[str]:
        raise NotImplementedError


# ── 本地磁盘 ──────────────────────────────────────────────────────────────────
class LocalStorage(BaseStorage):
    def __init__(self, root: str, base_url: str = "/static/uploads"):
        self.root = os.path.abspath(root)
        self.base_url = base_url.rstrip("/")
        os.makedirs(self.root, exist_ok=True)
        # 临时目录放在同一磁盘下，保证 move 走的是快速的同设备 rename（大视频不复制）
        self.temp_dir = os.path.join(self.root, ".tmp")
        os.makedirs(self.temp_dir, exist_ok=True)

    def _path(self, key: str) -> str:
        return os.path.join(self.root, key.replace("/", os.sep))

    def url_for(self, key: str) -> str:
        return f"{self.base_url}/{key}"

    def key_from_url(self, url: str) -> Optional[str]:
        if not url:
            return None
        prefix = self.base_url + "/"
        if url.startswith(prefix):
            return url[len(prefix):]
        return None

    def _ensure_dir(self, dest: str) -> None:
        os.makedirs(os.path.dirname(dest), exist_ok=True)

    def save_file(self, src_path, key, content_type=None) -> str:
        dest = self._path(key)
        self._ensure_dir(dest)
        if os.path.abspath(src_path) != os.path.abspath(dest):
            shutil.move(src_path, dest)
        return self.url_for(key)

    def persist(self, src_path, key, content_type=None) -> Tuple[str, str]:
        dest = self._path(key)
        self._ensure_dir(dest)
        if os.path.abspath(src_path) != os.path.abspath(dest):
            shutil.move(src_path, dest)
        return self.url_for(key), dest

    def release(self, local_path) -> None:
        # persist 返回的是最终文件，保留不删
        return

    def fetch_local(self, url) -> Optional[Tuple[str, bool]]:
        key = self.key_from_url(url)
        if not key:
            return None
        p = self._path(key)
        return (p, False) if os.path.isfile(p) else None

    def delete(self, url) -> None:
        key = self.key_from_url(url)
        if not key:
            return
        p = self._path(key)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError as exc:
                logger.warning("本地删除失败 %s: %s", p, exc)


# ── Cloudflare R2（S3 兼容）───────────────────────────────────────────────────
class R2Storage(BaseStorage):
    def __init__(self, bucket: str, endpoint: str, access_key: str,
                 secret_key: str, public_url: str, region: str = "auto"):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        self.public_url = public_url.rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )
        self.temp_dir = tempfile.gettempdir()

    def url_for(self, key: str) -> str:
        return f"{self.public_url}/{key}"

    def key_from_url(self, url: str) -> Optional[str]:
        if url and url.startswith(self.public_url + "/"):
            return url[len(self.public_url) + 1:]
        return None

    def _upload(self, src_path: str, key: str, content_type: Optional[str]) -> None:
        ct = content_type or mimetypes.guess_type(src_path)[0]
        extra = {"ContentType": ct} if ct else {}
        self.client.upload_file(src_path, self.bucket, key,
                                ExtraArgs=extra or None)
        logger.info("R2 上传完成 %s", key)

    def save_file(self, src_path, key, content_type=None) -> str:
        try:
            self._upload(src_path, key, content_type)
        finally:
            if os.path.isfile(src_path):
                try:
                    os.remove(src_path)
                except OSError:
                    pass
        return self.url_for(key)

    def persist(self, src_path, key, content_type=None) -> Tuple[str, str]:
        # 上传后保留本地临时文件给调用方（抽封面），release() 时再删
        self._upload(src_path, key, content_type)
        return self.url_for(key), src_path

    def release(self, local_path) -> None:
        if local_path and os.path.isfile(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass

    def fetch_local(self, url) -> Optional[Tuple[str, bool]]:
        key = self.key_from_url(url)
        if not key:
            return None
        fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(key)[1], dir=self.temp_dir)
        os.close(fd)
        try:
            self.client.download_file(self.bucket, key, tmp)
            return (tmp, True)
        except Exception as exc:
            logger.warning("R2 下载失败 %s: %s", key, exc)
            if os.path.isfile(tmp):
                os.remove(tmp)
            return None

    def delete(self, url) -> None:
        key = self.key_from_url(url)
        if not key:
            return
        try:
            self.client.delete_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            logger.warning("R2 删除失败 %s: %s", key, exc)


# ── 工厂：按 STORAGE_BACKEND 构建单例 ─────────────────────────────────────────
def _build_storage() -> BaseStorage:
    backend = os.getenv("STORAGE_BACKEND", "local").strip().lower()
    if backend == "r2":
        required = ("R2_BUCKET", "R2_ENDPOINT", "R2_PUBLIC_URL",
                    "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            logger.error("STORAGE_BACKEND=r2 但缺少配置 %s，回退到本地存储", missing)
        else:
            try:
                s = R2Storage(
                    bucket=os.getenv("R2_BUCKET"),
                    endpoint=os.getenv("R2_ENDPOINT"),
                    access_key=os.getenv("R2_ACCESS_KEY_ID"),
                    secret_key=os.getenv("R2_SECRET_ACCESS_KEY"),
                    public_url=os.getenv("R2_PUBLIC_URL"),
                    region=os.getenv("R2_REGION", "auto"),
                )
                logger.info("存储后端 = Cloudflare R2（bucket=%s）", os.getenv("R2_BUCKET"))
                return s
            except Exception as exc:
                logger.exception("R2 初始化失败，回退本地存储: %s", exc)

    root = os.getenv("UPLOAD_DIR")
    if not root or root == "../static/uploads":
        root = os.path.join(os.path.dirname(__file__), "..", "static", "uploads")
    s = LocalStorage(root)
    logger.info("存储后端 = 本地磁盘（%s）", s.root)
    return s


# 进程级单例，import 即用
STORAGE: BaseStorage = _build_storage()
