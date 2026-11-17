import importlib
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PIPELINE_PROVIDER_OVERRIDE", "mock")
    monkeypatch.setenv("NOTIFY_SYNC", "1")
    monkeypatch.setenv("WORKER", "0")            # 测试里手动跑后台任务
    monkeypatch.setenv("UPLOAD_PART_BYTES", "1000")
    import app.db, app.media, app.storage, app.main
    importlib.reload(app.db)
    importlib.reload(app.storage)
    importlib.reload(app.media)
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


def big_upload(c, eid, stage, field, name, data: bytes):
    up = c.post("/api/uploads", json={"episode_id": eid, "stage_key": stage, "field": field, "filename": name, "size": len(data)}).json()
    ps = up["part_size"]
    for n in range(up["parts_total"]):
        t = c.get(f"/api/uploads/{up['id']}/parts/{n}/target").json()
        assert c.put(t["url"], content=data[n * ps:(n + 1) * ps]).status_code == 200
    r = c.post(f"/api/uploads/{up['id']}/complete")
    assert r.status_code == 200, r.text
    return r.json()["file_id"]


def run_jobs():
    import app.main
    while app.media.run_one(app.main.store()):
        pass


def test_full_flow(client):
    c = client
    # 首次进入跳到 setup
    assert c.get("/", follow_redirects=False).headers["location"] == "/setup"
    c.post("/setup", data={"username": "producer", "password": "password1", "display_name": "制作人", "mobile": "100"})
    # 添加三个伙伴
    for u, role, m in [("rec", "recorder", "101"), ("ed", "editor", "102"), ("op", "operator", "103"), ("cw", "copywriter", "104")]:
        c.post("/users", data={"action": "create", "username": u, "password": "password1", "role": role, "mobile": m})
    assert "rec" in c.get("/users").text
    c.post("/settings", data={"section": "notify", "wecom_webhook": "https://example.invalid/hook", "base_url": "https://studio.test"})

    # 录制团队新建一期
    login(c, "rec", "password1")
    r = c.post("/episodes/new", data={"code": "EP01", "title": "测试一期", "assignee_upload": "2", "assignee_rough_cut": "1",
                                      "assignee_fine_cut": "3", "assignee_shownotes": "1", "assignee_copy_review": "5",
                                      "assignee_distribution": "4"},
               follow_redirects=False)
    url = r.headers["location"]
    eid = int(url.rsplit("/", 1)[1])
    assert "录制上传" in c.sent[-1][0] and c.sent[-1][1] == ["101"]

    # 缺必填项不能完成
    r = c.post(f"/episodes/{eid}/stages/upload", data={"action": "complete"}, follow_redirects=True)
    assert "还差" in r.text
    # 上传原片后完成
    big_upload(c, eid, "upload", "source_video", "C0001.MP4", b"x" * 2500)
    c.post(f"/episodes/{eid}/stages/upload", data={"action": "complete"})
    assert "https://studio.test/episodes/" in c.sent[-1][0] and c.sent[-1][1] == ["100"]

    # 精剪伙伴不能抢先操作粗剪
    login(c, "ed", "password1")
    r = c.post(f"/episodes/{eid}/stages/rough_cut", data={"action": "complete"}, follow_redirects=True)
    assert "不是你负责" in r.text
    r = c.post("/api/uploads", json={"episode_id": eid, "stage_key": "rough_cut", "field": "rough_video", "filename": "a.mp4", "size": 10})
    assert r.status_code == 403

    # 制作人完成粗剪
    login(c, "producer", "password1")
    big_upload(c, eid, "rough_cut", "rough_video", "粗剪.mp4", b"r" * 1200)
    c.post(f"/episodes/{eid}/stages/rough_cut", data={"action": "complete"})
    # 精剪
    login(c, "ed", "password1")
    assert "测试一期" in c.get("/").text  # 出现在待我处理
    big_upload(c, eid, "fine_cut", "final_video", "成品.mp4", b"f" * 900)
    c.post(f"/episodes/{eid}/stages/fine_cut", data={"action": "complete",
                                                    "final_transcript": "00:00 欢迎收听\n00:10 今天聊借钱不还"})
    # Show notes：AI 先做
    login(c, "producer", "password1")
    r = c.post(f"/episodes/{eid}/stages/shownotes", data={"action": "ai"}, follow_redirects=True)
    assert "AI 初稿已生成" in r.text and "独立审查" in r.text
    c.post(f"/episodes/{eid}/stages/shownotes", data={"action": "complete", "content": "# 标题\n- 测试"})
    # 文案审核：AI 先做，文案伙伴审核
    login(c, "cw", "password1")
    r = c.post(f"/episodes/{eid}/stages/copy_review", data={"action": "ai"}, follow_redirects=True)
    assert "AI 初稿已生成" in r.text
    c.post(f"/episodes/{eid}/stages/copy_review", data={"action": "complete", "content": "审核后的文案"})
    assert c.sent[-1][1] == ["103"]  # 通知运营
    # 编辑发布：运营
    login(c, "op", "password1")
    assert "审核后的文案" in c.get(f"/episodes/{eid}").text
    c.post(f"/episodes/{eid}/stages/distribution", data={"action": "complete", "published_links": "小宇宙 https://x"})
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


