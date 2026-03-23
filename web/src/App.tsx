import { useCallback, useEffect, useMemo, useState } from "react";

const API = "/api";
const LS_KEY = "rd_dashboard_v1";
const VIDEO_PAGE_SIZE = 200;

function apiFetch(url: string, init?: RequestInit) {
  return fetch(url, { ...init, credentials: "include" });
}

function clampMaxResults(n: unknown): number {
  const x = typeof n === "number" && Number.isFinite(n) ? n : 500;
  return Math.max(1, Math.round(x));
}

function formatDuration(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec)) return "—";
  const s = Math.floor(sec);
  const m = Math.floor(s / 60);
  const r = s % 60;
  return `${m}:${r.toString().padStart(2, "0")}`;
}

function shgshCategoryRu(c: string | null | undefined): string | null {
  if (!c) return null;
  const m: Record<string, string> = {
    reaction: "Реакции / перезаливы",
    series: "Серии сериала",
    team: "Команда ШГШ",
  };
  return m[c] ?? c;
}

function formatApiError(data: unknown): string {
  if (!data || typeof data !== "object") return "Ошибка запроса";
  const d = (data as { detail?: unknown }).detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          const loc = (item as { loc?: unknown }).loc;
          const path = Array.isArray(loc) ? loc.join(".") : "";
          return path ? `${path}: ${String((item as { msg: unknown }).msg)}` : String((item as { msg: unknown }).msg);
        }
        return JSON.stringify(item);
      })
      .join("; ");
  }
  return "Ошибка запроса";
}

type DurationListFilter = "any" | "missing" | "present";

function migrateDurationFilter(v: unknown): DurationListFilter {
  if (v === "missing" || v === "present") return v;
  return "any";
}

type Persist = {
  keywords: string;
  vk: boolean;
  rutube: boolean;
  searchIn: string;
  maxResults: number;
  useGigachat: boolean;
  gigachatDuringScan: boolean;
  fetchWorkersStr: string;
  aiFilter: string;
  platFilter: string;
  sortBy: "found_date" | "upload_date" | "duration";
  sortOrder: "asc" | "desc";
  durationFilter: DurationListFilter;
  categoryFilter: "any" | "reaction" | "series" | "team";
};

type ProfileRow = {
  id: number;
  name: string;
  keywords: string;
  platforms: string[];
  search_in: string;
  max_results: number;
  gigachat: boolean;
  gigachat_during_scan: boolean;
};

function loadPersist(): Partial<Persist> | null {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return null;
    return JSON.parse(raw) as Partial<Persist>;
  } catch {
    return null;
  }
}

function savePersist(p: Persist) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(p));
  } catch {
    /* ignore */
  }
}

type Stats = {
  videos: number;
  channels: number;
  vk: number;
  rutube: number;
  ai_relevant: number;
  ai_pending: number;
};

type VideoRow = {
  id: number;
  platform: string;
  title: string;
  video_url: string;
  thumbnail_url: string | null;
  views: number | null;
  duration: number | null;
  channel_name: string;
  channel_url: string;
  matched_keywords: string;
  match_location?: string;
  ai_match: boolean | null;
  ai_note: string | null;
  ai_category?: string | null;
};

type ScanStatus = {
  running: boolean;
  paused?: boolean;
  message: string;
  error: string | null;
  last_stats: Record<string, unknown> | null;
};

type ExcelExportForm = {
  platform: "any" | "vk" | "rutube";
  ai: "any" | "yes" | "no" | "pending";
  ai_category: "any" | "reaction" | "series" | "team";
  duration_filter: "any" | "missing" | "present";
};

function excelFormFromSettings(raw: Record<string, unknown> | undefined): ExcelExportForm {
  const e = raw || {};
  const pl = e.platform;
  const ai = e.ai;
  const cat = e.ai_category;
  const dur = e.duration_filter;
  return {
    platform: pl === "vk" || pl === "rutube" ? pl : "any",
    ai: ai === "yes" || ai === "no" || ai === "pending" ? ai : "any",
    ai_category:
      cat === "reaction" || cat === "series" || cat === "team" ? cat : "any",
    duration_filter: dur === "missing" || dur === "present" ? dur : "any",
  };
}

function excelFormToServerPatch(f: ExcelExportForm): Record<string, string | null> {
  return {
    platform: f.platform === "any" ? null : f.platform,
    ai: f.ai === "any" ? null : f.ai,
    ai_category: f.ai_category === "any" ? null : f.ai_category,
    duration_filter: f.duration_filter === "any" ? null : f.duration_filter,
  };
}

function buildExportQuery(f: ExcelExportForm): string {
  const p = new URLSearchParams();
  if (f.platform !== "any") p.set("platform", f.platform);
  if (f.ai !== "any") p.set("ai", f.ai);
  if (f.ai_category !== "any") p.set("ai_category", f.ai_category);
  if (f.duration_filter !== "any") p.set("duration_filter", f.duration_filter);
  const s = p.toString();
  return s ? `?${s}` : "";
}

