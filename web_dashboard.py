"""
HTTP API и раздача React-панели (см. каталог web/).
Запуск: python web_dashboard.py
В режиме разработки UI: cd web && npm install && npm run dev
"""

from __future__ import annotations

import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, nulls_last, select
from starlette.middleware.sessions import SessionMiddleware

import config
from core.app_settings import load_app_settings, save_app_settings
from core.database import init_db, session_scope
from core.models import Channel, DataProfile, ScanProfile, User, Video
from core.monitor_service import start_monitor_daemon
from core.scan_control import ScanControl
from core.scheduler import run_scan
from core.telegram_notify import test_telegram_connection
from core.users_util import hash_password, verify_password
from core.web_auth import BasicAuthMiddleware
from export.excel_export import _apply_export_filters, export_excel
from export.html_export import export_html

# --- состояние фонового сканирования ---
_scan_lock = threading.Lock()
_scan_running = False
_scan_message = ""
_scan_error: str | None = None
_scan_last_stats: dict[str, Any] | None = None
_active_scan_control: ScanControl | None = None

app = FastAPI(title="ReUpload Detector API", version="1.0")

app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    session_cookie="rd_session",
    same_site="lax",
    max_age=1209600,
)
_cors_origins = config.cors_allow_origins()
if _cors_origins == ["*"]:
    # cookie-сессии несовместимы с allow_origins=* — задайте WEB_CORS_ORIGINS в .env для продакшена
    _cors_origins = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(BasicAuthMiddleware)


def _session_uid(request: Request) -> int | None:
    raw = request.session.get("user_id")
    return int(raw) if raw is not None else None


def require_login(request: Request) -> int:
    uid = _session_uid(request)
    if uid is None:
        raise HTTPException(401, "Требуется войти")
    return uid


def active_data_profile_id(request: Request, user_id: int = Depends(require_login)) -> int:
    raw = request.session.get("data_profile_id")
    if raw is None:
        raise HTTPException(400, "Не выбран профиль данных")
    pid = int(raw)
    with session_scope() as s:
        row = s.get(DataProfile, pid)
        if not row or row.user_id != user_id:
            raise HTTPException(403, "Нет доступа к профилю данных")
    return pid


class SearchRequest(BaseModel):
    keywords: str = Field(..., min_length=1)
    platforms: list[str] = Field(default_factory=lambda: ["vk", "rutube"])
    search_in: str = "all"
    max_results: int = Field(default=100, ge=1)
    gigachat: bool = False
    """Классификация GigaChat после скана (если не включён фоновый режим)."""
    gigachat_during_scan: bool = False
    """Параллельные запросы к GigaChat во время парса (нужен gigachat=true)."""
    fetch_workers: int | None = Field(default=None, ge=1, le=32)
    """Параллельные запросы к VK/Rutube по разным ключам; None = из .env SCAN_FETCH_WORKERS."""

    @field_validator("keywords", mode="before")
    @classmethod
    def strip_keywords(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v


class ScanProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    keywords: str = Field(..., min_length=1)
    platforms: list[str] = Field(default_factory=lambda: ["vk", "rutube"])
    search_in: str = "all"
    max_results: int = Field(default=500, ge=1)
    gigachat: bool = False
    gigachat_during_scan: bool = False

    @field_validator("keywords", mode="before")
    @classmethod
    def strip_kw(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v


class ScanProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    keywords: str | None = None
    platforms: list[str] | None = None
    search_in: str | None = None
    max_results: int | None = Field(default=None, ge=1)
    gigachat: bool | None = None
    gigachat_during_scan: bool | None = None


class RegisterBody(BaseModel):
    login: str = Field(..., min_length=2, max_length=256)
    password: str = Field(..., min_length=8, max_length=256)


class LoginBody(BaseModel):
    login: str = Field(..., min_length=1, max_length=256)
    password: str = Field(..., min_length=1, max_length=256)


class DataProfileCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)


class DataProfileSelectBody(BaseModel):
    profile_id: int = Field(..., ge=1)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    start_monitor_daemon()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/me")
def api_me(request: Request) -> dict[str, Any]:
    init_db()
    uid = _session_uid(request)
    if uid is None:
        return {"user": None}
    with session_scope() as s:
        u = s.get(User, uid)
        if not u:
            request.session.clear()
            return {"user": None}
        profs = s.scalars(select(DataProfile).where(DataProfile.user_id == uid).order_by(DataProfile.id.asc())).all()
        pid = request.session.get("data_profile_id")
        if pid is not None:
            pid_i = int(pid)
            if not any(p.id == pid_i for p in profs):
                pid_i = profs[0].id if profs else None
                if pid_i is not None:
                    request.session["data_profile_id"] = pid_i
        elif profs:
            request.session["data_profile_id"] = profs[0].id
        out_pid = request.session.get("data_profile_id")
        items = [{"id": p.id, "name": p.name, "created_at": p.created_at.isoformat()} for p in profs]
    return {
        "user": {"id": u.id, "login": u.email},
        "data_profile_id": int(out_pid) if out_pid is not None else None,
        "data_profiles": items,
    }


@app.get("/api/auth/login")
def auth_login_get() -> RedirectResponse:
    """GET в адресной строке даёт 405 у POST — перенаправляем на интерфейс."""
    return RedirectResponse(url="/", status_code=302)


@app.get("/api/auth/register")
def auth_register_get() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=302)