def setup_episode(c):
    c.post("/setup", data={"username": "producer", "password": "password1", "display_name": "制作人"})
    r = c.post("/episodes/new", data={"code": "EP09", "title": "上传测试", "assignee_upload": "1"}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_resumable_upload(client, tmp_path):
    c = client
    eid = setup_episode(c)
    data = bytes(range(256)) * 10  # 2560 字节 → 3 片
    up = c.post("/api/uploads", json={"episode_id": eid, "stage_key": "upload", "field": "source_video", "filename": "a.mp4", "size": len(data)}).json()
    assert up["parts_total"] == 3 and up["received"] == []
    assert c.put(f"/api/uploads/{up['id']}/parts/1", content=data[1000:2000]).status_code == 200
    # 分片大小不对、序号越界都拒绝
    assert c.put(f"/api/uploads/{up['id']}/parts/0", content=b"short").status_code == 400
    assert c.put(f"/api/uploads/{up['id']}/parts/3", content=b"x").status_code == 400
    # 没传完不能合并
    assert c.post(f"/api/uploads/{up['id']}/complete").status_code == 400
    # 页面上提示未传完
    assert "未传完" in c.get(f"/episodes/{eid}").text
    # “关掉页面后重新选择同一个文件”：带 resume_id 初始化，拿到已收到的分片
    again = c.post("/api/uploads", json={"episode_id": eid, "stage_key": "upload", "field": "source_video", "filename": "a.mp4",
                                         "size": len(data), "resume_id": up["id"]}).json()
    assert again["id"] == up["id"] and again["received"] == [1]
    for n in (0, 2):
        c.put(f"/api/uploads/{up['id']}/parts/{n}", content=data[n * 1000:(n + 1) * 1000])
    fid = c.post(f"/api/uploads/{up['id']}/complete").json()["file_id"]
    assert c.get(f"/files/{fid}").content == data
    # 断点下载
    r = c.get(f"/files/{fid}", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and r.content == data[100:200]
    # 未登录不能用接口
    c.post("/logout")
    assert c.post("/api/uploads", json={}).status_code == 401
    assert c.put(f"/api/uploads/{up['id']}/parts/0", content=b"x").status_code == 404


def make_media(path, audio_src=None, seconds=3):
    import subprocess
    a = ["-i", audio_src] if audio_src else ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}:sample_rate=48000"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=25:duration={seconds}",
                    *a, "-shortest", "-c:v", "libx264", "-c:a", "aac", str(path)], check=True)
    return path.read_bytes()


def test_extract_audio_and_retry(client, tmp_path, monkeypatch):
    monkeypatch.delenv("ASR_MODEL_DIR", raising=False)
    c = client
    eid = setup_episode(c)
    video = make_media(tmp_path / "src.mp4")
    big_upload(c, eid, "upload", "source_video", "C0001.MP4", video)
    assert "排队中" in c.get(f"/episodes/{eid}").text
    run_jobs()
    page = c.get(f"/episodes/{eid}").text
    for name in ("C0001.flac", "C0001.wav", "C0001.mp3"):
        assert name in page
    assert "语音转写：未运行" in page and "320×180" in page and "<audio" in page

    # 坏文件：处理失败，管理员可以重试
    big_upload(c, eid, "upload", "source_video", "broken.mp4", b"not a video" * 200)
    run_jobs()
    page = c.get(f"/episodes/{eid}").text
    assert "提取音频：失败" in page
    import app.db
    with app.db.connect() as conn:
        jid = conn.execute("SELECT id FROM jobs WHERE status='failed'").fetchone()["id"]
    c.post(f"/jobs/{jid}/retry")
    assert "提取音频：排队中" in c.get(f"/episodes/{eid}").text


def test_transcribe_with_sensevoice(client, tmp_path, monkeypatch):
    import glob
    d = glob.glob("/tmp/models/sherpa-onnx-sense-voice-*")
    if not d:
        pytest.skip("没有下载 SenseVoice 模型")
    monkeypatch.setenv("ASR_MODEL_DIR", d[0])
    c = client
    eid = setup_episode(c)
    video = make_media(tmp_path / "zh.mp4", audio_src=d[0] + "/test_wavs/zh.wav", seconds=6)
    big_upload(c, eid, "upload", "source_video", "zh.mp4", video)
    run_jobs()
    page = c.get(f"/episodes/{eid}").text
    assert "语音转写：完成" in page and "zh.txt" in page
    import app.db
    with app.db.connect() as conn:
        f = conn.execute("SELECT id FROM files WHERE field='transcript_json'").fetchone()
    data = c.get(f"/files/{f['id']}").json()
    assert "开饭" in "".join(s["text"] for s in data["segments"]) and data["tokens"][0]["t"] > 0


