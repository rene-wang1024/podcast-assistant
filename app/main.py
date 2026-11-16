"""播客协作工作台（V1）。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from pipeline.config import ModelRef, load_config, load_show
from pipeline.core import generate_text

from . import db
from .auth import ROLES, hash_password, validate_new_password, verify_password
from .flow import STAGE_BY_KEY, STAGES, STATUS_LABEL, next_stage
from .notify import send_wecom

BASE = Path(__file__).resolve().parent
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "500"))
SECRET_KEY = os.environ.get("SECRET_KEY") or ""
if not SECRET_KEY:
    logging.getLogger("uvicorn.error").warning("未设置 SECRET_KEY：每次重启后所有人都要重新登录。请在 .env 里设置。")
    SECRET_KEY = secrets.token_hex(32)

@asynccontextmanager
async def lifespan(_app):
    db.init()
    yield


app = FastAPI(title="播客协作工作台", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, same_site="lax", max_age=60 * 60 * 24 * 14,
                   https_only=os.environ.get("COOKIE_SECURE", "0") == "1")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals.update(ROLES=ROLES, STATUS_LABEL=STATUS_LABEL, STAGES=STAGES)


# ---------- 通用 ----------

class Redirect(Exception):
    def __init__(self, url: str):
        self.url = url


@app.exception_handler(Redirect)
def _redirect(_req, exc: Redirect):
    return RedirectResponse(exc.url, status_code=303)


def flash(request: Request, msg: str, kind: str = "ok"):
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, user=None, **ctx):
    msgs = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {"user": user, "flashes": msgs, **ctx})


def current_user(request: Request, conn, *, admin: bool = False):
    uid = request.session.get("uid")
    user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone() if uid else None
    if not user:
        if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise Redirect("/setup")
        raise Redirect("/login")
    if admin and not user["is_admin"]:
        flash(request, "只有管理员可以进入这个页面", "error")
        raise Redirect("/")
    return user


def show_profile(conn) -> dict:
    return db.get_setting(conn, "show") or load_show()


def pipeline_config(conn, *, apply_env_override: bool = True):
    saved_env = os.environ.pop("PIPELINE_PROVIDER_OVERRIDE", None)  # load_config 也会读它，这里先拿原始配置
    try:
        cfg = load_config()
    finally:
        if saved_env is not None:
            os.environ["PIPELINE_PROVIDER_OVERRIDE"] = saved_env
    overrides = db.get_setting(conn, "models", {})
    for wf in cfg.workflows:
        o = overrides.get(wf.id, {})
        for role in ("executor", "reviewer"):
            if role in o and o[role].get("provider"):
                cur: ModelRef = getattr(wf, role)
                setattr(wf, role, ModelRef(provider=o[role]["provider"], model=o[role].get("model") or "default", kind=cur.kind))
    if apply_env_override and os.environ.get("PIPELINE_PROVIDER_OVERRIDE"):
        for wf in cfg.workflows:
            wf.executor.provider = wf.reviewer.provider = os.environ["PIPELINE_PROVIDER_OVERRIDE"]
    return cfg


def users_by_role(conn):
    out = {r: [] for r in ROLES}
    for u in conn.execute("SELECT * FROM users WHERE active=1 ORDER BY id"):
        out.setdefault(u["role"], []).append(u)
    return out


def valid_user_id(conn, raw) -> int | None:
    try:
        uid = int(raw)
    except (TypeError, ValueError):
        return None
    return uid if conn.execute("SELECT 1 FROM users WHERE id=? AND active=1", (uid,)).fetchone() else None


def episode_or_404(conn, eid: int):
    ep = conn.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
    if not ep:
        raise Redirect("/episodes")
    return ep


def stage_row(conn, eid: int, key: str):
    return conn.execute("SELECT * FROM stages WHERE episode_id=? AND key=?", (eid, key)).fetchone()


def can_edit(user, stage_row_) -> bool:
    """只有「待处理」的步骤能编辑：负责人或管理员。已完成的步骤要改，由管理员退回。"""
    return stage_row_["status"] == "todo" and (bool(user["is_admin"]) or stage_row_["assignee_id"] == user["id"])


def _send_async(conn, webhook: str, text: str, mobiles: list[str], episode_id: int | None, label: str) -> None:
    """企业微信通知放到后台线程发，不阻塞请求；结果记进动态。NOTIFY_SYNC=1 时同步发送（测试用）。"""
    if os.environ.get("NOTIFY_SYNC") == "1":
        ok, msg = send_wecom(webhook, text, mobiles)
        db.log(conn, episode_id, None, "通知", f"{label}：{msg}")
        return

    def work():
        ok, msg = send_wecom(webhook, text, mobiles)
        with db.connect() as c:
            db.log(c, episode_id, None, "通知", f"{label}：{msg}")
    threading.Thread(target=work, daemon=True).start()


def notify_assignee(conn, ep, stage_key: str, prefix: str = ""):
    st = stage_row(conn, ep["id"], stage_key)
    stage = STAGE_BY_KEY[stage_key]
    assignee = conn.execute("SELECT * FROM users WHERE id=?", (st["assignee_id"],)).fetchone() if st["assignee_id"] else None
    base_url = (db.get_setting(conn, "base_url") or "").rstrip("/")
    link = f"{base_url}/episodes/{ep['id']}" if base_url else f"/episodes/{ep['id']}"
    who = assignee["display_name"] if assignee else "（未指定负责人）"
    text = f"{prefix}【{ep['code']} {ep['title']}】到「{stage.name}」了，请 {who} 处理。\n{link}"
    _send_async(conn, db.get_setting(conn, "wecom_webhook") or "", text, [assignee["mobile"]] if assignee else [],
                ep["id"], stage.name)


# ---------- 首次设置 / 登录 ----------

@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request):
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise Redirect("/login")
    return render(request, "setup.html")


@app.post("/setup")
async def setup_submit(request: Request):
    form = await request.form()
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise Redirect("/login")
        err = validate_new_password(form.get("password", ""))
        if err or not form.get("username"):
            flash(request, err or "请填写用户名", "error")
            raise Redirect("/setup")
        cur = conn.execute(
            "INSERT INTO users(username, display_name, password_hash, role, is_admin, mobile, created_at) VALUES(?,?,?,?,1,?,?)",
            (form["username"].strip(), form.get("display_name") or form["username"], hash_password(form["password"]),
             "producer", form.get("mobile", ""), db.now()))
        request.session["uid"] = cur.lastrowid
    flash(request, "管理员账号已创建。下一步：在「成员」里添加伙伴，在「设置」里填节目信息和企业微信 Webhook。")
    raise Redirect("/settings")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    with db.connect() as conn:
        u = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (form.get("username", "").strip(),)).fetchone()
        if not u or not verify_password(form.get("password", ""), u["password_hash"]):
            flash(request, "用户名或密码不对", "error")
            raise Redirect("/login")
        request.session.clear()
        request.session["uid"] = u["id"]
    raise Redirect("/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    raise Redirect("/login")


# ---------- 待我处理 / 节目列表 ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    with db.connect() as conn:
        user = current_user(request, conn)
        todo = conn.execute("""
            SELECT s.key, s.started_at, e.* FROM stages s JOIN episodes e ON e.id = s.episode_id
            WHERE s.assignee_id=? AND s.status='todo' ORDER BY s.started_at""", (user["id"],)).fetchall()
        active = conn.execute("SELECT * FROM episodes WHERE finished_at IS NULL ORDER BY id DESC").fetchall()
        assignees = {r["episode_id"]: r["display_name"] for r in conn.execute(
            "SELECT s.episode_id, u.display_name FROM stages s JOIN episodes e ON e.id=s.episode_id "
            "LEFT JOIN users u ON u.id=s.assignee_id WHERE s.key=e.current_stage")}
    return render(request, "home.html", user, todo=todo, active=active, assignees=assignees, STAGE_BY_KEY=STAGE_BY_KEY)


@app.get("/episodes", response_class=HTMLResponse)
def episodes(request: Request):
    with db.connect() as conn:
        user = current_user(request, conn)
        rows = conn.execute("SELECT * FROM episodes ORDER BY id DESC").fetchall()
    return render(request, "episodes.html", user, episodes=rows, STAGE_BY_KEY=STAGE_BY_KEY)


@app.get("/episodes/new", response_class=HTMLResponse)
def episode_new(request: Request):
    with db.connect() as conn:
        user = current_user(request, conn)
        if not (user["is_admin"] or user["role"] == "recorder"):
            flash(request, "只有录制团队和管理员可以新建节目", "error")
            raise Redirect("/")
        return render(request, "episode_new.html", user, by_role=users_by_role(conn))


@app.post("/episodes/new")
async def episode_create(request: Request):
    form = await request.form()
    with db.connect() as conn:
        user = current_user(request, conn)
        if not (user["is_admin"] or user["role"] == "recorder"):
            raise Redirect("/")
        code, title = form.get("code", "").strip(), form.get("title", "").strip()
        if not code or not title:
            flash(request, "期号和标题都要填", "error")
            raise Redirect("/episodes/new")
        cur = conn.execute("INSERT INTO episodes(code, title, current_stage, created_by, created_at) VALUES(?,?,?,?,?)",
                           (code, title, STAGES[0].key, user["id"], db.now()))
        eid = cur.lastrowid
        for i, st in enumerate(STAGES):
            aid = valid_user_id(conn, form.get(f"assignee_{st.key}"))
            conn.execute("INSERT INTO stages(episode_id, key, status, assignee_id, started_at) VALUES(?,?,?,?,?)",
                         (eid, st.key, "todo" if i == 0 else "waiting", aid, db.now() if i == 0 else None))
        db.log(conn, eid, user["id"], "新建节目", f"{code} {title}")
        ep = episode_or_404(conn, eid)
        notify_assignee(conn, ep, STAGES[0].key, prefix="新的一期：")
    raise Redirect(f"/episodes/{eid}")


@app.get("/episodes/{eid}", response_class=HTMLResponse)
def episode_detail(request: Request, eid: int):
    with db.connect() as conn:
        user = current_user(request, conn)
        ep = episode_or_404(conn, eid)
        for st in STAGES:  # flow.py 新增步骤后，旧节目自动补上
            conn.execute("INSERT OR IGNORE INTO stages(episode_id, key, status) VALUES(?,?,'waiting')", (eid, st.key))
        rows = {r["key"]: r for r in conn.execute("SELECT * FROM stages WHERE episode_id=?", (eid,))}
        users = {u["id"]: u for u in conn.execute("SELECT * FROM users")}
        files: dict = {}
        for f in conn.execute("SELECT * FROM files WHERE episode_id=? ORDER BY id", (eid,)):
            files.setdefault((f["stage_key"], f["field"]), []).append(f)
        events = conn.execute("SELECT ev.*, u.display_name FROM events ev LEFT JOIN users u ON u.id=ev.user_id "
                              "WHERE episode_id=? ORDER BY ev.id DESC LIMIT 50", (eid,)).fetchall()
        cfg = pipeline_config(conn)
        stages = []
        for st in STAGES:
            r = rows[st.key]
            wf = cfg.get(st.ai) if st.ai else None
            stages.append({
                "def": st, "row": r, "data": json.loads(r["data"] or "{}"),
                "review": json.loads(r["ai_review"]) if r["ai_review"] else None,
                "meta": json.loads(r["ai_meta"]) if r["ai_meta"] else None,
                "assignee": users.get(r["assignee_id"]), "editable": can_edit(user, r),
                "models": (wf.executor.label(), wf.reviewer.label()) if wf else None,
            })
        return render(request, "episode.html", user, ep=ep, stages=stages, files=files, events=events,
                      by_role=users_by_role(conn), max_mb=MAX_UPLOAD_MB)


async def _save_stage(request: Request, conn, user, ep, key: str, form) -> dict:
    st = STAGE_BY_KEY[key]
    row = stage_row(conn, ep["id"], key)
    data = json.loads(row["data"] or "{}")
    for f in st.fields:
        if f.type == "file":
            for up in form.getlist(f.name):
                if not getattr(up, "filename", ""):
                    continue
                stored = f"{uuid.uuid4().hex}{Path(up.filename).suffix.lower()[:10]}"
                dest = db.UPLOAD_DIR / stored
                size = 0
                with dest.open("wb") as out:
                    while chunk := await up.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_UPLOAD_MB * 1024 * 1024:
                            out.close()
                            dest.unlink(missing_ok=True)
                            flash(request, f"{up.filename} 超过 {MAX_UPLOAD_MB} MB，未保存", "error")
                            break
                        out.write(chunk)
                    else:
                        conn.execute("INSERT INTO files(episode_id, stage_key, field, filename, stored_name, size, uploaded_by, created_at) "
                                     "VALUES(?,?,?,?,?,?,?,?)", (ep["id"], key, f.name, up.filename, stored, size, user["id"], db.now()))
                        db.log(conn, ep["id"], user["id"], "上传文件", f"{st.name} · {f.label}：{up.filename}")
        elif f.name in form:
            data[f.name] = str(form.get(f.name, "")).strip()
    conn.execute("UPDATE stages SET data=? WHERE id=?", (json.dumps(data, ensure_ascii=False), row["id"]))
    return data


def _missing(conn, ep, key: str, data: dict) -> list[str]:
    missing = []
    for f in STAGE_BY_KEY[key].fields:
        if not f.required:
            continue
        if f.type == "file":
            if not conn.execute("SELECT 1 FROM files WHERE episode_id=? AND stage_key=? AND field=?", (ep["id"], key, f.name)).fetchone():
                missing.append(f.label)
        elif not data.get(f.name):
            missing.append(f.label)
    return missing


@app.post("/episodes/{eid}/stages/{key}")
async def stage_action(request: Request, eid: int, key: str):
    if key not in STAGE_BY_KEY:
        raise Redirect(f"/episodes/{eid}")
    form = await request.form()
    action = form.get("action", "save")
    with db.connect() as conn:
        user = current_user(request, conn)
        ep = episode_or_404(conn, eid)
        row = stage_row(conn, eid, key)
        st = STAGE_BY_KEY[key]

        if action in ("save", "complete"):
            if not can_edit(user, row):
                flash(request, "这一步不是你负责，或者还没轮到", "error")
                raise Redirect(f"/episodes/{eid}")
            data = await _save_stage(request, conn, user, ep, key, form)
            if action == "save":
                flash(request, "已保存")
            else:
                missing = _missing(conn, ep, key, data)
                if missing:
                    flash(request, "已保存，但还差：" + "、".join(missing), "error")
                    conn.commit()
                    raise Redirect(f"/episodes/{eid}#stage-{key}")
                conn.execute("UPDATE stages SET status='done', completed_at=?, completed_by=? WHERE id=?",
                             (db.now(), user["id"], row["id"]))
                db.log(conn, eid, user["id"], "完成", st.name)
                nxt = next_stage(key)
                if nxt:
                    conn.execute("UPDATE stages SET status='todo', started_at=? WHERE episode_id=? AND key=?", (db.now(), eid, nxt.key))
                    conn.execute("UPDATE episodes SET current_stage=? WHERE id=?", (nxt.key, eid))
                    notify_assignee(conn, ep, nxt.key)
                    flash(request, f"已完成，交给「{nxt.name}」")
                else:
                    conn.execute("UPDATE episodes SET current_stage='done', finished_at=? WHERE id=?", (db.now(), eid))
                    _send_async(conn, db.get_setting(conn, "wecom_webhook") or "", f"【{ep['code']} {ep['title']}】全部完成 🎉", [], eid, "全部完成")
                    flash(request, "这一期全部完成了")

        elif action == "ai":
            if not can_edit(user, row) or not st.ai:
                raise Redirect(f"/episodes/{eid}")
            src_key, src_field = st.ai_input
            src = json.loads(stage_row(conn, eid, src_key)["data"] or "{}").get(src_field, "")
            if not src:
                flash(request, f"AI 需要「{STAGE_BY_KEY[src_key].name}」里的内容，现在是空的", "error")
                raise Redirect(f"/episodes/{eid}#stage-{key}")
            wf = pipeline_config(conn).get(st.ai)
            try:
                output, review = await run_in_threadpool(generate_text, wf, show_profile(conn), src)
            except Exception as e:
                flash(request, f"AI 生成失败：{e}", "error")
                db.log(conn, eid, user["id"], "AI 生成失败", f"{st.name}：{e}")
                conn.commit()
                raise Redirect(f"/episodes/{eid}#stage-{key}")
            data = json.loads(row["data"] or "{}")
            data[st.ai_target] = output.strip()
            meta = {"executor": wf.executor.label(), "reviewer": wf.reviewer.label(), "time": db.now()}
            conn.execute("UPDATE stages SET data=?, ai_output=?, ai_review=?, ai_meta=? WHERE id=?",
                         (json.dumps(data, ensure_ascii=False), output, json.dumps(review, ensure_ascii=False),
                          json.dumps(meta, ensure_ascii=False), row["id"]))
            db.log(conn, eid, user["id"], "AI 生成", f"{st.name}（{meta['executor']}，审查 {review.get('verdict')}）")
            flash(request, "AI 初稿已生成，请审核修改后再点完成")

        elif action == "assign" and user["is_admin"]:
            aid = valid_user_id(conn, form.get("assignee_id"))
            conn.execute("UPDATE stages SET assignee_id=? WHERE id=?", (aid, row["id"]))
            db.log(conn, eid, user["id"], "改负责人", st.name)
            if row["status"] == "todo":
                notify_assignee(conn, ep, key)
            flash(request, "负责人已更新")

        elif action == "reopen" and user["is_admin"] and row["status"] != "done":
            flash(request, "只能退回到已完成的步骤", "error")

        elif action == "reopen" and user["is_admin"]:
            keys = [s.key for s in STAGES]
            later = keys[keys.index(key) + 1:]
            conn.execute("UPDATE stages SET status='todo', completed_at=NULL, completed_by=NULL, started_at=? WHERE id=?", (db.now(), row["id"]))
            for k in later:
                conn.execute("UPDATE stages SET status='waiting' WHERE episode_id=? AND key=?", (eid, k))
            conn.execute("UPDATE episodes SET current_stage=?, finished_at=NULL WHERE id=?", (key, eid))
            db.log(conn, eid, user["id"], "退回", f"退回到「{st.name}」：{form.get('reason', '')}")
            notify_assignee(conn, ep, key, prefix=f"退回（{form.get('reason', '未写原因')}）：")
            flash(request, f"已退回到「{st.name}」")
    raise Redirect(f"/episodes/{eid}#stage-{key}")


@app.get("/files/{fid}")
def download(request: Request, fid: int):
    with db.connect() as conn:
        current_user(request, conn)
        f = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
    if not f:
        raise Redirect("/")
    return FileResponse(db.UPLOAD_DIR / f["stored_name"], filename=f["filename"])


# ---------- 成员 ----------

@app.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    with db.connect() as conn:
        user = current_user(request, conn, admin=True)
        rows = conn.execute("SELECT * FROM users ORDER BY active DESC, id").fetchall()
    return render(request, "users.html", user, users=rows)


@app.post("/users")
async def users_action(request: Request):
    form = await request.form()
    with db.connect() as conn:
        admin = current_user(request, conn, admin=True)
        action = form.get("action")
        if action == "create":
            err = validate_new_password(form.get("password", ""))
            uname = form.get("username", "").strip()
            if err or not uname or form.get("role") not in ROLES:
                flash(request, err or "请填写用户名和角色", "error")
            elif conn.execute("SELECT 1 FROM users WHERE username=?", (uname,)).fetchone():
                flash(request, "用户名已存在", "error")
            else:
                conn.execute("INSERT INTO users(username, display_name, password_hash, role, is_admin, mobile, created_at) VALUES(?,?,?,?,?,?,?)",
                             (uname, form.get("display_name") or uname, hash_password(form["password"]), form["role"],
                              1 if form.get("is_admin") else 0, form.get("mobile", ""), db.now()))
                db.log(conn, None, admin["id"], "新增成员", uname)
                flash(request, f"已添加 {uname}。把用户名和初始密码发给对方，登录后可以在「我的账号」里改密码。")
        elif action == "update":
            uid = valid_user_id(conn, form.get("id")) or (int(form["id"]) if str(form.get("id", "")).isdigit() else None)
            if uid is None or form.get("role") not in ROLES:
                flash(request, "参数不对", "error")
            elif form.get("new_password") and validate_new_password(form["new_password"]):
                flash(request, validate_new_password(form["new_password"]) + "，未保存", "error")
            elif uid == admin["id"] and (not form.get("is_admin") or not form.get("active")):
                flash(request, "不能取消自己的管理员权限或停用自己", "error")
            else:
                conn.execute("UPDATE users SET display_name=?, role=?, mobile=?, is_admin=?, active=? WHERE id=?",
                             (form.get("display_name", ""), form.get("role"), form.get("mobile", ""),
                              1 if form.get("is_admin") else 0, 1 if form.get("active") else 0, uid))
                if form.get("new_password"):
                    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(form["new_password"]), uid))
                flash(request, "已更新")
    raise Redirect("/users")


@app.get("/account", response_class=HTMLResponse)
def account(request: Request):
    with db.connect() as conn:
        user = current_user(request, conn)
    return render(request, "account.html", user)


@app.post("/account")
async def account_update(request: Request):
    form = await request.form()
    with db.connect() as conn:
        user = current_user(request, conn)
        conn.execute("UPDATE users SET display_name=?, mobile=? WHERE id=?",
                     (form.get("display_name") or user["display_name"], form.get("mobile", ""), user["id"]))
        if form.get("new_password"):
            if not verify_password(form.get("old_password", ""), user["password_hash"]):
                flash(request, "原密码不对，其他信息已保存", "error")
                conn.commit()
                raise Redirect("/account")
            err = validate_new_password(form["new_password"])
            if err:
                flash(request, err, "error")
                conn.commit()
                raise Redirect("/account")
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(form["new_password"]), user["id"]))
        flash(request, "已保存")
    raise Redirect("/account")


# ---------- 设置 ----------

PROVIDER_CHOICES = ["anthropic", "openai", "codex", "mock"]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with db.connect() as conn:
        user = current_user(request, conn, admin=True)
        show = show_profile(conn)
        cfg = pipeline_config(conn, apply_env_override=False)
        ctx = dict(
            env_override=os.environ.get("PIPELINE_PROVIDER_OVERRIDE", ""),
            show=show,
            show_yaml_lists={k: "\n".join(show.get(k) or []) for k in ("accuracy_rules", "platforms")},
            brand=show.get("brand") or {},
            webhook=db.get_setting(conn, "wecom_webhook") or "",
            base_url=db.get_setting(conn, "base_url") or "",
            workflows=[w for w in cfg.workflows if w.id in {s.ai for s in STAGES if s.ai}],
            all_workflows=cfg.workflows,
            providers=PROVIDER_CHOICES,
            keys={"OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")), "ANTHROPIC_API_KEY": bool(os.environ.get("ANTHROPIC_API_KEY"))},
        )
    return render(request, "settings.html", user, **ctx)


@app.post("/settings")
async def settings_update(request: Request):
    form = await request.form()
    with db.connect() as conn:
        current_user(request, conn, admin=True)
        section = form.get("section")
        if section == "show":
            lines = lambda k: [x.strip() for x in str(form.get(k, "")).splitlines() if x.strip()]
            show = deepcopy(show_profile(conn))
            show.update({k: str(form.get(k, "")).strip() for k in ("name", "tagline", "domain", "format", "disclaimer")})
            show["accuracy_rules"], show["platforms"] = lines("accuracy_rules"), lines("platforms")
            show["brand"] = {k: str(form.get(f"brand_{k}", "")).strip() for k in ("style", "title_font", "fixed_elements", "avoid")}
            db.set_setting(conn, "show", show)
            flash(request, "节目档案已保存")
        elif section == "notify":
            db.set_setting(conn, "wecom_webhook", str(form.get("wecom_webhook", "")).strip())
            db.set_setting(conn, "base_url", str(form.get("base_url", "")).strip())
            flash(request, "通知设置已保存")
        elif section == "notify_test":
            ok, msg = await run_in_threadpool(send_wecom, db.get_setting(conn, "wecom_webhook") or "", "这是一条来自播客协作工作台的测试消息")
            flash(request, f"测试消息：{msg}", "ok" if ok else "error")
        elif section == "models":
            models = db.get_setting(conn, "models", {})
            for wf in load_config().workflows:
                for role in ("executor", "reviewer"):
                    p = form.get(f"{wf.id}_{role}_provider")
                    if p:
                        model = str(form.get(f"{wf.id}_{role}_model", "")).strip()
                        if p in ("openai", "anthropic") and model in ("", "default"):
                            flash(request, f"{wf.name}：{p} 需要填写具体型号", "error")
                            continue
                        models.setdefault(wf.id, {})[role] = {"provider": p, "model": model or "default"}
            db.set_setting(conn, "models", models)
            flash(request, "模型分配已保存")
    raise Redirect("/settings")


@app.get("/healthz")
def healthz():
    return {"ok": True}