export default function App() {
  const saved = useMemo(() => (typeof window !== "undefined" ? loadPersist() : null), []);

  const [stats, setStats] = useState<Stats | null>(null);
  const [videos, setVideos] = useState<VideoRow[]>([]);
  const [scan, setScan] = useState<ScanStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [templateHint, setTemplateHint] = useState<string | null>(null);
  const [settingsOk, setSettingsOk] = useState<string | null>(null);

  const [authChecked, setAuthChecked] = useState(false);
  const [authUser, setAuthUser] = useState<{ id: number; login: string } | null>(null);
  const [dataProfiles, setDataProfiles] = useState<{ id: number; name: string }[]>([]);
  const [activeDataProfileId, setActiveDataProfileId] = useState<number | null>(null);
  const [authLogin, setAuthLogin] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [newDataProfileName, setNewDataProfileName] = useState("");

  const [listPage, setListPage] = useState(1);
  const [videoTotal, setVideoTotal] = useState(0);

  const [keywords, setKeywords] = useState(saved?.keywords ?? "школа, ШГШ");
  const [vk, setVk] = useState(saved?.vk ?? true);
  const [rutube, setRutube] = useState(saved?.rutube ?? true);
  const [searchIn, setSearchIn] = useState(saved?.searchIn ?? "all");
  const [maxResults, setMaxResults] = useState(() => clampMaxResults(saved?.maxResults));
  const [useGigachat, setUseGigachat] = useState(saved?.useGigachat ?? false);
  const [gigachatDuringScan, setGigachatDuringScan] = useState(saved?.gigachatDuringScan ?? false);
  const [fetchWorkersStr, setFetchWorkersStr] = useState(saved?.fetchWorkersStr ?? "");

  const [profiles, setProfiles] = useState<ProfileRow[]>([]);
  const [profileName, setProfileName] = useState("");
  const [selectedProfileId, setSelectedProfileId] = useState<number | "">("");

  const [aiFilter, setAiFilter] = useState(saved?.aiFilter ?? "any");
  const [platFilter, setPlatFilter] = useState(saved?.platFilter ?? "any");
  const [sortBy, setSortBy] = useState<"found_date" | "upload_date" | "duration">(saved?.sortBy ?? "found_date");
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">(saved?.sortOrder ?? "desc");
  const [durationFilter, setDurationFilter] = useState<DurationListFilter>(
    migrateDurationFilter(saved?.durationFilter),
  );
  const [categoryFilter, setCategoryFilter] = useState<"any" | "reaction" | "series" | "team">(
    saved?.categoryFilter ?? "any",
  );
  const [q, setQ] = useState("");

  const [tgToken, setTgToken] = useState("");
  const [tgChatId, setTgChatId] = useState("");
  const [tgReaction, setTgReaction] = useState(true);
  const [tgSeries, setTgSeries] = useState(true);
  const [tgTeam, setTgTeam] = useState(true);
  const [tgUnclassified, setTgUnclassified] = useState(false);

  const [monEnabled, setMonEnabled] = useState(false);
  const [monInterval, setMonInterval] = useState(60);
  const [monKeywords, setMonKeywords] = useState("");
  const [monVk, setMonVk] = useState(true);
  const [monRutube, setMonRutube] = useState(true);
  const [monSearchIn, setMonSearchIn] = useState("all");
  const [monMaxResults, setMonMaxResults] = useState(300);
  const [monGigachat, setMonGigachat] = useState(false);
  const [monGigachatDuring, setMonGigachatDuring] = useState(false);
  const [monDoExport, setMonDoExport] = useState(false);

  const [excelExport, setExcelExport] = useState<ExcelExportForm>(() => excelFormFromSettings(undefined));

  useEffect(() => {
    savePersist({
      keywords,
      vk,
      rutube,
      searchIn,
      maxResults,
      useGigachat,
      gigachatDuringScan,
      fetchWorkersStr,
      aiFilter,
      platFilter,
      sortBy,
      sortOrder,
      durationFilter,
      categoryFilter,
    });
  }, [
    keywords,
    vk,
    rutube,
    searchIn,
    maxResults,
    useGigachat,
    gigachatDuringScan,
    fetchWorkersStr,
    aiFilter,
    platFilter,
    sortBy,
    sortOrder,
    durationFilter,
    categoryFilter,
  ]);

  useEffect(() => {
    void (async () => {
      try {
        const r = await apiFetch(`${API}/config`);
        const j = (await r.json()) as {
          shgsh_keywords_template?: string;
          app_settings?: Record<string, unknown>;
        };
        if (j.shgsh_keywords_template) {
          setTemplateHint(
            j.shgsh_keywords_template.slice(0, 220) + (j.shgsh_keywords_template.length > 220 ? "…" : ""),
          );
        }
        const st = j.app_settings;
        if (st && typeof st === "object") {
          const tel = st.telegram as Record<string, unknown> | undefined;
          if (tel) {
            if (typeof tel.bot_token === "string") setTgToken(tel.bot_token);
            if (typeof tel.chat_id === "string") setTgChatId(tel.chat_id);
            if (typeof tel.notify_reaction === "boolean") setTgReaction(tel.notify_reaction);
            if (typeof tel.notify_series === "boolean") setTgSeries(tel.notify_series);
            if (typeof tel.notify_team === "boolean") setTgTeam(tel.notify_team);
            if (typeof tel.notify_unclassified === "boolean") setTgUnclassified(tel.notify_unclassified);
          }
          const mon = st.monitor as Record<string, unknown> | undefined;
          if (mon) {
            if (typeof mon.enabled === "boolean") setMonEnabled(mon.enabled);
            if (typeof mon.interval_minutes === "number") setMonInterval(Math.max(1, Math.round(mon.interval_minutes)));
            if (typeof mon.keywords === "string") setMonKeywords(mon.keywords);
            const pls = mon.platforms;
            if (Array.isArray(pls)) {
              setMonVk(pls.includes("vk"));
              setMonRutube(pls.includes("rutube"));
            }
            if (typeof mon.search_in === "string") setMonSearchIn(mon.search_in);
            if (typeof mon.max_results === "number") setMonMaxResults(clampMaxResults(mon.max_results));
            if (typeof mon.gigachat === "boolean") setMonGigachat(mon.gigachat);
            if (typeof mon.gigachat_during_scan === "boolean") setMonGigachatDuring(mon.gigachat_during_scan);
            if (typeof mon.do_export === "boolean") setMonDoExport(mon.do_export);
          }
          const xf = st.excel_export as Record<string, unknown> | undefined;
          setExcelExport(excelFormFromSettings(xf));
        }
      } catch {
        /* ignore */
      }
    })();
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        const r = await apiFetch(`${API}/me`);
        const j = (await r.json()) as {
          user: { id: number; login?: string; email?: string } | null;
          data_profile_id?: number | null;
          data_profiles?: { id: number; name: string }[];
        };
        if (j.user) {
          setAuthUser({
            id: j.user.id,
            login: j.user.login ?? j.user.email ?? "",
          });
          setDataProfiles(j.data_profiles ?? []);
          setActiveDataProfileId(j.data_profile_id ?? null);
        } else {
          setAuthUser(null);
          setDataProfiles([]);
          setActiveDataProfileId(null);
        }
      } catch {
        setAuthUser(null);
      } finally {
        setAuthChecked(true);
      }
    })();
  }, []);

  const loadProfiles = useCallback(async () => {
    if (!authUser) return;
    try {
      const r = await apiFetch(`${API}/profiles`);
      const j = (await r.json()) as { items?: ProfileRow[] };
      setProfiles(j.items ?? []);
    } catch {
      /* ignore */
    }
  }, [authUser]);

  useEffect(() => {
    void loadProfiles();
  }, [loadProfiles]);

  useEffect(() => {
    setListPage(1);
  }, [aiFilter, platFilter, durationFilter, categoryFilter, sortBy, sortOrder]);

  const refresh = useCallback(async () => {
    if (!authUser) return;
    try {
      const offset = (listPage - 1) * VIDEO_PAGE_SIZE;
      const vp = new URLSearchParams({
        limit: String(VIDEO_PAGE_SIZE),
        offset: String(offset),
      });
      if (aiFilter !== "any") vp.set("ai", aiFilter);
      if (platFilter !== "any") vp.set("platform", platFilter);
      if (durationFilter !== "any") vp.set("duration_filter", durationFilter);
      if (categoryFilter !== "any") vp.set("ai_category", categoryFilter);
      vp.set("sort", sortBy);
      vp.set("order", sortOrder);
      const [s, v, sc] = await Promise.all([
        apiFetch(`${API}/stats`).then((r) => r.json()),
        apiFetch(`${API}/videos?${vp.toString()}`).then((r) => r.json()),
        apiFetch(`${API}/scan/status`).then((r) => r.json()),
      ]);
      setStats(s);
      setVideos(v.items ?? []);
      setVideoTotal(typeof v.total === "number" ? v.total : 0);
      setScan(sc);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Ошибка сети");
    }
  }, [
    authUser,
    listPage,
    aiFilter,
    platFilter,
    durationFilter,
    categoryFilter,
    sortBy,
    sortOrder,
  ]);

  useEffect(() => {
    if (!authUser) return;
    void refresh();
  }, [refresh, authUser]);

  useEffect(() => {
    if (!authUser) return;
    const ms = scan?.running ? 500 : 3000;
    const t = window.setInterval(() => {
      void refresh();
    }, ms);
    return () => window.clearInterval(t);
  }, [refresh, scan?.running, authUser]);

  const patchSettings = async (body: Record<string, unknown> | object) => {
    setErr(null);
    setSettingsOk(null);
    const r = await apiFetch(`${API}/settings`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      setErr(formatApiError(j) || r.statusText);
      return false;
    }
    setSettingsOk("Настройки сохранены на сервере.");
    window.setTimeout(() => setSettingsOk(null), 3500);
    return true;
  };

  const saveTelegram = () =>
    void patchSettings({
      telegram: {
        bot_token: tgToken.trim(),
        chat_id: tgChatId.trim(),
        notify_reaction: tgReaction,
        notify_series: tgSeries,
        notify_team: tgTeam,
        notify_unclassified: tgUnclassified,
      },
    });

  const saveMonitor = () => {
    const p: string[] = [];
    if (monVk) p.push("vk");
    if (monRutube) p.push("rutube");
    void patchSettings({
      monitor: {
        enabled: monEnabled,
        interval_minutes: Math.max(1, Math.round(monInterval)),
        keywords: monKeywords.trim(),
        platforms: p.length ? p : ["vk", "rutube"],
        search_in: monSearchIn,
        max_results: clampMaxResults(monMaxResults),
        gigachat: monGigachat,
        gigachat_during_scan: monGigachatDuring,
        do_export: monDoExport,
      },
    });
  };

  const saveExcelExportFilters = () =>
    void patchSettings({ excel_export: excelFormToServerPatch(excelExport) });

  const testTelegram = async () => {
    setErr(null);
    const ok = await patchSettings({
      telegram: {
        bot_token: tgToken.trim(),
        chat_id: tgChatId.trim(),
        notify_reaction: tgReaction,
        notify_series: tgSeries,
        notify_team: tgTeam,
        notify_unclassified: tgUnclassified,
      },
    });
    if (!ok) return;
    setSettingsOk(null);
    const r = await apiFetch(`${API}/telegram/test`, { method: "POST" });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      setErr(formatApiError(j) || r.statusText);
      return;
    }
    setSettingsOk("Тест Telegram: сообщение отправлено.");
    window.setTimeout(() => setSettingsOk(null), 3500);
  };

  const startSearch = async () => {
    const platforms: string[] = [];
    if (vk) platforms.push("vk");
    if (rutube) platforms.push("rutube");
    if (!platforms.length) {
      setErr("Выберите хотя бы одну платформу");
      return;
    }
    const kw = keywords.trim();
    if (!kw) {
      setErr("Укажите хотя бы одно ключевое слово (или оставьте текст по умолчанию).");
      return;
    }
    const maxSafe = clampMaxResults(maxResults);
    setErr(null);
    let fetchWorkers: number | null = null;
    if (fetchWorkersStr.trim()) {
      const n = Number(fetchWorkersStr);
      if (Number.isFinite(n) && n >= 1 && n <= 32) fetchWorkers = Math.round(n);
    }
    const r = await apiFetch(`${API}/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        keywords: kw,
        platforms,
        search_in: searchIn,
        max_results: maxSafe,
        gigachat: useGigachat,
        gigachat_during_scan: gigachatDuringScan,
        fetch_workers: fetchWorkers,
      }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      setErr(formatApiError(j) || r.statusText);
      return;
    }
    void refresh();
  };

  const applyProfile = () => {
    const p = profiles.find((x) => x.id === selectedProfileId);
    if (!p) return;
    setKeywords(p.keywords);
    setVk(p.platforms.includes("vk"));
    setRutube(p.platforms.includes("rutube"));
    setSearchIn(p.search_in);
    setMaxResults(clampMaxResults(p.max_results));
    setUseGigachat(p.gigachat);
    setGigachatDuringScan(p.gigachat_during_scan);
  };

  const saveProfile = async () => {
    const name = profileName.trim();
    if (!name) {
      setErr("Введите имя профиля");
      return;
    }
    const platforms: string[] = [];
    if (vk) platforms.push("vk");
    if (rutube) platforms.push("rutube");
    const r = await apiFetch(`${API}/profiles`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        keywords,
        platforms,
        search_in: searchIn,
        max_results: maxResults,
        gigachat: useGigachat,
        gigachat_during_scan: gigachatDuringScan,
      }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      setErr(formatApiError(j) || r.statusText);
      return;
    }
    setErr(null);
    setProfileName("");
    void loadProfiles();
  };

  const scanPause = () => {
    void apiFetch(`${API}/search/pause`, { method: "POST" }).then(() => void refresh());
  };
  const scanResume = () => {
    void apiFetch(`${API}/search/resume`, { method: "POST" }).then(() => void refresh());
  };
  const scanStop = () => {
    void apiFetch(`${API}/search/stop`, { method: "POST" }).then(() => void refresh());
  };

  const copyText = async (text: string) => {
    const ok = () => {
      setSettingsOk("Скопировано в буфер.");
      window.setTimeout(() => setSettingsOk(null), 2000);
    };
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        ok();
        return;
      }
    } catch {
      /* fallback below */
    }
    try {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      const done = document.execCommand("copy");
      document.body.removeChild(ta);
      if (!done) throw new Error("execCommand failed");
      ok();
    } catch {
      setErr("Не удалось скопировать в буфер обмена.");
    }
  };

  const submitAuth = async () => {
    setErr(null);
    const path = authMode === "login" ? "/auth/login" : "/auth/register";
    const r = await apiFetch(`${API}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ login: authLogin.trim(), password: authPassword }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      setErr(formatApiError(j) || r.statusText);
      return;
    }
    const r2 = await apiFetch(`${API}/me`);
    const j2 = (await r2.json()) as {
      user: { id: number; login?: string; email?: string } | null;
      data_profiles?: { id: number; name: string }[];
      data_profile_id?: number | null;
    };
    if (j2.user) {
      setAuthUser({
        id: j2.user.id,
        login: j2.user.login ?? j2.user.email ?? "",
      });
      setDataProfiles(j2.data_profiles ?? []);
      setActiveDataProfileId(j2.data_profile_id ?? null);
    }
    setAuthPassword("");
  };

  const logout = async () => {
    await apiFetch(`${API}/auth/logout`, { method: "POST" });
    setAuthUser(null);
    setDataProfiles([]);
    setActiveDataProfileId(null);
    setVideos([]);
    setStats(null);
  };

  const createDataProfile = async () => {
    const name = newDataProfileName.trim();
    if (!name) {
      setErr("Введите имя профиля данных");
      return;
    }
    const r = await apiFetch(`${API}/data-profiles`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      setErr(formatApiError(j) || r.statusText);
      return;
    }
    setErr(null);
    setNewDataProfileName("");
    const r2 = await apiFetch(`${API}/me`);
    const j2 = (await r2.json()) as {
      data_profiles?: { id: number; name: string }[];
      data_profile_id?: number | null;
    };
    setDataProfiles(j2.data_profiles ?? []);
    setActiveDataProfileId(j2.data_profile_id ?? null);
    void refresh();
  };

  const selectDataProfile = async (id: number) => {
    const r = await apiFetch(`${API}/data-profiles/active`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: id }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      setErr(formatApiError(j) || r.statusText);
      return;
    }
    setErr(null);
    setActiveDataProfileId(id);
    setListPage(1);
    void refresh();
  };

  const exportQuery = useMemo(() => buildExportQuery(excelExport), [excelExport]);

  const filtered = videos.filter((v) => {
    if (!q.trim()) return true;
    const s = q.toLowerCase();
    return (
      v.title.toLowerCase().includes(s) ||
      v.channel_name.toLowerCase().includes(s) ||
      (v.matched_keywords || "").toLowerCase().includes(s)
    );
  });

  const totalPages = Math.max(1, Math.ceil(videoTotal / VIDEO_PAGE_SIZE));

  if (!authChecked) {
    return (
      <div className="app-shell">
        <p className="app-lead">Загрузка…</p>
      </div>
    );
  }

  if (!authUser) {
    return (
      <div className="app-shell">
        <header className="app-header auth-screen-header">
          <div>
            <h1>ReUpload Detector</h1>
            <p className="app-lead">Войдите или создайте аккаунт. Данные видео хранятся в выбранном профиле.</p>
          </div>
        </header>
        {err && <div className="app-alert app-alert--err">{err}</div>}
        <section className="panel auth-panel">
          <div className="auth-mode-switch" role="tablist" aria-label="Режим входа">
            <button
              type="button"
              role="tab"
              aria-selected={authMode === "login"}
              className={`auth-mode-tab${authMode === "login" ? " auth-mode-tab--active" : ""}`}
              onClick={() => {
                setAuthMode("login");
                setErr(null);
              }}
            >
              Вход
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={authMode === "register"}
              className={`auth-mode-tab${authMode === "register" ? " auth-mode-tab--active" : ""}`}
              onClick={() => {
                setAuthMode("register");
                setErr(null);
              }}
            >
              Регистрация
            </button>
          </div>
          <h2 className="auth-title">{authMode === "login" ? "Вход в аккаунт" : "Новый аккаунт"}</h2>
          <p className="auth-hint">
            {authMode === "login"
              ? "Введите логин и пароль. Если аккаунта нет — переключитесь на «Регистрация»."
              : "Придумайте логин и пароль не короче 8 символов."}
          </p>
          <div className="form-stack">
            <label className="field">
              <span className="field-label">Логин</span>
              <input
                className="input"
                value={authLogin}
                onChange={(e) => setAuthLogin(e.target.value)}
                autoComplete="username"
                placeholder="например admin или nickname"
              />
            </label>
            <label className="field">
              <span className="field-label">Пароль</span>
              <input
                type="password"
                className="input"
                value={authPassword}
                onChange={(e) => setAuthPassword(e.target.value)}
                autoComplete={authMode === "login" ? "current-password" : "new-password"}
                placeholder={authMode === "register" ? "не менее 8 символов" : ""}
              />
            </label>
            <button type="button" className="btn-primary auth-submit" onClick={() => void submitAuth()}>
              {authMode === "login" ? "Войти" : "Зарегистрироваться"}
            </button>
            <p className="panel-note">
              После миграции старой базы можно войти логином <code>admin@local</code>, пароль <code>admin123</code>.
            </p>
          </div>
        </section>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div>
          <h1>ReUpload Detector</h1>
          <p className="app-lead">
            Поиск перезаливов ШГШ: SQLite на сервере, интерфейс подхватывает новые ролики чаще во время сканирования.
            Настройки формы сохраняются в этом браузере.
          </p>
          {templateHint && (
            <p className="app-template-hint" title={templateHint}>
              К вашим ключам на сервере добавляется шаблон ШГШ (см. <code>SHGSH_KEYWORDS_TEMPLATE</code> в .env).
            </p>
          )}
        </div>
        <div className="app-user-bar">
          <span className="panel-note">{authUser.login}</span>
          <button type="button" className="btn-secondary" onClick={() => void logout()}>
            Выйти
          </button>
        </div>
      </header>

      {err && <div className="app-alert app-alert--err">{err}</div>}
      {settingsOk && <div className="app-alert">{settingsOk}</div>}

      <section className="stats-grid">
        <Stat label="Видео" value={stats?.videos ?? "—"} />
        <Stat label="Каналы" value={stats?.channels ?? "—"} />
        <Stat label="VK" value={stats?.vk ?? "—"} />
        <Stat label="Rutube" value={stats?.rutube ?? "—"} />
        <Stat label="GigaChat: релевантно" value={stats?.ai_relevant ?? "—"} accent />
        <Stat label="Без классификации ИИ" value={stats?.ai_pending ?? "—"} />
      </section>

      <div className="export-bar">
        <span className="export-bar-label">Экспорт базы:</span>
        <a className="export-link" href={`${API}/export/excel${exportQuery}`} download>
          Excel (.xlsx)
        </a>
        <a className="export-link" href={`${API}/export/html${exportQuery}`} download>
          HTML
        </a>
        <span className="export-bar-label" style={{ marginLeft: "0.5rem" }}>
          фильтры ниже → «Сохранить фильтры экспорта»
        </span>
      </div>

      {scan?.running && (
        <div className="app-alert app-alert--run scan-controls">
          <span className="pulse" />
          <span>
            {scan.paused ? "⏸ На паузе" : "Сканирование…"} {scan.message}
          </span>
          <div className="scan-btn-row">
            {!scan.paused ? (
              <button type="button" className="btn-secondary" onClick={scanPause}>
                Пауза
              </button>
            ) : (
              <button type="button" className="btn-secondary" onClick={scanResume}>
                Продолжить
              </button>
            )}
            <button type="button" className="btn-secondary btn-danger-outline" onClick={scanStop}>
              Остановить
            </button>
          </div>
        </div>
      )}
      {scan?.error && <pre className="app-trace">{scan.error}</pre>}

      <section className="panel">
        <h2>Telegram: новые видео</h2>
        <p className="panel-note">
          Укажите токен бота и ID чата (канал с ботом или личный чат). Уведомления приходят после классификации ИИ, если
          ролик подходит под отмеченные типы ШГШ.
        </p>
        <div className="form-stack">
          <label className="field">
            <span className="field-label">Токен бота</span>
            <input
              type="password"
              className="input"
              autoComplete="off"
              value={tgToken}
              onChange={(e) => setTgToken(e.target.value)}
              placeholder="123456:ABC..."
            />
          </label>
          <label className="field">
            <span className="field-label">ID чата</span>
            <input
              className="input"
              value={tgChatId}
              onChange={(e) => setTgChatId(e.target.value)}
              placeholder="-100… или числовой id"
            />
          </label>
          <div className="form-row">
            <label className="chk">
              <input type="checkbox" checked={tgReaction} onChange={(e) => setTgReaction(e.target.checked)} />
              Реакции / перезаливы
            </label>
            <label className="chk">
              <input type="checkbox" checked={tgSeries} onChange={(e) => setTgSeries(e.target.checked)} />
              Серии сериала
            </label>
            <label className="chk">
              <input type="checkbox" checked={tgTeam} onChange={(e) => setTgTeam(e.target.checked)} />
              Команда ШГШ
            </label>
            <label className="chk">
              <input type="checkbox" checked={tgUnclassified} onChange={(e) => setTgUnclassified(e.target.checked)} />
              Без классификации ИИ
            </label>
          </div>
          <div className="form-row">
            <button type="button" className="btn-secondary" onClick={() => void saveTelegram()}>
              Сохранить Telegram
            </button>
            <button type="button" className="btn-secondary" onClick={() => void testTelegram()}>
              Тест: отправить сообщение
            </button>
          </div>
        </div>
      </section>

      <section className="panel">
        <h2>Ожидание новых видео (фон по ключевым словам)</h2>
        <p className="panel-note">
          Периодический поиск по ключам из настроек ниже. Интервал — минуты между полными циклами. Пустые ключи — цикл
          пропускается.
        </p>
        <div className="form-stack">
          <label className="chk">
            <input type="checkbox" checked={monEnabled} onChange={(e) => setMonEnabled(e.target.checked)} />
            Включить мониторинг
          </label>
          <label className="field-inline">
            Интервал, мин
            <input
              type="number"
              min={1}
              max={10080}
              className="input-num"
              style={{ marginLeft: 8 }}
              value={monInterval}
              onChange={(e) => setMonInterval(Math.max(1, Number(e.target.value) || 1))}
            />
          </label>
          <label className="field">
            <span className="field-label">Ключевые слова (через запятую)</span>
            <textarea
              className="input"
              rows={2}
              value={monKeywords}
              onChange={(e) => setMonKeywords(e.target.value)}
              placeholder="школа, ШГШ"
            />
          </label>
          <div className="form-row">
            <label className="chk">
              <input type="checkbox" checked={monVk} onChange={(e) => setMonVk(e.target.checked)} /> VK
            </label>
            <label className="chk">
              <input type="checkbox" checked={monRutube} onChange={(e) => setMonRutube(e.target.checked)} /> Rutube
            </label>
            <label className="field-inline">
              Где искать{" "}
              <select value={monSearchIn} onChange={(e) => setMonSearchIn(e.target.value)} className="select">
                <option value="title">Название</option>
                <option value="description">Описание</option>
                <option value="channel">Канал</option>
                <option value="title+description">Название+описание</option>
                <option value="all">Везде</option>
              </select>
            </label>
            <label className="field-inline">
              Макс. результатов{" "}
              <input
                type="number"
                min={1}
                className="input-num"
                value={monMaxResults}
                onChange={(e) => setMonMaxResults(clampMaxResults(Number(e.target.value)))}
              />
            </label>
          </div>
          <div className="form-row">
            <label className="chk">
              <input type="checkbox" checked={monGigachat} onChange={(e) => setMonGigachat(e.target.checked)} />
              GigaChat после скана
            </label>
            <label className="chk">
              <input type="checkbox" checked={monGigachatDuring} onChange={(e) => setMonGigachatDuring(e.target.checked)} />
              GigaChat во время парса
            </label>
            <label className="chk">
              <input type="checkbox" checked={monDoExport} onChange={(e) => setMonDoExport(e.target.checked)} />
              После цикла — экспорт Excel/HTML
            </label>
          </div>
          <button type="button" className="btn-secondary" onClick={() => void saveMonitor()}>
            Сохранить мониторинг
          </button>
        </div>
      </section>

      <section className="panel">
        <h2>Импорт в Excel / HTML: какие видео попадают в файлы</h2>
        <p className="panel-note">
          Ниже задаётся подмножество строк на листе «Все видео» и в HTML-отчёте. Сохраните — ссылки «Экспорт базы»
          выше начнут использовать эти правила (и при экспорте без параметров в URL сервер подставит сохранённое).
        </p>
        <div className="form-row">
          <label className="field-inline">
            Платформа
            <select
              className="select"
              style={{ marginLeft: 8, minWidth: 140 }}
              value={excelExport.platform}
              onChange={(e) =>
                setExcelExport((x) => ({ ...x, platform: e.target.value as ExcelExportForm["platform"] }))
              }
            >
              <option value="any">Все</option>
              <option value="vk">Только VK</option>
              <option value="rutube">Только Rutube</option>
            </select>
          </label>
          <label className="field-inline">
            ИИ (ШГШ)
            <select
              className="select"
              style={{ marginLeft: 8, minWidth: 160 }}
              value={excelExport.ai}
              onChange={(e) => setExcelExport((x) => ({ ...x, ai: e.target.value as ExcelExportForm["ai"] }))}
            >
              <option value="any">Все</option>
              <option value="yes">Релевантно</option>
              <option value="no">Не релевантно</option>
              <option value="pending">Без классификации</option>
            </select>
          </label>
          <label className="field-inline">
            Тип ШГШ
            <select
              className="select"
              style={{ marginLeft: 8, minWidth: 180 }}
              value={excelExport.ai_category}
              onChange={(e) =>
                setExcelExport((x) => ({ ...x, ai_category: e.target.value as ExcelExportForm["ai_category"] }))
              }
            >
              <option value="any">Все</option>
              <option value="reaction">Реакции / перезаливы</option>
              <option value="series">Серии сериала</option>
              <option value="team">Команда ШГШ</option>
            </select>
          </label>
          <label className="field-inline">
            Длительность в БД
            <select
              className="select"
              style={{ marginLeft: 8, minWidth: 160 }}
              value={excelExport.duration_filter}
              onChange={(e) =>
                setExcelExport((x) => ({
                  ...x,
                  duration_filter: e.target.value as ExcelExportForm["duration_filter"],
                }))
              }
            >
              <option value="any">Все</option>
              <option value="missing">Нет длительности</option>
              <option value="present">Есть длительность</option>
            </select>
          </label>
          <button type="button" className="btn-primary" onClick={() => void saveExcelExportFilters()}>
            Сохранить фильтры экспорта
          </button>
        </div>
      </section>

      <section className="panel">
        <h2>Профиль данных (отдельная база видео)</h2>
        <p className="panel-note">
          Парс и экспорт идут в выбранный профиль. Новый профиль — пустая база; переключите перед сканированием.
        </p>
        <div className="form-row profile-row">
          <select
            className="select"
            value={activeDataProfileId ?? ""}
            onChange={(e) => void selectDataProfile(Number(e.target.value))}
          >
            {dataProfiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <input
            className="input"
            style={{ flex: "1 1 160px", maxWidth: 280 }}
            placeholder="Имя нового профиля данных"
            value={newDataProfileName}
            onChange={(e) => setNewDataProfileName(e.target.value)}
          />
          <button type="button" className="btn-secondary" onClick={() => void createDataProfile()}>
            Новый профиль данных
          </button>
        </div>
      </section>

      <section className="panel">
        <h2>Сценарии поиска (сохранённые ключи и платформы)</h2>
        <div className="form-row profile-row">
          <select
            className="select"
            value={selectedProfileId === "" ? "" : String(selectedProfileId)}
            onChange={(e) => setSelectedProfileId(e.target.value === "" ? "" : Number(e.target.value))}
          >
            <option value="">— выберите профиль —</option>
            {profiles.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <button type="button" className="btn-secondary" onClick={applyProfile}>
            Применить
          </button>
          <input
            className="input"
            style={{ flex: "1 1 160px", maxWidth: 280 }}
            placeholder="Имя нового профиля"
            value={profileName}
            onChange={(e) => setProfileName(e.target.value)}
          />
          <button type="button" className="btn-secondary" onClick={() => void saveProfile()}>
            Сохранить профиль
          </button>
        </div>
      </section>

      <section className="panel">
        <h2>Новый поиск</h2>
        <div className="form-stack">
          <label className="field">
            <span className="field-label">Ключевые слова (через запятую)</span>
            <textarea
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              rows={3}
              placeholder="школа, ШГШ, реакция"
              className="input"
            />
          </label>
          <div className="form-row">
            <label className="chk">
              <input type="checkbox" checked={vk} onChange={(e) => setVk(e.target.checked)} /> VK
            </label>
            <label className="chk">
              <input type="checkbox" checked={rutube} onChange={(e) => setRutube(e.target.checked)} /> Rutube
            </label>
            <label className="field-inline">
              Где искать{" "}
              <select value={searchIn} onChange={(e) => setSearchIn(e.target.value)} className="select">
                <option value="title">Название</option>
                <option value="description">Описание</option>
                <option value="channel">Канал</option>
                <option value="title+description">Название+описание</option>
                <option value="all">Везде</option>
              </select>
            </label>
            <label className="field-inline">
              Макс. результатов{" "}
              <input
                type="number"
                min={1}
                value={maxResults}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === "") {
                    setMaxResults(500);
                    return;
                  }
                  setMaxResults(clampMaxResults(Number(v)));
                }}
                className="input-num"
              />
            </label>
            <label className="chk">
              <input type="checkbox" checked={useGigachat} onChange={(e) => setUseGigachat(e.target.checked)} />
              GigaChat после скана
            </label>
            <label className="chk" title="Параллельно с загрузкой роликов (нужен ключ GigaChat в .env)">
              <input
                type="checkbox"
                checked={gigachatDuringScan}
                onChange={(e) => setGigachatDuringScan(e.target.checked)}
              />
              GigaChat во время парса
            </label>
            <label className="field-inline">
              Потоков API{" "}
              <input
                type="number"
                min={1}
                max={32}
                placeholder="авто"
                value={fetchWorkersStr}
                onChange={(e) => setFetchWorkersStr(e.target.value)}
                className="input-num"
                style={{ width: 72 }}
              />
            </label>
          </div>
          <button type="button" className="btn-primary" onClick={() => void startSearch()} disabled={scan?.running}>
            {scan?.running ? "Идёт сканирование…" : "Запустить сканирование"}
          </button>
        </div>
      </section>

      <div className="toolbar">
        <input
          type="search"
          placeholder="Фильтр по названию / каналу…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          className="input search-wide"
        />
        <select value={platFilter} onChange={(e) => setPlatFilter(e.target.value)} className="select">
          <option value="any">Все платформы</option>
          <option value="vk">Только VK</option>
          <option value="rutube">Только Rutube</option>
        </select>
        <select value={aiFilter} onChange={(e) => setAiFilter(e.target.value)} className="select">
          <option value="any">Все (ИИ)</option>
          <option value="yes">ИИ: релевантно ШГШ</option>
          <option value="no">ИИ: не релевантно</option>
          <option value="pending">Без классификации ИИ</option>
        </select>
        <select
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value as typeof categoryFilter)}
          className="select"
          title="Тип контента по классификации ШГШ"
        >
          <option value="any">Тип ШГШ: все</option>
          <option value="reaction">Реакции / перезаливы</option>
          <option value="series">Серии сериала</option>
          <option value="team">Команда ШГШ</option>
        </select>
        <select
          value={durationFilter}
          onChange={(e) => setDurationFilter(e.target.value as DurationListFilter)}
          className="select"
          title="Наличие длительности в базе (секунды с платформы)"
        >
          <option value="any">Длительность: все</option>
          <option value="missing">Нет длительности</option>
          <option value="present">Есть длительность</option>
        </select>
        <select value={sortBy} onChange={(e) => setSortBy(e.target.value as typeof sortBy)} className="select">
          <option value="found_date">Сортировка: дата найдено</option>
          <option value="upload_date">Сортировка: дата на платформе</option>
          <option value="duration">Сортировка: длительность</option>
        </select>
        <select value={sortOrder} onChange={(e) => setSortOrder(e.target.value as typeof sortOrder)} className="select">
          <option value="desc">По убыванию</option>
          <option value="asc">По возрастанию</option>
        </select>
      </div>

      {totalPages > 1 && (
        <div className="pagination-bar" style={{ display: "flex", flexWrap: "wrap", gap: "0.35rem", alignItems: "center" }}>
          <span className="panel-note" style={{ marginRight: "0.5rem" }}>
            Страницы ({videoTotal} видео):
          </span>
          {Array.from({ length: totalPages }, (_, i) => i + 1).map((p) => (
            <button
              key={p}
              type="button"
              className={p === listPage ? "btn-primary" : "btn-secondary"}
              style={{ minWidth: "2.5rem" }}
              onClick={() => {
                setListPage(p);
                window.scrollTo({ top: 0, behavior: "smooth" });
              }}
            >
              {p}
            </button>
          ))}
        </div>
      )}

      <section className="video-section">
        <h2>
          Видео на странице: {filtered.length}
          {q.trim() ? ` (отфильтровано из ${videos.length})` : ""} · всего в профиле: {videoTotal} · стр. {listPage} / {totalPages}
        </h2>
        <div className="video-grid">
          {filtered.map((v) => (
            <article key={v.id} className={`video-card${v.ai_match === true ? " video-card--ai" : ""}`}>
              <div className="video-thumb">
                {v.thumbnail_url ? (
                  <img src={v.thumbnail_url} alt="" loading="lazy" />
                ) : (
                  <div className="video-thumb-ph">Нет превью</div>
                )}
                <span className="badge-plat">{v.platform}</span>
                {v.ai_match === true && <span className="badge-ai">ШГШ</span>}
                {v.ai_match === true && v.ai_category && (
                  <span className={`badge-shgsh-cat badge-shgsh-cat--${v.ai_category}`}>
                    {shgshCategoryRu(v.ai_category) ?? v.ai_category}
                  </span>
                )}
              </div>
              <div className="video-body">
                <a href={v.video_url} target="_blank" rel="noreferrer" className="video-title">
                  {v.title}
                </a>
                <div className="video-copy-row">
                  <button type="button" className="btn-secondary" onClick={() => void copyText(v.video_url)}>
                    Копировать ссылку на видео
                  </button>
                  <button type="button" className="btn-secondary" onClick={() => void copyText(v.channel_url)}>
                    Копировать ссылку на канал
                  </button>
                </div>
                <div className="video-meta">{v.channel_name}</div>
                <div className="video-meta subtle">
                  👁 {v.views ?? "—"} · ⏱ {formatDuration(v.duration)}
                  {v.match_location ? ` · 📍 ${v.match_location}` : ""}
                  {v.matched_keywords ? ` · ${v.matched_keywords}` : ""}
                </div>
                {v.ai_note && <div className="video-ai">ИИ: {v.ai_note}</div>}
              </div>
            </article>
          ))}
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, accent }: { label: string; value: string | number; accent?: boolean }) {
  return (
    <div className={`stat-card${accent ? " stat-card--accent" : ""}`}>
      <div className="stat-value">{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}
