"""
Сопоставление ключевых фраз с полями видео и канала.
Поддержка опечаток и вариантов написания (нечёткое совпадение).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

# Пороги: partial — по всему тексту; token — по отдельным словам (фамилии, имена)
_FUZZ_PARTIAL: int = 82
_FUZZ_TOKEN: int = 78
_FUZZ_PHRASE: int = 80


@dataclass(frozen=True)
class KeywordEntry:
    """Одна фраза из списка пользователя (с кавычками или без)."""

    raw: str
    is_quoted: bool
    words: tuple[str, ...]


def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def _normalize_for_match(s: str) -> str:
    """
    Приводит строку к виду для сравнения: регистр, ё→е, Unicode NFKC,
    пунктуация заменена пробелами, лишние пробелы убраны.
    """
    if not s:
        return ""
    t = unicodedata.normalize("NFKC", s)
    t = t.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _parse_keyword_list(keywords: list[str]) -> list[KeywordEntry]:
    """Разбирает список фраз (после split по запятой на верхнем уровне)."""
    entries: list[KeywordEntry] = []
    for part in keywords:
        p = _normalize_spaces(part)
        if not p:
            continue
        if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
            inner = _normalize_spaces(p[1:-1])
            entries.append(KeywordEntry(raw=p, is_quoted=True, words=(inner.lower(),)))
            continue
        words = tuple(w.lower() for w in _normalize_spaces(p).split() if w)
        entries.append(KeywordEntry(raw=p, is_quoted=False, words=words))
    return entries


def _split_user_keywords(keywords_csv: str) -> list[str]:
    """Делит строку по запятым, не разрывая кавычки."""
    parts: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in keywords_csv:
        if ch == '"':
            in_quote = not in_quote
            buf.append(ch)
        elif ch == "," and not in_quote:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [_normalize_spaces(p) for p in parts if _normalize_spaces(p)]


def _fuzzy_word_in_text(word_norm: str, text_norm: str) -> bool:
    """
    Слово считается найденным, если есть точное вхождение подстроки,
    высокий partial_ratio по тексту или ratio с одним из токенов.
    Короткие слова (≤2 символа) — только точное совпадение с токеном или подстрокой.
    """
    if not word_norm or not text_norm:
        return False
    if word_norm in text_norm:
        return True
    if len(word_norm) <= 2:
        return word_norm in text_norm.split()

    if fuzz.partial_ratio(word_norm, text_norm) >= _FUZZ_PARTIAL:
        return True

    for tok in text_norm.split():
        if not tok:
            continue
        if abs(len(tok) - len(word_norm)) > max(3, len(word_norm) // 2) and len(word_norm) > 4:
            continue
        if fuzz.ratio(word_norm, tok) >= _FUZZ_TOKEN:
            return True
        if fuzz.partial_ratio(word_norm, tok) >= max(_FUZZ_TOKEN, 85):
            return True
    return False


def _fuzzy_phrase_in_text(phrase_norm: str, text_norm: str) -> bool:
    """Фраза в кавычках: подстрока или нечёткое совпадение по фрагменту текста."""
    if not phrase_norm or not text_norm:
        return False
    if phrase_norm in text_norm:
        return True
    return int(fuzz.partial_ratio(phrase_norm, text_norm)) >= _FUZZ_PHRASE


class KeywordMatcher:
    """
    Проверка совпадений ключевых фраз с учётом режима поиска.

    Без кавычек: все слова фразы должны быть найдены (нечётко) в целевом тексте.
    В кавычках: фраза целиком — подстрока или нечёткое вхождение.
    """

    def __init__(self, keywords: list[str], search_in: str = "all") -> None:
        """
        :param keywords: список фраз (уже разбитый по запятым).
        :param search_in: title | description | channel | title+description | all
        """
        self._entries = _parse_keyword_list(keywords)
        self.search_in = search_in

    @classmethod
    def from_csv(cls, keywords_csv: str, search_in: str = "all") -> "KeywordMatcher":
        """Создаёт матчер из строки «слово1, слово2» с поддержкой кавычек."""
        parts = _split_user_keywords(keywords_csv)
        return cls(parts, search_in=search_in)

    def _match_entry_in_text(self, entry: KeywordEntry, text_raw: str) -> bool:
        text_norm = _normalize_for_match(text_raw)
        if not text_norm.strip():
            return False
        if entry.is_quoted:
            phrase = entry.words[0] if entry.words else ""
            pn = _normalize_for_match(phrase)
            return _fuzzy_phrase_in_text(pn, text_norm)
        for w in entry.words:
            wn = _normalize_for_match(w)
            if not wn:
                continue
            if not _fuzzy_word_in_text(wn, text_norm):
                return False
        return True

    def _locations_for_entry(
        self,
        entry: KeywordEntry,
        title: str,
        description: str,
        channel_name: str,
    ) -> list[str]:
        locs: list[str] = []
        tl = title or ""
        dl = description or ""
        cl = channel_name or ""

        def entry_in(raw: str) -> bool:
            return self._match_entry_in_text(entry, raw)

        mode = self.search_in
        if mode == "title":
            if entry_in(tl):
                return ["title"]
            return []
        if mode == "description":
            if entry_in(dl):
                return ["description"]
            return []
        if mode == "channel":
            if entry_in(cl):
                return ["channel"]
            return []
        if mode == "title+description":
            combined = f"{tl} {dl}"
            if not entry_in(combined):
                return []
            in_t = entry_in(tl)
            in_d = entry_in(dl)
            if in_t:
                locs.append("title")
            if in_d:
                locs.append("description")
            if not locs:
                locs.append("title+description")
            return list(dict.fromkeys(locs))
        if entry_in(tl):
            locs.append("title")
        if entry_in(dl):
            locs.append("description")
        if entry_in(cl):
            locs.append("channel")
        return list(dict.fromkeys(locs))

    def match(self, title: str, description: str, channel_name: str) -> dict:
        """
        Возвращает словарь с полями is_match, matched_keywords, match_locations.
        """
        matched_kw: list[str] = []
        all_locs: list[str] = []

        for entry in self._entries:
            locs = self._locations_for_entry(entry, title, description, channel_name)
            if locs:
                matched_kw.append(entry.raw)
                all_locs.extend(locs)

        uniq_locs = list(dict.fromkeys(all_locs))
        return {
            "is_match": len(matched_kw) > 0,
            "matched_keywords": matched_kw,
            "match_locations": uniq_locs,
        }

    def match_location_string(self, title: str, description: str, channel_name: str) -> str:
        """Строка для поля БД: где найдено совпадение."""
        r = self.match(title, description, channel_name)
        return ",".join(r["match_locations"]) if r["match_locations"] else ""

    def matched_keywords_csv(self, title: str, description: str, channel_name: str) -> str:
        """Ключевые слова через запятую для БД."""
        r = self.match(title, description, channel_name)
        return ", ".join(r["matched_keywords"])

    def api_queries(self) -> list[str]:
        """
        Строки для поля поиска API (по одной на каждую фразу пользователя).
        Нечёткое сопоставление выполняется при фильтрации выдачи, не здесь.
        """
        out: list[str] = []
        for e in self._entries:
            if e.is_quoted:
                if e.words:
                    out.append(e.words[0])
            else:
                out.append(" ".join(e.words))
        seen: set[str] = set()
        uniq: list[str] = []
        for q in out:
            q = q.strip()
            if q and q not in seen:
                seen.add(q)
                uniq.append(q)
        return uniq
