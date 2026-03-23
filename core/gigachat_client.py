"""
Клиент OAuth и чата GigaChat (Сбер) для классификации роликов.
При отказе модели (модерация) — локальная эвристика без LLM.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import uuid
from typing import Any

import requests
import urllib3
from loguru import logger
from rapidfuzz import fuzz

import config

if not config.GIGACHAT_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

# Короткие нейтральные промпты — длинные тексты про «школу» часто попадают под фильтр GigaChat
SYSTEM_SHORT_RU = (
    "Ты классификатор метаданных. Отвечай только одним JSON-объектом, без текста до или после него."
)
USER_SHORT_RU = """Проект «Школа глазами школьника» (ШГШ), автор Руслан Гладенко — веб-сериал, стримы, реакции, перезаливы.

Правила (важно):
- match=true: в канале ИЛИ в заголовке/описании есть «шгш», «школа глазами», «глазами школьника», «гладенко» — это проект (включая стримы без слова «школа»).
- match=true: «Школа 6/7/8/9» + серия/сезон/реакция/стрим/смотрю/ТипоТоп/от первого лица — типичный перезалив.
- match=false: только явно чужой контент без связи с ШГШ (другой сериал, другая школа без ШГШ в контексте).

Поля:
- платформа: {platform}
- канал: {channel}
- заголовок: {title}
- описание: {description}

