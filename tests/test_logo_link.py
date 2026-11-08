"""Logo 跳转网址（logo_link）接口测试。

运行：pip install -r requirements.txt -r requirements-dev.txt && pytest tests/test_logo_link.py
数据库由 tests/conftest.py 指向临时 SQLite，空库首次启动时按原有逻辑写入种子数据。
"""
import pytest
from fastapi.testclient import TestClient

from backend.auth import get_password_hash
from backend.database import SessionLocal, engine
from backend.main import app, normalize_http_url, PUBLIC_SETTING_KEYS
from backend.models import SiteSetting, User

assert "my-project-tests-" in str(engine.url), f"测试必须使用临时数据库，当前为 {engine.url}"

ADMIN = ("t_admin", "t-admin-pass")
USER = ("t_user", "t-user-pass")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:            # 触发 lifespan：建表、迁移、空库种子
        yield c


def _login(client, username, password):
    r = client.post("/api/auth/login", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    db = SessionLocal()
    for (name, pwd), role in ((ADMIN, "admin"), (USER, "user")):
        if not db.query(User).filter(User.username == name).first():
            db.add(User(username=name, email=f"{name}@test.local",
                        password_hash=get_password_hash(pwd), role=role))
    db.commit(); db.close()
    return _login(client, *ADMIN)


def _settings(client, admin):
    return client.get("/api/admin/settings", headers=admin).json()


def _set_raw(key, value):
    """直接改库，模拟旧数据或被篡改的数据。value=None 表示删除该键。"""
    db = SessionLocal()
    row = db.query(SiteSetting).filter(SiteSetting.key == key).first()
    if value is None:
        if row: db.delete(row)
    elif row:
        row.value = value
    else:
        db.add(SiteSetting(key=key, value=value))
    db.commit(); db.close()


# ── 旧数据兼容 / 公开接口字段 ────────────────────────────────────────────────
def test_old_data_without_field_returns_empty(client, admin):
    _set_raw("logo_link", None)
    r = client.get("/api/public/settings")
    assert r.status_code == 200
    assert r.json()["logo_link"] == ""


def test_public_settings_whitelist(client, admin):
    client.post("/api/admin/settings", headers=admin, json={"announcement_text": "公告"})
    _set_raw("internal_only_key", "不应公开")                     # 模拟白名单外的设置项
    pub = client.get("/api/public/settings").json()
    assert set(pub) <= set(PUBLIC_SETTING_KEYS)
    assert pub["announcement_text"] == "公告"                    # 原有公告字段保持兼容
    assert "internal_only_key" not in pub
    assert "internal_only_key" in _settings(client, admin)       # 后台接口不受影响
    _set_raw("internal_only_key", None)


def test_public_settings_keeps_previous_fields():
    """上线前线上公开接口返回的字段，新白名单必须全部包含（兼容旧调用方）。"""
    previous = {"site_name", "site_description", "site_keywords", "footer_text",
                "announcement_enabled", "announcement_text", "announcement_link",
                "announcement_auto_hide", "announcement_color"}
    assert previous <= set(PUBLIC_SETTING_KEYS)


# ── 鉴权 ──────────────────────────────────────────────────────────────────────
def test_anonymous_and_bad_token_rejected(client, admin):
    before = _settings(client, admin).get("logo_link")
    assert client.post("/api/admin/settings", json={"logo_link": "https://a.example.com"}).status_code == 401
    assert client.post("/api/admin/settings", json={"logo_link": "https://a.example.com"},
                       headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert _settings(client, admin).get("logo_link") == before


def test_normal_user_rejected(client, admin):
    user = _login(client, *USER)
    assert client.post("/api/admin/settings", headers=user,
                       json={"logo_link": "https://u.example.com"}).status_code == 403
    assert client.get("/api/admin/settings", headers=user).status_code == 403
    assert _settings(client, admin).get("logo_link") != "https://u.example.com"


# ── 保存 / 重新读取 / 不影响其他设置 ─────────────────────────────────────────
def test_save_reload_and_others_untouched(client, admin):
    client.post("/api/admin/settings", headers=admin, json={
        "site_name": "测试站", "footer_text": "页脚", "announcement_link": "https://ann.example.com"})
    before = _settings(client, admin)
    r = client.post("/api/admin/settings", headers=admin,
                    json={"logo_link": "  https://pkone.example.com/p?a=1&b=2#top  "})
    assert r.status_code == 200, r.text
    after = _settings(client, admin)
    assert after["logo_link"] == "https://pkone.example.com/p?a=1&b=2#top"      # 去掉首尾空白
    assert {k: v for k, v in after.items() if k != "logo_link"} == \
           {k: v for k, v in before.items() if k != "logo_link"}
    assert client.get("/api/public/settings").json()["logo_link"] == after["logo_link"]


def test_omitted_field_keeps_existing_value(client, admin):
    """旧前端或其他调用方不带 logo_link 时，不能覆盖已保存的值。"""
    client.post("/api/admin/settings", headers=admin, json={"logo_link": "https://keep.example.com"})
    assert client.post("/api/admin/settings", headers=admin,
                       json={"site_name": "新站名"}).status_code == 200
    s = _settings(client, admin)
    assert s["logo_link"] == "https://keep.example.com"
    assert s["site_name"] == "新站名"


def test_null_field_keeps_existing_value(client, admin):
    client.post("/api/admin/settings", headers=admin, json={"logo_link": "https://keep2.example.com"})
    assert client.post("/api/admin/settings", headers=admin, json={"logo_link": None}).status_code == 200
    assert _settings(client, admin)["logo_link"] == "https://keep2.example.com"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_clears(client, admin, blank):
    client.post("/api/admin/settings", headers=admin, json={"logo_link": "https://x.example.com"})
    assert client.post("/api/admin/settings", headers=admin, json={"logo_link": blank}).status_code == 200
    assert _settings(client, admin)["logo_link"] == ""
    assert client.get("/api/public/settings").json()["logo_link"] == ""


# ── 非法网址：整个请求不生效 ──────────────────────────────────────────────────
BAD = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)",
       "data:text/html,<script>alert(1)</script>", "vbscript:msgbox(1)", "file:///etc/passwd",
       "ftp://example.com", "//evil.example.com", "example.com", "www.example.com", "/index.html",
       "http://", "https://", "https:///path", "https://user:pw@example.com", "https://evil@example.com",
       "https://exa mple.com", "https://example.com/\nSet-Cookie:x", "https://example.com:99999",
       "https://" + "a" * 2050 + ".com", "http:/example.com", "https:example.com"]


