# -*- coding: utf-8 -*-
"""
plugin.video.rutube
Неофициальный Kodi-аддон для просмотра видео с Rutube.ru.

Использует те же публичные веб-эндпоинты, что и обычный плеер/сайт rutube.ru.
Rutube не предоставляет официального API для сторонних приложений, поэтому
структура ответов может измениться без предупреждения. Чтобы такие поломки
можно было быстро диагностировать и чинить, КАЖДЫЙ сетевой запрос подробно
логируется (см. LOG_TAG ниже) — включая URL, HTTP-статус и форму ответа.
Кроме того, в главном меню есть пункт "Диагностика", который сохраняет
сырые ответы API в файл для более глубокого разбора.

Как посмотреть лог:
  Kodi -> Настройки -> Система -> Журналирование -> включить "Debug logging"
  Дальше лог обычно лежит в:
    Windows: %APPDATA%/Kodi/kodi.log
    Linux:   ~/.kodi/temp/kodi.log  (или через journalctl для LibreELEC/CoreELEC)
    Android: /Android/data/org.xbmc.kodi/files/.kodi/temp/kodi.log
  Ищите строки, начинающиеся с "[plugin.video.rutube]".
"""

import sys
import json
import re
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.parse
import urllib.error

import xbmc
import xbmcgui
import xbmcplugin
import xbmcaddon
import xbmcvfs

ADDON = xbmcaddon.Addon()
ADDON_HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]

LOG_TAG = "[plugin.video.rutube]"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://rutube.ru/",
    "Accept": "application/json",
}

SEARCH_API = "https://rutube.ru/api/search/video/"
SEARCH_AUTHORS_API = "https://rutube.ru/api/v3/search/authors"
PERSON_API = "https://rutube.ru/api/video/person/{author_id}/"
PLAYLIST_USER_API = "https://rutube.ru/api/playlist/user/{author_id}/"
PLAYLIST_VIDEOS_API = "https://rutube.ru/api/playlist/custom/{playlist_id}/videos/"
# Просмотр ПО ВСЕМУ Rutube в рамках одной категории (не только один канал) —
# подсмотрено в старом плагине: там у каждого видео в контекстном меню есть
# переход "Каталог: <категория>", который даёт намного больше контента, чем
# каталог одного канала.
CATEGORY_API = "https://rutube.ru/api/video/category/{category_id}/"
# "Витрина" Rutube — та же лента готовых подборок, что и на главной странице
# сайта ("Топ сериалов", тематические карусели и т.д.). Найдено в старом
# стороннем аддоне: именно отсюда там гораздо более богатый и организованный
# список сериалов, а не из каталога одного канала.
FEED_MAIN_URL = "https://rutube.ru/pangolin/api/web/feed/new_main_page/?limit=100&sort=series_d"
PLAY_OPTIONS_API = "https://rutube.ru/api/play/options/{video_id}/"

# Сколько страниц выдачи максимум подгружать за один заход, чтобы собрать
# эпизоды одного сериала вместе перед группировкой. Больше страниц = полнее
# группировка, но больше запросов и дольше открывается список.
MAX_FETCH_PAGES = 5

# Когда пользователь уже кликнул на конкретный сериал (channel_series /
# search_series) — это осознанное действие по одному объекту, поэтому здесь
# не жалко покопать глубже, чтобы не пропустить серии с дальних страниц.
SERIES_LOOKUP_MAX_PAGES = 15

# Сколько страниц максимум читать, когда нужен ПОЛНЫЙ список серий сериала
# (плейлист или просмотр каталога канала в поисках всех серий).
SERIES_FULL_MAX_PAGES = 40
# Сколько страниц плейлистов канала читать при построении индекса, чтобы
# найти плейлист сериала по названию (у больших каналов их сотни).
PLAYLIST_INDEX_MAX_PAGES = 40

# /api/tags/popular/ подтверждённо отдаёт HTTP 404 (проверено по логам,
# 27.09.2026) — Rutube убрал этот эндпоинт. Поэтому категории строятся
# сразу на базе поиска, который точно работает.
CATEGORIES = [
    ("Фильмы", "фильм"),
    ("Сериалы", "сериал"),
    ("Мультфильмы", "мультфильм"),
    ("Документальное", "документальный фильм"),
    ("Юмор", "юмор"),
    ("Музыка", "музыка"),
    ("Спорт", "спорт"),
    ("Новости", "новости"),
]

# Заголовки вида "Название 9 серия ...", "Название 3 сезон 3 серия ...",
# "Название 9-я серия ...", "Название Серия 9 ...". Несколько вариантов
# формулировок, т.к. разные каналы на Rutube оформляют заголовки по-разному.
EPISODE_PATTERNS = [
    # Название [N сезон] N[-я] серия
    re.compile(
        r"^(?P<base>.+?)(?:[.,]?\s+(?P<season>\d{1,2})\s*сезон)?\s*[.,]?\s+"
        r"(?P<episode>\d{1,4})[-\s]*(?:я\s+)?(?:серия|серии|эпизод)\b",
        re.IGNORECASE,
    ),
    # Название Серия N / Название Эпизод N (слово перед числом)
    re.compile(
        r"^(?P<base>.+?)[.,]?\s+(?:серия|эпизод)\s*[№#]?\s*(?P<episode>\d{1,4})\b",
        re.IGNORECASE,
    ),
    # Название S01E09 (редко, но встречается у некоторых каналов)
    re.compile(
        r"^(?P<base>.+?)\s+[Ss](?P<season>\d{1,2})[Ee](?P<episode>\d{1,4})\b"
    ),
]