def test_split_points():
    import numpy as np
    from app.media import frame_energy, split_points
    sr = 16000
    x = (np.random.default_rng(0).standard_normal(sr * 60) * 3000).astype(np.int16)
    x[sr * 12: sr * 12 + 1600] = 0  # 12 秒处有一段静音
    cuts = split_points(frame_energy(x))
    assert cuts[0] == 0 and cuts[-1] == 6000
    assert all(800 <= b - a <= 1800 for a, b in zip(cuts, cuts[1:-1]))
    assert abs(cuts[1] - 1200) <= 10


def test_upload_edge_cases(client, monkeypatch):
    import app.db, app.main
    c = client
    eid = setup_episode(c)
    data = b"z" * 1500
    # 传完但没收到回复：带 resume_id 再初始化，直接告诉浏览器已完成，不用重传
    up = c.post("/api/uploads", json={"episode_id": eid, "stage_key": "upload", "field": "source_video", "filename": "a.mp4", "size": 1500}).json()
    for n in range(2):
        c.put(f"/api/uploads/{up['id']}/parts/{n}", content=data[n * 1000:(n + 1) * 1000])
    c.post(f"/api/uploads/{up['id']}/complete")
    again = c.post("/api/uploads", json={"episode_id": eid, "stage_key": "upload", "field": "source_video", "filename": "a.mp4",
                                         "size": 1500, "resume_id": up["id"]}).json()
    assert again["status"] == "done" and again["file_id"]
    # 重复 complete 是安全的
    assert c.post(f"/api/uploads/{up['id']}/complete").json()["file_id"] == again["file_id"]

    # 临时文件丢了：续传改成重新开始
    up2 = c.post("/api/uploads", json={"episode_id": eid, "stage_key": "upload", "field": "source_video", "filename": "b.mp4", "size": 1500}).json()
    c.put(f"/api/uploads/{up2['id']}/parts/0", content=data[:1000])
    app.main.store().abort(_stored(up2["id"]))
    up3 = c.post("/api/uploads", json={"episode_id": eid, "stage_key": "upload", "field": "source_video", "filename": "b.mp4",
                                       "size": 1500, "resume_id": up2["id"]}).json()
    assert up3["id"] != up2["id"] and up3["received"] == []

    # 步骤提交后，没合并的上传不能再挂上去
    c.post(f"/episodes/{eid}/stages/upload", data={"action": "complete"})
    for n in range(2):
        c.put(f"/api/uploads/{up3['id']}/parts/{n}", content=data[n * 1000:(n + 1) * 1000])
    assert c.post(f"/api/uploads/{up3['id']}/complete").status_code == 403

    # 磁盘不够时拒绝开始
    class U: free = 10
    monkeypatch.setattr(app.main.shutil, "disk_usage", lambda p: U)
    c.post(f"/episodes/{eid}/stages/rough_cut", data={"action": "assign"})
    with app.db.connect() as conn:
        conn.execute("UPDATE stages SET assignee_id=1 WHERE key='rough_cut'")
    r = c.post("/api/uploads", json={"episode_id": eid, "stage_key": "rough_cut", "field": "rough_video", "filename": "r.mp4", "size": 100})
    assert r.status_code == 507 and "磁盘" in r.json()["error"]


def _stored(uid):
    import app.db
    with app.db.connect() as conn:
        return conn.execute("SELECT stored_name FROM uploads WHERE id=?", (uid,)).fetchone()["stored_name"]


def test_legacy_v1_fields_still_shown(client):
    import json as _json
    import app.db
    c = client
    eid = setup_episode(c)
    with app.db.connect() as conn:
        conn.execute("UPDATE stages SET data=? WHERE episode_id=? AND key='upload'",
                     (_json.dumps({"netdisk_url": "https://pan.baidu.com/s/old", "netdisk_code": "ab12"}), eid))
        conn.execute("INSERT INTO files(episode_id, stage_key, field, filename, stored_name, size, created_at) VALUES(?, 'upload', 'audio', 'old.mp3', 'old.mp3', 3, 'x')", (eid,))
    page = c.get(f"/episodes/{eid}").text
    assert "旧版本的字段" in page and "https://pan.baidu.com/s/old" in page and "old.mp3" in page and "压缩音频" in page


def test_worker_recovery_and_sweep(client):
    import app.db, app.main, app.media
    c = client
    eid = setup_episode(c)
    with app.db.connect() as conn:
        conn.execute("INSERT INTO jobs(episode_id, kind, file_id, status, attempts, created_at) VALUES(?, 'transcribe', NULL, 'running', 3, 'x')", (eid,))
        conn.execute("INSERT INTO jobs(episode_id, kind, file_id, status, attempts, created_at) VALUES(?, 'transcribe', NULL, 'running', 1, 'x')", (eid,))
        app.media.recover(conn)
        st = [r["status"] for r in conn.execute("SELECT status FROM jobs ORDER BY id")]
    assert st == ["failed", "queued"]
    up = c.post("/api/uploads", json={"episode_id": eid, "stage_key": "upload", "field": "source_video", "filename": "s.mp4", "size": 10}).json()
    with app.db.connect() as conn:
        conn.execute("UPDATE uploads SET updated_at='2000-01-01T00:00:00' WHERE id=?", (up["id"],))
    assert app.media.sweep_uploads(app.main.store()) == 1
    assert not app.main.store().partial_exists(_stored(up["id"]))