@pytest.mark.parametrize("bad", BAD)
def test_rejects_bad_url_atomically(client, admin, bad):
    client.post("/api/admin/settings", headers=admin, json={"logo_link": "https://safe.example.com"})
    before = _settings(client, admin)
    r = client.post("/api/admin/settings", headers=admin, json={"logo_link": bad, "site_name": "不应写入"})
    assert r.status_code == 400
    assert "http://" in r.json()["detail"]
    assert _settings(client, admin) == before


GOOD = ["http://example.com", "https://example.com", "HTTPS://EXAMPLE.COM/A",
        "https://sub.example.co.id/path?q=1", "https://example.com:8443/x",
        "https://例子.中国/路径", "http://127.0.0.1:8080/"]


@pytest.mark.parametrize("good", GOOD)
def test_accepts_good_url(client, admin, good):
    assert client.post("/api/admin/settings", headers=admin, json={"logo_link": good}).status_code == 200
    assert client.get("/api/public/settings").json()["logo_link"] == good


def test_tampered_value_not_served(client, admin):
    _set_raw("logo_link", "javascript:alert(1)")
    assert client.get("/api/public/settings").json()["logo_link"] == ""


def test_normalize_http_url():
    assert normalize_http_url(None) == ""
    assert normalize_http_url("  ") == ""
    assert normalize_http_url(" https://a.com ") == "https://a.com"
    assert normalize_http_url("javascript:alert(1)") is None