@app.get("/api/auth/logout")
def auth_logout_get() -> RedirectResponse:
    return RedirectResponse(url="/", status_code=302)


@app.post("/api/auth/register")
def auth_register(request: Request, body: RegisterBody) -> dict[str, Any]:
    init_db()
    login_key = body.login.strip().lower()
    if not login_key:
        raise HTTPException(400, "Укажите логин")
    with session_scope() as s:
        existing = s.scalar(select(User).where(User.email == login_key))
        if existing:
            raise HTTPException(409, "Этот логин уже занят")
        now = datetime.now()
        u = User(email=login_key, password_hash=hash_password(body.password), created_at=now)
        s.add(u)
        s.flush()
        dp = DataProfile(user_id=u.id, name="Основной", created_at=now)
        s.add(dp)
        s.flush()
        uid, did = u.id, dp.id
    request.session["user_id"] = uid
    request.session["data_profile_id"] = did
    return {"ok": True, "user_id": uid, "data_profile_id": did}


@app.post("/api/auth/login")
def auth_login(request: Request, body: LoginBody) -> dict[str, Any]:
    init_db()
    login_key = body.login.strip().lower()
    with session_scope() as s:
        u = s.scalar(select(User).where(User.email == login_key))
        if not u or not verify_password(body.password, u.password_hash):
            raise HTTPException(401, "Неверный логин или пароль")
        profs = list(s.scalars(select(DataProfile).where(DataProfile.user_id == u.id).order_by(DataProfile.id.asc())).all())
        if not profs:
            dp = DataProfile(user_id=u.id, name="Основной", created_at=datetime.now())
            s.add(dp)
            s.flush()
            profs = [dp]
        uid = u.id
        sess_pid = request.session.get("data_profile_id")
        chosen = profs[0].id
        if sess_pid is not None:
            sp = int(sess_pid)
            if any(p.id == sp for p in profs):
                chosen = sp
    request.session["user_id"] = uid
    request.session["data_profile_id"] = chosen
    return {"ok": True, "user_id": uid, "data_profile_id": chosen}


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> dict[str, str]:
    request.session.clear()
    return {"ok": "true"}


@app.get("/api/data-profiles")
def list_data_profiles(user_id: int = Depends(require_login)) -> dict[str, Any]:
    init_db()
    with session_scope() as s:
        rows = s.scalars(select(DataProfile).where(DataProfile.user_id == user_id).order_by(DataProfile.id.asc())).all()
        items = [{"id": r.id, "name": r.name, "created_at": r.created_at.isoformat()} for r in rows]
    return {"items": items}


