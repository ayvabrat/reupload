"""
Генерация одностраничного HTML-отчёта на Jinja2.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import config
from core.models import Channel, Video
from export.excel_export import _apply_export_filters, _profile_id_from_filters


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y %H:%M")


def _parse_keyword_tags(keywords_csv: str) -> list[str]:
    parts = [p.strip() for p in keywords_csv.split(",") if p.strip()]
    return parts[:50]


def export_html(
    session: Session,
    keywords_used: str,
    out_dir: Path | None = None,
    *,
    export_filters: dict | None = None,
) -> Path:
    """
    Создаёт reupload_report_YYYY-MM-DD_HH-MM.html и возвращает путь.
    """
    config.ensure_directories()
    out = out_dir or config.EXPORT_DIR
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
    path = out / f"reupload_report_{ts}.html"

    tpl_dir = config.BUNDLE_ROOT / "templates"
    env = Environment(
        loader=FileSystemLoader(str(tpl_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html")

    xf0 = export_filters or {}
    pid = _profile_id_from_filters(xf0)

    vtot = select(func.count()).select_from(Video)
    ctot = select(func.count()).select_from(Channel)
    cvk = select(func.count()).select_from(Video).where(Video.platform == "vk")
    crt = select(func.count()).select_from(Video).where(Video.platform == "rutube")
    cai = select(func.count()).select_from(Video).where(Video.ai_match.is_(True))
    if pid is not None:
        vtot = select(func.count()).select_from(Video).where(Video.profile_id == pid)
        ctot = select(func.count()).select_from(Channel).where(Channel.profile_id == pid)
        cvk = select(func.count()).select_from(Video).where(Video.profile_id == pid, Video.platform == "vk")
        crt = select(func.count()).select_from(Video).where(Video.profile_id == pid, Video.platform == "rutube")
        cai = select(func.count()).select_from(Video).where(Video.profile_id == pid, Video.ai_match.is_(True))
    total_videos = session.scalar(vtot) or 0
    total_channels = session.scalar(ctot) or 0
    count_vk = session.scalar(cvk) or 0
    count_rutube = session.scalar(crt) or 0
    count_ai_yes = session.scalar(cai) or 0

    tch_q = select(Channel).order_by(Channel.total_matching_videos.desc()).limit(20)
    if pid is not None:
        tch_q = tch_q.where(Channel.profile_id == pid)
    top_ch = session.execute(tch_q).scalars().all()

    top_channels_ctx: list[dict] = []
    for c in top_ch:
        vq = select(Video).where(Video.channel_id == c.id)
        if pid is not None:
            vq = vq.where(Video.profile_id == pid)
        vids = session.execute(vq.limit(100)).scalars().all()
        top_channels_ctx.append(
            {
                "id": c.id,
                "platform": c.platform,
                "channel_name": c.channel_name,
                "channel_url": c.channel_url,
                "avatar_url": c.avatar_url,
                "subscriber_count": c.subscriber_count,
                "total_matching_videos": c.total_matching_videos,
                "first_seen_fmt": _fmt_dt(c.first_seen),
                "videos": [
                    {
                        "title": v.title,
                        "video_url": v.video_url,
                        "views": v.views,
                        "matched_keywords": v.matched_keywords,
                    }
                    for v in vids
                ],
            }
        )

    pl = xf0.get("platform")
    if pl not in ("vk", "rutube"):
        pl = None
    ai_f = xf0.get("ai")
    if ai_f not in ("yes", "no", "pending"):
        ai_f = None
    cat_f = xf0.get("ai_category")
    if cat_f not in ("reaction", "series", "team"):
        cat_f = None
    dur_f = xf0.get("duration_filter")
    if dur_f not in ("missing", "present"):
        dur_f = None

    q = select(Video, Channel).join(Channel, Video.channel_id == Channel.id)
    q = _apply_export_filters(q, pl, ai_f, cat_f, dur_f, pid)
    rows = session.execute(q).all()

    all_videos: list[dict] = []
    for v, ch in rows:
        ch_cnt = ch.total_matching_videos
        tags = [t.strip() for t in (v.matched_keywords or "").split(",") if t.strip()][:20]
        upload_ts = int(v.upload_date.timestamp()) if v.upload_date else 0
        found_ts = int(v.found_date.timestamp()) if v.found_date else 0
        ai_val = v.ai_match
        ai_data = "1" if ai_val is True else ("0" if ai_val is False else "")
        cat = (v.ai_category or "").strip().lower()
        all_videos.append(
            {
                "platform": v.platform,
                "channel_name": ch.channel_name,
                "channel_url": ch.channel_url,
                "title": v.title,
                "video_url": v.video_url,
                "thumbnail_url": v.thumbnail_url,
                "views": v.views,
                "upload_date_fmt": _fmt_dt(v.upload_date),
                "upload_ts": upload_ts,
                "found_date_fmt": _fmt_dt(v.found_date),
                "found_ts": found_ts,
                "matched_keywords": v.matched_keywords,
                "match_location": v.match_location,
                "kw_tags": tags,
                "channel_video_count": ch_cnt,
                "ai_match": ai_val,
                "ai_data": ai_data,
                "ai_category": v.ai_category,
                "ai_category_data": cat,
                "has_duration": v.duration is not None,
                "ai_note": (v.ai_note or "")[:500],
            }
        )

    html = template.render(
        generated_at=_fmt_dt(datetime.now()),
        keyword_list=_parse_keyword_tags(keywords_used),
        total_videos=total_videos,
        total_channels=total_channels,
        count_vk=count_vk,
        count_rutube=count_rutube,
        count_ai_yes=count_ai_yes,
        top_channels=top_channels_ctx,
        all_videos=all_videos,
    )
    path.write_text(html, encoding="utf-8")
    return path
