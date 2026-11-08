"""Logo 跳转网址：浏览器端到端测试（后台设置 → 首页 / 视频页跳转）。

需要：requirements-dev.txt 里的 playwright（并执行 `playwright install chromium`），
以及环境变量 FRONTEND_DIR 指向 blog-frontend 的本地检出目录。缺任一项自动跳过。

运行：FRONTEND_DIR=/path/to/blog-frontend pytest tests/e2e -v

隔离方式：
- 后端用 uvicorn 子进程启动，DATABASE_URL 指向临时目录里的新 SQLite（空库自动写入种子数据）；
- 前端用本测试内置的静态服务提供 FRONTEND_DIR，只在内存中把 js/api.js 的 API_BASE 改成本地后端；
- 浏览器拦截所有外部请求，跳转目标是测试内置的占位页，不访问任何外部网站或生产后端。
"""
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

FRONTEND_DIR = os.environ.get("FRONTEND_DIR", "")
sync_api = pytest.importorskip("playwright.sync_api", reason="未安装 playwright")
if not os.path.isfile(os.path.join(FRONTEND_DIR, "js", "api.js")):
    pytest.skip("未设置 FRONTEND_DIR（blog-frontend 检出目录）", allow_module_level=True)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FE_PORT = 5500                      # 后端 CORS 白名单里的本地开发端口
BE_PORT = int(os.environ.get("E2E_BACKEND_PORT", "8011"))
FE = f"http://127.0.0.1:{FE_PORT}"
BE = f"http://127.0.0.1:{BE_PORT}"
TARGET = "https://pkone-target.example.com/landing?from=logo"
ADMIN = ("e2e_admin", "e2e-admin-pass")
API_BASE_RE = re.compile(r"export const API_BASE = '[^']*';")


def _wait(url, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2); return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"服务未就绪: {url}")


def _port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture(scope="module")
def stack():
    if not (_port_free(FE_PORT) and _port_free(BE_PORT)):
        pytest.skip(f"端口 {FE_PORT} 或 {BE_PORT} 已被占用")
    tmp = tempfile.mkdtemp(prefix="logo-link-e2e-")
    db_path = os.path.join(tmp, "e2e.db")
    env = {**os.environ, "DATABASE_URL": "sqlite:///" + db_path, "BACKUP_ENABLED": "false"}
    be = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
                           "--port", str(BE_PORT)], cwd=ROOT, env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            if self.path.split("?")[0] == "/js/api.js":
                src = open(os.path.join(FRONTEND_DIR, "js", "api.js"), encoding="utf-8").read()
                data, n = API_BASE_RE.subn(f"export const API_BASE = '{BE}';", src)
                assert n == 1, "api.js 中未找到 API_BASE"
                body = data.encode()
                self.send_response(200); self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                return
            return super().do_GET()

    fe = ThreadingHTTPServer(("127.0.0.1", FE_PORT), partial(Handler, directory=FRONTEND_DIR))
    threading.Thread(target=fe.serve_forever, daemon=True).start()
    try:
        _wait(BE + "/health"); _wait(FE + "/index.html")
        # 测试专用管理员：直接写入临时库（不依赖种子账号）
        sys.path.insert(0, ROOT)
        from backend.auth import get_password_hash
        con = sqlite3.connect(db_path)
        con.execute("INSERT INTO users (username, email, password_hash, role, is_active, created_at) "
                    "VALUES (?, ?, ?, 'admin', 1, datetime('now'))",
                    (ADMIN[0], f"{ADMIN[0]}@test.local", get_password_hash(ADMIN[1])))
        con.commit(); con.close()
        yield db_path
    finally:
        fe.shutdown(); be.terminate(); be.wait(timeout=10)
        shutil.rmtree(tmp, ignore_errors=True)


def _api(path, token=None, body=None):
    req = urllib.request.Request(BE + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


@pytest.fixture(scope="module")
def browser(stack):
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _ctx(browser, width=1440, height=900, dsf=1):
    ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=dsf, locale="zh-CN")

    def handle(route):
        u = route.request.url
        if u.startswith((FE, BE)):
            return route.continue_()
        if u.startswith("https://pkone-target.example.com/"):
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<title>target</title>")
        return route.abort()
    ctx.route(re.compile(".*"), handle)
    return ctx


def _admin_settings_page(ctx):
    pg = ctx.new_page()
    pg.goto(FE + "/admin.html")
    pg.fill("#loginUser", ADMIN[0]); pg.fill("#loginPass", ADMIN[1]); pg.click("#loginBtn")
    pg.wait_for_selector('.side-link[data-panel="settings"]', state="visible")
    pg.click('.side-link[data-panel="settings"]')
    pg.wait_for_selector("#setLogoLink", state="visible"); pg.wait_for_load_state("networkidle")
    return pg, pg.evaluate("localStorage.getItem('vh_token')")


def _save(pg):
    pg.click('button[onclick="saveSettings()"]')


def _save_and_wait(pg, expect_current):
    """点保存并等到 POST 返回、页面重新读取出预期值（不依赖可能仍在显示的旧横幅）。"""
    with pg.expect_response(lambda r: "/api/admin/settings" in r.url and r.request.method == "POST"):
        _save(pg)
    pg.wait_for_function("t => document.getElementById('setLogoLinkCurrent').textContent === t", arg=expect_current)


def _logo(pg):
    return pg.evaluate("""() => { const a = document.querySelector('.navbar-brand');
        const r = a.querySelector('img').getBoundingClientRect();
        return { href: a.getAttribute('href'), target: a.getAttribute('target'), h: Math.round(r.height),
                 hscroll: document.documentElement.scrollWidth > innerWidth }; }""")


