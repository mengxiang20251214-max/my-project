"""
blog-video-pro startup script / 启动脚本
Usage: python start.py
"""
import os
import sys
import shutil
import uvicorn

# Fix Windows console encoding
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 切换到项目根目录（start.py 所在位置）
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

# 自动复制 .env
if not os.path.exists(".env") and os.path.exists(".env.example"):
    shutil.copy(".env.example", ".env")
    print("✓ 已从 .env.example 创建 .env，请按需修改配置")

# 确保上传目录存在
for d in ["static/uploads/videos", "static/uploads/covers", "static/uploads/avatars"]:
    os.makedirs(d, exist_ok=True)

if __name__ == "__main__":
    print("=" * 50)
    print("  VideoHub Pro - 专业视频博客平台")
    print("=" * 50)
    print("  前台地址: http://127.0.0.1:8000")
    print("  管理后台: http://127.0.0.1:8000/admin/dashboard")
    print("  默认账号: admin / admin123")
    print("  API 文档: http://127.0.0.1:8000/docs")
    print("=" * 50)
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=["backend"],
    )