Ответ только JSON: {{"match": true/false, "reason": "кратко", "category": "reaction"|"series"|"team"}}
category: reaction — реакция, перезалив, обзор, ТипоТоп; series — выпуск/серия оригинального сериала «Школа N»; team — подкаст, блог, стрим/контент команды проекта без «серии сериала». При match=false category: null или пустая строка."""

SYSTEM_SHORT_EN = "You are a JSON-only classifier. Reply with a single JSON object, nothing else."
USER_SHORT_EN = """Task: Russian web series «Shkola glazami shkolnika» (ШГШ / SHGSH), author Ruslan Gladenko.
Return JSON only: {{"match": true/false, "reason": "short phrase", "category": "reaction"|"series"|"team"|null}}
category: reaction=reuploads/reactions; series=original episode titles; team=podcasts/cast/official channel extras.
match=true if SHGSH-related; match=false if unrelated.
platform={platform}
channel={channel}
title={title}
description={description}"""


def _norm_blob(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "")
    t = t.lower().replace("ё", "е")
    t = re.sub(r"\s+", " ", t).strip()
    return t


# Сезоны «Школа 1» … «Школа 9» (в т.ч. 4–5; ранее были только 6–9 — серии ошибочно шли в «команда»)
_SCHOOL_NUM_RE = re.compile(
    r"(школ[ауеыо]\s*[1-9]\b|школа\s*[1-9]\b|школу\s*[1-9]\b|школе\s*[1-9]\b|"
    r"\bш[1-9]\b|школа[1-9]|школу[1-9])",
    re.IGNORECASE,
)
_REACT_FORMAT = (
    "реакц",
    "смотр",
    "стрим",
    "перезалив",
    "обсужд",
    "разбор",
    "от первого",
    "сериал",
    "выпуск",
    "серия",
    "серию",
    "эпизод",
    "сезон",
    "типотоп",
    "типо топ",
)

AI_CATEGORY_REACTION = "reaction"
AI_CATEGORY_SERIES = "series"
AI_CATEGORY_TEAM = "team"
_VALID_AI_CATEGORIES = frozenset({AI_CATEGORY_REACTION, AI_CATEGORY_SERIES, AI_CATEGORY_TEAM})


def infer_shgsh_category(channel_name: str, title: str, description: str | None) -> str:
    """
    Грубая рубрика: реакции/перезаливы, серии сериала, контент команды (подкасты, официальный канал).
    """
    ch = _norm_blob(channel_name)
    blob = _norm_blob(f"{channel_name} {title} {description or ''}")
    brand_ch = "шгш" in ch or "школа глазами" in ch

    react_kw = (
        "реакц",
        "перезалив",
        "типотоп",
        "типо топ",
        "разбор",
        "обзор",
        "смотрю",
        "смотрим",
        "смотрят",
    )
    team_kw = ("подкаст", "готовк", "кухня", "интервью", "беседа", "лампа", "влог", "vlog")
    series_kw = ("серия", "серию", "выпуск", "эпизод", "сезон")
    has_school = bool(_SCHOOL_NUM_RE.search(blob))

    if any(k in blob for k in react_kw):
        return AI_CATEGORY_REACTION
    if has_school and any(k in blob for k in ("стрим", "от первого")) and not brand_ch:
        return AI_CATEGORY_REACTION

    # Официальные POV-выпуски на канале ШГШ: «От первого лица: Школа N …» — это серии, не «команда»
    if brand_ch and "от первого" in blob and has_school:
        return AI_CATEGORY_SERIES

    if has_school and any(k in blob for k in series_kw):
        return AI_CATEGORY_SERIES
    if "школа глазами школьника" in blob or ("глазами школьника" in blob and "школ" in blob):
        return AI_CATEGORY_SERIES

    if brand_ch:
        if any(k in blob for k in team_kw):
            return AI_CATEGORY_TEAM
        if "стрим" in blob and "реакц" not in blob:
            return AI_CATEGORY_TEAM
        if has_school:
            return AI_CATEGORY_SERIES
        return AI_CATEGORY_TEAM

    if has_school:
        return AI_CATEGORY_SERIES
    return AI_CATEGORY_REACTION


def pre_relevance_shgsh(
    channel_name: str,
    title: str,
    description: str | None,
) -> tuple[bool, str, str] | None:
    """
    Жёсткие правила до вызова LLM: официальные каналы ШГШ и типичные перезаливы «Школа N».
    Возвращает (True, причина, category) или None.
    """
    ch = _norm_blob(channel_name)
    blob = _norm_blob(f"{title} {description or ''}")
    full = _norm_blob(f"{channel_name} {title} {description or ''}")
    cat = infer_shgsh_category(channel_name, title, description)

    brand_in_channel = "шгш" in ch or "школа глазами" in ch
    if brand_in_channel:
        return True, "канал с брендом ШГШ — контент проекта (включая стримы без слова «школа» в названии)", cat

    # Сокращение / бренд в названии или описании (не только в канале)
    if "шгш" in blob:
        return True, "в метаданных есть «ШГШ»", cat

    if "глазами школьника" in blob or "глазами школьник" in blob:
        return True, "формулировка «глазами школьника» (сериал ШГШ)", cat

    if "школа глазами школьника" in full or ("школа глазами" in full and "школьник" in full):
        return True, "упоминание полного названия проекта", cat

    if "гладенко" in full or "ruslan" in full or "руслан" in full:
        return True, "упоминание автора (Р. Гладенко)", cat

    if _SCHOOL_NUM_RE.search(blob):
        if any(h in blob for h in _REACT_FORMAT):
            return True, "серия «Школа N» + формат реакции/стрима/разбора (типичный перезалив)", cat
        # Типичные заголовки: «Школа 7 — N серия», «2 сезон», без слова «реакция»
        if "сезон" in blob or "серия" in blob or "серию" in blob or "выпуск" in blob or "эпизод" in blob:
            return True, "«Школа N» + серия/сезон/выпуск (формат оригинала или перезалив)", cat

    return None


def _local_fuzzy_scores(
    platform: str,
    channel_name: str,
    title: str,
    description: str | None,
) -> tuple[bool, str, bool]:
    """
    Локальная оценка без pre_relevance. Возвращает (match, note, strong).
    strong=True: есть явное вхождение ключей, очень высокий fuzzy или связка «Школа N»+формат
    (переоценка ответа LLM «нет» разрешена только при strong=True).
    """
    blob = _norm_blob(f"{platform} {channel_name} {title} {description or ''}")
    needles = (
        "школа глазами школьника",
        "школа глазами",
        "глазами школьника",
        "шгш",
        "гладенко",
        "gladenko",
        "ruslan",
        "руслан",
        "школа 7",
        "школа 8",
        "школа 9",
        "школа 6",
        "школу 7",
        "школу 8",
        "школу 9",
        "типотоп",
        "типо топ",
        "от первого лица",
    )
    best = 0
    hits = 0
    for n in needles:
        if len(n) >= 3 and n in blob:
            hits += 1
        best = max(best, fuzz.partial_ratio(n, blob))
    match = best >= 72 or hits >= 2 or (hits >= 1 and best >= 60)
    school_react = bool(_SCHOOL_NUM_RE.search(blob) and any(h in blob for h in _REACT_FORMAT))
    if school_react:
        match = True
    strong = hits >= 1 or best >= 93 or school_react
    note = f"локально (без LLM): partial≈{best}, явных вхождений={hits}"
    return match, note, strong


def local_fallback_classify(
    platform: str,
    channel_name: str,
    title: str,
    description: str | None,
) -> tuple[bool, str, bool]:
    """
    Локальная оценка (без API): сначала жёсткие правила, затем fuzzy.
    Возвращает (match, note, strong).
    """
    pre = pre_relevance_shgsh(channel_name, title, description)
    if pre is not None:
        return True, pre[1], True
    return _local_fuzzy_scores(platform, channel_name, title, description)


def classify_shgsh_metadata_only(
    platform: str,
    channel_name: str,
    title: str,
    description: str | None,
) -> tuple[bool, str, str | None]:
    """Только эвристика (без GigaChat): ai_match только при «сильном» сигнале или pre_relevance."""
    pre = pre_relevance_shgsh(channel_name, title, description)
    if pre is not None:
        return True, pre[1], pre[2]
    m, note, strong = _local_fuzzy_scores(platform, channel_name, title, description)
    if not m:
        return False, note, None
    if not strong:
        return False, f"{note} (слабый сигнал без явных маркеров — не ШГШ)", None
    return True, note, infer_shgsh_category(channel_name, title, description)


def _is_refusal_or_disclaimer(text: str) -> bool:
    """Ответ модели не по задаче — отказ/дисклеймер модерации GigaChat."""
    if not text or len(text) < 15:
        return False
    low = text.lower()
    markers = (
        "временно ограничен",
        "чувствительн",
        "генеративные языковые модели",
        "не обладают собственным мнением",
        "к сожалению, иногда",
        "неправильного толкования",
        "разговоры на чувствительные",
        "ограничены. благодарим",
    )
    return any(m in low for m in markers)


def _normalize_category(raw: object) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        return None
    c = raw.strip().lower()
    if c in _VALID_AI_CATEGORIES:
        return c
    return None


def _parse_match_json(content: str) -> tuple[bool, str, str | None] | None:
    """Возвращает (match, reason, category) или None если это не валидный ответ задачи."""
    text = content.strip()
    if _is_refusal_or_disclaimer(text):
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        obj = json.loads(text)
        match = bool(obj.get("match"))
        reason = str(obj.get("reason", "")).strip() or "—"
        category = _normalize_category(obj.get("category"))
        return match, reason[:2000], category
    except (json.JSONDecodeError, TypeError):
        if _is_refusal_or_disclaimer(content):
            return None
        low = content.lower()
        if '"match": true' in low or '"match":true' in low:
            return True, content[:500], None
        if '"match": false' in low or '"match":false' in low:
            return False, content[:500], None
        logger.warning("GigaChat: не JSON и не отказ: {}", content[:200])
        return None


class GigaChatClient:
    """Получение токена и chat/completions."""

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._token_deadline: float = 0.0
        self._session = requests.Session()
        self._io_lock = threading.Lock()

    def _verify(self) -> bool:
        return bool(config.GIGACHAT_VERIFY_SSL)

    def _auth_header(self) -> str:
        key = (config.GIGACHAT_AUTH_KEY or "").strip()
        if not key:
            raise RuntimeError("GIGACHAT_AUTH_KEY не задан в .env")
        if key.lower().startswith("basic "):
            return key
        return f"Basic {key}"

    def get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._token_deadline - 30:
            return self._access_token

        headers = {
            "Authorization": self._auth_header(),
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        data = {
            "scope": config.GIGACHAT_SCOPE or "GIGACHAT_API_PERS",
            "grant_type": "client_credentials",
        }
        r = self._session.post(
            GIGACHAT_OAUTH_URL,
            headers=headers,
            data=data,
            timeout=config.REQUEST_TIMEOUT,
            verify=self._verify(),
        )
        r.raise_for_status()
        js: dict[str, Any] = r.json()
        token = js.get("access_token")
        if not token or not isinstance(token, str):
            raise RuntimeError(f"GigaChat OAuth: нет access_token в ответе: {js}")
        exp = int(js.get("expires_in", 1800))
        self._access_token = token
        self._token_deadline = now + max(60, exp)
        logger.debug("GigaChat: получен access_token, expires_in≈{}", exp)
        return token

    def _chat(self, system: str, user: str) -> str:
        token = self.get_access_token()
        body: dict[str, Any] = {
            "model": "GigaChat",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.05,
            "max_tokens": 320,
        }

        r = self._session.post(
            GIGACHAT_CHAT_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=body,
            timeout=max(45, config.REQUEST_TIMEOUT),
            verify=self._verify(),
        )
        r.raise_for_status()
        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"GigaChat: пустой choices: {data}")
        return (choices[0].get("message") or {}).get("content") or ""

    def classify_video_relevance(
        self,
        platform: str,
        channel_name: str,
        title: str,
        description: str | None,
    ) -> tuple[bool, str, str | None]:
        """
        Возвращает (match, reason, category). category: reaction | series | team | None.
        При отказе LLM — та же строгая эвристика, что при отключённом GigaChat.
        """
        with self._io_lock:
            return self._classify_video_relevance_unlocked(platform, channel_name, title, description)

    def _classify_video_relevance_unlocked(
        self,
        platform: str,
        channel_name: str,
        title: str,
        description: str | None,
    ) -> tuple[bool, str, str | None]:
        pre = pre_relevance_shgsh(channel_name, title, description)
        if pre is not None:
            if getattr(config, "GIGACHAT_SKIP_LLM_ON_STRONG_HEURISTIC", True):
                return True, pre[1], pre[2]

        if config.GIGACHAT_LOCAL_ONLY:
            return classify_shgsh_metadata_only(platform, channel_name, title, description)

        desc = (description or "")[:1800]
        t_short = (title or "")[:1200]

        attempts: list[tuple[str, str]] = [
            (SYSTEM_SHORT_RU, USER_SHORT_RU.format(platform=platform, channel=channel_name, title=t_short, description=desc)),
            (SYSTEM_SHORT_EN, USER_SHORT_EN.format(platform=platform, channel=channel_name, title=t_short, description=desc)),
        ]

        for sys_p, usr_p in attempts:
            try:
                content = self._chat(sys_p, usr_p)
                parsed = _parse_match_json(content)
                if parsed is not None:
                    m, r, cat_llm = parsed
                    if m:
                        cfin = cat_llm or infer_shgsh_category(channel_name, title, description)
                        return True, r, cfin
                    fb_m, fb_note, strong = local_fallback_classify(platform, channel_name, title, description)
                    if fb_m and strong:
                        cfin = infer_shgsh_category(channel_name, title, description)
                        return True, f"{r} → переоценка: {fb_note}", cfin
                    return False, r, cat_llm
                logger.info("GigaChat: пропуск ответа (отказ/не JSON), пробуем следующий промпт или fallback")
            except requests.RequestException as e:
                logger.warning("GigaChat HTTP: {}", e)
            except Exception as e:
                logger.warning("GigaChat: {}", e)

        m, note, strong = local_fallback_classify(platform, channel_name, title, description)
        logger.info("GigaChat: локальный fallback — match={} strong={}", m, strong)
        if m and strong:
            return True, note, infer_shgsh_category(channel_name, title, description)
        return classify_shgsh_metadata_only(platform, channel_name, title, description)
