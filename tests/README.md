# 测试说明

所有测试都使用临时目录里的独立 SQLite 数据库，不读写 `.env` 中配置的数据库、仓库里的 `blog_video.db`
或任何生产资源，也不包含生产凭据。

## 安装

```bash
pip install -r requirements.txt -r requirements-dev.txt
playwright install chromium        # 只跑接口测试可跳过
```

## 接口测试（Logo 跳转网址 logo_link）

```bash
pytest tests/test_logo_link.py -q
```

`tests/conftest.py` 会在导入后端前把 `DATABASE_URL` 指向临时文件，并关闭自动备份。
覆盖：旧数据兼容、公开接口按白名单返回（含原有公告字段）、未登录 401 / 普通用户 403、保存后重新读取、
不带该字段不覆盖旧值、留空清除、非法网址整单拒绝、库内非法值不下发。

## 浏览器端到端测试（后台设置 → 首页 / 视频页）

```bash
FRONTEND_DIR=/path/to/blog-frontend pytest tests/e2e -v
```

- 自动在临时目录建库，并用 uvicorn 子进程在 `127.0.0.1:8011` 启动后端（可用 `E2E_BACKEND_PORT` 修改）；
- 前端在 `127.0.0.1:5500` 提供（该端口在后端 CORS 白名单内），只在内存中把 `js/api.js` 的 `API_BASE` 指向本地后端；
- 浏览器拦截所有外部请求，跳转目标是测试内置的占位页；
- 未安装 playwright、未设置 `FRONTEND_DIR` 或端口被占用时自动跳过。
