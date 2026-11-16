import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_PROVIDER_OVERRIDE", "mock")
    monkeypatch.setenv("NOTIFY_SYNC", "1")
    import app.db, app.main
    importlib.reload(app.db)
    importlib.reload(app.main)
    sent = []
    monkeypatch.setattr(app.main, "send_wecom", lambda hook, text, mobiles=None: (sent.append((text, mobiles)) or (True, "已发送")))
    with TestClient(app.main.app) as c:
        c.sent = sent
        yield c


def login(c, u, p):
    c.post("/logout")
    r = c.post("/login", data={"username": u, "password": p}, follow_redirects=False)
    assert r.headers["location"] == "/", r.text


def test_full_flow(client):
    c = client
    # 首次进入跳到 setup
    assert c.get("/", follow_redirects=False).headers["location"] == "/setup"
    c.post("/setup", data={"username": "producer", "password": "password1", "display_name": "制作人", "mobile": "100"})
    # 添加三个伙伴
    for u, role, m in [("rec", "recorder", "101"), ("ed", "editor", "102"), ("op", "operator", "103")]:
        c.post("/users", data={"action": "create", "username": u, "password": "password1", "role": role, "mobile": m})
    assert "rec" in c.get("/users").text
    c.post("/settings", data={"section": "notify", "wecom_webhook": "https://example.invalid/hook", "base_url": "https://studio.test"})

    # 录制团队新建一期
    login(c, "rec", "password1")
    r = c.post("/episodes/new", data={"code": "EP01", "title": "测试一期", "assignee_upload": "2", "assignee_rough_cut": "1",
                                      "assignee_fine_cut": "3", "assignee_shownotes": "1", "assignee_distribution": "4"},
               follow_redirects=False)
    url = r.headers["location"]
    eid = int(url.rsplit("/", 1)[1])
    assert "录制上传" in c.sent[-1][0] and c.sent[-1][1] == ["101"]

    # 缺必填项不能完成
    r = c.post(f"/episodes/{eid}/stages/upload", data={"action": "complete"}, follow_redirects=True)
    assert "还差" in r.text
    # 上传音频 + 链接后完成
    c.post(f"/episodes/{eid}/stages/upload", data={"action": "complete", "netdisk_url": "https://pan.baidu.com/s/x", "netdisk_code": "ab12"},
           files={"audio": ("ep01.mp3", b"ID3fake", "audio/mpeg")})
    assert "https://studio.test/episodes/" in c.sent[-1][0] and c.sent[-1][1] == ["100"]

    # 精剪伙伴不能抢先操作粗剪
    login(c, "ed", "password1")
    r = c.post(f"/episodes/{eid}/stages/rough_cut", data={"action": "complete", "netdisk_url": "x"}, follow_redirects=True)
    assert "不是你负责" in r.text

    # 制作人完成粗剪
    login(c, "producer", "password1")
    c.post(f"/episodes/{eid}/stages/rough_cut", data={"action": "complete", "netdisk_url": "https://pan.baidu.com/s/y"})
    # 精剪
    login(c, "ed", "password1")
    assert "测试一期" in c.get("/").text  # 出现在待我处理
    c.post(f"/episodes/{eid}/stages/fine_cut", data={"action": "complete", "netdisk_url": "https://pan.baidu.com/s/z",
                                                    "final_transcript": "00:00 欢迎收听\n00:10 今天聊借钱不还"})
    # Show notes：AI 先做
    login(c, "producer", "password1")
    r = c.post(f"/episodes/{eid}/stages/shownotes", data={"action": "ai"}, follow_redirects=True)
    assert "AI 初稿已生成" in r.text and "独立审查" in r.text
    c.post(f"/episodes/{eid}/stages/shownotes", data={"action": "complete", "content": "# 标题\n- 测试"})
    # 分发
    login(c, "op", "password1")
    c.post(f"/episodes/{eid}/stages/distribution", data={"action": "ai"})
    c.post(f"/episodes/{eid}/stages/distribution", data={"action": "complete", "content": "文案", "published_links": "小宇宙 https://x"})
    page = c.get(f"/episodes/{eid}").text
    assert "全部完成 ·" in page

    # 管理员退回
    login(c, "producer", "password1")
    c.post(f"/episodes/{eid}/stages/fine_cut", data={"action": "reopen", "reason": "有一处吞字"})
    assert "退回" in c.sent[-1][0] and c.sent[-1][1] == ["102"]
    page = c.get(f"/episodes/{eid}").text
    assert "全部完成 ·" not in page

    # 不能退回到还没走到的步骤；负责人乱填不报错
    r = c.post(f"/episodes/{eid}/stages/distribution", data={"action": "reopen"}, follow_redirects=True)
    assert "只能退回到已完成的步骤" in r.text
    r = c.post(f"/episodes/{eid}/stages/upload", data={"action": "assign", "assignee_id": "abc"}, follow_redirects=True)
    assert r.status_code == 200
    r = c.post("/episodes/new", data={"code": "EP02", "title": "x", "assignee_upload": "999"}, follow_redirects=True)
    assert r.status_code == 200

    # 文件下载需要登录
    c.post("/logout")
    assert c.get("/files/1", follow_redirects=False).headers["location"] == "/login"


def test_settings_show_and_models(client):
    c = client
    c.post("/setup", data={"username": "admin", "password": "password1"})
    c.post("/settings", data={"section": "show", "name": "示例历史播客", "domain": "历史", "format": "单人讲述",
                              "tagline": "t", "accuracy_rules": "年份要准\n区分史料和推测", "platforms": "小宇宙\nB站", "disclaimer": ""})
    c.post("/settings", data={"section": "models", "shownotes_executor_provider": "openai", "shownotes_executor_model": "gpt-x"})
    page = c.get("/settings").text
    assert "示例历史播客" in page and "gpt-x" in page


def test_password_hash():
    from app.auth import hash_password, verify_password
    h = hash_password("secret123")
    assert verify_password("secret123", h) and not verify_password("nope", h)