def _open(ctx, path):
    pg = ctx.new_page(); pg.goto(FE + path); pg.wait_for_load_state("networkidle"); pg.wait_for_timeout(300)
    return pg


def _click_logo(ctx, pg):
    pages = len(ctx.pages)
    with pg.expect_navigation():
        pg.click(".navbar-brand")
    return pg.url, len(ctx.pages) == pages


PAGES = ["/index.html", "/video.html?id=1"]


def test_01_old_data_goes_home(browser):
    ctx = _ctx(browser)
    for path in PAGES:
        pg = _open(ctx, path)
        assert _logo(pg)["href"] == "index.html"
        url, same_tab = _click_logo(ctx, pg)
        assert url.endswith("/index.html") and same_tab
    ctx.close()


def test_02_admin_rejects_invalid_without_request(browser):
    ctx = _ctx(browser)
    pg, token = _admin_settings_page(ctx)
    assert pg.input_value("#setLogoLink") == ""
    assert pg.inner_text("#setLogoLinkCurrent") == "返回首页"
    posts = []
    pg.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "/api/admin/settings" in r.url else None)
    for bad in ["javascript:alert(1)", "data:text/html,x", "www.example.com", "//evil.example.com"]:
        pg.fill("#setLogoLink", bad); _save(pg); pg.wait_for_timeout(200)
        assert pg.is_visible("#setLogoLinkErr"), bad
    assert not posts
    assert _api("/api/admin/settings", token).get("logo_link") in (None, "")
    ctx.close()


def test_03_save_reload_and_others_unchanged(browser):
    ctx = _ctx(browser)
    pg, token = _admin_settings_page(ctx)
    _save_and_wait(pg, "返回首页")                                            # 先整表保存一次作为基准
    before = _api("/api/admin/settings", token)
    pg.fill("#setLogoLink", "  " + TARGET + "  ")
    _save_and_wait(pg, TARGET)
    assert pg.is_visible("#settingsSaved")
    toasts = pg.evaluate("[...document.querySelectorAll('.toast')].map(t => t.textContent).join('|')")
    assert "设置已保存" in toasts
    after = _api("/api/admin/settings", token)
    assert after["logo_link"] == TARGET
    assert {k: v for k, v in after.items() if k != "logo_link"} == \
           {k: v for k, v in before.items() if k != "logo_link"}
    pg.reload(); pg.wait_for_selector('.side-link[data-panel="settings"]', state="visible")
    pg.click('.side-link[data-panel="settings"]')
    pg.wait_for_function("t => document.getElementById('setLogoLink').value === t", arg=TARGET)
    assert pg.inner_text("#setLogoLinkCurrent") == TARGET
    ctx.close()


@pytest.mark.parametrize("width,dsf,logo_h", [(1440, 1, 34), (390, 2, 26), (320, 2, 21)])
def test_04_pages_follow_link_in_same_tab(browser, width, dsf, logo_h):
    ctx = _ctx(browser, width, 800, dsf)
    for path in PAGES:
        pg = _open(ctx, path)
        st = _logo(pg)
        assert st["href"] == TARGET and st["target"] is None
        assert st["h"] == logo_h and not st["hscroll"]
        url, same_tab = _click_logo(ctx, pg)
        assert url == TARGET and same_tab
    ctx.close()


def test_05_settings_load_failure_goes_home(browser):
    ctx = _ctx(browser)
    ctx.route(BE + "/api/public/settings", lambda r: r.abort())
    for path in PAGES:
        pg = _open(ctx, path)
        assert _logo(pg)["href"] == "index.html"
    ctx.close()


def test_06_admin_load_failure_blocks_save(browser):
    ctx = _ctx(browser)
    pg, token = _admin_settings_page(ctx)
    before = _api("/api/admin/settings", token)
    posts = []
    pg.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "/api/admin/settings" in r.url else None)
    pg.route(BE + "/api/admin/settings", lambda r: r.abort() if r.request.method == "GET" else r.continue_())
    pg.click('.side-link[data-panel="dashboard"]'); pg.click('.side-link[data-panel="settings"]'); pg.wait_for_timeout(600)
    _save(pg); pg.wait_for_timeout(300)
    assert not posts
    assert _api("/api/admin/settings", token) == before
    ctx.close()


def test_07_server_reject_shows_error_not_success(browser):
    ctx = _ctx(browser)
    pg, token = _admin_settings_page(ctx)
    pg.route(BE + "/api/admin/settings", lambda r: r.fulfill(
        status=400, content_type="application/json",
        body=json.dumps({"detail": "Logo 跳转网址必须是完整的 http:// 或 https:// 地址，例如 https://example.com"}))
        if r.request.method == "POST" else r.continue_())
    pg.fill("#setLogoLink", "https://ok.example.com"); _save(pg); pg.wait_for_timeout(500)
    assert pg.is_visible("#setLogoLinkErr")
    assert not pg.is_visible("#settingsSaved")
    toasts = pg.evaluate("[...document.querySelectorAll('.toast')].map(t => t.textContent).join('|')")
    assert "设置已保存" not in toasts
    ctx.close()


def test_08_clear_goes_home(browser):
    ctx = _ctx(browser, 390, 900, 2)
    pg, token = _admin_settings_page(ctx)
    pg.fill("#setLogoLink", ""); _save_and_wait(pg, "返回首页")
    assert _api("/api/admin/settings", token)["logo_link"] == ""
    assert pg.inner_text("#setLogoLinkCurrent") == "返回首页"
    for path in PAGES:
        p2 = _open(ctx, path)
        url, same_tab = _click_logo(ctx, p2)
        assert url.endswith("/index.html") and same_tab
    ctx.close()