@app.post("/api/data-profiles")
def create_data_profile(request: Request, body: DataProfileCreateBody, user_id: int = Depends(require_login)) -> dict[str, Any]:
    init_db()
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Укажите имя профиля")
    with session_scope() as s:
        row = DataProfile(user_id=user_id, name=name, created_at=datetime.now())
        s.add(row)
        s.flush()
        pid = row.id
    request.session["data_profile_id"] = pid
    return {"id": pid, "name": name}


@app.post("/api/data-profiles/active")
def select_data_profile(request: Request, body: DataProfileSelectBody, user_id: int = Depends(require_login)) -> dict[str, str]:
    init_db()
    with session_scope() as s:
        row = s.get(DataProfile, body.profile_id)
        if not row or row.user_id != user_id:
            raise HTTPException(404, "Профиль не найден")
    request.session["data_profile_id"] = body.profile_id
    return {"status": "ok"}


@app.delete("/api/data-profiles/{profile_id}")
def delete_data_profile(
    request: Request,
    profile_id: int,
    user_id: int = Depends(require_login),
) -> dict[str, str]:
    init_db()
    with session_scope() as s:
        rows = list(s.scalars(select(DataProfile).where(DataProfile.user_id == user_id).order_by(DataProfile.id.asc())).all())
        if len(rows) <= 1:
            raise HTTPException(400, "Нельзя удалить единственный профиль данных")
        target = s.get(DataProfile, profile_id)
        if not target or target.user_id != user_id:
            raise HTTPException(404, "Профиль не найден")
        s.delete(target)
    if request.session.get("data_profile_id") == profile_id:
        with session_scope() as s2:
            first = s2.scalars(select(DataProfile).where(DataProfile.user_id == user_id).order_by(DataProfile.id.asc())).first()
            if first:
                request.session["data_profile_id"] = first.id
    return {"status": "ok"}


@app.get("/api/config")
def api_config() -> dict[str, Any]:
    """Шаблон ключевых слов и сохранённые настройки приложения."""
    init_db()
    app_st = load_app_settings()
    return {
        "shgsh_keywords_template": (config.SHGSH_KEYWORDS_TEMPLATE or "").strip(),
        "template_merge_default": config.SHGSH_TEMPLATE_MERGE,
        "scan_fetch_workers_default": config.SCAN_FETCH_WORKERS,
        "gigachat_parallel_workers_default": config.GIGACHAT_PARALLEL_WORKERS,
        "app_settings": app_st,
    }


@app.patch("/api/settings")
def patch_app_settings(body: dict[str, Any], _uid: int = Depends(require_login)) -> dict[str, Any]:
    """Сливает настройки (Telegram, мониторинг, фильтр Excel) в data/app_settings.json."""
    return save_app_settings(body)


@app.post("/api/telegram/test")
def telegram_test(_uid: int = Depends(require_login)) -> dict[str, Any]:
    ok, msg = test_telegram_connection()
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "message": msg}


@app.get("/api/scan/status")
def scan_status(_uid: int = Depends(require_login)) -> dict[str, Any]:
    with _scan_lock:
        paused = bool(_active_scan_control and _active_scan_control.is_paused)
        return {
            "running": _scan_running,
            "paused": paused,
            "message": _scan_message,
            "error": _scan_error,
            "last_stats": _scan_last_stats,
        }


@app.post("/api/search/pause")
def search_pause(_uid: int = Depends(require_login)) -> dict[str, str]:
    global _active_scan_control
    with _scan_lock:
        if not _scan_running:
            raise HTTPException(409, "Сканирование не выполняется")
        if _active_scan_control:
            _active_scan_control.pause()
    return {"status": "paused"}


@app.post("/api/search/resume")
def search_resume(_uid: int = Depends(require_login)) -> dict[str, str]:
    with _scan_lock:
        if not _scan_running:
            raise HTTPException(409, "Сканирование не выполняется")
        if _active_scan_control:
            _active_scan_control.resume()
    return {"status": "resumed"}