def _normalize_key(base):
    """Ключ для группировки: без учёта регистра и мелких различий в
    пунктуации/пробелах, чтобы 'Название:' и 'Название' не считались
    разными сериалами."""
    key = base.casefold()
    key = re.sub(r"[«»\"'.,:;!?()\-–—]", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    return key


if ADDON_HANDLE >= 0:
    xbmcplugin.setContent(ADDON_HANDLE, "videos")


# --------------------------------------------------------------- logging

def log(msg, level=xbmc.LOGINFO):
    xbmc.log("{0} {1}".format(LOG_TAG, msg), level)


def log_error(msg):
    log(msg, xbmc.LOGERROR)


def notify(msg):
    xbmcgui.Dialog().notification("Rutube", msg, xbmcgui.NOTIFICATION_ERROR, 4000)


def describe_shape(data):
    """Короткое текстовое описание формы JSON-ответа для логов."""
    if data is None:
        return "None"
    if isinstance(data, dict):
        return "dict, keys={0}".format(list(data.keys()))
    if isinstance(data, list):
        first = describe_shape(data[0]) if data else "empty"
        return "list, len={0}, first_item=({1})".format(len(data), first)
    return "{0}: {1}".format(type(data).__name__, repr(data)[:200])


def get_url(**kwargs):
    return "{0}?{1}".format(BASE_URL, urllib.parse.urlencode(kwargs))


# Небольшая пауза между запросами подряд (fetch_batch, поиск плейлиста и
# т.д.), чтобы не слать пачку запросов слишком часто и не словить троттлинг
# Rutube — это стало заметно после увеличения limit=100 и глубины просмотра.
REQUEST_DELAY_SECONDS = 0.5
# Сколько раз повторить запрос при HTTP 429/5xx (временная перегрузка/троттлинг
# на стороне Rutube), прежде чем сдаться.
MAX_RETRIES = 3


def http_get_json(url, context="", quiet=False, headers=None):
    """GET url, разобрать JSON, подробно залогировать каждый шаг.
    Повторяет попытку при HTTP 429/5xx — это обычно означает временный
    троттлинг на стороне Rutube, а не настоящую ошибку."""
    def _n(msg):
        if not quiet:
            notify(msg)

    for attempt in range(MAX_RETRIES + 1):
        log("--> [{0}] запрос (попытка {1}/{2}): {3}".format(
            context, attempt + 1, MAX_RETRIES + 1, url))
        req = urllib.request.Request(url, headers=headers or HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", errors="replace")
                log("<-- [{0}] HTTP {1}, {2} байт".format(context, status, len(raw)))
                try:
                    data = json.loads(raw)
                except ValueError as e:
                    log_error(
                        "[{0}] ответ не является JSON: {1}. Первые 300 символов: {2}".format(
                            context, e, raw[:300]
                        )
                    )
                    _n("Некорректный ответ сервера ({0})".format(context))
                    return None
                log("[{0}] форма ответа: {1}".format(context, describe_shape(data)))
                time.sleep(REQUEST_DELAY_SECONDS)
                return data
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            # HTTP 400 НЕ повторяем: диагностика показала, что это не троттлинг,
            # а честный отказ ("invalid value of SortEnum: series_d" — Rutube
            # убрал такое значение sort у плейлистов). Повтор лишь тратил ~7 с
            # на каждую страницу. fetch_batch сам переходит к запасному URL.
            retryable = e.code == 429 or e.code >= 500
            if retryable and attempt < MAX_RETRIES:
                wait = REQUEST_DELAY_SECONDS * (2 ** (attempt + 1))
                log_error(
                    "[{0}] HTTP {1} для {2} — похоже на троттлинг, "
                    "повтор через {3:.1f}с".format(context, e.code, url, wait)
                )
                time.sleep(wait)
                continue
            (log if quiet else log_error)("[{0}] HTTP {1} для {2}. Тело: {3}".format(context, e.code, url, body))
            _n("Ошибка сети ({0}: HTTP {1})".format(context, e.code))
            return None
        except urllib.error.URLError as e:
            log_error("[{0}] URLError для {1}: {2}".format(context, url, e))
            _n("Нет соединения с Rutube ({0})".format(context))
            return None
        except Exception as e:  # noqa: BLE001 - хотим залогировать вообще всё
            log_error("[{0}] непредвиденная ошибка для {1}: {2}".format(context, url, e))
            _n("Ошибка ({0})".format(context))
            return None
    return None


# ---------------------------------------------------------------- listings

# ------------------------------------------------------------------ история
# История просмотра и поиска хранится ТОЛЬКО на этом устройстве, в профиле аддона
# (history.json), и никуда не отправляется. Отключается в настройках аддона.
HISTORY_VIEWS_MAX = 100
HISTORY_SEARCH_MAX = 30
HISTORY_KINDS = ("views", "search", "channels")
LAST_PLAY_META = {}


def _history_enabled():
    return _setting_bool("save_history", True)


def _history_path():
    folder = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    if not xbmcvfs.exists(folder):
        xbmcvfs.mkdirs(folder)
    return os.path.join(folder, "history.json")


def _history_load():
    data = {"views": [], "search": [], "channels": []}
    try:
        path = _history_path()
        if xbmcvfs.exists(path):
            f = xbmcvfs.File(path)
            try:
                raw = f.read()
            finally:
                f.close()
            if isinstance(raw, (bytes, bytearray)):
                raw = bytes(raw).decode("utf-8", errors="replace")
            loaded = json.loads(raw) if raw else {}
            if isinstance(loaded, dict):
                for key in data:
                    if isinstance(loaded.get(key), list):
                        data[key] = loaded[key]
    except Exception as exc:  # noqa: BLE001
        log_error("history: не удалось прочитать историю: {0}".format(exc))
    return data


def _history_save(data):
    try:
        _write_text(_history_path(), json.dumps(data, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        log_error("history: не удалось сохранить историю: {0}".format(exc))


def history_add_search(kind, query):
    query = (query or "").strip()
    if kind not in ("search", "channels") or not query or not _history_enabled():
        return
    data = _history_load()
    items = [q for q in data[kind] if str(q).lower() != query.lower()]
    items.insert(0, query)
    data[kind] = items[:HISTORY_SEARCH_MAX]
    _history_save(data)


def record_view(video_id, title):
    """Запоминает просмотренное видео (вызывается, когда ссылка на поток уже получена).
    Любая ошибка здесь не должна мешать воспроизведению."""
    try:
        if not video_id or not _history_enabled():
            return
        try:
            duration = int(LAST_PLAY_META.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        data = _history_load()
        views = [v for v in data["views"] if v.get("id") != video_id]
        views.insert(0, {
            "id": video_id, "title": str(title or video_id),
            "thumb": LAST_PLAY_META.get("thumb") or "", "duration": duration,
            "ts": int(time.time()),
        })
        data["views"] = views[:HISTORY_VIEWS_MAX]
        _history_save(data)
    except Exception as exc:  # noqa: BLE001
        log_error("history: не удалось записать просмотр: {0}".format(exc))


def history_remove(kind, value):
    if kind not in HISTORY_KINDS or not value:
        return
    data = _history_load()
    if kind == "views":
        data["views"] = [v for v in data["views"] if v.get("id") != value]
    else:
        data[kind] = [q for q in data[kind] if q != value]
    _history_save(data)
    xbmc.executebuiltin("Container.Refresh")


def history_clear(kind):
    if kind not in HISTORY_KINDS:
        return
    what = "историю просмотра" if kind == "views" else "историю поиска"
    if not xbmcgui.Dialog().yesno("Rutube", "Очистить {0}?".format(what)):
        return
    data = _history_load()
    data[kind] = []
    _history_save(data)
    xbmc.executebuiltin("Container.Refresh")


def _action_item(label, icon, **params):
    """Пункт-действие (не папка и не видео): Kodi запускает плагин и ничего не воспроизводит."""
    li = xbmcgui.ListItem(label=label)
    li.setArt({"icon": icon})
    li.setProperty("IsPlayable", "false")
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(**params), li, False)


def list_history():
    views = _history_load()["views"]
    if not views:
        notify("История просмотра пуста" if _history_enabled()
               else "История отключена в настройках")
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    _action_item("Очистить историю просмотра", "DefaultIconError.png",
                 action="history_clear", kind="views")
    for v in views:
        video_id = v.get("id")
        if not video_id:
            continue
        title = v.get("title") or video_id
        thumb = v.get("thumb") or ""
        li = xbmcgui.ListItem(label=title)
        li.setArt({"thumb": thumb, "icon": thumb, "fanart": thumb})
        tag = li.getVideoInfoTag()
        tag.setTitle(title)
        try:
            when = time.strftime("%d.%m.%Y %H:%M", time.localtime(int(v.get("ts") or 0)))
            tag.setPlot("Просмотрено: {0}".format(when))
            if v.get("duration"):
                tag.setDuration(int(v["duration"]))
        except (TypeError, ValueError, OverflowError, OSError):
            pass
        li.setProperty("IsPlayable", "true")
        li.addContextMenuItems([(
            "Убрать из истории",
            "RunPlugin({0})".format(get_url(action="history_remove", kind="views", value=video_id)),
        )])
        xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="play", video_id=video_id), li, False)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_search_history(kind):
    """Экран «Поиск»: «Новый поиск» и последние запросы (kind: search | channels)."""
    queries = _history_load()[kind]
    li = xbmcgui.ListItem(label="Новый поиск" if kind == "search" else "Новый поиск канала")
    li.setArt({"icon": "DefaultAddonsSearch.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="search_new", kind=kind), li, True)
    for q in queries:
        li = xbmcgui.ListItem(label=str(q))
        li.setArt({"icon": "DefaultAddonsSearch.png"})
        li.addContextMenuItems([(
            "Убрать из истории поиска",
            "RunPlugin({0})".format(get_url(action="history_remove", kind=kind, value=q)),
        )])
        xbmcplugin.addDirectoryItem(
            ADDON_HANDLE, get_url(action="search_hist", kind=kind, query=q), li, True)
    _action_item("Очистить историю поиска", "DefaultIconError.png",
                 action="history_clear", kind=kind)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_main_menu():
    log("Открыто главное меню")

    try:
        if xbmcgui.Window(10000).getProperty(DL_PROP_ACTIVE) == "1":
            li = xbmcgui.ListItem(label="Остановить текущую загрузку")
            li.setArt({"icon": "DefaultIconError.png"})
            xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="download_cancel"), li, False)
    except Exception:  # noqa: BLE001
        pass

    li = xbmcgui.ListItem(label="Витрина Rutube (топ сериалов, подборки)")
    li.setArt({"icon": "DefaultAddonsUpdates.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="feed_tabs"), li, True)

    li = xbmcgui.ListItem(label="Мои каналы ТВ (свои номера)")
    li.setArt({"icon": "DefaultTVShows.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="tv_my"), li, True)

    li = xbmcgui.ListItem(label="ТВ: цифровые кнопки — {0}".format(
        "отключить" if xbmcvfs.exists(_tv_keymap_path()) else "включить"))
    li.setArt({"icon": "DefaultAddonsUpdates.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="tv_keys_toggle"), li, True)

    li = xbmcgui.ListItem(label="Поиск")
    li.setArt({"icon": "DefaultAddonsSearch.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="search"), li, True)

    li = xbmcgui.ListItem(label="Поиск каналов (например, Фильмач)")
    li.setArt({"icon": "DefaultActor.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="search_channels"), li, True)

    li = xbmcgui.ListItem(label="История просмотра")
    li.setArt({"icon": "DefaultRecentlyAddedMovies.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="history"), li, True)

    li = xbmcgui.ListItem(label="Открыть канал по ссылке / ID")
    li.setArt({"icon": "DefaultActor.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="open_channel"), li, True)

    li = xbmcgui.ListItem(label="Категории")
    li.setArt({"icon": "DefaultGenre.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="categories"), li, True)

    li = xbmcgui.ListItem(label="Открыть по ссылке / ID видео")
    li.setArt({"icon": "DefaultAddonsSearch.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="by_id"), li, True)

    li = xbmcgui.ListItem(label="Диагностика (для отладки)")
    li.setArt({"icon": "DefaultAddonsUpdates.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="diag"), li, True)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_categories():
    log("Категории: строю список из {0} готовых тем (без сетевого запроса)".format(
        len(CATEGORIES)))
    for name, query in CATEGORIES:
        url = get_url(action="search", query=query, page=1)
        li = xbmcgui.ListItem(label=name)
        li.setArt({"icon": "DefaultGenre.png"})
        xbmcplugin.addDirectoryItem(ADDON_HANDLE, url, li, True)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


# ------------------------------------------------------- fetch + group + sort

def parse_video_results(data, context=""):
    if not data:
        return [], None
    if not isinstance(data, dict):
        log_error("[{0}] ожидался dict с 'results', получено: {1}".format(
            context, describe_shape(data)))
        return [], None
    results = data.get("results")
    if results is None:
        log_error("[{0}] в ответе нет ключа 'results'. Ключи: {1}".format(
            context, list(data.keys())))
        results = []
    next_val = data.get("next")
    log("[{0}] найдено видео: {1}, есть_следующая_страница={2}, per_page={3}, count/num_pages={4}/{5}".format(
        context, len(results), bool(next_val), data.get("per_page"),
        data.get("count"), data.get("num_pages")))
    if results and isinstance(results[0], dict):
        log("[{0}] поля первого видео в ответе: {1}".format(context, list(results[0].keys())))
    return results, next_val


def _as_candidates(u):
    return [u] if isinstance(u, str) else list(u)


def _setting_bool(key, default=False):
    try:
        return bool(ADDON.getSettingBool(key))
    except Exception:  # noqa: BLE001
        return default


def _setting_int(key, default=0):
    try:
        return int(ADDON.getSettingInt(key))
    except Exception:  # noqa: BLE001
        return default


_FILTERS = {}
AGE_LIMITS = {1: 18, 2: 16, 3: 12}   # индекс настройки "Родительский фильтр" -> возраст


def _item_hidden(it):
    """Скрывать ли видео по настройкам: Shorts (origin_type=rshorts) и возрастной
    рейтинг (pg_rating.age / is_adult). Объекты без этих полей не затрагиваются."""
    if not isinstance(it, dict):
        return False
    if not _FILTERS:
        _FILTERS["shorts"] = _setting_bool("hide_shorts")
        _FILTERS["age"] = AGE_LIMITS.get(_setting_int("age_limit"))
    if _FILTERS["shorts"] and it.get("origin_type") == "rshorts":
        return True
    limit = _FILTERS["age"]
    if limit:
        pg = it.get("pg_rating")
        age = pg.get("age") if isinstance(pg, dict) else None
        if isinstance(age, int) and age >= limit:
            return True
        if it.get("is_adult"):
            return True
    return False


def fetch_batch(url_for_page, start_page, max_pages, context, on_page=None, silent=False):
    """Скачивает до max_pages страниц подряд начиная со start_page.
    url_for_page(p) может вернуть строку или СПИСОК запасных URL (пробуются
    по очереди, если основной даёт ошибку — так обходим HTTP 400 на
    капризных сочетаниях параметров). Для следующих страниц используется
    готовая ссылка "next" из ответа API (как в старом плагине), а не наша
    догадка о номере страницы.
    Возвращает (items, continue_page) — continue_page не None, если есть
    ещё страницы за пределами загруженных (для кнопки "Показать ещё")."""
    items = []
    seen = set()
    page = start_page
    last_page = start_page
    has_next = False
    next_url = None
    pref = 0
    for _ in range(max_pages):
        last_page = page
        cands = _as_candidates(url_for_page(page))
        order = list(range(len(cands)))
        if pref in order:
            order.remove(pref)
            order.insert(0, pref)
        attempts = []
        if next_url:
            attempts.append((None, next_url))
        attempts.extend((i, cands[i]) for i in order)
        ctx = "{0}:стр{1}".format(context, page)
        data = None
        for n, (idx, url) in enumerate(attempts):
            data = http_get_json(url, context=ctx, quiet=(silent or n < len(attempts) - 1))
            if data is not None:
                if idx is not None:
                    pref = idx
                break
        if data is None:
            has_next = False
            break
        results, next_val = parse_video_results(data, context=ctx)
        for r in results:
            if not isinstance(r, dict) or _item_hidden(r):
                continue
            rid = r.get("id")
            if rid is not None:
                if rid in seen:
                    continue
                seen.add(rid)
            items.append(r)
        if on_page:
            try:
                on_page(page)
            except Exception:  # noqa: BLE001
                pass
        has_next = bool(next_val)
        next_url = next_val if isinstance(next_val, str) and next_val.startswith("http") else None
        if not has_next:
            break
        page += 1
    # ВАЖНО: раньше тут было page + 1 после уже сделанного page += 1 — из-за
    # этого "Показать ещё" перескакивало через целую страницу (теряло ~40 видео).
    continue_page = last_page + 1 if has_next else None
    return items, continue_page


def dedupe_items(items):
    seen = set()
    out = []
    for it in items:
        rid = it.get("id") if isinstance(it, dict) else None
        if rid is not None:
            if rid in seen:
                continue
            seen.add(rid)
        out.append(it)
    return out


def is_series_like(items):
    """Похоже ли, что это серии одного сериала (а не подборка фильмов):
    у большинства заголовков распознаётся номер серии."""
    if not items:
        return False
    parsed = sum(1 for it in items if parse_episode_info(it.get("title") or "")[2] is not None)
    return parsed / float(len(items)) >= 0.5


def sort_episodes_ascending(items):
    """Серии по возрастанию. Если в списке несколько сериалов/мини-сериалов
    (плейлист канала НТВ: «Вербовщик», «Мост»... по 2 серии), серии НЕ
    перемешиваются: сначала все серии первого сюжета, потом второго и т.д.
    Сюжеты идут по дате публикации (старые первыми), серии внутри — по
    (сезон, номер). Видео без номера серии — в конце."""
    groups = {}
    loose = []
    for it in items:
        base, season, ep = parse_episode_info(it.get("title") or "")
        pub = it.get("publication_ts") or it.get("created_ts") or ""
        if ep is None:
            loose.append((pub, it))
            continue
        g = groups.setdefault(_normalize_key(base), {"first": pub, "eps": []})
        if pub and (not g["first"] or pub < g["first"]):
            g["first"] = pub
        g["eps"].append(((season or 0, ep, pub), it))
    out = []
    for key in sorted(groups, key=lambda k: (groups[k]["first"], k)):
        out.extend(it for _k, it in sorted(groups[key]["eps"], key=lambda t: t[0]))
    out.extend(it for _p, it in sorted(loose, key=lambda t: t[0]))
    return out


def sort_by_publication_desc(items):
    """Фильмы и прочее — от новых к старым."""
    return sorted(
        items, key=lambda it: it.get("publication_ts") or it.get("created_ts") or "",
        reverse=True,
    )


def parse_episode_info(title):
    """Пытается вытащить (базовое_название, номер_сезона, номер_серии) из
    заголовка видео. Если заголовок не похож на серию — episode будет None,
    и видео показывается как обычный отдельный ролик, без группировки."""
    if not title:
        return (title or "Без названия"), None, None
    title = title.strip()
    for pattern in EPISODE_PATTERNS:
        m = pattern.search(title)
        if not m:
            continue
        base = m.group("base").strip(" -–—:\u00ab\u00bb.,")
        if not base or len(base) > 120:
            # Слишком длинный/пустой "base" обычно значит, что регэксп
            # зацепился не за то место в заголовке — пропускаем такой матч.
            continue
        groupdict = m.groupdict()
        season = int(groupdict["season"]) if groupdict.get("season") else None
        episode = int(m.group("episode"))
        return base, season, episode
    return title, None, None


# Порядок показа категорий: сериалы и фильмы всегда первые (это основной
# сценарий использования), остальное — по алфавиту следом.
PREFERRED_CATEGORY_ORDER = ["Сериалы", "Фильмы"]


def _category_name(item):
    cat = item.get("category") if isinstance(item, dict) else None
    name = cat.get("name") if isinstance(cat, dict) else None
    return name or "Без категории"


def split_by_category(items):
    """Делит видео по полю category.name из ответа API (Сериалы/Фильмы/...).
    Это НАСТОЯЩЕЕ поле Rutube, а не наша догадка — гораздо надёжнее, чем
    пытаться отличить фильм от сериала по заголовку."""
    buckets = {}
    order = []
    for item in items:
        name = _category_name(item)
        if name not in buckets:
            buckets[name] = []
            order.append(name)
        buckets[name].append(item)

    def sort_key(name):
        if name in PREFERRED_CATEGORY_ORDER:
            return (0, PREFERRED_CATEGORY_ORDER.index(name))
        return (1, name)

    order.sort(key=sort_key)
    return order, buckets


def group_by_series(items):
    """Группирует список видео по названию сериала. Возвращает:
    order — порядок появления сериалов (нормализованные ключи),
    groups — {ключ: [(сезон, серия, item), ...]} отсортированные по возрастанию,
    display_names — {ключ: оригинальное_название_для_показа},
    standalone — видео без номера серии.

    Ключ группировки нормализован (без регистра/пунктуации), чтобы
    "Название:" и "Название" не считались разными сериалами из-за мелких
    различий в оформлении заголовка разными каналами."""
    groups = {}
    order = []
    display_names = {}
    standalone = []
    for item in items:
        title = item.get("title") or ""
        base, season, episode = parse_episode_info(title)
        if episode is None:
            standalone.append(item)
            continue
        key = _normalize_key(base)
        if key not in groups:
            groups[key] = []
            order.append(key)
            display_names[key] = base
        groups[key].append((season or 0, episode, item))
    for key in groups:
        groups[key].sort(key=lambda t: (t[0], t[1]))
    return order, groups, display_names, standalone


def add_single_video_item(item, label_prefix="", tv_channel=False):
    video_id = item.get("id")
    # Если в id лежит не настоящий 32-символьный ID видео (в подборках витрины
    # бывают числовые id), берём его из video_url.
    m_vu = re.search(r"/video/(?:private/)?([0-9a-fA-F]{32})", str(item.get("video_url") or ""))
    if m_vu and not re.fullmatch(r"[0-9a-fA-F]{32}", str(video_id or "")):
        video_id = m_vu.group(1)
    if item.get("is_deleted") or _item_hidden(item):
        return
    title = item.get("title") or "Без названия"
    if item.get("is_livestream"):
        title = "[LIVE] " + title
    # Подписочный контент (например, PREMIER; текстовая метка — не у всех есть шрифт
    # с эмодзи): is_paid при этом False, зато в элементе
    # есть непустой список common_subscription_product_codes. Подтверждено на двух
    # видео: с этим полем Rutube отвечает "нужна подписка", без него поток отдаётся.
    if item.get("common_subscription_product_codes"):
        title = "[Подписка] " + title
    thumb = item.get("thumbnail_url") or ""
    duration = item.get("duration") or 0
    description = item.get("description") or ""
    if not video_id:
        log_error("Пропущено видео без id: {0}".format(describe_shape(item)))
        return

    li = xbmcgui.ListItem(label=label_prefix + title)
    li.setArt({"thumb": thumb, "icon": thumb, "fanart": thumb})
    info_tag = li.getVideoInfoTag()
    info_tag.setTitle(title)
    info_tag.setPlot(description)
    if duration:
        try:
            info_tag.setDuration(int(duration))
        except (TypeError, ValueError):
            pass
    li.setProperty("IsPlayable", "true")

    # "Все видео канала" убрано из основного списка (раньше засоряло выдачу),
    # но осталось доступно через контекстное меню (долгое нажатие / ПКМ).
    # Ведёт на меню канала (Все видео / Плейлисты), а не сразу на плоский список.
    context_items = []
    author = item.get("author")
    if isinstance(author, dict) and author.get("id") is not None:
        channel_url = get_url(action="channel_menu", author_id=author["id"])
        author_name = author.get("name") or "канала"
        context_items.append(
            ("Канал: {0}".format(author_name), "Container.Update({0})".format(channel_url))
        )

    category = item.get("category")
    if isinstance(category, dict) and category.get("id") is not None:
        category_id = category["id"]
        category_name = category.get("name") or "категория"
        category_url = get_url(
            action="category_browse", category_id=category_id, category_name=category_name, page=1
        )
        context_items.append((
            "Вся категория на Rutube: {0}".format(category_name),
            "Container.Update({0})".format(category_url),
        ))

    if (tv_channel or item.get("is_livestream")) and re.fullmatch(r"[0-9a-fA-F]{32}", str(video_id or "")):
        mine = tv_custom_number_of(video_id)
        context_items.append((
            "Изменить номер канала…" if mine else "Назначить номер канала…",
            "RunPlugin({0})".format(get_url(action="tv_assign", video_id=video_id, name=item.get("title") or "")),
        ))
        if mine:
            context_items.append((
                "Снять номер канала",
                "RunPlugin({0})".format(get_url(action="tv_unassign", video_id=video_id)),
            ))
    if re.fullmatch(r"[0-9a-fA-F]{32}", str(video_id or "")):
        context_items.append((
            "Похожие видео",
            "Container.Update({0})".format(get_url(action="similar", video_id=video_id, title=title)),
        ))

    if re.fullmatch(r"[0-9a-fA-F]{32}", str(video_id or "")):
        context_items.append((
            "Комментарии",
            "RunPlugin({0})".format(get_url(action="comments", video_id=video_id, title=title)),
        ))

    # «Скачать видео»: только для обычных роликов — не для эфиров и не для платного
    # контента (подписка/покупка): их поток мы не загружаем.
    downloadable = not (
        item.get("is_livestream") or item.get("is_paid") or item.get("common_subscription_product_codes")
    )
    if downloadable and re.fullmatch(r"[0-9a-fA-F]{32}", str(video_id or "")):
        context_items.append((
            "Скачать видео",
            "RunPlugin({0})".format(get_url(action="download", video_id=video_id, title=title)),
        ))

    if context_items:
        li.addContextMenuItems(context_items)

    url = get_url(action="play", video_id=video_id)
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, url, li, False)


def render_grouped(order, groups, display_names, standalone, nav):
    """nav — словарь для случая, когда у эпизода нет данных об авторе:
    {'kind': 'search', 'query': ...} или {'kind': 'channel', 'author_id': ...}"""
    for key in order:
        episodes = groups[key]
        display_name = display_names.get(key, key)
        if len(episodes) == 1:
            # Единственная найденная серия — нет смысла делать папку.
            add_single_video_item(episodes[0][2])
            continue
        first_item = episodes[0][2]
        li = xbmcgui.ListItem(label="{0} ({1} серий)".format(display_name, len(episodes)))
        li.setArt({
            "icon": "DefaultVideoPlaylists.png",
            "thumb": first_item.get("thumbnail_url") or "",
        })

        # Поиск отдаёт не более 100 результатов по релевантности запроса —
        # это ЧАСТЬ каталога, а не весь сериал (это и было причиной "по 1-2
        # серии"). Полный список серий обычно лежит в каталоге канала автора,
        # поэтому переходим именно туда, если у эпизода есть данные об авторе.
        author = first_item.get("author") if isinstance(first_item.get("author"), dict) else None
        author_id = author.get("id") if author else None
        if author_id is None and nav["kind"] == "channel":
            author_id = nav.get("author_id")

        if author_id is not None:
            url = get_url(action="channel_series", author_id=author_id, series=key)
            li.addContextMenuItems([(
                "Добавить в медиатеку Kodi",
                "RunPlugin({0})".format(get_url(
                    action="library_add", kind="series", author_id=author_id, series=key, name=display_name)),
            )])
        else:
            # Резервный вариант — если у видео совсем нет данных об авторе.
            url = get_url(action="search_series", query=nav.get("query", key), series=key)
        xbmcplugin.addDirectoryItem(ADDON_HANDLE, url, li, True)

    for item in sort_by_publication_desc(standalone):
        add_single_video_item(item)


def add_continue_item(action, extra_params, continue_page):
    if not continue_page:
        return
    li = xbmcgui.ListItem(label=">> Показать ещё")
    li.setArt({"icon": "DefaultFolder.png"})
    params = dict(extra_params)
    params["action"] = action
    params["page"] = continue_page
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(**params), li, True)


def render_items_grouped(items, nav):
    """Группирует видео по сериалам (по названию) и выводит список."""
    order, groups, display_names, standalone = group_by_series(items)
    render_grouped(order, groups, display_names, standalone, nav)


def render_category_chooser(items, action, extra_params):
    """Если в выдаче есть видео разных категорий (Сериалы/Фильмы/...) —
    сначала показываем выбор категории, чтобы не мешать фильмы с сериалами
    в одной куче. Возвращает True, если показал выбор (и вызывающему больше
    ничего показывать не нужно), False — если категория всего одна и можно
    сразу показывать сгруппированный список."""
    cat_order, buckets = split_by_category(items)
    if len(cat_order) <= 1:
        return False
    for name in cat_order:
        count = len(buckets[name])
        li = xbmcgui.ListItem(label="{0} ({1})".format(name, count))
        li.setArt({"icon": "DefaultGenre.png"})
        params = dict(extra_params)
        params["action"] = action
        params["category"] = name
        params["page"] = 1
        xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(**params), li, True)
    return True


# ------------------------------------------------------------------ search

def _search_urls(query):
    def f(p):
        q = urllib.parse.quote(query)
        return [
            "{0}?query={1}&page={2}&limit=100&sort=series_d&format=json".format(SEARCH_API, q, p),
            "{0}?query={1}&page={2}&limit=100&format=json".format(SEARCH_API, q, p),
            "{0}?query={1}&page={2}".format(SEARCH_API, q, p),
        ]
    return f


def do_search(query, page, record=False):
    if not query:
        kb = xbmcgui.Dialog().input("Поиск на Rutube", type=xbmcgui.INPUT_ALPHANUM)
        if not kb:
            xbmcplugin.endOfDirectory(ADDON_HANDLE)
            return
        query = kb
        record = True
    if record and int(page) == 1:
        history_add_search("search", query)

    def url_for_page(p):
        # limit=100 — подсмотрено у другого rutube-аддона (Zeltorix), реально
        # увеличивает размер страницы (по умолчанию было ~20) и уменьшает
        # число запросов, нужных чтобы собрать все серии сериала.
        return _search_urls(query)(p)

    items, continue_page = fetch_batch(
        url_for_page, page, MAX_FETCH_PAGES, context="search:{0}".format(query)
    )
    if int(page) == 1:
        li = xbmcgui.ListItem(label="Каналы по запросу «{0}»".format(query))
        li.setArt({"icon": "DefaultActor.png"})
        xbmcplugin.addDirectoryItem(
            ADDON_HANDLE, get_url(action="search_channels", query=query, page=1), li, True
        )
    if not render_category_chooser(items, "search_category", {"query": query}):
        render_items_grouped(items, {"kind": "search", "query": query})
    add_continue_item("search", {"query": query}, continue_page)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def do_search_channels(query, page, record=False):
    """Поиск КАНАЛОВ (как в старом плагине): находим канал по названию и
    открываем его меню (Плейлисты / Все видео). Это самый короткий путь к
    большим каналам вроде «Фильмач» — через обычный поиск видео доступны
    только первые 100 совпадений по слову."""
    if not query:
        kb = xbmcgui.Dialog().input("Поиск канала на Rutube", type=xbmcgui.INPUT_ALPHANUM)
        if not kb:
            xbmcplugin.endOfDirectory(ADDON_HANDLE)
            return
        query = kb
        record = True
    if record and int(page) == 1:
        history_add_search("channels", query)

    def url_for_page(p):
        q = urllib.parse.quote(query)
        return [
            "{0}?query={1}&page={2}&limit=100".format(SEARCH_AUTHORS_API, q, p),
            "{0}?query={1}&page={2}".format(SEARCH_AUTHORS_API, q, p),
        ]

    items, continue_page = fetch_batch(
        url_for_page, page, MAX_FETCH_PAGES, context="search_channels:{0}".format(query)
    )
    if not items:
        notify("Каналы не найдены")
    for ch in items:
        cid = ch.get("id")
        name = ch.get("name") or ch.get("title") or "Без названия"
        if cid is None:
            continue
        li = xbmcgui.ListItem(label=str(name).replace("\n", " ").strip())
        picture = ch.get("picture") or ch.get("avatar_url") or ""
        li.setArt({"icon": picture or "DefaultActor.png", "thumb": picture})
        try:
            li.getVideoInfoTag().setPlot(str(ch.get("description") or ""))
        except Exception:  # noqa: BLE001
            pass
        xbmcplugin.addDirectoryItem(
            ADDON_HANDLE, get_url(action="channel_menu", author_id=cid), li, True
        )
    add_continue_item("search_channels", {"query": query}, continue_page)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def show_search_category(query, category, page):
    """Тот же поиск, но отфильтрованный по конкретной категории
    (пользователь уже выбрал 'Сериалы' или 'Фильмы' на предыдущем шаге)."""

    def url_for_page(p):
        return _search_urls(query)(p)

    items, continue_page = fetch_batch(
        url_for_page, page, SERIES_LOOKUP_MAX_PAGES, context="search_category:{0}".format(query)
    )
    filtered = [it for it in items if _category_name(it) == category]
    render_items_grouped(filtered, {"kind": "search", "query": query})
    add_continue_item("search_category", {"query": query, "category": category}, continue_page)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def show_search_series(query, series):
    def url_for_page(p):
        # limit=100 — подсмотрено у другого rutube-аддона (Zeltorix), реально
        # увеличивает размер страницы (по умолчанию было ~20) и уменьшает
        # число запросов, нужных чтобы собрать все серии сериала.
        return _search_urls(query)(p)

    items, _ = fetch_batch(
        url_for_page, 1, SERIES_LOOKUP_MAX_PAGES, context="search_series:{0}".format(query)
    )
    _, groups, _, _ = group_by_series(items)
    episodes = groups.get(series, [])
    log("search_series: '{0}' -> найдено серий: {1}".format(series, len(episodes)))
    if not episodes:
        notify("Серии не найдены — возможно, поменялись заголовки")
    for _season, _episode, item in episodes:
        add_single_video_item(item)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


# ----------------------------------------------------------------- channel

def list_channel_menu(author_id):
    """Точка входа в канал. Если у канала нет плейлистов (типично для
    личных каналов и блогеров) — сразу показываем ленту всех видео, без
    лишнего меню с пустым пунктом."""
    if not channel_has_playlists(author_id):
        list_channel(author_id, 1)
        return

    li = xbmcgui.ListItem(label="Плейлисты канала (точнее и полнее — начните отсюда)")
    li.setArt({"icon": "DefaultVideoPlaylists.png"})
    xbmcplugin.addDirectoryItem(
        ADDON_HANDLE, get_url(action="channel_playlists", author_id=author_id, page=1), li, True
    )

    li = xbmcgui.ListItem(label="Все видео канала (новые сверху)")
    li.setArt({"icon": "DefaultFolder.png"})
    xbmcplugin.addDirectoryItem(
        ADDON_HANDLE, get_url(action="channel", author_id=author_id, page=1), li, True
    )

    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def open_channel_by_link():
    """Пункт главного меню: открыть канал по ссылке (rutube.ru/channel/ID/...) или ID."""
    kb = xbmcgui.Dialog().input(
        "Ссылка на канал или его ID", type=xbmcgui.INPUT_ALPHANUM
    )
    if not kb:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    author_id = extract_channel_id(kb)
    if not author_id:
        notify("Не похоже на ссылку на канал (rutube.ru/channel/...)")
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    log("open_channel: '{0}' -> канал {1}".format(kb.strip(), author_id))
    list_channel_menu(author_id)


def list_channel_playlists(author_id, page):
    url_for_page = _playlists_urls(author_id)

    items, continue_page = fetch_batch(
        url_for_page, page, MAX_FETCH_PAGES, context="channel_playlists:{0}".format(author_id)
    )
    if not items:
        notify("Плейлисты не найдены — либо их нет, либо см. лог")
    for pl in items:
        playlist_id = pl.get("id")
        if playlist_id is None:
            continue
        title = pl.get("title") or pl.get("name") or "Без названия"
        thumb = pl.get("thumbnail_url") or ""
        li = xbmcgui.ListItem(label=str(title).replace("\n", " ").strip())
        li.setArt({"icon": "DefaultVideoPlaylists.png", "thumb": thumb})
        url = get_url(action="playlist_videos", playlist_id=playlist_id, page=1)
        li.addContextMenuItems([(
            "Добавить в медиатеку Kodi",
            "RunPlugin({0})".format(get_url(
                action="library_add", kind="playlist", id=playlist_id, name=str(title).replace("\n", " ").strip())),
        )])
        xbmcplugin.addDirectoryItem(ADDON_HANDLE, url, li, True)
    add_continue_item("channel_playlists", {"author_id": author_id}, continue_page)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def _playlists_urls(author_id):
    def f(p):
        base = PLAYLIST_USER_API.format(author_id=author_id)
        return [
            "{0}?page={1}&limit=100&sort=series_d&format=json".format(base, p),
            "{0}?page={1}&limit=100&format=json".format(base, p),
            "{0}?page={1}".format(base, p),
        ]
    return f


def _playlist_videos_urls(playlist_id):
    def f(p):
        base = PLAYLIST_VIDEOS_API.format(playlist_id=playlist_id)
        # sort=series_d здесь больше не принимается (HTTP 400) — не используем;
        # порядок серий всё равно выставляем сами по номерам из заголовков.
        return [
            "{0}?page={1}&limit=100&format=json".format(base, p),
            "{0}?page={1}".format(base, p),
        ]
    return f


def show_video_list_sorted(url_for_page, page, ctx, cont_action, cont_params):
    """Общий показ списка видео из плейлиста/ленты сериала. Если это сериал
    (у заголовков распознаются номера серий) — подгружаем ВСЕ страницы и
    показываем строго по возрастанию (сезон, серия). Если это подборка
    фильмов — от новых к старым, по страницам с кнопкой "Показать ещё"."""
    bg = None
    try:
        bg = xbmcgui.DialogProgressBG()
        bg.create("Rutube", "Загружаю список...")
    except Exception:  # noqa: BLE001
        bg = None

    def on_page(p):
        if bg:
            bg.update(min(95, int(p) * 5), "Rutube", "Загружено страниц: {0}".format(p))

    try:
        items, continue_page = fetch_batch(
            url_for_page, page, MAX_FETCH_PAGES, context=ctx, on_page=on_page
        )
        if items and is_series_like(items) and continue_page:
            more, continue_page = fetch_batch(
                url_for_page, continue_page, SERIES_FULL_MAX_PAGES,
                context=ctx + ":все", on_page=on_page
            )
            items = dedupe_items(items + more)
    finally:
        if bg:
            bg.close()

    if not items:
        notify("Видео не найдены — см. лог")
    if is_series_like(items):
        items = sort_episodes_ascending(items)
    else:
        items = sort_by_publication_desc(items)
    log("{0}: показываю {1} видео, продолжение со стр. {2}".format(ctx, len(items), continue_page))
    for item in items:
        add_single_video_item(item)
    add_continue_item(cont_action, cont_params, continue_page)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_playlist_videos(playlist_id, page):
    show_video_list_sorted(
        _playlist_videos_urls(playlist_id), page,
        "playlist_videos:{0}".format(playlist_id),
        "playlist_videos", {"playlist_id": playlist_id},
    )


def _with_param(url, key, value):
    """Подставляет/добавляет параметр key=value в готовую ссылку."""
    if re.search(r"[?&]{0}=".format(re.escape(key)), url):
        return re.sub(r"([?&]{0}=)[^&]*".format(re.escape(key)), r"\g<1>{0}".format(value), url)
    return "{0}{1}{2}={3}".format(url, "&" if "?" in url else "?", key, value)


def _with_page(url, p):
    """Подставляет номер страницы в готовую ссылку ленты."""
    if int(p) <= 1 and "page=" not in url:
        return url
    if "page=" in url:
        return re.sub(r"page=\d+", "page={0}".format(p), url)
    return "{0}{1}page={2}".format(url, "&" if "?" in url else "?", p)


# ------------------------------------------------------------- витрина (feed)

def _feed_normalize_url(url):
    """Приводит относительную ссылку ресурса витрины к полному API URL —
    та же логика нормализации, что в прообразе этой функции."""
    if url.startswith("/tags/video/") or url.startswith("/video/person/"):
        return "https://rutube.ru/api{0}?limit=100&sort=series_d".format(url)
    if url.startswith("/feeds/"):
        slug = url.split("/")[2]
        return "https://rutube.ru/pangolin/api/web/feed/{0}/?limit=100".format(slug)
    if "limit=100" in url:
        return url
    if "limit=" in url:
        return re.sub(r"limit=\d+", "limit=100", url)
    sep = "&" if "?" in url else "?"
    return "{0}{1}limit=100".format(url, sep)


_HEX32 = re.compile(r"[0-9a-fA-F]{32}")
# "target" и button.button_url — реальные поля карточек promogroup («МУЗЫКА», «ТВ-каналы»)
_CARD_URL_KEYS = ("target", "button_url", "url", "content", "link", "href", "target_url",
                  "api_url", "resource_url", "feed_url", "api_link")


def _looks_like_video(it):
    """Похож ли элемент подборки на настоящее видео (а не на карточку раздела)."""
    if _HEX32.fullmatch(str(it.get("id") or "")):
        return True
    if re.search(r"/video/(?:private/)?[0-9a-fA-F]{32}",
                 str(it.get("video_url") or it.get("embed_url") or "")):
        return True
    return bool(it.get("duration") and isinstance(it.get("author"), dict))


def _resolve_card_url(value):
    """Ссылка из карточки раздела. Возвращает адрес API (https://...), либо
    «сырой» путь (/channel/ID, /video/ID) для каналов и трансляций, либо None."""
    if not isinstance(value, str):
        return None
    path = re.sub(r"^https?://rutube\.ru", "", value.strip())
    if not path.startswith("/"):
        return None
    if path.startswith(("/api/", "/pangolin/api/")):
        return "https://rutube.ru" + path
    if path.startswith("/feeds/"):
        return _feed_normalize_url(path)
    if path.startswith(("/tags/", "/video/person/", "/video/category/", "/video/topic/", "/playlist/")):
        return "https://rutube.ru/api" + path
    if re.match(r"/(?:channel/\d+|(?:live/)?video/(?:private/)?[0-9a-fA-F]{32})", path):
        return path
    return None


def _card_target(it):
    """Куда ведёт карточка: известные поля, потом любые строки, потом вложенные
    словари на один уровень (например button)."""
    def scan(d):
        for k in _CARD_URL_KEYS:
            u = _resolve_card_url(d.get(k))
            if u:
                return u
        for v in d.values():
            u = _resolve_card_url(v)
            if u:
                return u
        return None

    found = scan(it)
    if found:
        return found
    for v in it.values():
        if isinstance(v, dict):
            found = scan(v)
            if found:
                return found
    return None


def list_feed_tabs(feed_url):
    """Вкладки витрины Rutube верхнего уровня (например, 'Кино и сериалы')."""
    data = http_get_json(feed_url, context="feed_tabs")
    feed = (data.get("result") or {}).get("feed") if isinstance(data, dict) else None
    tabs = (feed or {}).get("tabs") or []
    log("feed_tabs: вкладок={0}, названия={1}".format(len(tabs), [t.get("name") for t in tabs]))
    if not tabs:
        notify("Витрина недоступна или формат изменился — см. лог")
    for tab in tabs:
        slug = tab.get("slug")
        name = tab.get("name") or "Без названия"
        if not slug:
            continue
        li = xbmcgui.ListItem(label=name)
        li.setArt({"icon": "DefaultFolder.png"})
        url = get_url(action="feed_tab", slug=slug, feed_url=feed_url)
        xbmcplugin.addDirectoryItem(ADDON_HANDLE, url, li, True)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


# Эти id подборок на главной требуют авторизации либо ведут на сторонний
# сайт — не показываем их, как и в прообразе этой функции.
FEED_SKIP_RESOURCE_IDS = {54414, 53074, 54680}


def list_feed_tab(slug, feed_url):
    """Подборки (карусели) внутри одной вкладки витрины."""
    data = http_get_json(feed_url, context="feed_tab:{0}".format(slug))
    feed = (data.get("result") or {}).get("feed") if isinstance(data, dict) else None
    tabs = (feed or {}).get("tabs") or []
    resources = []
    for tab in tabs:
        if tab.get("slug") == slug:
            resources = tab.get("resources") or []
            break
    log("feed_tab '{0}': подборок={1}, названия={2}".format(
        slug, len(resources), [r.get("name") for r in resources]))
    if not resources:
        notify("Подборки не найдены — формат мог измениться, см. лог")
    for res in resources:
        if res.get("id") in FEED_SKIP_RESOURCE_IDS:
            continue
        name = res.get("name") or "Без названия"
        url_raw = res.get("url") or ""
        if not url_raw:
            continue
        # Баннеры (widget_type=banner-*) — это ссылка на главную страницу сайта
        # (https://rutube.ru?widget_type=...), в ответ приходит HTML на ~1,3 МБ,
        # а не JSON со списком видео. Папка всё равно не откроется — не показываем.
        if "widget_type=banner" in url_raw:
            log("feed_tab '{0}': пропускаю баннер '{1}' ({2})".format(slug, name, url_raw))
            continue
        li = xbmcgui.ListItem(label=name)
        li.setArt({"icon": "DefaultVideoPlaylists.png"})
        nav_url = get_url(
            action="feed_resource", resource_url=_feed_normalize_url(url_raw), title=name
        )
        xbmcplugin.addDirectoryItem(ADDON_HANDLE, nav_url, li, True)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def add_continue_url_item(action, extra_params, next_url):
    """Кнопка "Показать ещё", которая несёт готовую ссылку next от API
    (а не номер страницы, который мы угадываем сами)."""
    li = xbmcgui.ListItem(label=">> Показать ещё")
    li.setArt({"icon": "DefaultFolder.png"})
    params = dict(extra_params)
    params["action"] = action
    params["resource_url"] = next_url
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(**params), li, True)


def list_feed_resource(resource_url, title, page=None, tvstart=0):
    """Содержимое одной подборки витрины. Форма ответа заранее не известна
    (это может быть обычный список видео, ещё один уровень вложенных
    подборок, или ещё одна витрина с вкладками) — поэтому разбираем
    защитно и подробно логируем, что реально пришло.
    resource_url используется КАК ЕСТЬ (в том числе ссылка next из ответа
    API) — страницу сами не подставляем."""
    url = resource_url
    if page and int(page) > 1:
        # совместимость со старыми ссылками, где передавался номер страницы
        if "page=" in url:
            url = re.sub(r"page=\d+", "page={0}".format(page), url)
        else:
            url = "{0}{1}page={2}".format(url, "&" if "?" in url else "?", page)

    # Подборка-плейлист — это сериал/коллекция: показываем целиком по порядку.
    m = re.search(r"/playlist/custom/(\d+)", url)
    if m:
        list_playlist_videos(m.group(1), 1)
        return

    # Лента конкретного сериала/передачи (metainfo/tv) — тоже целиком по порядку.
    if re.search(r"/api/metainfo/(?:tv|person)/\d+/video", resource_url):
        show_video_list_sorted(
            lambda p: [_with_page(resource_url, p)], int(page or 1),
            "feed_series:{0}".format(title),
            "feed_resource", {"resource_url": resource_url, "title": title},
        )
        return

    data = http_get_json(url, context="feed_resource:{0}".format(title))
    if not isinstance(data, dict):
        notify("Не удалось загрузить подборку — см. лог")
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    results = data.get("results")
    next_val = data.get("next")
    pagination = data.get("pagination")
    if not next_val and isinstance(pagination, dict):
        log("feed_resource '{0}': pagination={1}".format(
            title, json.dumps(pagination, ensure_ascii=False)[:300]))
        next_val = pagination.get("next") or pagination.get("next_url")
        if not next_val:
            # формат {"offset": 0, "limit": 100, "total": 12}
            off, lim, tot = (pagination.get(k) for k in ("offset", "limit", "total"))
            if all(isinstance(x, int) for x in (off, lim, tot)) and lim > 0 and off + lim < tot:
                next_val = _with_param(url, "offset", off + lim)
    next_url = next_val if isinstance(next_val, str) and next_val.startswith("http") else None
    if isinstance(results, list) and results and isinstance(results[0], dict):
        log("feed_resource '{0}': первый элемент: {1}".format(
            title, json.dumps(results[0], ensure_ascii=False)[:700]))

    # Карточки разделов (promoitem: жанры в «Музыке», «ТВ-каналы»...): это НЕ видео,
    # а ссылки на другие подборки (поле target: /tags/video/3230/, /feeds/friday/ ...),
    # каналы или трансляции. Раньше их пытались проиграть по числовому id, и Rutube
    # отвечал «видео не существует» / «автор скрыл видео».
    # Элементы вида {"object": {"name": ..., "content": ...}} (RUTUBE x PREMIER,
    # «Рекомендуем сериалы») — это вложенные подборки, их разбирает «Вариант 2» ниже.
    if isinstance(results, list) and results and isinstance(results[0], dict) \
            and not isinstance(results[0].get("object"), dict):
        dicts = [r for r in results if isinstance(r, dict)]
        videos = [r for r in dicts if _looks_like_video(r)]
        cards = [(r, _card_target(r)) for r in dicts
                 if not _looks_like_video(r) and r.get("activity_status", "active") == "active"]
        cards = [(r, t) for r, t in cards if t]
        if cards and len(cards) >= len(videos):
            log("feed_resource '{0}': карточки разделов: {1}, видео: {2}".format(
                title, len(cards), len(videos)))
            live_cards = sum(1 for _r, t in cards
                             if t.startswith("/") and not re.match(r"/channel/", t))
            tv_mode = live_cards >= 3
            tv_n, tv_channels = (int(tvstart or 0), tv_load_channels() if tvstart else [])
            if tv_mode:
                add_tv_dial_item()
            for r, target in cards:
                name = str(r.get("title") or r.get("name") or "Без названия").replace("\n", " ").strip()
                pic = r.get("picture") or r.get("picture_url") or r.get("thumbnail_url") or ""
                pic = pic if isinstance(pic, str) else ""
                if target.startswith("/"):
                    mc = re.match(r"/channel/(\d+)", target)
                    if mc:
                        li = xbmcgui.ListItem(label=name)
                        li.setArt({"icon": "DefaultFolder.png", "thumb": pic})
                        xbmcplugin.addDirectoryItem(
                            ADDON_HANDLE, get_url(action="channel_menu", author_id=mc.group(1)), li, True)
                    else:
                        mv = _HEX32.search(target)
                        if tv_mode and tv_custom_load():
                            num = tv_custom_number_of(mv.group(0))
                            add_single_video_item({"id": mv.group(0), "title": name, "thumbnail_url": pic},
                                                  label_prefix="{0}. ".format(num) if num else "", tv_channel=True)
                        elif tv_mode:
                            tv_n += 1
                            tv_channels.append([tv_n, mv.group(0), name])
                            add_single_video_item({"id": mv.group(0), "title": name, "thumbnail_url": pic},
                                                  label_prefix="{0}. ".format(tv_n), tv_channel=True)
                        else:
                            add_single_video_item({"id": mv.group(0), "title": name, "thumbnail_url": pic})
                    continue
                li = xbmcgui.ListItem(label=name)
                li.setArt({"icon": "DefaultFolder.png", "thumb": pic})
                xbmcplugin.addDirectoryItem(
                    ADDON_HANDLE,
                    get_url(action="feed_resource", resource_url=_feed_normalize_url(target), title=name),
                    li, True,
                )
            for r in videos:
                add_single_video_item(r)
            if tv_mode and not tv_custom_load():
                tv_save_channels(tv_channels)
            if next_url:
                add_continue_url_item("feed_resource",
                                      {"title": title, **({"tvstart": tv_n} if tv_mode else {})}, next_url)
            xbmcplugin.endOfDirectory(ADDON_HANDLE)
            return
        if not videos and not cards:
            log_error("feed_resource '{0}': элементы не похожи ни на видео, ни на ссылки на разделы "
                      "(первый: {1})".format(title, json.dumps(dicts[0], ensure_ascii=False)[:500]))

    # Вариант 1: обычный список видео Rutube (как в поиске/канале/плейлисте).
    if isinstance(results, list) and results and isinstance(results[0], dict) \
            and "title" in results[0] and "id" in results[0]:
        items = [r for r in results if isinstance(r, dict)]
        log("feed_resource '{0}': список видео, {1} шт., next={2}".format(
            title, len(items), bool(next_url)))
        extra = {"title": title}
        if _is_tv_list(items, title):
            extra["tvstart"] = render_tv_list(items, tvstart)
        else:
            render_items_grouped(items, {"kind": "feed", "resource_url": resource_url})
        if next_url:
            add_continue_url_item("feed_resource", extra, next_url)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # Вариант 2: ещё один уровень вложенных именованных подборок.
    if isinstance(results, list) and results and isinstance(results[0], dict) \
            and isinstance(results[0].get("object"), dict):
        log("feed_resource '{0}': вложенные подборки, {1} шт., next={2}".format(
            title, len(results), bool(next_url)))
        for item in results:
            obj = item.get("object") or {}
            name = obj.get("name") or "Без названия"
            content_url = obj.get("content")
            if not content_url:
                continue
            li = xbmcgui.ListItem(label=name)
            li.setArt({"icon": "DefaultVideoPlaylists.png"})
            if re.search(r"/metainfo/tv/\d+/video", content_url):
                li.addContextMenuItems([(
                    "Добавить в медиатеку Kodi",
                    "RunPlugin({0})".format(get_url(
                        action="library_add", kind="feed", url=_feed_normalize_url(content_url), name=name)),
                )])
            nav_url = get_url(
                action="feed_resource", resource_url=_feed_normalize_url(content_url), title=name
            )
            xbmcplugin.addDirectoryItem(ADDON_HANDLE, nav_url, li, True)
        # Раньше здесь не было кнопки следующей страницы — поэтому витрина
        # показывала только первую страницу сериалов.
        if next_url:
            add_continue_url_item("feed_resource", {"title": title}, next_url)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    # Вариант 3: это ещё одна витрина с вкладками.
    if (((data.get("result") or {}).get("feed") or {}).get("tabs")):
        log("feed_resource '{0}': это вложенная витрина с вкладками".format(title))
        list_feed_tabs(resource_url)
        return

    log_error("feed_resource '{0}': неизвестная форма ответа: {1}".format(
        title, describe_shape(data)))
    notify("Неизвестный формат подборки — см. лог")
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_category_browse(category_id, category_name, page):
    """Все видео ОДНОЙ категории ПО ВСЕМУ Rutube (не ограничено одним
    каналом) — даёт намного больше контента, чем каталог одного канала.
    Группировка по сериалам работает так же, как и везде."""

    def url_for_page(p):
        return "{0}?page={1}&limit=100&sort=series_d&format=json".format(
            CATEGORY_API.format(category_id=category_id), p
        )

    items, continue_page = fetch_batch(
        url_for_page, page, MAX_FETCH_PAGES, context="category_browse:{0}".format(category_id)
    )
    if not items:
        notify("Видео этой категории не найдены — см. лог")
    if category_name:
        xbmcplugin.setPluginCategory(ADDON_HANDLE, "Категория: {0}".format(category_name))
    render_items_grouped(items, {"kind": "category", "category_id": category_id})
    add_continue_item(
        "category_browse", {"category_id": category_id, "category_name": category_name},
        continue_page,
    )
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def list_channel(author_id, page):
    """Все видео одного канала/автора. Если в выдаче смешаны разные
    категории (Сериалы/Фильмы/...) — сначала предлагаем выбрать категорию,
    чтобы не сваливать всё в одну кучу; внутри категории — группировка по
    названию сериала, отсортированная по возрастанию номера серии."""

    def url_for_page(p):
        base = PERSON_API.format(author_id=author_id)
        return [
            "{0}?page={1}&limit=100&sort=publication_d&format=json".format(base, p),
            "{0}?page={1}&limit=100&format=json".format(base, p),
        ]

    items, continue_page = fetch_batch(
        url_for_page, page, MAX_FETCH_PAGES, context="channel:{0}".format(author_id)
    )
    if not items:
        notify("Видео канала не найдены — см. лог")

    if int(page) == 1 and items:
        name = (items[0].get("author") or {}).get("name") if isinstance(items[0].get("author"), dict) else None
        if name:
            xbmcplugin.setPluginCategory(ADDON_HANDLE, "Канал: {0}".format(name))

    # Ссылка на плейлисты — только если они у канала есть (раньше вела в пустоту).
    if int(page) == 1 and channel_has_playlists(author_id):
        li = xbmcgui.ListItem(label="Плейлисты канала (точнее и полнее, чем список ниже)")
        li.setArt({"icon": "DefaultVideoPlaylists.png"})
        xbmcplugin.addDirectoryItem(
            ADDON_HANDLE,
            get_url(action="channel_playlists", author_id=author_id, page=1),
            li, True,
        )

    if looks_like_media_catalog(items):
        # Канал с фильмами/сериалами: выбор категории и группировка по сериям.
        nav = {"kind": "channel", "author_id": author_id}
        if not render_category_chooser(items, "channel_category", {"author_id": author_id}):
            render_items_grouped(items, nav)
    else:
        # Обычный канал: простая лента, как на сайте — все видео, новые сверху.
        for item in items:
            add_single_video_item(item)
    add_continue_item("channel", {"author_id": author_id}, continue_page)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


def show_channel_category(author_id, category, page):
    """Видео канала, отфильтрованные по конкретной категории (пользователь
    уже выбрал 'Сериалы' или 'Фильмы' на предыдущем шаге)."""

    def url_for_page(p):
        base = PERSON_API.format(author_id=author_id)
        return [
            "{0}?page={1}&limit=100&sort=publication_d&format=json".format(base, p),
            "{0}?page={1}&limit=100&format=json".format(base, p),
        ]

    items, continue_page = fetch_batch(
        url_for_page, page, MAX_FETCH_PAGES,
        context="channel_category:{0}".format(author_id)
    )
    filtered = [it for it in items if _category_name(it) == category]
    render_items_grouped(filtered, {"kind": "channel", "author_id": author_id})
    add_continue_item(
        "channel_category", {"author_id": author_id, "category": category}, continue_page
    )
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


# Заголовки плейлистов Rutube обычно оформлены как "Название (сериал, 2026)"
# или "Название - сезон 4" — отрезаем эти хвосты, чтобы получить чистое
# название сериала и сравнить его с "base" из parse_episode_info.
PLAYLIST_TITLE_SUFFIX_RE = re.compile(r"\s*\([^()]*\)\s*$")
PLAYLIST_SEASON_TAIL_RE = re.compile(
    r"\s*[-–—:,.]?\s*(?:\d+\s*)?(?:сезон\w*|мини-сериал).*$", re.IGNORECASE
)
PLAYLIST_SEASON_NUM_RE = re.compile(r"(\d+)\s*сезон|сезон\s*(\d+)", re.IGNORECASE)
PLAYLIST_INDEX_TTL = 3600  # секунд


def playlist_series_key(title):
    t = PLAYLIST_TITLE_SUFFIX_RE.sub("", title or "").strip()
    t = PLAYLIST_SEASON_TAIL_RE.sub("", t).strip()
    return _normalize_key(t)


def playlist_season_number(title):
    m = PLAYLIST_SEASON_NUM_RE.search(title or "")
    if not m:
        return 0
    return int(m.group(1) or m.group(2))


def get_playlist_index(author_id):
    """Список всех плейлистов канала [(id, title), ...]. Читается до
    PLAYLIST_INDEX_MAX_PAGES страниц и кэшируется на час в свойстве окна Kodi
    (между запусками аддона в рамках одной сессии) — иначе при каждом
    открытии сериала пришлось бы заново читать десятки страниц."""
    key = "rutube.playlists.{0}".format(author_id)
    win = None
    try:
        win = xbmcgui.Window(10000)
        raw = win.getProperty(key)
        if raw:
            obj = json.loads(raw)
            if time.time() - obj.get("ts", 0) < PLAYLIST_INDEX_TTL:
                log("get_playlist_index: из кэша, {0} шт.".format(len(obj["items"])))
                return obj["items"]
    except Exception as e:  # noqa: BLE001
        log("get_playlist_index: кэш недоступен: {0}".format(e))

    bg = None
    try:
        bg = xbmcgui.DialogProgressBG()
        bg.create("Rutube", "Ищу плейлист сериала...")
    except Exception:  # noqa: BLE001
        bg = None

    def on_page(p):
        if bg:
            bg.update(min(95, int(p) * 3), "Rutube", "Просмотрено страниц плейлистов: {0}".format(p))

    try:
        playlists, _ = fetch_batch(
            _playlists_urls(author_id), 1, PLAYLIST_INDEX_MAX_PAGES,
            context="playlist_index:{0}".format(author_id), on_page=on_page,
        )
    finally:
        if bg:
            bg.close()

    index = []
    for pl in playlists:
        title = pl.get("title") or pl.get("name") or ""
        if pl.get("id") is not None and title:
            index.append([pl["id"], title])
    log("get_playlist_index: прочитано плейлистов канала {0}: {1}".format(author_id, len(index)))
    if win is not None and index:
        try:
            win.setProperty(key, json.dumps({"ts": time.time(), "items": index}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log("get_playlist_index: не удалось сохранить кэш: {0}".format(e))
    return index


def find_matching_playlists(author_id, series_key):
    """Все плейлисты канала, относящиеся к сериалу (обычно по одному на
    сезон: 'Джек Ричер - сезон 3', 'Джек Ричер - сезон 4'). Сначала точное
    совпадение названия, потом — по началу названия."""
    index = get_playlist_index(author_id)
    exact = []
    prefix = []
    for pid, title in index:
        pk = playlist_series_key(title)
        if not pk:
            continue
        if pk == series_key:
            exact.append((pid, title))
        elif len(pk) >= 4 and len(series_key) >= 4 and (
            pk.startswith(series_key + " ") or series_key.startswith(pk + " ")
        ):
            prefix.append((pid, title))
    found = exact or prefix
    found.sort(key=lambda t: (playlist_season_number(t[1]), t[1]))
    log("find_matching_playlists: '{0}' -> {1} шт. ({2})".format(
        series_key, len(found), "точно" if exact else "по началу названия"))
    return found


def collect_series_episodes(author_id, series_key):
    """Запасной путь, если плейлиста нет: ищем серии среди видео канала.
    Выдача sort=series_d кладёт серии одного сериала подряд, поэтому читаем
    страницы, пока серии находятся, и останавливаемся после нескольких
    страниц без совпадений (раньше окно в 15 страниц обрывалось посреди
    сериала — отсюда было 'только последние 10 серий из 140').
    Дополнительно ищем через поиск Rutube и объединяем результаты."""
    base = PERSON_API.format(author_id=author_id)

    def url_for_page(p):
        return [
            "{0}?page={1}&limit=100&sort=series_d&format=json".format(base, p),
            "{0}?page={1}&limit=100&format=json".format(base, p),
            "{0}?page={1}".format(base, p),
        ]

    def matches(it):
        b, _s, ep = parse_episode_info(it.get("title") or "")
        return ep is not None and _normalize_key(b) == series_key

    matched = {}
    miss_streak = 0
    page = 1
    bg = None
    try:
        bg = xbmcgui.DialogProgressBG()
        bg.create("Rutube", "Ищу серии сериала...")
    except Exception:  # noqa: BLE001
        bg = None
    try:
        while page <= SERIES_FULL_MAX_PAGES:
            if bg:
                bg.update(min(95, page * 2), "Rutube", "Просмотрено страниц канала: {0}".format(page))
            items, cont = fetch_batch(
                url_for_page, page, 1, context="series_scan:{0}".format(author_id)
            )
            found = 0
            for it in items:
                if matches(it) and it.get("id") is not None:
                    matched[it["id"]] = it
                    found += 1
            if found:
                miss_streak = 0
            elif matched:
                miss_streak += 1
            if not cont or (matched and miss_streak >= 3):
                break
            page = cont
    finally:
        if bg:
            bg.close()

    # Дополнение через поиск (один запрос, до 100 результатов по релевантности).
    def search_urls(p):
        q = urllib.parse.quote(series_key)
        return [
            "{0}?query={1}&page={2}&limit=100&format=json".format(SEARCH_API, q, p),
            "{0}?query={1}&page={2}&format=json".format(SEARCH_API, q, p),
        ]

    extra, _ = fetch_batch(search_urls, 1, 1, context="series_search:{0}".format(series_key))
    for it in extra:
        author = it.get("author") if isinstance(it.get("author"), dict) else {}
        if str(author.get("id")) == str(author_id) and matches(it) and it.get("id") is not None:
            matched.setdefault(it["id"], it)

    log("collect_series_episodes: '{0}' -> {1} серий".format(series_key, len(matched)))
    return list(matched.values())


def show_channel_series(author_id, series):
    matches_ = find_matching_playlists(author_id, series)
    if len(matches_) == 1:
        # Один плейлист — показываем его серии сразу, по порядку.
        list_playlist_videos(matches_[0][0], 1)
        return
    if len(matches_) > 1:
        # Несколько плейлистов (обычно по сезонам) — папки по порядку сезонов.
        for pid, title in matches_:
            li = xbmcgui.ListItem(label=str(title).replace("\n", " ").strip())
            li.setArt({"icon": "DefaultVideoPlaylists.png"})
            xbmcplugin.addDirectoryItem(
                ADDON_HANDLE, get_url(action="playlist_videos", playlist_id=pid, page=1), li, True
            )
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return

    episodes = sort_episodes_ascending(collect_series_episodes(author_id, series))
    if not episodes:
        notify("Серии не найдены — см. лог")
    for item in episodes:
        add_single_video_item(item)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


# -------------------------------------------------------------- play logic

def extract_channel_id(text):
    """ID канала из ссылки rutube.ru/channel/ID/..., rutube.ru/video/person/ID/
    или из голого числа. Для ссылок на видео возвращает None."""
    text = (text or "").strip()
    m = re.search(r"rutube\.ru/(?:channel|video/person)/(\d+)", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"\d{4,}", text):
        return text
    return None


MEDIA_CATEGORY_NAMES = {"сериалы", "фильмы", "мультфильмы", "телепередачи", "аниме"}


def looks_like_media_catalog(items):
    """Канал с фильмами/сериалами (где уместны выбор категории и группировка по
    сериям) или обычный канал блогера (где нужна простая лента: новые сверху)."""
    if not items:
        return False
    media = sum(
        1 for it in items
        if str((it.get("category") or {}).get("name", "")).strip().lower() in MEDIA_CATEGORY_NAMES
    )
    series = sum(1 for it in items if parse_episode_info(it.get("title") or "")[2] is not None)
    n = float(len(items))
    return media / n >= 0.5 or series / n >= 0.3


def channel_has_playlists(author_id):
    """Есть ли у канала плейлисты (один лёгкий запрос, результат на час в кэше окна Kodi)."""
    key = "rutube.hasplaylists.{0}".format(author_id)
    win = None
    try:
        win = xbmcgui.Window(10000)
        cached = win.getProperty(key)
        if cached:
            obj = json.loads(cached)
            if time.time() - obj.get("ts", 0) < PLAYLIST_INDEX_TTL:
                return bool(obj.get("v"))
    except Exception:  # noqa: BLE001
        pass
    items, _ = fetch_batch(
        _playlists_urls(author_id), 1, 1, context="has_playlists:{0}".format(author_id)
    )
    result = bool(items)
    if win is not None:
        try:
            win.setProperty(key, json.dumps({"ts": time.time(), "v": result}))
        except Exception:  # noqa: BLE001
            pass
    return result


def extract_video_id(text):
    """Достаёт ID видео из ссылки: /video/ID/, /video/private/ID/,
    /live/video/ID/ (прямые эфиры), /play/embed/ID — либо из голого ID."""
    text = text.strip()
    patterns = [
        r"rutube\.ru/(?:live/)?video/(?:private/)?([0-9a-fA-F]{32})",
        r"rutube\.ru/play/embed/([0-9a-fA-F]{32})",
        r"\b([0-9a-fA-F]{32})\b",
        r"rutube\.ru/(?:live/)?(?:video/(?:private/)?|play/embed/)(?!person|category|playlist)([A-Za-z0-9]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            log("extract_video_id: '{0}' -> '{1}'".format(text, m.group(1)))
            return m.group(1)
    log("extract_video_id: '{0}' используется как есть (не похоже на ссылку)".format(text))
    return text


def _allowed_for_rutube(domains):
    """Разрешён ли rutube.ru в списке allowedDomains внешнего плеера
    (поддерживаются маски вида *.rutube.ru). Подменять чужие домены не будем."""
    host = "rutube.ru"
    for d in domains:
        d = str(d).strip().lower()
        if d == host or (d.startswith("*.") and (host == d[2:] or host.endswith(d[1:]))):
            return True
    return False


def resolve_mediavitrina(iframe_url):
    """Ссылка на поток внешнего плеера Mediavitrina (так встроены многие ТВ-каналы
    на Rutube). Повторяем то, что делает встроенный плеер на странице Rutube:
    читаем страницу плеера, берём из неё адрес конфигурации и запрашиваем его
    с Referer самого плеера. Возвращает URL HLS или None."""
    page_url = iframe_url if iframe_url.startswith("http") else "https:" + iframe_url
    parsed = urllib.parse.urlparse(page_url)
    origin = "{0}://{1}".format(parsed.scheme, parsed.netloc)
    html = http_get_text(page_url, context="mediavitrina:страница")
    if not html:
        return None
    m = re.search(r"allowedDomains:\s*(\[.*?\])", html, re.S)
    if not m:
        log_error("mediavitrina: в странице плеера нет allowedDomains")
        return None
    try:
        domains = json.loads(m.group(1))
    except ValueError:
        domains = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    if not _allowed_for_rutube(domains):
        log_error("mediavitrina: rutube.ru не входит в allowedDomains {0}".format(domains))
        return None
    mu = re.search(r"\burl:\s*'([^']+)'", html) or re.search(r'\burl:\s*"([^"]+)"', html)
    if not mu:
        log_error("mediavitrina: в странице плеера не найден адрес конфигурации")
        return None
    config_url = (
        mu.group(1)
        .replace("{{APPLICATION_ID}}", "")
        .replace("{{PLAYER_REFERER_HOSTNAME}}", "rutube.ru")
        .replace("{{CONFIG_CHECKSUM_SHA256}}", "")
    )
    if config_url.startswith("//"):
        config_url = "https:" + config_url
    hdrs = dict(HEADERS)
    hdrs["Referer"] = origin + "/"
    hdrs["Origin"] = origin
    cfg = http_get_json(config_url, context="mediavitrina:конфиг", headers=hdrs)
    hls = cfg.get("hls") if isinstance(cfg, dict) else None
    if isinstance(hls, list) and hls:
        first = hls[0]
        url = first.get("url") if isinstance(first, dict) else first
        if isinstance(url, str) and url:
            log("mediavitrina: получен поток ({0} вариантов)".format(len(hls)))
            return url
    log_error("mediavitrina: в конфигурации нет hls: {0}".format(describe_shape(cfg)))
    return None


def dump_play_options(video_id, data):
    """Сохраняет ответ play_options в файл, когда поток найти не удалось —
    чтобы по нему можно было разобраться (присылайте его при обращении)."""
    try:
        profile_dir = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
        if not xbmcvfs.exists(profile_dir):
            xbmcvfs.mkdirs(profile_dir)
        if write_json_file(profile_dir + "diag_play_failed.json", {"video_id": video_id, "data": data}):
            log("play_options сохранён в diag_play_failed.json")
    except Exception as e:  # noqa: BLE001
        log("не удалось сохранить play_options: {0}".format(e))


# Выставляется в get_stream_url, когда Rutube отказал в доступе (подписка,
# stream_access_fail). Тогда не нужно, чтобы Kodi перебирал следующие видео из очереди.
ACCESS_DENIED = {"flag": False}


def stop_playlist_cascade():
    """При отказе в доступе Kodi помечает элемент неигральным и сам переходит на
    следующий из очереди (очередь = все видео открытого списка). Если в списке много
    подписочных серий, он перебирает их подряд. Очищаем видео-очередь ДО возврата
    setResolvedUrl(False): playlist.GetPlayable() становится 0, и Kodi прекращает
    перебор. Вызывается только при отказе в доступе — обычный автопереход на
    следующую серию (при успешном воспроизведении) не затрагивается."""
    try:
        pl = xbmc.PlayList(xbmc.PLAYLIST_VIDEO)
        size = pl.size()
        pl.clear()
        log("отказ в доступе: очередь видео очищена ({0} шт.), перебор следующих отключён".format(size))
    except Exception as exc:  # noqa: BLE001
        log_error("не удалось очистить очередь видео: {0}".format(exc))


def _unavailable(title, text):
    """Причина недоступности видео. При переключении каналов цифрами (недавно) — тихое
    уведомление, чтобы окно с кнопкой OK не мешало перебирать каналы."""
    try:
        zap = float(xbmcgui.Window(10000).getProperty(TV_PROP_ZAP) or 0)
    except ValueError:
        zap = 0
    if time.time() - zap < 15:
        xbmcgui.Dialog().notification(title, str(text).replace("\n", " ")[:120], xbmcgui.NOTIFICATION_ERROR, 5000)
    else:
        xbmcgui.Dialog().ok(title, text)


def describe_detail(detail):
    """(заголовок, текст) для объекта detail из play_options."""
    if isinstance(detail, str):
        return "Видео недоступно", detail
    if isinstance(detail, dict):
        langs = detail.get("languages") or []
        first = langs[0] if langs and isinstance(langs[0], dict) else {}
        title = first.get("title") or detail.get("title") or detail.get("message") or "Видео недоступно"
        text = first.get("description") or detail.get("description") or ""
        code = detail.get("name") or detail.get("type") or ""
        if code:
            text = "{0}\n[{1}]".format(text, code).strip()
        return title, text or "Rutube не отдаёт поток для этого видео."
    return "Видео недоступно", str(detail)[:300]


def get_stream_url(video_id):
    ACCESS_DENIED["flag"] = False
    LAST_PLAY_META.clear()
    """Возвращает (url, title, use_rutube_headers). Порядок: ошибка доступа ->
    video_balancer (обычные видео) -> live_streams (прямые эфиры) ->
    iframe_url (внешний плеер Mediavitrina)."""
    opts_url = "{0}?no_404=true&referer={1}&pver=v2".format(
        PLAY_OPTIONS_API.format(video_id=video_id),
        urllib.parse.quote("https://rutube.ru/", safe=""),
    )
    data = http_get_json(opts_url, context="play_options:video_id={0}".format(video_id))
    if not data:
        return None, None, True
    if not isinstance(data, dict):
        log_error("play_options: ожидался dict, получено {0}".format(describe_shape(data)))
        return None, None, True

    title = data.get("title")
    LAST_PLAY_META.update(thumb=data.get("thumbnail_url") or "", duration=data.get("duration") or 0)

    detail = data.get("detail")
    if isinstance(detail, dict) and detail.get("name") == "stream_access_fail":
        langs = detail.get("languages") or []
        first = langs[0] if langs and isinstance(langs[0], dict) else {}
        _unavailable(first.get("title") or "Доступ ограничен", first.get("description") or "")
        ACCESS_DENIED["flag"] = True
        log_error("play_options: stream_access_fail: {0}".format(json.dumps(detail, ensure_ascii=False)[:400]))
        return None, title, True

    # Платная подписка (например, RUTUBE x PREMIER): Rutube отвечает HTTP 200 и
    # объектом detail типа common_subscription, потока в ответе нет. Обхода нет и
    # не будет — просто объясняем причину вместо общего «см. лог».
    if isinstance(detail, dict) and (
        detail.get("type") == "common_subscription"
        or str(detail.get("name") or "").startswith("subscription:")
    ):
        langs = detail.get("languages") or []
        first = langs[0] if langs and isinstance(langs[0], dict) else {}
        ACCESS_DENIED["flag"] = True
        xbmcgui.Dialog().ok(
            "Нужна подписка",
            first.get("title") or str(detail.get("name") or "Платный контент"),
        )
        log_error("play_options: нужна подписка: {0}".format(
            json.dumps({k: detail.get(k) for k in ("name", "type", "product_codes")},
                       ensure_ascii=False)[:400]))
        return None, title, True

    balancer = data.get("video_balancer")
    if isinstance(balancer, dict) and balancer:
        log("play_options: video_balancer ключи: {0}".format(list(balancer.keys())))
        url = balancer.get("m3u8") or balancer.get("hls") or balancer.get("default")
        if url:
            return url, title, True

    live = data.get("live_streams")
    if isinstance(live, dict):
        hls = live.get("hls")
        if isinstance(hls, list) and hls:
            first = hls[0]
            url = first.get("url") if isinstance(first, dict) else first
            if isinstance(url, str) and url:
                log("play_options: прямой эфир, live_streams.hls ({0} шт.)".format(len(hls)))
                return url, title, True

    iframe = data.get("iframe_url")
    if iframe:
        log("play_options: внешний плеер iframe_url={0}".format(iframe))
        if "mediavitrina" in str(iframe):
            url = resolve_mediavitrina(str(iframe))
            if url:
                return url, title, False
        else:
            xbmcgui.Dialog().ok("Сторонний источник", str(iframe))
            return None, title, True

    if detail:
        # Rutube ответил объектом detail (у «недоступных» видео это HTTP 244 с
        # одним только ключом detail). Раньше причина терялась — показываем её
        # и отключаем автоперебор следующих элементов списка.
        reason_title, reason_text = describe_detail(detail)
        ACCESS_DENIED["flag"] = True
        log_error("play_options: видео недоступно, detail={0}".format(
            json.dumps(detail, ensure_ascii=False)[:600]))
        dump_play_options(video_id, data)
        _unavailable(reason_title, reason_text)
        return None, title, True

    log_error(
        "play_options: поток не найден. has_video={0}, iframe_url={1}, live_streams={2}, "
        "video_balancer={3}, is_livestream={4}".format(
            data.get("has_video"), iframe, describe_shape(live), describe_shape(balancer),
            data.get("is_livestream"),
        )
    )
    dump_play_options(video_id, data)
    return None, title, True


# Пределы разрешения (ширина, высота) для режимов качества: 0 = 1080p, 3 = 720p, 4 = 480p.
QUALITY_LIMITS = {0: (1920, 1080), 3: (1280, 720), 4: (854, 480)}
DIRECT_CANCELLED = "cancelled"


def parse_hls_variants(text, base_url):
    """Разбирает мастер-плейлист HLS: список вариантов {w, h, bw, url}. Rutube отдаёт
    каждый вариант дважды (два CDN-хоста) — берём первое вхождение каждого разрешения.
    Звук в вариантах уже смуксован (CODECS="avc1…, mp4a…"), отдельных EXT-X-MEDIA нет."""
    variants = []
    seen = set()
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        bw = re.search(r"BANDWIDTH=(\d+)", line)
        if not res:
            continue
        uri = ""
        for nxt in lines[i + 1:]:
            nxt = nxt.strip()
            if nxt and not nxt.startswith("#"):
                uri = nxt
                break
        if not uri:
            continue
        key = (int(res.group(1)), int(res.group(2)))
        if key in seen:
            continue
        seen.add(key)
        variants.append({
            "w": key[0], "h": key[1],
            "bw": int(bw.group(1)) if bw else 0,
            "url": urllib.parse.urljoin(base_url, uri),
        })
    return variants


def pick_direct_variant(master_url, quality_mode):
    """Для GstPlayer и внешних плееров, которые сами не перебирают потоки: выбираем
    качество здесь и отдаём плееру ссылку на конкретный вариант вместо мастер-плейлиста.
    Возвращает URL варианта, DIRECT_CANCELLED (пользователь закрыл выбор) или None
    (не получилось — тогда играем как обычно, мастер-плейлистом)."""
    if quality_mode == 2:  # ABR невозможен без мастер-плейлиста
        return None
    text = http_get_text(master_url, context="direct:master")
    variants = parse_hls_variants(text, master_url) if text else []
    if len(variants) < 2:
        log_error("direct: в мастер-плейлисте найдено вариантов: {0} — играем как обычно".format(len(variants)))
        return None
    variants.sort(key=lambda v: (v["w"] * v["h"], v["bw"]), reverse=True)
    log("direct: варианты: {0}".format(
        [(v["w"], v["h"], v["bw"]) for v in variants]))

    if quality_mode == 1:
        labels = ["{0}x{1}  —  {2:.1f} Мбит/с".format(v["w"], v["h"], v["bw"] / 1e6)
                  for v in variants]
        idx = xbmcgui.Dialog().select("Качество видео", labels)
        if idx < 0:
            return DIRECT_CANCELLED
        chosen = variants[idx]
    else:
        lim_w, lim_h = QUALITY_LIMITS.get(quality_mode, QUALITY_LIMITS[0])
        fitting = [v for v in variants if v["w"] <= lim_w and v["h"] <= lim_h]
        chosen = fitting[0] if fitting else variants[-1]
    log("direct: выбрано {0}x{1}, {2} бит/с".format(chosen["w"], chosen["h"], chosen["bw"]))
    return chosen["url"]


def get_kodi_major_version():
    """Старшая версия Kodi (20, 21...) из System.BuildVersion, например
    '20.5.0 (20.5.0) Git:...'. None, если определить не удалось."""
    try:
        m = re.match(r"\s*(\d+)", xbmc.getInfoLabel("System.BuildVersion") or "")
        return int(m.group(1)) if m else None
    except Exception:  # noqa: BLE001
        return None


def play_video(video_id):
    if not video_id:
        log_error("play_video вызван без video_id")
        notify("Не указан ID видео")
        xbmcplugin.setResolvedUrl(ADDON_HANDLE, False, xbmcgui.ListItem())
        return

    stream_url, title, use_headers = get_stream_url(video_id)
    if not stream_url:
        if ACCESS_DENIED["flag"]:
            # причина уже показана диалогом; общее уведомление не нужно
            stop_playlist_cascade()
        else:
            notify("Не удалось получить ссылку на видео — см. лог")
        xbmcplugin.setResolvedUrl(ADDON_HANDLE, False, xbmcgui.ListItem())
        return

    log("Воспроизведение video_id={0}, поток={1}".format(video_id, stream_url))

    # Необязательный режим (Настройки): качество выбирает плагин, плеер получает
    # прямую ссылку на вариант. Нужен для GstPlayer и внешних плееров.
    try:
        direct_mode = ADDON.getSettingBool("direct_variant")
    except Exception:  # noqa: BLE001
        direct_mode = False
    if direct_mode and ".m3u8" in stream_url:
        try:
            direct_quality = ADDON.getSettingInt("quality_mode")
        except Exception:  # noqa: BLE001
            direct_quality = 0
        direct = pick_direct_variant(stream_url, direct_quality)
        if direct == DIRECT_CANCELLED:
            # выбор закрыт — не даём Kodi перебирать следующие видео списка
            stop_playlist_cascade()
            xbmcplugin.setResolvedUrl(ADDON_HANDLE, False, xbmcgui.ListItem())
            return
        if direct:
            dli = xbmcgui.ListItem(label=title or video_id, path=direct)
            dli.setMimeType("application/vnd.apple.mpegurl")
            dli.setContentLookup(False)
            record_view(video_id, title)
            xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, dli)
            return

    li = xbmcgui.ListItem(label=title or video_id, path=stream_url)
    li.setMimeType("application/vnd.apple.mpegurl")
    li.setContentLookup(False)

    if ".m3u8" in stream_url:
        li.setProperty("inputstream", "inputstream.adaptive")

        # inputstream.adaptive из Kodi 20 и старше сам тип манифеста не определяет
        # и требует явного inputstream.adaptive.manifest_type. В Kodi 21 свойство
        # устарело (тип определяется автоматически), поэтому там его НЕ задаём.
        kodi_major = get_kodi_major_version()
        log("play_video: Kodi major={0}".format(kodi_major))
        if kodi_major is not None and kodi_major < 21:
            li.setProperty("inputstream.adaptive.manifest_type", "hls")

        # Официальный способ (Kodi 20+) передать заголовки в inputstream.adaptive
        # для запроса самого манифеста и сегментов — раньше использовался хак
        # через "url|Header=value", но правильный вариант — это свойство
        # manifest_headers (см. рабочий сторонний rutube-аддон, ключ view.py).
        if use_headers:
            stream_headers = urllib.parse.urlencode({
                "User-Agent": HEADERS["User-Agent"],
                "Referer": "https://rutube.ru/",
                "Origin": "https://rutube.ru",
            })
            li.setProperty("inputstream.adaptive.manifest_headers", stream_headers)
            li.setProperty("inputstream.adaptive.stream_headers", stream_headers)

        # Режим выбора качества настраивается пользователем (Настройки аддона):
        # 0 = всегда максимальное без диалога (по умолчанию), 1 = спросить
        # перед каждым просмотром (как диалог "Видеопоток" у других аддонов),
        # 2 = довериться автоматическому ABR Kodi, 3 = не выше 720p, 4 = не выше 480p.
        try:
            quality_mode = ADDON.getSettingInt("quality_mode")
        except Exception:  # noqa: BLE001
            quality_mode = 0
        log("play_video: quality_mode={0}".format(quality_mode))

        if quality_mode == 1:
            li.setProperty("inputstream.adaptive.stream_selection_type", "ask-quality")
        elif quality_mode == 2:
            li.setProperty("inputstream.adaptive.stream_selection_type", "adaptive")
        elif quality_mode in (3, 4):
            # Ограничение сверху: 3 = не выше 720p, 4 = не выше 480p. Для слабых
            # устройств и медленного интернета. inputstream.adaptive выбирает
            # лучшее представление, помещающееся в этот предел.
            limit = "720p" if quality_mode == 3 else "480p"
            log("play_video: ограничение качества {0}".format(limit))
            li.setProperty("inputstream.adaptive.stream_selection_type", "fixed-res")
            li.setProperty("inputstream.adaptive.chooser_resolution_max", limit)
            li.setProperty("inputstream.adaptive.chooser_resolution_secure_max", limit)
        else:
            # ВАЖНО: манифест реально содержит все качества вплоть до 1920x1080
            # (проверено диагностикой), но автоматический выбор (ABR) в Kodi
            # почему-то упорно выбирает нижнее качество — похоже, из-за того,
            # что inputstream.adaptive иногда распознаёт HLS-манифест Rutube
            # как "live" вместо VOD. Поэтому по умолчанию явно фиксируем режим
            # "макс. разрешение" без диалога выбора (как в рабочем стороннем
            # аддоне), а не полагаемся на ABR.
            li.setProperty("inputstream.adaptive.stream_selection_type", "fixed-res")
            li.setProperty("inputstream.adaptive.chooser_resolution_max", "1080p")
            li.setProperty("inputstream.adaptive.chooser_resolution_secure_max", "1080p")

    record_view(video_id, title)
    xbmcplugin.setResolvedUrl(ADDON_HANDLE, True, li)


def by_id_and_play():
    kb = xbmcgui.Dialog().input("Ссылка на видео или ID", type=xbmcgui.INPUT_ALPHANUM)
    if not kb:
        return
    channel_id = extract_channel_id(kb)
    if channel_id:
        # Вставили ссылку на канал, а не на видео — открываем канал.
        log("by_id: это ссылка на канал {0}".format(channel_id))
        list_channel_menu(channel_id)
        return
    video_id = extract_video_id(kb)
    play_video(video_id)


# ------------------------------------------------------------------ ТВ: номера каналов

TV_FILE = "tv_channels.json"
TV_PROP_CACHE = "rutube.tv.channels"
TV_PROP_BUF = "rutube.tv.buf"
TV_PROP_TOKEN = "rutube.tv.token"
TV_PROP_ZAP = "rutube.tv.zap"
TV_CUSTOM_FILE = "tv_custom.json"
TV_DIGIT_TIMEOUT = 1.8        # секунд ожидания следующей цифры, как на телевизоре
TV_KEYMAP_NAME = "rutube_tv_digits.xml"


def _tv_profile_file():
    folder = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    if not xbmcvfs.exists(folder):
        xbmcvfs.mkdirs(folder)
    return folder.rstrip("/\\") + "/" + TV_FILE


def tv_save_channels(channels):
    """Запоминаем нумерацию последнего открытого списка ТВ: [[номер, id, название], ...]."""
    data = json.dumps(channels, ensure_ascii=False)
    try:
        xbmcgui.Window(10000).setProperty(TV_PROP_CACHE, data)
        _write_text(_tv_profile_file(), data)
    except Exception as e:  # noqa: BLE001
        log_error("tv: не удалось сохранить список каналов: {0}".format(e))
    log("tv: сохранено каналов: {0}".format(len(channels)))


def tv_load_channels():
    try:
        raw = xbmcgui.Window(10000).getProperty(TV_PROP_CACHE)
        if not raw:
            path = _tv_profile_file()
            if xbmcvfs.exists(path):
                f = xbmcvfs.File(path)
                try:
                    raw = f.read()
                finally:
                    f.close()
        return json.loads(raw) if raw else []
    except Exception as e:  # noqa: BLE001
        log_error("tv: не удалось прочитать список каналов: {0}".format(e))
        return []


def _is_tv_list(items, title):
    """Список прямых эфиров/телеканалов — тогда нумеруем его, как в телевизоре."""
    live = sum(1 for it in items if it.get("is_livestream") and _HEX32.fullmatch(str(it.get("id") or "")))
    if live >= 3 and live >= len(items) * 0.5:
        return True
    return live >= 1 and bool(re.search(r"эфир|телеканал|тв[- ]канал", str(title), re.IGNORECASE))


def add_tv_dial_item():
    li = xbmcgui.ListItem(label="Набрать номер канала…")
    li.setArt({"icon": "DefaultAddonsSearch.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="tv_dial"), li, True)


def render_tv_list(items, start=0):
    """Нумерованный список каналов: «1. Название». Возвращает последний номер."""
    channels = tv_load_channels() if start else []
    n = int(start or 0)
    mine = tv_custom_load()          # если пользователь назначил свои номера — работаем только с ними
    if not start:
        add_tv_dial_item()
    for it in items:
        if it.get("is_deleted") or _item_hidden(it) or not _HEX32.fullmatch(str(it.get("id") or "")):
            continue
        if mine:
            num = tv_custom_number_of(it["id"])
            add_single_video_item(it, label_prefix="{0}. ".format(num) if num else "", tv_channel=True)
            continue
        n += 1
        channels.append([n, it["id"], it.get("title") or ""])
        add_single_video_item(it, label_prefix="{0}. ".format(n), tv_channel=True)
    if not mine:
        tv_save_channels(channels)
    return n


def tv_active_channels():
    """Какая нумерация действует: свои номера (если назначены) или автоматическая."""
    mine = tv_custom_load()
    if mine:
        return [[int(k), v[0], v[1]] for k, v in mine.items()]
    return tv_load_channels()


def tv_play_number(number):
    found = next((c for c in tv_active_channels() if c[0] == number), None)
    if not found:
        notify("Канал {0} не назначен".format(number) if tv_custom_load()
               else "Канала {0} нет в последнем открытом списке ТВ".format(number))
        return
    notify_info("Канал {0}: {1}".format(number, found[2]))
    xbmcgui.Window(10000).setProperty(TV_PROP_ZAP, "{0:.0f}".format(time.time()))
    xbmc.executebuiltin('PlayMedia("{0}")'.format(get_url(action="play", video_id=found[1])))


def tv_dial():
    """Пункт «Набрать номер канала…»: окно ввода числа, затем переключение."""
    value = xbmcgui.Dialog().numeric(0, "Номер канала")
    if value and str(value).strip().isdigit():
        tv_play_number(int(value))
    xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=False)


def tv_digit(d):
    """Нажатие цифры (из keymap): копим номер и переключаем канал после паузы
    TV_DIGIT_TIMEOUT или сразу, если набраны все цифры максимального номера."""
    if d is None or not str(d).isdigit():
        return
    win = xbmcgui.Window(10000)
    now = time.time()
    token = "{0:.6f}".format(now)
    buf = win.getProperty(TV_PROP_BUF)
    last = float(win.getProperty(TV_PROP_BUF + ".ts") or 0)
    if now - last > TV_DIGIT_TIMEOUT:
        buf = ""
    buf += str(d)
    win.setProperty(TV_PROP_BUF, buf)
    win.setProperty(TV_PROP_BUF + ".ts", token)
    win.setProperty(TV_PROP_TOKEN, token)
    xbmcgui.Dialog().notification("Канал", buf, xbmcgui.NOTIFICATION_INFO, 1500, False)
    channels = tv_active_channels()
    max_n = max([c[0] for c in channels] or [0])
    if len(buf) < len(str(max_n)):
        time.sleep(TV_DIGIT_TIMEOUT)
        if win.getProperty(TV_PROP_TOKEN) != token:
            return          # пришла следующая цифра — переключит она
    win.setProperty(TV_PROP_BUF, "")
    tv_play_number(int(buf))


def _tv_keymap_path():
    return xbmcvfs.translatePath("special://userdata/keymaps/").rstrip("/\\") + "/" + TV_KEYMAP_NAME


def _tv_keymap_xml():
    from xml.sax.saxutils import escape
    names = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    def section(prefix):
        return "\n".join(
            "      <{0}{1}>{2}</{0}{1}>".format(prefix, nm, escape("RunPlugin(plugin://{0}/?action=tv_digit&d={1})".format(
                ADDON.getAddonInfo("id"), i))) for i, nm in enumerate(names))
    return ("<keymap>\n  <FullscreenVideo>\n    <keyboard>\n" + section("") + "\n"
            + section("numpad") + "\n    </keyboard>\n    <remote>\n" + section("") + "\n"
            "    </remote>\n  </FullscreenVideo>\n</keymap>\n")


def tv_keys_toggle():
    """Включает/выключает цифровые кнопки: файл keymap в userdata/keymaps. Во время
    просмотра канала цифры (1, 2, 12…) переключают каналы из последнего открытого
    нумерованного списка ТВ. Эксперимент: имена кнопок пульта у разных устройств отличаются."""
    path = _tv_keymap_path()
    if xbmcvfs.exists(path):
        xbmcvfs.delete(path)
        xbmc.executebuiltin("Action(reloadkeymaps)")
        xbmcgui.Dialog().ok("ТВ: цифровые кнопки", "Отключено: файл раскладки удалён.")
    elif xbmcgui.Dialog().yesno(
            "ТВ: цифровые кнопки",
            "Во время просмотра цифры будут переключать каналы по номерам из последнего открытого списка "
            "«ТВ-каналы»/«Популярные эфиры» (набор: 1, 2 → канал 12, пауза около 2 с). "
            "Будет создан файл {0}. Включить?".format(TV_KEYMAP_NAME)):
        _write_text(path, _tv_keymap_xml())
        xbmc.executebuiltin("Action(reloadkeymaps)")
        xbmcgui.Dialog().ok("ТВ: цифровые кнопки", "Включено. Откройте список каналов, запустите любой канал и "
                            "набирайте номер на цифровых кнопках. Выключить — тем же пунктом меню.")
    xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=False)


# ------------------------------------------------------------------ ТВ: свои номера

_TV_CUSTOM = {}


def _tv_custom_file():
    folder = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    if not xbmcvfs.exists(folder):
        xbmcvfs.mkdirs(folder)
    return folder.rstrip("/\\") + "/" + TV_CUSTOM_FILE


def tv_custom_load():
    """{'5': [video_id, название], ...} — номера, назначенные пользователем."""
    if "data" not in _TV_CUSTOM:
        data = {}
        try:
            path = _tv_custom_file()
            if xbmcvfs.exists(path):
                f = xbmcvfs.File(path)
                try:
                    data = json.loads(f.read() or "{}")
                finally:
                    f.close()
        except Exception as e:  # noqa: BLE001
            log_error("tv: не удалось прочитать свои номера: {0}".format(e))
        _TV_CUSTOM["data"] = data if isinstance(data, dict) else {}
    return _TV_CUSTOM["data"]


def tv_custom_save(data):
    _TV_CUSTOM["data"] = data
    _write_text(_tv_custom_file(), json.dumps(data, ensure_ascii=False))


def tv_custom_number_of(video_id):
    for k, v in tv_custom_load().items():
        if v and v[0] == video_id:
            return int(k)
    return None


def tv_assign(video_id, name):
    """Контекстное меню канала: «Назначить номер канала…»."""
    value = xbmcgui.Dialog().numeric(0, "Номер для канала «{0}»".format(str(name or "")[:40]))
    if not value or not str(value).strip().isdigit() or int(value) <= 0:
        return
    n = str(int(value))
    data = dict(tv_custom_load())
    taken = data.get(n)
    if taken and taken[0] != video_id and not xbmcgui.Dialog().yesno(
            "Номер занят", "Номер {0} уже назначен каналу «{1}». Заменить?".format(n, taken[1])):
        return
    data = {k: v for k, v in data.items() if v[0] != video_id}   # у канала один номер
    data[n] = [video_id, str(name or "")]
    tv_custom_save(data)
    notify_info("Канал «{0}» → кнопки {1}".format(str(name or "")[:30], n))
    xbmc.executebuiltin("Container.Refresh")


def tv_unassign(video_id):
    data = {k: v for k, v in tv_custom_load().items() if v[0] != video_id}
    tv_custom_save(data)
    notify_info("Номер канала снят" + ("" if data else ". Свои номера закончились — снова включена автонумерация"))
    xbmc.executebuiltin("Container.Refresh")


def tv_reset():
    if xbmcgui.Dialog().yesno("Мои каналы ТВ", "Снять все назначенные номера и вернуться к автонумерации?"):
        tv_custom_save({})
        notify_info("Свои номера сброшены")
    xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=False)


def tv_my_channels():
    """Главное меню → «Мои каналы ТВ»: список назначенных номеров."""
    mine = tv_custom_load()
    if not mine:
        xbmcgui.Dialog().ok(
            "Мои каналы ТВ",
            "Пока ничего не назначено. Откройте список каналов, вызовите контекстное меню канала → "
            "«Назначить номер канала…». После первого назначения цифровые кнопки работают только по вашим номерам.")
        xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=False)
        return
    add_tv_dial_item()
    for k in sorted(mine, key=int):
        vid, nm = mine[k]
        add_single_video_item({"id": vid, "title": nm, "is_livestream": True},
                              label_prefix="{0}. ".format(k), tv_channel=True)
    li = xbmcgui.ListItem(label="Снять все номера…")
    li.setArt({"icon": "DefaultIconError.png"})
    xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(action="tv_reset"), li, True)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


# ------------------------------------------------------------------ экспорт в медиатеку

def _xml(text):
    from xml.sax.saxutils import escape
    return escape(str(text or ""))


def _write_text(path, text):
    f = xbmcvfs.File(path, "w")
    try:
        f.write(bytearray(text.encode("utf-8")))
    finally:
        f.close()


def _lib_dir():
    folder = ADDON.getSetting("library_dir")
    if not folder:
        folder = xbmcgui.Dialog().browseSingle(
            3, "Папка для медиатеки Kodi (её нужно один раз добавить как источник ТВ-шоу)", "files")
        if folder:
            ADDON.setSetting("library_dir", folder)
    return folder or None


def _jp(*parts):
    return "/".join(str(p).strip("/\\") if i else str(p).rstrip("/\\") for i, p in enumerate(parts))


def _all_playlist_items(playlist_id):
    items, _ = fetch_batch(
        _playlist_videos_urls(playlist_id), 1, SERIES_FULL_MAX_PAGES,
        context="library:playlist:{0}".format(playlist_id))
    return items


def _library_collect(kind, params):
    """[(item, подсказка_сезона)] для выбранного сериала — теми же способами, что при открытии."""
    entries = []
    if kind == "playlist":
        entries = [(it, None) for it in _all_playlist_items(params.get("id"))]
    elif kind == "series":
        author_id, key = params.get("author_id"), params.get("series")
        matched = find_matching_playlists(author_id, key)
        if matched:
            for pid, ptitle in matched:
                hint = playlist_season_number(ptitle) or None
                entries.extend((it, hint) for it in _all_playlist_items(pid))
        else:
            entries = [(it, None) for it in collect_series_episodes(author_id, key)]
    elif kind == "feed":
        url = params.get("url")
        items, _ = fetch_batch(lambda p: [_with_page(url, p)], 1, SERIES_FULL_MAX_PAGES,
                               context="library:feed")
        entries = [(it, None) for it in items]
    return entries


def _library_source_registered(folder):
    """Добавлена ли папка (или её родитель) в источники видео Kodi."""
    try:
        resp = json.loads(xbmc.executeJSONRPC(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "Files.GetSources", "params": {"media": "video"}})))
        norm = lambda x: str(x).replace("\\", "/").rstrip("/").lower()
        target = norm(xbmcvfs.translatePath(folder)) if os.path.isdir(xbmcvfs.translatePath(folder)) else norm(folder)
        for src in resp.get("result", {}).get("sources", []):
            sp = norm(src.get("file", ""))
            if sp and (target == sp or target.startswith(sp + "/")):
                return True
    except Exception as e:  # noqa: BLE001
        log("library: не удалось проверить источники: {0}".format(e))
    return False


def library_add(kind, name, params):
    """Экспорт ОДНОГО выбранного сериала в папку медиатеки: .strm на каждую серию
    (ссылка на воспроизведение через этот аддон) и, по настройке, .nfo с данными Rutube.
    Повторный запуск добавляет только новые серии."""
    root = _lib_dir()
    if not root:
        return
    series = _sanitize_filename(name or "Сериал")
    bg = None
    try:
        bg = xbmcgui.DialogProgressBG()
        bg.create("Rutube — медиатека", series)
    except Exception:  # noqa: BLE001
        bg = None
    try:
        entries = _library_collect(kind, params)
    finally:
        if bg:
            bg.close()

    episodes, skipped = {}, 0
    for it, hint in entries:
        if (it.get("is_deleted") or it.get("is_livestream") or it.get("is_paid")
                or it.get("common_subscription_product_codes")
                or not _HEX32.fullmatch(str(it.get("id") or ""))):
            continue
        base, season, ep = parse_episode_info(it.get("title") or "")
        if ep is None:
            skipped += 1
            continue
        episodes.setdefault((season or hint or 1, ep), it)
    if not episodes:
        xbmcgui.Dialog().ok("Медиатека", "Не найдено серий с номерами в названиях — экспортировать нечего.")
        return

    use_nfo = _setting_int("library_meta") == 0
    series_dir = _jp(root, series)
    xbmcvfs.mkdirs(series_dir)
    first = episodes[sorted(episodes)[0]]
    if use_nfo and not xbmcvfs.exists(_jp(series_dir, "tvshow.nfo")):
        _write_text(_jp(series_dir, "tvshow.nfo"),
                    '<?xml version="1.0" encoding="UTF-8"?>\n<tvshow>\n  <title>{0}</title>\n'
                    '  <studio>Rutube</studio>\n  <thumb aspect="poster">{1}</thumb>\n</tvshow>\n'.format(
                        _xml(series), _xml(first.get("thumbnail_url"))))
    added = 0
    for (season, ep), it in sorted(episodes.items()):
        season_dir = _jp(series_dir, "Season {0:02d}".format(season))
        xbmcvfs.mkdirs(season_dir)
        stem = _jp(season_dir, "{0} - S{1:02d}E{2:02d}".format(series, season, ep))
        if xbmcvfs.exists(stem + ".strm"):
            continue
        _write_text(stem + ".strm", get_url(action="play", video_id=it["id"]))
        if use_nfo:
            mt = re.search(r"«([^»]+)»", it.get("title") or "")
            _write_text(stem + ".nfo",
                        '<?xml version="1.0" encoding="UTF-8"?>\n<episodedetails>\n'
                        '  <title>{0}</title>\n  <season>{1}</season>\n  <episode>{2}</episode>\n'
                        '  <plot>{3}</plot>\n  <runtime>{4}</runtime>\n  <aired>{5}</aired>\n'
                        '  <thumb>{6}</thumb>\n</episodedetails>\n'.format(
                            _xml(mt.group(1) if mt else "Серия {0}".format(ep)), season, ep,
                            _xml(it.get("description")), int((it.get("duration") or 0) // 60),
                            _xml(str(it.get("publication_ts") or "")[:10]), _xml(it.get("thumbnail_url"))))
        added += 1
    log("library: '{0}': всего серий {1}, добавлено {2}, без номера {3}".format(
        series, len(episodes), added, skipped))
    notify_info("{0}: добавлено серий {1} из {2}".format(series, added, len(episodes)))

    if _library_source_registered(root):
        xbmc.executebuiltin('UpdateLibrary(video,"{0}/")'.format(series_dir))
    else:
        xbmcgui.Dialog().ok(
            "Медиатека Kodi: один раз добавьте папку",
            "Видео → Файлы → Добавить видео… → Обзор → выберите папку {0} → «Эта папка содержит: "
            "ТВ-шоу». После этого сериал появится в Видео → ТВ-шоу.".format(root))


# ------------------------------------------------------------------ комментарии

# Адрес API комментариев Rutube нигде не документирован — пробуем вероятные
# варианты по очереди (тихо, без красных уведомлений). Какой сработал и как
# выглядит первый комментарий, пишется в kodi.log (строки comments:...).
COMMENTS_URLS = (
    "https://rutube.ru/api/comments/video/{id}/?client=wdp&page={p}",
    "https://rutube.ru/api/comments/video/{id}/?page={p}",
    "https://rutube.ru/api/v2/comments/video/{id}/?page={p}",
    "https://rutube.ru/api/v1/comments/video/{id}/?page={p}",
    "https://rutube.ru/api/video/{id}/comments/?page={p}",
)
COMMENTS_MAX_PAGES = 3


def _extract_list(data):
    """(список, есть_ли_следующая) из ответа — терпимо к разным формам."""
    if isinstance(data, list):
        return data, False
    if isinstance(data, dict):
        for key in ("results", "comments", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key], bool(data.get("next") or data.get("has_next"))
    return None, False


def _fetch_comments(video_id):
    """-> (комментарии, есть_ещё, получилось_ли_достучаться)."""
    comments, seen = [], set()
    working, reached, more = None, False, False
    for page in range(1, COMMENTS_MAX_PAGES + 1):
        lst, nxt = None, False
        for i in ([working] if working is not None else range(len(COMMENTS_URLS))):
            data = http_get_json(
                COMMENTS_URLS[i].format(id=video_id, p=page),
                context="comments:стр{0}".format(page), quiet=True)
            lst, nxt = _extract_list(data)
            if lst is not None:
                working = i
                reached = True
                if page == 1:
                    log("comments: рабочий адрес #{0}; форма: {1}".format(i, describe_shape(data)))
                    if lst and isinstance(lst[0], dict):
                        log("comments: первый комментарий: {0}".format(
                            json.dumps(lst[0], ensure_ascii=False)[:600]))
                break
        if lst is None:
            break
        for c in lst:
            cid = c.get("id") if isinstance(c, dict) else None
            if isinstance(c, dict) and (cid is None or cid not in seen):
                if cid is not None:
                    seen.add(cid)
                comments.append(c)
        more = nxt
        if not nxt or not lst:
            more = False
            break
    return comments, more, reached


def _fmt_ts(value):
    v = str(value or "")
    return (v[:10] + " " + v[11:16]) if re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d", v) else v


def _esc(text):
    """Квадратные скобки в тексте комментария Kodi принял бы за свою разметку."""
    return str(text or "").replace("[", "(").replace("]", ")")


def _comment_lines(c, indent=""):
    user = c.get("user") if isinstance(c.get("user"), dict) else (
        c.get("author") if isinstance(c.get("author"), dict) else {})
    name = (user.get("name") or user.get("username") or c.get("user_name")
            or c.get("author_name") or "Аноним")
    text = c.get("text") or c.get("comment") or c.get("content") or c.get("message") or ""
    likes = c.get("likes") or c.get("like_count") or c.get("likes_count") or 0
    if isinstance(likes, dict):
        likes = likes.get("count") or likes.get("likes") or 0
    replies = c.get("replies")
    reply_count = (len(replies) if isinstance(replies, list) else 0) or c.get("replies_count") \
        or c.get("reply_count") or c.get("answers_count") or 0
    meta = [m for m in (_fmt_ts(c.get("created_ts") or c.get("created") or c.get("date")),
                        "+{0}".format(likes) if likes else "") if m]
    head = "{0}[B]{1}[/B]".format(indent, _esc(name)) + ("   " + "  ·  ".join(meta) if meta else "")
    lines = [head, indent + _esc(text).replace("\n", "\n" + indent)]
    if isinstance(replies, list):
        for r in replies:
            if isinstance(r, dict):
                lines.append("")
                lines.extend(_comment_lines(r, indent + "    > "))
    elif reply_count:
        lines.append("{0}(ответов: {1})".format(indent, reply_count))
    return lines


def show_comments(video_id, title):
    """«Комментарии» из контекстного меню: читаем несколько страниц и показываем
    одним прокручиваемым окном."""
    video_id = extract_video_id(video_id or "")
    comments, more, reached = _fetch_comments(video_id)
    if not reached:
        log_error("comments: ни один адрес не ответил для {0}".format(video_id))
        xbmcgui.Dialog().ok(
            "Комментарии недоступны",
            "Rutube не ответил на известные адреса комментариев. Подробности в kodi.log (строки comments:).")
        return
    comments = [c for c in comments if not (c.get("is_deleted") or c.get("deleted"))]
    if not comments:
        xbmcgui.Dialog().ok("Комментарии", "Под этим видео пока нет комментариев.")
        return
    parts = []
    for c in comments:
        parts.extend(_comment_lines(c))
        parts.append("")
    if more:
        parts.append("… показаны первые {0} комментариев".format(len(comments)))
    xbmcgui.Dialog().textviewer(
        "Комментарии ({0}): {1}".format(len(comments), _esc(title or "")[:60]), "\n".join(parts))


# ------------------------------------------------------------------ похожие видео

# Эндпоинт «похожих» у Rutube нигде не документирован: пробуем вероятные адреса
# (по аналогии с recommendation/main из витрины) и, если не ответят, строим
# запросы поиска сами. Какой адрес сработал — пишется в лог (similar:recommend).
# Проверено по логу (28.10.2026): оба вероятных адреса —
#   /api/v2/video/recommendation/{id}  и  /api/video/{id}/related/  — отвечают HTTP 404,
# поэтому проба отключена. Найдёте рабочий адрес — впишите его сюда.
SIMILAR_RECOMMEND_URLS = ()
SIMILAR_NOISE_RE = re.compile(
    r"\b(official|video|music|lyrics?|audio|hd|4k|1080p|720p|full|live|remastered|"
    r"клип|видео|официальн\w*|премьера|смотреть|онлайн)\b", re.IGNORECASE)


def _clean_query(text, max_words=6):
    """Название ролика -> короткий поисковый запрос (без скобок, хештегов, шума)."""
    t = str(text or "")
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", t)
    t = re.sub(r"#\w+", " ", t)
    t = SIMILAR_NOISE_RE.sub(" ", t)
    t = re.sub(r"[^\w\s'-]", " ", t)
    t = re.sub(r"\s+-\s+|\s+", " ", t).strip(" -")
    return " ".join(t.split()[:max_words])


def _artist_of(title):
    """'Исполнитель - Трек' -> 'Исполнитель' (пусто, если формат не подходит)."""
    t = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", str(title or ""))
    parts = re.split(r"\s[-–—|]\s|\s:\s", t, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return _clean_query(parts[0], 4)
    return ""


def _hashtags(*texts):
    seen, out = set(), []
    for t in texts:
        for tag in re.findall(r"#(\w+)", str(t or "")):
            if tag.lower() not in seen:
                seen.add(tag.lower())
                out.append(tag)
    return out


def similar_queries(title, description, tags):
    """[(подпись, запрос)] — варианты поиска похожего, без повторов."""
    cands = [
        ("Похожие по названию", _clean_query(title)),
        ("Исполнитель / автор", _artist_of(title)),
    ]
    raw_tags = [str(t.get("name") if isinstance(t, dict) else t).strip() for t in (tags or [])]
    raw_tags = [t for t in raw_tags if t]
    if raw_tags:
        # теги Rutube — целые фразы: берём короткие (до 3 слов), каждую отдельным запросом
        short = [t for t in raw_tags if len(t.split()) <= 3][:3] or [_clean_query(raw_tags[0], 3)]
        for t in short:
            cands.append(("Тег", t))
    else:
        hs = _hashtags(title, description)
        if hs:
            cands.append(("По хештегам", " ".join(hs[:3])))
    out, seen = [], set()
    for label, q in cands:
        key = q.lower().strip()
        if key and key not in seen:
            seen.add(key)
            out.append((label, q.strip()))
    return out


def similar_query(default):
    q = xbmcgui.Dialog().input("Что искать?", defaultt=default or "", type=xbmcgui.INPUT_ALPHANUM)
    if not q:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    do_search(q, 1)


def list_similar(video_id, title):
    """«Похожие видео»: рекомендации Rutube (если ответят) либо варианты поиска
    по названию, исполнителю и тегам + собственный запрос."""
    video_id = extract_video_id(video_id or "")
    info = http_get_json(
        "{0}?no_404=true&referer={1}&pver=v2".format(
            PLAY_OPTIONS_API.format(video_id=video_id), urllib.parse.quote("https://rutube.ru/", safe="")),
        context="similar:info", quiet=True) or {}
    src_title = title or info.get("title") or ""
    queries = similar_queries(src_title, info.get("description"), info.get("tags"))
    log("similar: '{0}' -> запросы {1}".format(src_title, queries))

    xbmcplugin.setPluginCategory(ADDON_HANDLE, "Похожие: {0}".format(src_title[:60]))

    def add_folder(label, **params):
        li = xbmcgui.ListItem(label=label)
        li.setArt({"icon": "DefaultFolder.png"})
        xbmcplugin.addDirectoryItem(ADDON_HANDLE, get_url(**params), li, True)

    rec = []
    if SIMILAR_RECOMMEND_URLS:
        rec, _ = fetch_batch(
            lambda p: [u.format(id=video_id) for u in SIMILAR_RECOMMEND_URLS], 1, 1,
            context="similar:recommend", silent=True)
        rec = [it for it in rec if _looks_like_video(it) and it.get("id") != video_id]
        log("similar: рекомендаций Rutube: {0}".format(len(rec)))

    for label, q in queries:
        add_folder("{0}: «{1}»".format(label, q), action="search", query=q, page=1)
    author = info.get("author") if isinstance(info.get("author"), dict) else None
    if author and author.get("id") is not None:
        add_folder("Ещё с канала: {0}".format(author.get("name") or author["id"]),
                   action="channel_menu", author_id=author["id"])
    add_folder("Свой запрос…", action="similar_query",
               query=queries[0][1] if queries else _clean_query(src_title))
    for it in rec:
        add_single_video_item(it)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


# ------------------------------------------------------------------ загрузка видео

DL_PROP_ACTIVE = "rutube.dl.active"
DL_PROP_CANCEL = "rutube.dl.cancel"
DL_WORKERS = 4   # сегментов одновременно
DL_BATCH = 8     # сегментов в одной порции (порядок записи сохраняется)


def notify_info(msg):
    xbmcgui.Dialog().notification("Rutube", msg, xbmcgui.NOTIFICATION_INFO, 5000)


def _sanitize_filename(name):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", str(name or "video"))
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "video")[:120]


def _dl_get(url, headers, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise last


def _parse_attrs(line):
    return {m.group(1): m.group(2).strip('"')
            for m in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line)}


def _parse_master(text, base):
    """Варианты качества из мастер-плейлиста HLS: [{bandwidth, height, url}]."""
    variants = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if not l.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs = _parse_attrs(l.split(":", 1)[1])
        uri = next((x for x in lines[i + 1:] if not x.startswith("#")), None)
        if not uri:
            continue
        m = re.match(r"(\d+)x(\d+)", attrs.get("RESOLUTION", ""))
        variants.append({
            "bandwidth": int(attrs.get("BANDWIDTH") or 0),
            "height": int(m.group(2)) if m else 0,
            "url": urllib.parse.urljoin(base, uri),
        })
    return variants


def _pick_variants(variants, max_height):
    """Лучшее качество не выше max_height (None — без ограничения). Возвращает
    список равнозначных вариантов (у Rutube есть запасные CDN-адреса)."""
    pool = [v for v in variants if not max_height or (v["height"] and v["height"] <= max_height)]
    if not pool:
        pool = [min(variants, key=lambda v: (v["height"] or 10 ** 6, v["bandwidth"]))]
    best = max(pool, key=lambda v: (v["height"], v["bandwidth"]))
    return [v for v in pool if (v["height"], v["bandwidth"]) == (best["height"], best["bandwidth"])]


def _parse_media(text, base):
    info = {"segments": [], "init": None, "encrypted": False, "endlist": False,
            "byterange": False, "duration": 0.0}
    pending = 0.0
    for l in (x.strip() for x in text.splitlines()):
        if not l:
            continue
        if l.startswith("#EXT-X-KEY:"):
            if _parse_attrs(l.split(":", 1)[1]).get("METHOD", "NONE").upper() != "NONE":
                info["encrypted"] = True
        elif l.startswith("#EXT-X-MAP:"):
            uri = _parse_attrs(l.split(":", 1)[1]).get("URI")
            if uri:
                info["init"] = urllib.parse.urljoin(base, uri)
        elif l.startswith("#EXT-X-BYTERANGE"):
            info["byterange"] = True
        elif l.startswith("#EXT-X-ENDLIST"):
            info["endlist"] = True
        elif l.startswith("#EXTINF:"):
            try:
                pending = float(l.split(":", 1)[1].split(",")[0])
            except ValueError:
                pending = 0.0
        elif not l.startswith("#"):
            info["segments"].append(urllib.parse.urljoin(base, l))
            info["duration"] += pending
            pending = 0.0
    return info


class _OutFile(object):
    """Запись в локальный файл или (если папка — сетевой ресурс Kodi) через xbmcvfs."""

    def __init__(self, folder, filename):
        local = xbmcvfs.translatePath(folder)
        self.local_dir = local if os.path.isdir(local) else None
        if self.local_dir:
            self.path = os.path.join(self.local_dir, filename)
            self._f = open(self.path, "wb")
        else:
            self.path = folder.rstrip("/\\") + "/" + filename
            self._f = xbmcvfs.File(self.path, "w")

    def write(self, data):
        if self.local_dir:
            self._f.write(data)
        else:
            self._f.write(bytearray(data))

    def close(self):
        self._f.close()

    def delete(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.local_dir:
                os.remove(self.path)
            else:
                xbmcvfs.delete(self.path)
        except Exception:  # noqa: BLE001
            pass


def _download_dir():
    folder = ADDON.getSetting("download_dir")
    if not folder:
        folder = xbmcgui.Dialog().browseSingle(3, "Папка для загрузок", "files")
        if folder:
            ADDON.setSetting("download_dir", folder)
    return folder or None


def _unique_name(folder, base, ext):
    local = xbmcvfs.translatePath(folder)
    name = base + ext
    n = 2
    while (os.path.exists(os.path.join(local, name)) if os.path.isdir(local)
           else xbmcvfs.exists(folder.rstrip("/\\") + "/" + name)):
        name = "{0} ({1}){2}".format(base, n, ext)
        n += 1
    return name


def download_cancel():
    xbmcgui.Window(10000).setProperty(DL_PROP_CANCEL, "1")
    notify_info("Загрузка будет остановлена")


def download_video(video_id, title):
    """Скачивает видео (HLS) в выбранную папку одним файлом без перекодирования
    (.ts, либо .mp4 для fMP4). Без внешних программ. Не загружает: эфиры,
    зашифрованные потоки и контент, к которому у Rutube нет доступа без
    подписки/покупки (там поток вообще не выдаётся)."""
    win = xbmcgui.Window(10000)
    if win.getProperty(DL_PROP_ACTIVE) == "1":
        notify("Уже идёт другая загрузка")
        return
    video_id = extract_video_id(video_id or "")
    url, real_title, use_headers = get_stream_url(video_id)
    if not url:
        return  # причину (доступ, регион, удалено) уже показал get_stream_url
    folder = _download_dir()
    if not folder:
        return

    headers = dict(HEADERS) if use_headers else {"User-Agent": HEADERS["User-Agent"]}
    headers["Accept"] = "*/*"
    base_name = _sanitize_filename(title or real_title or video_id)
    try:
        quality_mode = ADDON.getSettingInt("quality_mode")
    except Exception:  # noqa: BLE001
        quality_mode = 0
    max_height = {3: 720, 4: 480}.get(quality_mode)

    win.setProperty(DL_PROP_ACTIVE, "1")
    win.setProperty(DL_PROP_CANCEL, "")
    out = None
    bg = None
    try:
        text = _dl_get(url, headers).decode("utf-8", errors="replace")
        media_url, bandwidth = url, 0
        if "#EXT-X-STREAM-INF" in text:
            media = None
            for v in _pick_variants(_parse_master(text, url), max_height):
                try:
                    media = _dl_get(v["url"], headers).decode("utf-8", errors="replace")
                    media_url, bandwidth = v["url"], v["bandwidth"]
                    log("download: выбрано качество {0}p, {1} бит/с".format(v["height"], v["bandwidth"]))
                    break
                except Exception as e:  # noqa: BLE001
                    log_error("download: вариант {0} недоступен: {1}".format(v["url"], e))
            if media is None:
                notify("Не удалось получить список сегментов")
                return
            text = media
        info = _parse_media(text, media_url)
        if info["encrypted"]:
            xbmcgui.Dialog().ok("Загрузка невозможна", "Поток зашифрован — такие видео аддон не загружает.")
            return
        if not info["endlist"]:
            xbmcgui.Dialog().ok("Загрузка невозможна", "Это прямой эфир — его нельзя сохранить целиком.")
            return
        if info["byterange"] or not info["segments"]:
            notify("Формат потока не поддерживается для загрузки")
            return

        segments = ([info["init"]] if info["init"] else []) + info["segments"]
        ext = ".mp4" if info["init"] else ".ts"
        # свободное место (только для локальных папок)
        local = xbmcvfs.translatePath(folder)
        if os.path.isdir(local) and bandwidth and info["duration"]:
            need = int(bandwidth * info["duration"] / 8 * 1.05)
            free = shutil.disk_usage(local).free
            if free < need:
                xbmcgui.Dialog().ok(
                    "Недостаточно места",
                    "Нужно около {0} МБ, свободно {1} МБ.".format(need // 2 ** 20, free // 2 ** 20))
                return

        filename = _unique_name(folder, base_name, ext)
        out = _OutFile(folder, filename)
        bg = xbmcgui.DialogProgressBG()
        bg.create("Rutube — загрузка", filename)
        monitor = xbmc.Monitor()
        total, done = len(segments), 0
        notify_info("Загрузка началась: {0}".format(filename))
        with ThreadPoolExecutor(max_workers=DL_WORKERS) as pool:
            for start in range(0, total, DL_BATCH):
                if win.getProperty(DL_PROP_CANCEL) == "1" or monitor.abortRequested():
                    raise RuntimeError("cancelled")
                batch = segments[start:start + DL_BATCH]
                for data in pool.map(lambda u: _dl_get(u, headers), batch):
                    out.write(data)
                    done += 1
                bg.update(int(done * 100 / total), "Rutube — загрузка", "{0}: {1}%".format(filename, int(done * 100 / total)))
        out.close()
        log("download: готово {0} ({1} сегментов)".format(out.path, total))
        notify_info("Загрузка завершена: {0}".format(filename))
        out = None
    except RuntimeError:
        notify_info("Загрузка остановлена")
    except Exception as e:  # noqa: BLE001
        log_error("download: ошибка: {0}".format(e))
        notify("Загрузка прервана: {0}".format(str(e)[:80]))
    finally:
        if out is not None:
            out.delete()   # недокачанный файл не оставляем
        if bg:
            bg.close()
        win.setProperty(DL_PROP_ACTIVE, "")
        win.setProperty(DL_PROP_CANCEL, "")


# ------------------------------------------------------------- diagnostics

def http_probe(url):
    """Один запрос БЕЗ повторов и уведомлений — возвращает статус, начало
    тела ответа (в том числе при ошибке!) и разобранный JSON при успехе.
    Только для диагностики."""
    result = {"url": url, "status": None, "body_head": "", "json": None}
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            result["status"] = resp.status
            result["body_head"] = raw[:300]
            try:
                result["json"] = json.loads(raw)
            except ValueError:
                pass
    except urllib.error.HTTPError as e:
        result["status"] = e.code
        try:
            result["body_head"] = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        result["status"] = "ошибка: {0}".format(e)
    time.sleep(REQUEST_DELAY_SECONDS)
    return result


def http_get_text(url, context=""):
    """Как http_get_json, но для не-JSON ответов (например, m3u8-манифест).
    Использует те же заголовки, что и реальное воспроизведение (Referer),
    чтобы результат совпадал с тем, что видит плеер."""
    log("--> [{0}] запрос (текст): {1}".format(context, url))
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            log("<-- [{0}] HTTP {1}, {2} байт".format(context, resp.status, len(raw)))
            return raw
    except Exception as e:  # noqa: BLE001
        log_error("[{0}] ошибка при скачивании: {1}".format(context, e))
        return None


def write_text_file(path, text):
    try:
        f = xbmcvfs.File(path, "w")
        f.write(text)
        f.close()
        log("diag: записан файл {0}".format(path))
        return True
    except Exception as e:  # noqa: BLE001
        log_error("diag: не удалось записать {0}: {1}".format(path, e))
        return False


def write_json_file(path, data):
    try:
        f = xbmcvfs.File(path, "w")
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
        f.close()
        log("diag: записан файл {0}".format(path))
        return True
    except Exception as e:  # noqa: BLE001
        log_error("diag: не удалось записать {0}: {1}".format(path, e))
        return False


def run_diagnostics():
    """Делает пробный поиск и запрос play_options, сохраняет ПОЛНЫЕ сырые
    JSON-ответы в файл в профиле аддона — чтобы можно было прислать их для
    точного анализа структуры API, вместо гадания по urllам."""
    query = xbmcgui.Dialog().input(
        "Тестовый поисковый запрос", defaultt="сериал", type=xbmcgui.INPUT_ALPHANUM
    )
    if not query:
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        return
    chan_text = xbmcgui.Dialog().input(
        "Ссылка или ID канала для проверки (можно пропустить)", type=xbmcgui.INPUT_ALPHANUM
    )
    forced_channel_id = extract_channel_id(chan_text) if chan_text else None

    profile_dir = xbmcvfs.translatePath(ADDON.getAddonInfo("profile"))
    if not xbmcvfs.exists(profile_dir):
        xbmcvfs.mkdirs(profile_dir)

    saved_files = []

    # Витрина Rutube (главная страница) — новая и самая непроверенная часть,
    # поэтому дампим её в первую очередь и подробно.
    feed_data = http_get_json(FEED_MAIN_URL, context="diag:feed_main")
    feed_path = profile_dir + "diag_feed_main.json"
    if write_json_file(feed_path, feed_data):
        saved_files.append(feed_path)
    if isinstance(feed_data, dict):
        feed = (feed_data.get("result") or {}).get("feed") or {}
        tabs = feed.get("tabs") or []
        log("diag: витрина — вкладок: {0}, названия: {1}".format(
            len(tabs), json.dumps([t.get("name") for t in tabs], ensure_ascii=False)))
        if tabs:
            first_tab = tabs[0]
            resources = first_tab.get("resources") or []
            log("diag: вкладка '{0}' — подборок: {1}, названия: {2}".format(
                first_tab.get("name"), len(resources),
                json.dumps([r.get("name") for r in resources], ensure_ascii=False)))
            if resources:
                log("diag: ПОЛНАЯ первая подборка вкладки '{0}': {1}".format(
                    first_tab.get("name"), json.dumps(resources[0], ensure_ascii=False)))

    search_url = "{0}?query={1}&page=1&format=json".format(
        SEARCH_API, urllib.parse.quote(query)
    )
    search_data = http_get_json(search_url, context="diag:search")
    search_path = profile_dir + "diag_search.json"
    if write_json_file(search_path, search_data):
        saved_files.append(search_path)

    first_item = None
    if isinstance(search_data, dict):
        results = search_data.get("results") or []
        if results and isinstance(results[0], dict):
            first_item = results[0]
            log("diag: ПОЛНЫЙ первый элемент результатов поиска: {0}".format(
                json.dumps(first_item, ensure_ascii=False)))

    play_data = None
    if first_item and first_item.get("id"):
        video_id = first_item["id"]
        opts_url = "{0}?no_404=true&referer={1}&pver=v2".format(
            PLAY_OPTIONS_API.format(video_id=video_id),
            urllib.parse.quote("https://rutube.ru/", safe=""),
        )
        play_data = http_get_json(opts_url, context="diag:play_options")
        play_path = profile_dir + "diag_play_options.json"
        if write_json_file(play_path, play_data):
            saved_files.append(play_path)
        if isinstance(play_data, dict):
            log("diag: ВСЕ ключи play_options: {0}".format(list(play_data.keys())))
            episode_field = play_data.get("episode")
            log("diag: play_options['episode'] = {0}".format(
                json.dumps(episode_field, ensure_ascii=False)
                if episode_field is not None else "поля нет"
            ))
            season_field = play_data.get("season")
            log("diag: play_options['season'] = {0}".format(
                json.dumps(season_field, ensure_ascii=False)
                if season_field is not None else "поля нет"
            ))

            # Скачиваем САМ манифест теми же заголовками, что использует
            # плеер при реальном воспроизведении — чтобы увидеть, что сервер
            # действительно отдаёт (а не что просто перечислено в URL).
            balancer = play_data.get("video_balancer") or {}
            manifest_url = balancer.get("m3u8") or balancer.get("default")
            if manifest_url:
                manifest_text = http_get_text(manifest_url, context="diag:manifest")
                if manifest_text is not None:
                    manifest_path = profile_dir + "diag_manifest.m3u8"
                    if write_text_file(manifest_path, manifest_text):
                        saved_files.append(manifest_path)
                    log("diag: манифест ({0} байт): {1}".format(
                        len(manifest_text), manifest_text[:3000]))

        author = first_item.get("author") if isinstance(first_item.get("author"), dict) else None
        if forced_channel_id:
            author = {"id": forced_channel_id}
        if author and author.get("id") is not None:
            author_id = author["id"]
            channel_url = "{0}?page=1&limit=100&sort=series_d&format=json".format(
                PERSON_API.format(author_id=author_id)
            )
            channel_data = http_get_json(channel_url, context="diag:channel")
            channel_path = profile_dir + "diag_channel.json"
            if write_json_file(channel_path, channel_data):
                saved_files.append(channel_path)
            if isinstance(channel_data, dict):
                titles = [
                    it.get("title") for it in (channel_data.get("results") or [])
                    if isinstance(it, dict)
                ]
                log("diag: заголовки видео канала {0} ({1} шт.): {2}".format(
                    author_id, len(titles), json.dumps(titles, ensure_ascii=False)))

            playlists_url = "{0}?page=1&limit=100&sort=series_d&format=json".format(
                PLAYLIST_USER_API.format(author_id=author_id)
            )
            playlists_data = http_get_json(playlists_url, context="diag:playlists")
            playlists_path = profile_dir + "diag_playlists.json"
            if write_json_file(playlists_path, playlists_data):
                saved_files.append(playlists_path)

            if isinstance(playlists_data, dict):
                pl_results = playlists_data.get("results") or []
                log("diag: плейлистов у канала {0}: {1}".format(author_id, len(pl_results)))
                if pl_results and isinstance(pl_results[0], dict):
                    first_playlist_id = pl_results[0].get("id")
                    if first_playlist_id is not None:
                        # Пробуем все варианты URL и сохраняем СТАТУС И ТЕЛО
                        # каждого ответа — раньше при ошибке в файл писалось
                        # просто null, и причину HTTP 400 было не увидеть.
                        variants = _playlist_videos_urls(first_playlist_id)(1)
                        probe_results = []
                        pv_data = None
                        for vurl in variants:
                            probe = http_probe(vurl)
                            probe_results.append(probe)
                            if pv_data is None and probe.get("json") is not None:
                                pv_data = probe["json"]
                            probe.pop("json", None)
                        pv_path = profile_dir + "diag_playlist_videos.json"
                        if write_json_file(pv_path, pv_data):
                            saved_files.append(pv_path)
                        probe_path = profile_dir + "diag_playlist_variants.json"
                        if write_json_file(probe_path, probe_results):
                            saved_files.append(probe_path)
                        log("diag: варианты URL плейлиста {0}: {1}".format(
                            first_playlist_id, json.dumps(probe_results, ensure_ascii=False)))
                        if isinstance(pv_data, dict):
                            pv_titles = [
                                it.get("title") for it in (pv_data.get("results") or [])
                                if isinstance(it, dict)
                            ]
                            log("diag: видео в плейлисте {0} ({1} шт.): {2}".format(
                                first_playlist_id, len(pv_titles),
                                json.dumps(pv_titles, ensure_ascii=False)))

    message = (
        "Файлы сохранены в:\n{0}\n\n"
        "Пришлите их (или соответствующие строки '[plugin.video.rutube] diag:' "
        "из kodi.log) для анализа.".format(profile_dir)
    )
    log("diag: готово. Сохранённые файлы: {0}".format(saved_files))
    xbmcgui.Dialog().ok("Rutube — диагностика", message)
    xbmcplugin.endOfDirectory(ADDON_HANDLE)


# --------------------------------------------------------------------- router

def router(paramstring):
    params = dict(urllib.parse.parse_qsl(paramstring))
    action = params.get("action")
    log("router: action={0}, params={1}".format(action, params))

    if action is None:
        list_main_menu()
    elif action == "library_add":
        library_add(params.get("kind"), params.get("name"), params)
    elif action == "comments":
        show_comments(params.get("video_id"), params.get("title"))
    elif action == "similar":
        list_similar(params.get("video_id"), params.get("title"))
    elif action == "similar_query":
        similar_query(params.get("query"))
    elif action == "download":
        download_video(params.get("video_id"), params.get("title"))
    elif action == "download_cancel":
        download_cancel()
    elif action == "open_channel":
        open_channel_by_link()
    elif action == "search_channels":
        if not params.get("query") and _history_enabled() and _history_load()["channels"]:
            list_search_history("channels")
        else:
            do_search_channels(params.get("query"), int(params.get("page", 1)))
    elif action == "search":
        if not params.get("query") and _history_enabled() and _history_load()["search"]:
            list_search_history("search")
        else:
            do_search(params.get("query"), int(params.get("page", 1)))
    elif action == "search_new":
        if params.get("kind") == "channels":
            do_search_channels(None, 1)
        else:
            do_search(None, 1)
    elif action == "search_hist":
        if params.get("kind") == "channels":
            do_search_channels(params.get("query"), 1, record=True)
        else:
            do_search(params.get("query"), 1, record=True)
    elif action == "history":
        list_history()
    elif action == "history_remove":
        history_remove(params.get("kind"), params.get("value"))
    elif action == "history_clear":
        history_clear(params.get("kind"))
    elif action == "search_series":
        show_search_series(params.get("query"), params.get("series"))
    elif action == "search_category":
        show_search_category(
            params.get("query"), params.get("category"), int(params.get("page", 1))
        )
    elif action == "categories":
        list_categories()
    elif action == "channel":
        list_channel(params.get("author_id"), int(params.get("page", 1)))
    elif action == "category_browse":
        list_category_browse(
            params.get("category_id"), params.get("category_name", ""),
            int(params.get("page", 1)),
        )
    elif action == "channel_category":
        show_channel_category(
            params.get("author_id"), params.get("category"), int(params.get("page", 1))
        )
    elif action == "channel_menu":
        list_channel_menu(params.get("author_id"))
    elif action == "channel_playlists":
        list_channel_playlists(params.get("author_id"), int(params.get("page", 1)))
    elif action == "playlist_videos":
        list_playlist_videos(params.get("playlist_id"), int(params.get("page", 1)))
    elif action == "channel_series":
        show_channel_series(params.get("author_id"), params.get("series"))
    elif action == "by_id":
        by_id_and_play()
    elif action == "play":
        play_video(params.get("video_id"))
    elif action == "feed_tabs":
        list_feed_tabs(params.get("feed_url", FEED_MAIN_URL))
    elif action == "feed_tab":
        list_feed_tab(params.get("slug"), params.get("feed_url", FEED_MAIN_URL))
    elif action == "feed_resource":
        list_feed_resource(
            params.get("resource_url"), params.get("title", ""),
            int(params["page"]) if params.get("page") else None,
            int(params.get("tvstart") or 0),
        )
    elif action == "tv_assign":
        tv_assign(params.get("video_id"), params.get("name"))
    elif action == "tv_unassign":
        tv_unassign(params.get("video_id"))
    elif action == "tv_reset":
        tv_reset()
    elif action == "tv_my":
        tv_my_channels()
    elif action == "tv_dial":
        tv_dial()
    elif action == "tv_digit":
        tv_digit(params.get("d"))
    elif action == "tv_keys_toggle":
        tv_keys_toggle()
    elif action == "diag":
        run_diagnostics()
    else:
        log_error("Неизвестное действие: {0}".format(action))
        list_main_menu()


if __name__ == "__main__":
    log("=== запуск, sys.argv={0} ===".format(sys.argv))
    router(sys.argv[2][1:])