@app.post("/api/search/stop")
def search_stop(_uid: int = Depends(require_login)) -> dict[str, str]:
    global _active_scan_control
    with _scan_lock:
        if not _scan_running:
            raise HTTPException(409, "Сканирование не выполняется")
        if _active_scan_control:
            _active_scan_control.request_stop()
    return {"status": "stopping"}


def _merge_export_filters(
    platform: str | None,
    ai: str | None,
    ai_category: str | None,
    duration_filter: str | None,
    profile_id: int | None = None,
) -> dict[str, Any]:
    base = dict(load_app_settings().get("excel_export") or {})
    if profile_id is not None:
        base["profile_id"] = profile_id
    if platform in ("vk", "rutube", "any"):
        base["platform"] = None if platform == "any" else platform
    if ai in ("yes", "no", "pending", "any"):
        base["ai"] = None if ai == "any" else ai
    if ai_category in ("reaction", "series", "team", "any"):
        base["ai_category"] = None if ai_category == "any" else ai_category
    if duration_filter in ("missing", "present", "any"):
        base["duration_filter"] = None if duration_filter == "any" else duration_filter
    return base


@app.get("/api/export/excel")
def download_excel(
    platform: str | None = Query(None),
    ai: str | None = Query(None),
    ai_category: str | None = Query(None),
    duration_filter: str | None = Query(None),
    profile_id: int = Depends(active_data_profile_id),
) -> FileResponse:
    init_db()
    xf = _merge_export_filters(platform, ai, ai_category, duration_filter, profile_id=profile_id)
    with session_scope() as session:
        path = export_excel(session, "export", export_filters=xf)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/export/html")
def download_html(
    platform: str | None = Query(None),
    ai: str | None = Query(None),
    ai_category: str | None = Query(None),
    duration_filter: str | None = Query(None),
    profile_id: int = Depends(active_data_profile_id),
) -> FileResponse:
    init_db()
    xf = _merge_export_filters(platform, ai, ai_category, duration_filter, profile_id=profile_id)
    with session_scope() as session:
        path = export_html(session, "export", export_filters=xf)
    return FileResponse(path, filename=path.name, media_type="text/html; charset=utf-8")


@app.get("/api/profiles")
def list_profiles(_uid: int = Depends(require_login)) -> dict[str, Any]:
    init_db()
    with session_scope() as session:
        rows = session.scalars(select(ScanProfile).order_by(ScanProfile.name.asc())).all()
        items = [
            {
                "id": r.id,
                "name": r.name,
                "keywords": r.keywords,
                "platforms": r.platforms.split(",") if r.platforms else [],
                "search_in": r.search_in,
                "max_results": r.max_results,
                "gigachat": r.gigachat,
                "gigachat_during_scan": r.gigachat_during_scan,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    return {"items": items}


@app.post("/api/profiles")
def create_profile(body: ScanProfileCreate, _uid: int = Depends(require_login)) -> dict[str, Any]:
    pls = [p.lower().strip() for p in body.platforms if p.strip()]
    if not pls:
        pls = ["vk", "rutube"]
    if not {"vk", "rutube"} >= set(pls):
        raise HTTPException(400, "platforms: только vk и/или rutube")
    init_db()
    with session_scope() as session:
        existing = session.execute(
            select(ScanProfile).where(ScanProfile.name == body.name.strip())
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "Профиль с таким именем уже есть")
        row = ScanProfile(
            name=body.name.strip(),
            keywords=body.keywords.strip(),
            platforms=",".join(pls),
            search_in=body.search_in,
            max_results=body.max_results,
            gigachat=body.gigachat,
            gigachat_during_scan=body.gigachat_during_scan,
            created_at=datetime.now(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {"id": row.id, "name": row.name}


@app.patch("/api/profiles/{profile_id}")
def patch_profile(profile_id: int, body: ScanProfilePatch, _uid: int = Depends(require_login)) -> dict[str, str]:
    init_db()
    with session_scope() as session:
        row = session.get(ScanProfile, profile_id)
        if not row:
            raise HTTPException(404, "Профиль не найден")
        if body.name is not None:
            row.name = body.name.strip()
        if body.keywords is not None:
            row.keywords = body.keywords.strip()
        if body.platforms is not None:
            pls = [p.lower().strip() for p in body.platforms if p.strip()] or ["vk", "rutube"]
            if not {"vk", "rutube"} >= set(pls):
                raise HTTPException(400, "platforms: только vk и/или rutube")
            row.platforms = ",".join(pls)
        if body.search_in is not None:
            row.search_in = body.search_in
        if body.max_results is not None:
            row.max_results = body.max_results
        if body.gigachat is not None:
            row.gigachat = body.gigachat
        if body.gigachat_during_scan is not None:
            row.gigachat_during_scan = body.gigachat_during_scan
    return {"status": "ok"}


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int, _uid: int = Depends(require_login)) -> dict[str, str]:
    init_db()
    with session_scope() as session:
        row = session.get(ScanProfile, profile_id)
        if not row:
            raise HTTPException(404, "Профиль не найден")
        session.delete(row)
    return {"status": "ok"}


@app.post("/api/search")
def start_search(body: SearchRequest, profile_id: int = Depends(active_data_profile_id)) -> JSONResponse:
    global _scan_running, _scan_message, _scan_error, _scan_last_stats, _active_scan_control
    pls = [p.lower().strip() for p in body.platforms if p.strip()]
    if not pls:
        pls = ["vk", "rutube"]
    if not {"vk", "rutube"} >= set(pls):
        raise HTTPException(400, "platforms: только vk и/или rutube")

    ctrl = ScanControl()

    with _scan_lock:
        if _scan_running:
            raise HTTPException(409, "Сканирование уже выполняется")
        _scan_error = None
        _scan_message = "Запуск…"
        _scan_running = True
        _active_scan_control = ctrl

    def worker() -> None:
        global _scan_running, _scan_message, _scan_error, _scan_last_stats, _active_scan_control
        try:
            _scan_message = "Сканирование платформ…"
            run_giga = bool(body.gigachat or body.gigachat_during_scan)
            stats = run_scan(
                body.keywords,
                pls,
                body.search_in,
                body.max_results,
                progress_cb=lambda pl, cur, tot, msg: None,
                do_export=True,
                run_gigachat=run_giga,
                gigachat_during_scan=body.gigachat_during_scan,
                control=ctrl,
                fetch_workers=body.fetch_workers,
                profile_id=profile_id,
            )
            _scan_last_stats = stats
            msg = "Остановлено" if stats.get("cancelled") else "Готово"
            _scan_message = msg
        except Exception as e:
            _scan_error = f"{e}\n{traceback.format_exc()}"
            _scan_message = "Ошибка"
        finally:
            with _scan_lock:
                _scan_running = False
                _active_scan_control = None

    threading.Thread(target=worker, daemon=True).start()
    return JSONResponse({"started": True})


@app.get("/api/stats")
def api_stats(profile_id: int = Depends(active_data_profile_id)) -> dict[str, Any]:
    init_db()
    with session_scope() as session:
        vc = (
            session.scalar(select(func.count()).select_from(Video).where(Video.profile_id == profile_id)) or 0
        )
        cc = (
            session.scalar(select(func.count()).select_from(Channel).where(Channel.profile_id == profile_id)) or 0
        )
        by_p = dict(
            session.execute(
                select(Video.platform, func.count(Video.id))
                .where(Video.profile_id == profile_id)
                .group_by(Video.platform)
            ).all()
        )
        ai_yes = (
            session.scalar(
                select(func.count())
                .select_from(Video)
                .where(Video.profile_id == profile_id, Video.ai_match.is_(True))
            )
            or 0
        )
        ai_pending = (
            session.scalar(
                select(func.count())
                .select_from(Video)
                .where(Video.profile_id == profile_id, Video.ai_match.is_(None))
            )
            or 0
        )
    return {
        "videos": vc,
        "channels": cc,
        "vk": by_p.get("vk", 0),
        "rutube": by_p.get("rutube", 0),
        "ai_relevant": ai_yes,
        "ai_pending": ai_pending,
    }


@app.get("/api/videos")
def list_videos(
    limit: int = 80,
    offset: int = 0,
    platform: str | None = None,
    ai: str | None = None,
    ai_category: str | None = None,
    duration_filter: str | None = None,
    sort: str = "found_date",
    order: str = "desc",
    profile_id: int = Depends(active_data_profile_id),
) -> dict[str, Any]:
    """ai: yes | no | pending. duration_filter: missing | present (длительность в БД). sort: found_date | upload_date | duration."""
    if platform not in ("vk", "rutube"):
        platform = None
    if ai not in ("yes", "no", "pending"):
        ai = None
    if duration_filter not in ("missing", "present"):
        duration_filter = None
    if ai_category not in ("reaction", "series", "team"):
        ai_category = None
    sort_key = (sort or "found_date").lower()
    if sort_key not in ("found_date", "upload_date", "duration"):
        sort_key = "found_date"
    ord_key = (order or "desc").lower()
    if ord_key not in ("asc", "desc"):
        ord_key = "desc"

    init_db()
    limit = min(max(limit, 1), 500)
    offset = max(offset, 0)
    with session_scope() as session:
        q = select(Video, Channel).join(Channel, Video.channel_id == Channel.id)
        q = _apply_export_filters(q, platform, ai, ai_category, duration_filter, profile_id)
        q_count = select(func.count(Video.id)).select_from(Video).join(Channel, Video.channel_id == Channel.id)
        q_count = _apply_export_filters(q_count, platform, ai, ai_category, duration_filter, profile_id)
        total = session.scalar(q_count) or 0

        sort_col = {
            "found_date": Video.found_date,
            "upload_date": Video.upload_date,
            "duration": Video.duration,
        }[sort_key]
        if ord_key == "asc":
            q = q.order_by(nulls_last(sort_col.asc()))
        else:
            q = q.order_by(nulls_last(sort_col.desc()))

        q = q.offset(offset).limit(limit)
        rows = session.execute(q).all()
        out: list[dict[str, Any]] = []
        for v, ch in rows:
            out.append(
                {
                    "id": v.id,
                    "platform": v.platform,
                    "platform_video_id": v.platform_video_id,
                    "title": v.title,
                    "description": (v.description or "")[:500],
                    "video_url": v.video_url,
                    "thumbnail_url": v.thumbnail_url,
                    "duration": v.duration,
                    "views": v.views,
                    "upload_date": v.upload_date.isoformat() if v.upload_date else None,
                    "found_date": v.found_date.isoformat(),
                    "matched_keywords": v.matched_keywords,
                    "match_location": v.match_location,
                    "channel_name": ch.channel_name,
                    "channel_url": ch.channel_url,
                    "ai_match": v.ai_match,
                    "ai_note": v.ai_note,
                    "ai_category": v.ai_category,
                }
            )
    return {
        "items": out,
        "total": total,
        "limit": limit,
        "offset": offset,
        "sort": sort_key,
        "order": ord_key,
        "duration_filter": duration_filter or "any",
        "ai_category": ai_category or "any",
    }


def _get_web_dist() -> Path:
    return config.BUNDLE_ROOT / "web" / "dist"


def _mount_frontend() -> None:
    dist = _get_web_dist()
    assets = dist / "assets"
    if dist.is_dir() and (dist / "index.html").is_file():
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/")
        def _root() -> FileResponse:
            return FileResponse(dist / "index.html")

        @app.get("/{full_path:path}")
        def _spa(full_path: str) -> FileResponse:
            if full_path.startswith("api"):
                raise HTTPException(404)
            return FileResponse(dist / "index.html")


if _get_web_dist().joinpath("index.html").is_file():
    _mount_frontend()
else:

    @app.get("/")
    def _api_only_root() -> dict[str, str]:
        return {
            "message": "ReUpload Detector API. Сборка UI: cd web && npm install && npm run build, затем перезапустите сервер. Разработка: npm run dev (прокси /api).",
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "web_dashboard:app",
        host=config.WEB_API_HOST,
        port=config.WEB_API_PORT,
        reload=False,
    )
