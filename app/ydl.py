#!/usr/bin/env python3
"""ydl — локальная страница для скачивания публичных папок Яндекс.Диска (13.10.2026).

Вставляешь публичные ссылки на корневые папки -> сервис листит, качает с докачкой, проверяет MD5,
повторяет структуру папок, сам ждёт и повторяет, когда Яндекс перестаёт отдавать данные (лимит скорости).
Только стандартная библиотека (браузер Playwright нужен лишь для ссылок под паролем).
Настройки — переменные окружения (см. docker-compose.yml): YDL_DATA, YDL_CONFIG, YDL_PORT, YDL_SLOTS, YDL_PASSWORD.
Данные: $YDL_DATA/<имя папки на Яндексе>/...   Состояние и служебные файлы: $YDL_CONFIG.
"""
import base64, hashlib, hmac, html as htmllib, json, mimetypes, os, queue, re, shutil, signal, sqlite3, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("YDL_DATA", "/data")                 # сюда складываются скачанные файлы
CONFIG = os.environ.get("YDL_CONFIG", "/config")           # состояние, списки файлов, сессии, журнал метрик
STATE = os.path.join(CONFIG, "state.json")
LISTS = os.path.join(CONFIG, "lists")
PORT = int(os.environ.get("YDL_PORT", "8085"))
AUTH_PASSWORD = os.environ.get("YDL_PASSWORD", "")         # пусто = страница без пароля (только для домашней сети!)
AUTH_FILE = os.path.join(CONFIG, "auth.json")              # пароль, заданный командой `passwd`; важнее переменных окружения
try:
    VERSION = read_text(os.path.join(APP_DIR, "VERSION")).strip()
except OSError:
    VERSION = "dev"
try:
    STATUS = read_text(os.path.join(APP_DIR, "STATUS")).strip()   # alpha / beta / stable
except OSError:
    STATUS = ""
PAGE_FILE = os.path.join(APP_DIR, "page.html")
BEHIND_PROXY = os.environ.get("YDL_BEHIND_PROXY") == "1"   # за Caddy (HTTPS): доверяем X-Forwarded-*
FILES_PAGE = os.path.join(APP_DIR, "files.html")
ALLOWED_HOSTS = {h.strip().lower() for h in (os.environ.get("YDL_ALLOWED_HOSTS", "") + "," + os.environ.get("YDL_HTTPS_HOST", "")).split(",") if h.strip()}
try:
    MIN_FREE = int(float(os.environ.get("YDL_MIN_FREE_GB", "2") or 2) * 2**30)   # меньше столько свободного места — загрузка приостанавливается
except ValueError:
    MIN_FREE = 2 * 2**30
MAX_BODY = 8 * 2**20                                       # больше этого POST-запрос не принимаем
WORKERS = 3
STALL_SECS = 30          # столько без байтов — считаем «Яндекс замолчал»
COOLDOWN = 45            # пауза после зависания, секунд (13.10.2026: было 600 — пропускали окна, когда Яндекс отдаёт)
API = "https://cloud-api.yandex.net/v1/disk/public/resources"

LOCK = threading.RLock()
S = {"links": [], "paused": False, "cooldown_until": 0, "speed": 0.0, "log": []}
SPEED_WIN = []           # (время, байт)


def log(msg):
    line = f"{time.strftime('%F %T')} {msg}"
    with LOCK:
        S["log"] = (S["log"] + [line])[-60:]
    print(line, flush=True)


def write_json(path, obj, mode=None):
    """Атомарная запись JSON: во временный файл (закрытый до замены), затем замена. Сбой посреди записи не портит старый файл."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def save():
    with LOCK:
        write_json(STATE, S, 0o600)


def safe_name(n):
    """Имя папки со стороны Яндекса становится частью пути на диске: убираем разделители и спецзначения."""
    n = str(n).replace("/", "_").replace("\\", "_").replace("\0", "_").strip()
    return "_" if n in ("", ".", "..") else n


YA_HOST = re.compile(r"(^|\.)(yandex\.(ru|com|net|by|kz|uz|ua|az|md|kg|tj|tm|fr|com\.tr|com\.ge)|ya\.ru)$")


def yandex_url_ok(u):
    """Скачиваем только по https и только с адресов Яндекса (защита, если ответ API или перенаправление подменили)."""
    try:
        p = urllib.parse.urlparse(u)
        return p.scheme == "https" and bool(YA_HOST.search((p.hostname or "").lower()))
    except Exception:
        return False


_DISK = {"t": 0.0, "low": False}


def disk_low():
    """True, если на диске загрузок свободно меньше YDL_MIN_FREE_GB (проверка не чаще раза в 5 секунд)."""
    now = time.time()
    if now - _DISK["t"] > 5:
        _DISK["t"] = now
        try:
            low = shutil.disk_usage(BASE).free < MIN_FREE
        except OSError:
            low = False
        if low and not _DISK["low"]:
            log(f"МАЛО МЕСТА на диске загрузок (меньше {MIN_FREE / 2**30:.1f} ГБ): загрузка приостановлена. Освободите место.")
        _DISK["low"] = low
    return _DISK["low"]


# ===== защита от блокировки Яндексом =====
GUARD = {"until": 0.0, "level": 0, "last": 0.0, "reason": "", "trips": 0, "soft": False}
MIN_GAP = 0.25            # не чаще 4 служебных запросов к API/перенаправлениям в секунду на весь сервис
_TH = {"t": 0.0}
THLOCK = threading.Lock()


def throttle():
    with THLOCK:
        now = time.time()
        w = max(0.0, _TH["t"] + MIN_GAP - now)
        _TH["t"] = now + w
    if w:
        time.sleep(w)


def guard_active():
    return GUARD["until"] > time.time()


def guard_trip(why, retry_after=0, escalate=True):
    """Яндекс просит притормозить (429/503/капча): общая пауза всех слотов, каждый повтор подряд вдвое длиннее (1 мин ... 1 час)."""
    now = time.time()
    with LOCK:
        if now < GUARD["until"]:
            return
        if now - GUARD["last"] > 1800:
            GUARD["level"] = 0
        GUARD["trips"] += 1
        if escalate:
            GUARD["level"] += 1
            pause = max(int(retry_after or 0), min(3600, 60 * 2 ** (GUARD["level"] - 1)))
        else:
            pause = max(int(retry_after or 0), 30)
        GUARD.update(until=now + pause, last=now, reason=why, soft=not escalate)
    log(f"ЗАЩИТА: Яндекс ответил «{why}». Все слоты на паузе {pause // 60} мин {pause % 60} с, чтобы не получить блокировку")


def banned(e):
    """HTTPError -> True, если это просьба притормозить (и включает защиту)."""
    if e.code in (429, 503):
        try:
            ra = int(e.headers.get("Retry-After") or 0)
        except Exception:
            ra = 0
        try:
            pu = urllib.parse.urlparse(e.geturl() or "")
            where = pu.netloc + "/".join(pu.path.split("/")[:4])
        except Exception:
            where = "?"
        # 503 чаще всего «сервис Яндекса временно недоступен» (не наша частота): короткая пауза 30 с без нарастания; 429 — нарастающая
        guard_trip(f"HTTP {e.code} от {where}", ra, escalate=(e.code == 429))
        return True
    loc = (e.headers.get("Location") or "") if e.headers else ""
    if loc:
        u = urllib.parse.urlparse(loc)
        # имя файла в адресе хранилища (…captcha.php) капчей не считаем: смотрим только хост и путь чужого сервера
        if not u.netloc.endswith("storage.yandex.net") and "captcha" in (u.netloc + u.path).lower():
            guard_trip("капча (" + u.netloc + ")")
            return True
    return False


def jget(url, tries=6):
    last = None
    for a in range(tries):
        if guard_active():
            raise Stall("пауза")
        throttle()
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last = e
            if banned(e):
                raise Stall("пауза")
            if e.code in (400, 403, 404):
                raise
            time.sleep(2 * (a + 1))
        except Exception as e:
            last = e
            time.sleep(2 * (a + 1))
    raise RuntimeError(f"API не отвечает: {last}")


def walk(key, path="/", link=None):
    items, offset = [], 0
    while True:
        ck = f"{path}|{offset}"
        d = wc_get(link, ck) if link is not None else None
        if d is None:
            q = urllib.parse.urlencode({"public_key": key, "path": path, "limit": 200, "offset": offset})
            d = jget(API + "?" + q)
            if link is not None:
                wc_put(link, ck, {"_embedded": d.get("_embedded", {})})
        emb = d.get("_embedded", {})
        got = emb.get("items", [])
        if link is not None and offset == 0:
            link["scan_dirs"] = link.get("scan_dirs", 0) + 1
        for i in got:
            p = path.rstrip("/") + "/" + i["name"]
            if i["type"] == "dir":
                items += walk(key, p, link)
            else:
                items.append([p, int(i.get("size") or 0), i.get("md5")])
                if link is not None:
                    link["scan_files"] = link.get("scan_files", 0) + 1
        offset += len(got)
        if not got or offset >= emb.get("total", 0):
            break
    return items


def safe_local(root, rel):
    local = os.path.normpath(os.path.join(root, rel.lstrip("/")))
    if not (local + os.sep).startswith(os.path.normpath(root) + os.sep):
        raise RuntimeError(f"подозрительный путь: {rel}")
    return local


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 22), b""):
            h.update(c)
    return h.hexdigest()


WALKC = {}      # id ссылки -> {ключ папки|offset: ответ}  (кэш обхода, переживает перезапуск)
WALKC_N = {}


def _wc_path(link):
    return os.path.join(LISTS, link["id"] + ".walk.json")


def wc_get(link, k):
    c = WALKC.get(link["id"])
    if c is None:
        try:
            c = read_json(_wc_path(link))
        except Exception:
            c = {}
        WALKC[link["id"]] = c
    return c.get(k)


def wc_flush(link):
    write_json(_wc_path(link), WALKC.get(link["id"], {}))


def wc_put(link, k, v):
    WALKC.setdefault(link["id"], {})[k] = v
    WALKC_N[link["id"]] = WALKC_N.get(link["id"], 0) + 1
    if WALKC_N[link["id"]] % 20 == 0:
        wc_flush(link)


def wc_drop(link):
    WALKC.pop(link["id"], None)
    WALKC_N.pop(link["id"], None)
    try:
        os.remove(_wc_path(link))
    except Exception:
        pass


class Stall(Exception):
    def __init__(self, msg="", until=None):
        super().__init__(msg)
        self.until = until   # время, раньше которого пробовать файл бессмысленно («больной» узел хранилища)


H360 = "https://disk.360.yandex.ru"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36"
SESS_DIR = os.path.join(CONFIG, "sessions")
NODE = {}                 # узел хранилища -> {"n": сбоев, "bad_until": время}
HMAP = {}                 # id ссылки -> {путь: hash} (для ссылок под паролем)
LOGIN_LOCK = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


_NOREDIR = urllib.request.build_opener(_NoRedirect)


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not yandex_url_ok(newurl):
            raise urllib.error.URLError("перенаправление не на адрес Яндекса отклонено")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_DL_OPENER = urllib.request.build_opener(_SafeRedirect)


def node_bad(host):
    return NODE.get(host, {}).get("bad_until", 0) > time.time()


def dc_add(host, ok):
    m = re.match(r"s\d+([a-z]+)\.", host or "")
    dc = m.group(1) if m else (host or "?")
    with LOCK:
        d = S.setdefault("dc", {}).setdefault(dc, {"ok": 0, "bad": 0})
        d["ok" if ok else "bad"] += 1


GOOD = {}                 # узел хранилища -> время последней успешной передачи данных
GOOD_TTL = 900            # столько секунд узел считается «заведомо здоровым» после успеха
LAST_NODE_FAIL = [0.0]    # когда в последний раз какой-то узел не ответил


def node_fail(host):
    dc_add(host, False)
    GOOD.pop(host, None)
    LAST_NODE_FAIL[0] = time.time()
    n = NODE.setdefault(host, {"n": 0, "bad_until": 0})
    n["n"] += 1
    wait = min(60, 10 * 2 ** min(n["n"] - 1, 3))
    n["bad_until"] = time.time() + wait
    log(f"узел хранилища {host} не отдаёт данные ({n['n']}-й раз), его файлы пропускаю на {wait} с")
    return n["bad_until"]


def resolve(href):
    """Мгновенно узнаёт узел хранилища, на который ведёт ссылка (по перенаправлению), не скачивая данные."""
    if not yandex_url_ok(href):
        raise RuntimeError("адрес скачивания не принадлежит Яндексу, отклонён")
    throttle()
    try:
        r = _NOREDIR.open(urllib.request.Request(href, headers={"User-Agent": UA}), timeout=15)
        r.close()
        return href, urllib.parse.urlparse(href).netloc
    except urllib.error.HTTPError as e:
        if banned(e):
            raise Stall("пауза")
        loc = e.headers.get("Location")
        if e.code in (301, 302, 303, 307, 308) and loc:
            if not yandex_url_ok(loc):
                raise RuntimeError("перенаправление не на адрес Яндекса отклонено")
            return loc, urllib.parse.urlparse(loc).netloc
        raise


def pw_session(link, force=False):
    sp = os.path.join(SESS_DIR, link["id"] + ".json")

    def load():
        try:
            return read_json(sp)
        except Exception:
            return None

    def fresh(x):
        return bool(x) and not x.get("error") and time.time() - x.get("ts", 0) <= 3.2 * 3600

    sess = load()
    if force or not fresh(sess):
        with LOGIN_LOCK:  # один браузер за раз; остальные ссылки при этом продолжают работать
            cur = load()
            need = (not fresh(cur)) or (force and (cur is None or cur.get("ts") == (sess or {}).get("ts")))
            if need:
                log(f"«{link.get('name') or link['url']}»: вхожу по паролю через браузер…")
                subprocess.run([sys.executable, os.path.join(APP_DIR, "pwlogin.py"), link["id"]], timeout=180, capture_output=True)
                cur = load()
            sess = cur
    if not sess:
        raise RuntimeError("вход по паролю не удался: нет ответа")
    if sess.get("error"):
        raise RuntimeError("вход по паролю не удался: " + sess["error"])
    return sess


def pw_api(link, name, payload, _retry=True):
    if guard_active():
        raise Stall("пауза")
    throttle()
    sess = pw_session(link)
    body = urllib.parse.quote(json.dumps(dict(payload, sk=sess["sk"], passToken=sess["passToken"]), separators=(",", ":")), safe="").encode()
    req = urllib.request.Request(f"{H360}/public/api/{name}", data=body, headers={
        "Content-Type": "text/plain", "X-Requested-With": "XMLHttpRequest", "X-Retpath-Y": link["url"],
        "Cookie": sess["cookie"], "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.load(r)
        if isinstance(d, dict) and d.get("error"):
            raise urllib.error.HTTPError(req.full_url, 400, "api error", {}, None)
        return d
    except urllib.error.HTTPError as e:
        if banned(e):
            raise Stall("пауза")
        if _retry and e.code in (400, 401, 403):
            pw_session(link, force=True)
            return pw_api(link, name, payload, _retry=False)
        raise
    except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
        raise Stall(f"API Яндекса не отвечает: {str(e)[:60]}")


def pw_walk(link, h, base=""):
    out, offset = [], 0
    prefix = h + ("/" if ":" in h else ":/")
    while True:
        ck = f"{h}|{offset}"
        d = wc_get(link, ck)
        if d is None:
            d = pw_api(link, "fetch-list", {"hash": h, "offset": offset, "withSizes": True})
            wc_put(link, ck, {"resources": d.get("resources", []), "completed": d.get("completed", True)})
        kids = [x for x in d.get("resources", []) if x.get("path", "").startswith(prefix)]
        if offset == 0:
            link["scan_dirs"] = link.get("scan_dirs", 0) + 1
        for x in kids:
            rel = base + "/" + x["name"]
            if x.get("type") == "dir":
                out += pw_walk(link, x["path"], rel)
            else:
                out.append((rel, int((x.get("meta") or {}).get("size") or 0), x["path"]))
                link["scan_files"] = link.get("scan_files", 0) + 1
        offset += len(kids)
        if d.get("completed", True) or not kids:
            break
    return out


def pw_single(link, sess):
    """Ссылка под паролем ведёт на один файл (fetch-list даёт 500): размер берём из Content-Range ответа хранилища."""
    last = None
    for _ in range(6):
        try:
            u = pw_api(link, "download-url", {"hash": sess["root_hash"]})["data"]["url"]
            url, node = resolve("https:" + u if u.startswith("//") else u)
            r = _DL_OPENER.open(urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": UA}), timeout=25)
            cr = r.headers.get("Content-Range", "")
            r.close()
            return [("/" + sess["name"], int(cr.split("/")[-1]), sess["root_hash"])]
        except Stall:
            raise
        except Exception as e:
            last = e
            time.sleep(3)
    raise RuntimeError(f"не удалось узнать размер файла: {str(last)[:80]}")


def get_href(link, rel):
    if link.get("pw"):
        u = pw_api(link, "download-url", {"hash": HMAP[link["id"]][rel]})["data"]["url"]
        return "https:" + u if u.startswith("//") else u
    q = {"public_key": link["url"]} if link.get("single") else {"public_key": link["url"], "path": rel}
    return jget(API + "/download?" + urllib.parse.urlencode(q))["href"]


TS = {}                   # номер слота -> что он делает сейчас {lid, link, file, size, phase, t0, got, last, node}
TSL = threading.local()   # номер слота текущего потока


def add_bytes(link, n):
    now = time.time()
    with LOCK:
        link["done_bytes"] += n
        link["last_progress"] = now
        SPEED_WIN.append((now, n))
        while SPEED_WIN and now - SPEED_WIN[0][0] > 15:
            SPEED_WIN.pop(0)
        S["speed"] = sum(b for _, b in SPEED_WIN) / 15.0
    if n > 0:
        hist_add("b", n)
        CNT["b"] += n
        d = TS.get(getattr(TSL, "idx", None))
        if d is not None:
            d["phase"], d["got"], d["last"] = "recv", d["got"] + n, now


def fetch_file(link, rel, size, md5):
    root = os.path.join(BASE, link["name"])
    local = safe_local(root, rel)
    if os.path.exists(local) and os.path.getsize(local) == size:
        return "skip"
    os.makedirs(os.path.dirname(local), exist_ok=True)
    if size == 0:
        open(local, "wb").close()
        return "ok"
    part = local + ".part"
    err = ""
    for attempt in range(4):
        if S["paused"] or link.get("stopped"):
            raise Stall("остановлено" if link.get("stopped") else "пауза")
        try:
            url, node = resolve(get_href(link, rel))
            FILENODE[(link["id"], rel)] = node
            d_ = TS.get(getattr(TSL, "idx", None))
            if d_ is not None:
                d_["node"] = node
            if node_bad(node):
                raise Stall(f"узел {node} пока недоступен", until=NODE[node]["bad_until"])
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have > size:
                os.remove(part)
                have = 0
            if have < size:
                req = urllib.request.Request(url, headers={"Range": f"bytes={have}-", "User-Agent": UA} if have else {"User-Agent": UA})
                try:
                    r = _DL_OPENER.open(req, timeout=STALL_SECS)
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        guard_trip("HTTP 429 от узла хранилища")
                        raise Stall("пауза")
                    raise RuntimeError(f"HTTP {e.code} от узла {node}")
                except (TimeoutError, urllib.error.URLError, OSError):
                    raise Stall(f"узел {node} не отвечает", until=node_fail(node))
                with r:
                    NODE.pop(node, None)
                    GOOD[node] = time.time()
                    dc_add(node, True)
                    append = have > 0 and r.status == 206
                    with open(part, "ab" if append else "wb") as f:
                        if not append and have:
                            add_bytes(link, -have)
                        try:
                            nchunk = 0
                            while True:
                                b = r.read(1 << 20)
                                if not b:
                                    break
                                f.write(b)
                                add_bytes(link, len(b))
                                nchunk += 1
                                if S["paused"] or link.get("stopped"):
                                    raise Stall("остановлено" if link.get("stopped") else "пауза")
                                if nchunk % 16 == 0 and disk_low():
                                    raise Stall("пауза")
                        except (TimeoutError, OSError):
                            raise Stall(f"узел {node}: тишина посреди передачи", until=node_fail(node))
            if os.path.getsize(part) < size:
                raise Stall(f"обрыв передачи ({os.path.getsize(part)} из {size})")  # вернёмся с докачкой
            if os.path.getsize(part) != size:
                raise RuntimeError(f"размер {os.path.getsize(part)} != {size}")
            if md5 and md5_of(part) != md5:
                os.remove(part)
                raise RuntimeError("MD5 не совпал")
            os.replace(part, local)
            return "ok"
        except Stall:
            raise
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
            raise Stall(str(e)[:100])
        except Exception as e:
            err = str(e)[:100]
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(err or "ошибка")


def is_excluded(link, rel):
    for e in link.get("excluded", []):
        if rel == e or rel.startswith(e + "/"):
            return True
    return False


def recount(link):
    """Пересчитывает прогресс по ВЫБРАННЫМ файлам; возвращает список ещё не скачанного."""
    lp = os.path.join(LISTS, link["id"] + ".json")
    files = read_json(lp)
    root = os.path.join(BASE, link["name"])
    pending, d_f, d_b, t_f, t_b, s_f, s_b = [], 0, 0, 0, 0, 0, 0
    for rel, size, md5 in files:
        if is_excluded(link, rel):
            s_f += 1
            s_b += size
            continue
        t_f += 1
        t_b += size
        try:
            local = safe_local(root, rel)
        except Exception:
            continue
        if os.path.exists(local) and os.path.getsize(local) == size:
            d_f += 1
            d_b += size
        else:
            pending.append((rel, size, md5))
    with LOCK:
        link.update(total_files=t_f, total_bytes=t_b, done_files=d_f, done_bytes=d_b, skipped_files=s_f, skipped_bytes=s_b)
        link["failed"] = [f for f in link.get("failed", []) if any(f["rel"] == q[0] for q in pending)]
    return pending


def ensure_list(link):
    """Получает структуру ссылки (для ссылок под паролем сначала входит), если её ещё нет."""
    lp = os.path.join(LISTS, link["id"] + ".json")
    hp = os.path.join(LISTS, link["id"] + ".hash.json")
    if not os.path.exists(lp) and link.get("pw"):
        link["status"] = "листинг"
        link["scan_dirs"] = link["scan_files"] = 0
        sess = pw_session(link)
        link["name"] = safe_name(sess["name"])
        try:
            files = pw_walk(link, sess["root_hash"])
        except urllib.error.HTTPError as e:
            if e.code != 500:
                raise
            files = pw_single(link, sess)
            link["single"] = True
            link["name"] = safe_name("Файл — " + sess["name"])
        write_json(lp, [[r_, s_, None] for r_, s_, h_ in files])
        write_json(hp, {r_: h_ for r_, s_, h_ in files})
        wc_drop(link)
        log(f"«{link['name']}» (под паролем): файлов {len(files)}, {sum(f[1] for f in files)/1048576:.0f} МБ")
        save()
    elif not os.path.exists(lp):
        link["status"] = "листинг"
        link["scan_dirs"] = link["scan_files"] = 0
        top = jget(API + "?" + urllib.parse.urlencode({"public_key": link["url"], "limit": 1}))
        if top.get("type") == "file":   # ссылка ведёт на один файл, а не на папку
            link["single"] = True
            link["name"] = safe_name("Файл — " + top["name"])
            files = [["/" + top["name"], int(top.get("size") or 0), top.get("md5")]]
        else:
            link["name"] = safe_name(top["name"])
            files = walk(link["url"], "/", link)
        write_json(lp, files)
        wc_drop(link)
        link["total_files"] = len(files)
        link["total_bytes"] = sum(f[1] for f in files)
        log(f"«{link['name']}»: файлов {len(files)}, {link['total_bytes']/1048576:.0f} МБ")
        save()
    if link.get("pw") and link["id"] not in HMAP:
        HMAP[link["id"]] = read_json(hp)
    if not link.get("name"):  # список уже был на диске (ссылку добавили повторно) — имя папки берём заново
        if link.get("pw"):
            link["name"] = safe_name(pw_session(link)["name"])
        else:
            link["name"] = safe_name(jget(API + "?" + urllib.parse.urlencode({"public_key": link["url"], "limit": 1}))["name"])


def prepare(link):
    """Новая ссылка: проверка (пароль) и получение структуры; качать начнём только после «В работу»."""
    link["status"] = "проверка"
    if link.get("pw"):
        link["pw_state"] = "проверяю пароль…"
        pw_session(link, force=bool(link.pop("check_pw", False)))
        link["pw_state"] = "✓ пароль принят"
    ensure_list(link)
    recount(link)
    if link.get("single"):
        link["started"] = True     # один файл: выбирать нечего, сразу в работу
    link["status"] = "в очереди" if link.get("started") else "выбор файлов"
    link["dirty"] = True
    if link.get("single"):
        log(f"«{link['name']}»: один файл, {link['total_bytes']/1048576:.0f} МБ — запускаю сразу")
    else:
        log(f"«{link['name']}»: структура получена ({link['total_files']} файлов, {link['total_bytes']/1048576:.0f} МБ) — выбери файлы и нажми «В работу»")


# ===================== общий планировщик скачивания =====================
SLOTS = max(1, min(32, int(os.environ.get("YDL_SLOTS", "8") or 8)))   # одновременных скачиваний по всем ссылкам
PER_LINK_MAX = 3           # не больше стольких слотов на одну ссылку, если ссылок несколько
STREAK_LIMIT = 10 ** 9           # столько зависаний (по таймауту) подряд — и ссылка «отдыхает»
ACTIVE_ST = ("в очереди", "качаю", "ждём Яндекс", "ждём (есть ошибки)")
LQ = {}                    # id ссылки -> {"queue": [...], "nb": {файл: время}, "fails": {файл: n}}
INFL = {}                  # id ссылки -> множество файлов, которые качаются прямо сейчас
LQLOCK = threading.RLock()
RR = [0]                   # указатель кругового обхода ссылок
FILENODE = {}              # (id ссылки, файл) -> узел хранилища
PROBE = set()              # узлы, которые сейчас проверяет один из слотов


CNT = {"ok": 0, "st": 0, "ok_last": 0, "st_last": 0, "b": 0, "b_last": 0, "t_last": 0.0}   # накопительные счётчики для журнала метрик


def hist_add(key, n=1):
    if key in ("ok", "st"):
        CNT[key] += n
    h = time.strftime("%F %H")
    with LOCK:
        hs = S.setdefault("hist", {})
        d = hs.setdefault(h, {"b": 0, "ok": 0, "st": 0})
        d[key] += n
        for k in sorted(hs)[:-72]:
            hs.pop(k, None)


def lq_build(link):
    pending = recount(link)
    if link.get("pw") and link["id"] not in HMAP:
        HMAP[link["id"]] = read_json(os.path.join(LISTS, link["id"] + ".hash.json"))
    q = {"queue": sorted(pending, key=lambda x: x[1]), "nb": {}, "fails": {}}  # сначала мелкие
    LQ[link["id"]] = q
    link["dirty"] = False
    return q


def lq_finalize(link):
    pending = recount(link)
    LQ.pop(link["id"], None)
    if not pending:
        link["status"] = "готово"
        link["finished"] = time.strftime("%F %T")
        link["wait_reason"], link["stuck_count"] = "", 0
        sk = link.get("skipped_files", 0)
        log(f"«{link['name']}» ГОТОВО ({link['done_files']} файлов" + (f", пропущено по выбору: {sk}" if sk else "") + "). Ссылку на Яндексе можно закрыть.")
    else:
        link["dirty"] = True


def pick():
    """Следующий файл для свободного слота: ссылки по кругу, пропуская отдыхающие и файлы на недоступных узлах."""
    now = time.time()
    if guard_active() or disk_low():
        return None
    with LQLOCK:
        links = [l for l in S["links"] if l["status"] in ACTIVE_ST and l.get("started", True) and not l.get("stopped")]
        n = len(links)
        if not n:
            return None
        cap = PER_LINK_MAX if n > 1 else SLOTS
        for k in range(n):
            link = links[(RR[0] + k) % n]
            lid = link["id"]
            if link.get("resume_at", 0) > now:
                continue
            q = LQ.get(lid)
            if q is None or link.get("dirty"):
                try:
                    q = lq_build(link)
                except Exception as e:
                    link["status"], link["error"] = "проверка", ""   # списка нет — пусть prepare получит его заново
                    log(f"«{link.get('name') or link['url']}»: список файлов не найден ({str(e)[:50]}), получаю заново")
                    continue
            infl = INFL.setdefault(lid, set())
            if len(infl) >= cap:
                continue
            best_wait = None
            for phase in (0, 1):                  # 0: файлы на узлах, с которых недавно успешно скачали; 1: все остальные
                i = 0
                while i < len(q["queue"]):
                    it = q["queue"][i]
                    rel = it[0]
                    if is_excluded(link, rel):
                        q["queue"].pop(i)
                        continue
                    if rel in infl:
                        i += 1
                        continue
                    h = FILENODE.get((lid, rel))
                    if phase == 0 and not (h and GOOD.get(h, 0) > now - GOOD_TTL):
                        i += 1
                        continue
                    at = q["nb"].get(rel, 0)
                    if h:
                        nd = NODE.get(h)
                        if nd and nd.get("bad_until", 0) > now:
                            at = max(at, nd["bad_until"])
                        elif h in PROBE:
                            at = max(at, now + 10)
                    if at <= now:
                        q["queue"].pop(i)
                        infl.add(rel)
                        RR[0] = (RR[0] + k + 1) % n
                        return link, it
                    if phase == 1:
                        best_wait = at if best_wait is None else min(best_wait, at)
                    i += 1
            if not q["queue"] and not infl:
                lq_finalize(link)
            elif q["queue"] and not infl and best_wait is not None:
                link["status"] = "ждём Яндекс"
                link["resume_at"] = best_wait
                link["stuck_count"] = len(q["queue"])
                link["wait_reason"] = "все оставшиеся файлы пока недоступны (узлы хранилища не отвечают)"
        return None


# ===== разведчик: заранее узнаёт узел хранилища у ближайших файлов (только пока Яндекс сбоит) =====
SCOUT_ON = os.environ.get("YDL_SCOUT", "1") != "0"
SCOUT_WINDOW = 100        # смотрим на столько файлов вперёд в очереди каждой ссылки
SCOUT_GAP = 2.0           # не чаще одного файла в 2 секунды (на файл уходит 2 лёгких запроса)
SCOUT_SKIP = {}           # (id ссылки, файл) -> до какого времени не пробовать снова
SC_RR = [0]


def scout_step():
    """Определяет узел одного из ближайших файлов, чтобы планировщик ставил первыми файлы на здоровых узлах.
    Работает только пока есть недавние сбои узлов: когда у Яндекса всё хорошо, лишних запросов нет. Возвращает True, если сделал запрос."""
    now = time.time()
    if not SCOUT_ON or S["paused"] or guard_active():
        return False
    if not (any(n.get("bad_until", 0) > now for n in NODE.values()) or now - LAST_NODE_FAIL[0] < 1800):
        return False
    cand = None
    with LQLOCK:
        links = [l for l in S["links"] if l["status"] in ACTIVE_ST and l.get("started", True) and not l.get("stopped")]
        n = len(links)
        for k in range(n):
            link = links[(SC_RR[0] + k) % n]
            q = LQ.get(link["id"])
            if not q:
                continue
            infl = INFL.get(link["id"], set())
            seen = 0
            for it in q["queue"]:
                rel = it[0]
                if is_excluded(link, rel) or rel in infl:
                    continue
                seen += 1
                if seen > SCOUT_WINDOW:
                    break
                key = (link["id"], rel)
                if key in FILENODE or SCOUT_SKIP.get(key, 0) > now:
                    continue
                cand = (link, rel)
                break
            if cand:
                SC_RR[0] = (SC_RR[0] + k + 1) % n
                break
    if not cand:
        return False
    link, rel = cand
    try:
        _, node = resolve(get_href(link, rel))
        FILENODE[(link["id"], rel)] = node
    except Exception:
        SCOUT_SKIP[(link["id"], rel)] = now + 600
    return True


def scout_loop():
    while True:
        try:
            did = scout_step()
        except Exception as e:
            log(f"разведчик: {e}")
            did = False
        time.sleep(SCOUT_GAP if did else 5)


def slot_worker(idx=0):
    TSL.idx = idx
    while True:
        try:
            if S["paused"]:
                time.sleep(2)
                continue
            got = pick()
            if not got:
                time.sleep(2)
                continue
            link, it = got
            lid = link["id"]
            rel, size, md5 = it
            with LOCK:
                link["active"] = link.get("active", 0) + 1
                link["status"] = "качаю"
                link["wait_reason"] = ""
            node = FILENODE.get((lid, rel))
            probing = False
            if node and node in NODE:
                with LQLOCK:
                    if node not in PROBE:
                        PROBE.add(node)
                        probing = True
            TS[idx] = {"lid": lid, "link": link.get("name") or link["url"][-10:], "file": rel.split("/")[-1], "size": size,
                       "phase": "conn", "t0": time.time(), "got": 0, "last": 0, "node": ""}
            try:
                st = fetch_file(link, rel, size, md5)
                with LOCK:
                    if st == "ok":
                        link["done_files"] += 1
                        link["last_file"] = rel
                    link.get("bad", {}).pop(rel, None)
                    link["failed"] = [f for f in link.get("failed", []) if f["rel"] != rel]
                    link["streak"], link["cool_n"] = 0, 0
                if st == "ok":
                    hist_add("ok")
                with LQLOCK:
                    q = LQ.get(lid)
                    if q:
                        q["nb"].pop(rel, None)
                        q["fails"].pop(rel, None)
            except Stall as e:
                msg = str(e)
                with LQLOCK:
                    q = LQ.get(lid)
                    if q is not None and not any(x[0] == rel for x in q["queue"]):
                        q["queue"].append(it)
                if msg not in ("пауза", "остановлено"):
                    with LOCK:
                        link.setdefault("bad", {})[rel] = msg[:80]
                    hist_add("st")
                    with LQLOCK:
                        q = LQ.get(lid)
                        if e.until:
                            if q is not None:
                                q["nb"][rel] = e.until
                        else:
                            n_ = 1
                            if q is not None:
                                n_ = q["fails"][rel] = q["fails"].get(rel, 0) + 1
                                q["nb"][rel] = time.time() + min(60, 10 * 2 ** min(n_ - 1, 3))
                            log(f"«{link['name']}»: {rel.split('/')[-1]} завис ({n_}-й раз): {msg[:70]}")
                    if not e.until:
                        with LOCK:
                            link["streak"] = link.get("streak", 0) + 1
                            if link["streak"] >= STREAK_LIMIT:
                                link["cool_n"] = link.get("cool_n", 0) + 1
                                wait = min(3600, 120 * 2 ** min(link["cool_n"] - 1, 5))
                                link["resume_at"] = time.time() + wait
                                link["status"] = "ждём Яндекс"
                                link["wait_reason"] = f"{STREAK_LIMIT} попыток подряд без данных; пауза {wait//60} мин"
                                link["streak"] = 0
                                log(f"«{link['name']}»: {STREAK_LIMIT} зависаний подряд, ссылка отдыхает {wait//60} мин, слоты уходят к другим")
            except Exception as e:
                with LOCK:
                    f = link.setdefault("failed", [])
                    if not any(x["rel"] == rel for x in f):
                        f.append({"rel": rel, "err": str(e)[:100]})
                with LQLOCK:
                    q = LQ.get(lid)
                    if q is not None:
                        k_ = q["fails"][rel] = q["fails"].get(rel, 0) + 1
                        q["nb"][rel] = time.time() + min(120, 20 * 2 ** min(k_ - 1, 3))
                        if not any(x[0] == rel for x in q["queue"]):
                            q["queue"].append(it)
                log(f"ОШИБКА {rel}: {e}")
            finally:
                TS.pop(idx, None)
                with LQLOCK:
                    if probing:
                        PROBE.discard(node)
                    INFL.get(lid, set()).discard(rel)
                with LOCK:
                    link["active"] = max(0, link.get("active", 1) - 1)
        except Exception as e:
            log(f"слот: {e}")
            time.sleep(3)


METRICS_DB = os.path.join(CONFIG, "metrics.db")
MLOCK = threading.Lock()
_MDB = [None]
RANGES = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000, "all": 0}


def mdb():
    if _MDB[0] is None:
        db = sqlite3.connect(METRICS_DB, check_same_thread=False, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS m (ts INTEGER PRIMARY KEY, speed REAL, busy INTEGER, links INTEGER, stuck INTEGER,"
                   " nodes INTEGER, files_done INTEGER, bytes_done INTEGER, files_total INTEGER, bytes_total INTEGER,"
                   " ok INTEGER, st INTEGER, disk_free INTEGER)")
        db.commit()
        _MDB[0] = db
    return _MDB[0]


def rate_since(sec):
    """Средняя скорость (байт/с) по журналу метрик за последние sec секунд и число замеров."""
    try:
        with MLOCK:
            r = mdb().execute("SELECT AVG(speed), COUNT(*) FROM m WHERE ts >= ?", (int(time.time()) - sec,)).fetchone()
        return (r[0] or 0.0), (r[1] or 0)
    except Exception:
        return 0.0, 0


def metrics_loop():
    last_prune = 0
    while True:
        time.sleep(10)
        try:
            with LOCK:
                t = compute_stats()
            ok, st = CNT["ok"] - CNT["ok_last"], CNT["st"] - CNT["st_last"]
            CNT["ok_last"], CNT["st_last"] = CNT["ok"], CNT["st"]
            nowm = time.time()
            dt_ = nowm - CNT["t_last"] if CNT["t_last"] else 10.0
            sp_ = max(0.0, (CNT["b"] - CNT["b_last"]) / max(1.0, dt_))   # точная средняя скорость за интервал между записями
            CNT["b_last"], CNT["t_last"] = CNT["b"], nowm
            with MLOCK:
                db = mdb()
                db.execute("INSERT OR REPLACE INTO m VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (int(time.time()), sp_, t["slots_busy"], t["links_active"], t["stuck"], len(t["bad_nodes"]),
                            t["files_done"], t["bytes_done"], t["files_total"], t["bytes_total"], ok, st, t["disk_free"]))
                db.commit()
                if time.time() - last_prune > 3600:
                    db.execute("DELETE FROM m WHERE ts < ?", (int(time.time()) - 90 * 86400,))
                    db.commit()
                    last_prune = time.time()
        except Exception as e:
            log(f"метрики: {e}")


def metrics_query(rng):
    span = RANGES.get(rng, 86400)
    now = int(time.time())
    with MLOCK:
        db = mdb()
        t0 = now - span if span else (db.execute("SELECT MIN(ts) FROM m").fetchone()[0] or now)
        bucket = max(10, ((max(60, now - t0) // 300 + 9) // 10) * 10)
        rows = db.execute(
            "SELECT (ts/?)*? AS b, AVG(speed), AVG(busy), MAX(busy), MAX(stuck), MAX(nodes), MAX(bytes_done), SUM(ok), SUM(st),"
            " MIN(disk_free), MAX(links) FROM m WHERE ts >= ? GROUP BY b ORDER BY b", (bucket, bucket, t0)).fetchall()
        sm = db.execute(
            "SELECT AVG(speed), MAX(speed), SUM(speed), SUM(ok), SUM(st), AVG(busy), MIN(disk_free), COUNT(*),"
            " SUM(CASE WHEN speed < 1024 THEN 1 ELSE 0 END) FROM m WHERE ts >= ?", (t0,)).fetchone()
    n = sm[7] or 0
    return {
        "range": rng, "from": t0, "to": now, "bucket": bucket,
        "points": [{"t": r[0], "speed": r[1] or 0, "busy": r[2] or 0, "busy_max": r[3] or 0, "stuck": r[4] or 0, "nodes": r[5] or 0,
                    "bytes_done": r[6] or 0, "ok": r[7] or 0, "st": r[8] or 0, "disk_free": r[9] or 0, "links": r[10] or 0} for r in rows],
        "summary": {"avg_speed": sm[0] or 0, "max_speed": sm[1] or 0, "bytes": (sm[2] or 0) * 10, "ok": sm[3] or 0, "st": sm[4] or 0,
                    "avg_busy": sm[5] or 0, "min_disk": sm[6] or 0, "samples": n, "idle_pct": round(100 * (sm[8] or 0) / n, 1) if n else 0},
    }


def compute_stats():
    now = time.time()
    links = S["links"]
    while SPEED_WIN and now - SPEED_WIN[0][0] > 15:
        SPEED_WIN.pop(0)
    speed = sum(b for _, b in SPEED_WIN) / 15.0
    ft = sum(l.get("total_files", 0) for l in links)
    fd = sum(l.get("done_files", 0) for l in links)
    bt = sum(l.get("total_bytes", 0) for l in links)
    bd = sum(l.get("done_bytes", 0) for l in links)
    waiting = []
    for l in links:
        if l["status"] in ("готово", "ошибка", "остановлено", "выбор файлов", "проверка", "листинг") or not l.get("started", True):
            continue
        if l.get("active", 0) == 0 and (l.get("resume_at", 0) > now or l["status"] in ("ждём Яндекс", "ждём (есть ошибки)")):
            until = l.get("resume_at", 0)
            why = l.get("wait_reason") or "файлы пока недоступны"
            if until > now:
                why += "; повтор в " + time.strftime("%H:%M", time.localtime(until))
            waiting.append({"name": l.get("name") or l["url"][-10:], "reason": why})
    try:
        du = shutil.disk_usage(BASE)
        disk_free, disk_total = du.free, du.total
    except Exception:
        disk_free = disk_total = 0
    rate6, n6 = rate_since(6 * 3600)
    left = bt - bd
    eta = left / rate6 if (n6 >= 6 and rate6 > 20 * 1024 and left > 0) else None
    slots, rc, cc = [], {}, {}
    for i in range(SLOTS):
        d = dict(TS.get(i) or {})
        if not d:
            slots.append(None)
            continue
        st_ = d["phase"]
        if st_ == "recv" and now - d["last"] > 8:
            st_ = "quiet"
        if st_ == "recv":
            rc[d["lid"]] = rc.get(d["lid"], 0) + 1
        else:
            cc[d["lid"]] = cc.get(d["lid"], 0) + 1
        slots.append({"link": d["link"], "file": d["file"], "size": d["size"], "state": st_, "age": int(now - d["t0"]),
                      "got": d["got"], "node": (d.get("node") or "").split(".")[0]})
    qs = []
    for l in sorted(links, key=lambda x: (x.get("name") or x["url"]).lower()):
        if l["status"] not in ACTIVE_ST or not l.get("started", True) or l.get("stopped"):
            continue
        lid = l["id"]
        q = LQ.get(lid)
        ready = cooling = 0
        nxt = 0
        if q:
            for it in list(q["queue"]):
                at = q["nb"].get(it[0], 0)
                nd = NODE.get(FILENODE.get((lid, it[0])))
                if nd and nd.get("bad_until", 0) > at:
                    at = nd["bad_until"]
                if at > now:
                    cooling += 1
                    nxt = at if not nxt else min(nxt, at)
                else:
                    ready += 1
        tot_, dn_ = l.get("total_files", 0), l.get("done_files", 0)
        r_, c_ = rc.get(lid, 0), cc.get(lid, 0)
        qs.append({"id": lid, "name": l.get("name") or l["url"][-10:], "status": l["status"], "total": tot_, "done": dn_,
                   "recv": r_, "conn": c_, "ready": ready, "cooling": cooling,
                   "other": max(0, tot_ - dn_ - r_ - c_ - ready - cooling), "next_at": nxt, "resume_at": l.get("resume_at", 0),
                   "reason": l.get("wait_reason") or ""})
    hs = S.get("hist", {})
    keys = sorted(hs)
    cur = time.strftime("%F %H")
    last24 = keys[-24:]
    c = hs.get(cur, {"b": 0, "ok": 0, "st": 0})
    return {
        "slots_total": SLOTS, "slots_busy": sum(l.get("active", 0) for l in links), "speed": speed,
        "links_active": sum(1 for l in links if l["status"] in ACTIVE_ST and l.get("started", True)),
        "files_total": ft, "files_done": fd, "files_left": max(0, ft - fd), "pct": round(100 * fd / ft, 1) if ft else 0,
        "bytes_total": bt, "bytes_done": bd,
        "stuck": sum(len(l.get("bad", {})) for l in links),
        "bad_nodes": [{"node": h, "n": v["n"], "until": v["bad_until"]} for h, v in NODE.items() if v.get("bad_until", 0) > now],
        "disk_free": disk_free, "disk_total": disk_total, "disk_low": disk_low(), "disk_min": MIN_FREE, "disk_path": os.environ.get("YDL_HOST_PATH") or BASE, "eta_s": eta, "rate6h": rate6,
        "slots": slots, "queues": qs,
        "guard": {"active": guard_active(), "until": GUARD["until"], "reason": GUARD["reason"], "level": GUARD["level"], "trips": GUARD["trips"], "soft": GUARD["soft"]},
        "waiting": waiting,
        "dc": [{"dc": k, "ok": v["ok"], "bad": v["bad"]} for k, v in sorted(S.get("dc", {}).items(), key=lambda kv: -(kv[1]["ok"] + kv[1]["bad"]))],
        "hour_bytes": c["b"], "hour_ok": c["ok"], "hour_stalls": c["st"],
        "day_bytes": sum(hs[k]["b"] for k in last24), "day_ok": sum(hs[k]["ok"] for k in last24), "day_stalls": sum(hs[k]["st"] for k in last24),
        "hist": [{"h": k[-2:] + ":00", "b": hs[k]["b"], "ok": hs[k]["ok"], "st": hs[k]["st"]} for k in reversed(keys[-8:])],
    }


def check_pw_now(link):
    """Кнопка «Проверить пароль»: реальный вход через браузер, результат — на карточке."""
    link["pw_state"] = "проверяю пароль…"
    try:
        pw_session(link, force=True)
        link["pw_state"] = "✓ пароль принят"
    except Exception as e:
        link["pw_state"] = "✗ " + str(e)[:120]
    log(f"«{link.get('name') or link['url']}»: {link['pw_state']}")
    save()


RUNNING = set()                          # id ссылок, с которыми сейчас работает свой поток проверки


def _run(link, fn):
    """Проверка новой ссылки (пароль, структура) в своём потоке с единой обработкой ошибок."""
    try:
        fn(link)
    except Stall as e:
        link["status"] = "проверка"
        link["resume_at"] = time.time() + 60
        log(f"«{link.get('name') or link['url']}»: {str(e)[:80]}; повторю через 1 мин")
    except urllib.error.HTTPError as e:
        link["status"] = "ошибка"
        link["error"] = f"HTTP {e.code}: ссылка закрыта?"
        log(f"«{link.get('name') or link['url']}»: {link['error']}")
    except Exception as e:
        link["status"] = "ошибка"
        link["error"] = str(e)[:150]
        if link.get("pw"):
            link["pw_state"] = "✗ " + str(e)[:120]
        log(f"«{link.get('name') or link['url']}»: {link['error']}")
    finally:
        RUNNING.discard(link["id"])
        save()


def manager():
    """Диспетчер проверки: для каждой новой ссылки запускает отдельный поток (скачиванием занимаются слоты)."""
    while True:
        try:
            time.sleep(2)
            now = time.time()
            for l in list(S["links"]):
                if l["id"] not in RUNNING and l["status"] == "проверка" and l.get("resume_at", 0) <= now:
                    RUNNING.add(l["id"])
                    threading.Thread(target=_run, args=(l, prepare), daemon=True).start()
            if int(now) % 30 < 2:
                save()
        except Exception as e:
            log(f"менеджер: {e}")
            time.sleep(5)


# ===== вход по паролю (страница /login, сессия в cookie) =====
COOKIE = "ydl_session"
SESSION_DAYS = 30
LOGIN_FILE = os.path.join(APP_DIR, "login.html")
SECRET_FILE = os.path.join(CONFIG, "secret.key")
_AUTH = {"mtime": None, "cfg": None}
_FAILS = {}               # ip -> [подряд неудач, заблокирован до]
_FAILS_LOCK = threading.Lock()


def hash_password(pw, salt=None):
    salt = salt or os.urandom(16).hex()
    return salt, hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()


def auth_config():
    """None — пароль выключен; иначе (проверка пароля, отпечаток пароля для подписи сессий).
    Источники по приоритету: auth.json (команда `passwd`), затем переменная YDL_PASSWORD."""
    try:
        mt = os.stat(AUTH_FILE).st_mtime
    except OSError:
        mt = None
    if mt != _AUTH["mtime"]:
        _AUTH.update(mtime=mt, cfg=None)
        if mt is not None:
            try:
                _AUTH["cfg"] = read_json(AUTH_FILE)
            except Exception:
                log("не смог прочитать auth.json — использую пароль из переменных окружения")
                _AUTH["mtime"] = None
    cfg = _AUTH["cfg"]
    if cfg is not None:
        if cfg.get("disabled"):
            return None
        return (lambda pw: hmac.compare_digest(hash_password(pw, cfg["salt"])[1], cfg["hash"])), cfg["hash"]
    if AUTH_PASSWORD:
        return (lambda pw: hmac.compare_digest(pw.encode("utf-8"), AUTH_PASSWORD.encode("utf-8"))), hashlib.sha256(AUTH_PASSWORD.encode()).hexdigest()
    return None


def _secret():
    try:
        return read_text(SECRET_FILE).strip()
    except OSError:
        sec = os.urandom(32).hex()
        os.makedirs(CONFIG, exist_ok=True)
        with open(SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(sec)
        os.chmod(SECRET_FILE, 0o600)
        return sec


def _sign(pwid, exp):
    return hmac.new((_secret() + pwid).encode(), str(exp).encode(), hashlib.sha256).hexdigest()


def make_session(pwid):
    exp = int(time.time()) + SESSION_DAYS * 86400
    return f"{exp}.{_sign(pwid, exp)}"


def session_valid(token, pwid):
    """Подпись зависит от текущего пароля: смена пароля сразу разлогинивает все устройства."""
    try:
        exp, sig = token.split(".", 1)
        return int(exp) > time.time() and hmac.compare_digest(sig, _sign(pwid, int(exp)))
    except Exception:
        return False


def passwd_cli(args):
    """docker compose exec ydl python /app/ydl.py passwd [--off] [новый пароль]   (без пароля — сгенерирует)"""
    off = "--off" in args
    pw = next((a for a in args if not a.startswith("--") and a != "on"), None)
    os.makedirs(CONFIG, exist_ok=True)
    if off:
        cfg = {"disabled": True}
        msg = "Пароль на страницу ОТКЛЮЧЁН. Не оставляйте так сервер, доступный из интернета."
    else:
        import secrets
        generated = pw is None
        pw = pw or secrets.token_hex(8)
        salt, h = hash_password(pw)
        cfg = {"salt": salt, "hash": h}
        msg = f"Пароль для входа: {pw}" + ("   (сгенерирован — запишите его)" if generated else "")
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    st = os.stat(CONFIG)                      # файл должен читаться пользователем, от которого работает сервис
    os.chmod(tmp, 0o600)
    try:
        os.chown(tmp, st.st_uid, st.st_gid)
    except OSError:
        pass
    os.replace(tmp, AUTH_FILE)
    print(msg + "\nГотово, перезапускать сервис не нужно (все устройства придётся войти заново).")


# ===== файловый менеджер: просмотр, поиск, новые папки, zip-архивы рядом с папкой =====
ARCH_FILE = os.path.join(CONFIG, "archives.json")
ALOCK = threading.RLock()
ARCH = {"jobs": []}          # задачи архивации; удалять можно только архивы из этого реестра
AQUEUE = queue.Queue()
ACANCEL = set()              # id задач, которые просят остановить
TMP_PREFIX = ".ydl-archive-"
RESERVE = 512 * 2**20        # столько места оставляем свободным при архивации


def arch_save():
    with ALOCK:
        write_json(ARCH_FILE, ARCH)


def norm_rel(d):
    """Путь внутри папки данных в каноническом виде («/a/b»): без «..» и повторных слэшей. Выходит за пределы — ошибка."""
    local = safe_local(BASE, d)
    rel = os.path.relpath(local, BASE).replace(os.sep, "/")
    return "/" if rel == "." else "/" + rel


def valid_name(n):
    return isinstance(n, str) and n not in ("", ".", "..") and "/" not in n and "\0" not in n and len(n.encode()) <= 255


def rel_join(d, n):
    return (d.rstrip("/") + "/" + n) if d.strip("/") else "/" + n


def inside_ok(local):
    """Путь (без разыменования ссылки на последнем звене) лежит внутри папки данных, а ссылки не уводят наружу."""
    real = os.path.realpath(local)
    base = os.path.realpath(BASE)
    return real == base or real.startswith(base + os.sep)


def fm_entries(local):
    out = []
    with os.scandir(local) as it:
        for e in it:
            if e.name.startswith(TMP_PREFIX):
                continue
            try:
                if not inside_ok(e.path):
                    continue
                st = e.stat()
                isd = e.is_dir()
            except OSError:
                continue
            out.append({"n": e.name, "d": isd, "s": 0 if isd else st.st_size, "t": int(st.st_mtime), "p": (not isd) and e.name.endswith(".part")})
    return out


def fm_walk(local, root_rel_name):
    """Файлы и пустые папки для архива: (путь на диске, имя в архиве, размер или None для папки)."""
    skip_out = {os.path.normpath(os.path.join(BASE, j["out"].lstrip("/"))) for j in ARCH["jobs"] if j.get("out")}
    if os.path.isfile(local):
        return [(local, root_rel_name, os.path.getsize(local))]
    res = []
    for dp, dns, fns in os.walk(local, followlinks=False):
        rel = os.path.relpath(dp, local)
        arc_dir = root_rel_name if rel == "." else root_rel_name + "/" + rel.replace(os.sep, "/")
        dns[:] = [d for d in dns if not os.path.islink(os.path.join(dp, d))]
        if not dns and not fns:
            res.append((dp, arc_dir + "/", None))
        for f in fns:
            fp = os.path.join(dp, f)
            if f.endswith(".part") or f.startswith(TMP_PREFIX) or os.path.islink(fp) or os.path.normpath(fp) in skip_out:
                continue
            try:
                res.append((fp, arc_dir + "/" + f, os.path.getsize(fp)))
            except OSError:
                pass
    return res


def fm_size(d, names, limit_s=15):
    t0, total, files, partial = time.time(), 0, 0, False
    for n in names:
        try:
            local = safe_local(BASE, rel_join(d, n))
        except Exception:
            continue
        for dp, dns, fns in os.walk(local, followlinks=False) if os.path.isdir(local) else [(None, [], [local])]:
            for f in fns:
                fp = f if dp is None else os.path.join(dp, f)
                if f.endswith(".part") or os.path.islink(fp):
                    continue
                try:
                    total += os.path.getsize(fp)
                    files += 1
                except OSError:
                    pass
            if time.time() - t0 > limit_s:
                return {"bytes": total, "files": files, "partial": True}
    return {"bytes": total, "files": files, "partial": partial}


def arch_public(j):
    return {k: j.get(k) for k in ("id", "dir", "names", "out", "out_name", "status", "files_total", "files_done", "bytes_total", "bytes_done", "size", "error", "created", "finished")}


def arch_unique_out(d, base):
    base = re.sub(r"[\\/\0]", "_", base).strip() or "files"
    n, cand = 1, base + ".zip"
    taken = {j.get("out") for j in ARCH["jobs"]}
    while os.path.exists(os.path.join(BASE, rel_join(d, cand).lstrip("/"))) or rel_join(d, cand) in taken:
        n += 1
        cand = f"{base}-{n}.zip"
    return rel_join(d, cand)


def arch_create(d, names):
    if not isinstance(d, str) or not d.startswith("/") or not isinstance(names, list) or not names or not all(valid_name(n) for n in names):
        return 400, {"error": "некорректный запрос"}
    try:
        d = norm_rel(d)
        local = safe_local(BASE, d)
    except Exception:
        return 400, {"error": "некорректный путь"}
    if not os.path.isdir(local):
        return 404, {"error": "папка не найдена"}
    for n in names:
        if not os.path.lexists(os.path.join(local, n)):
            return 404, {"error": f"не найдено: {n}"}
    if len(names) == 1:
        base = names[0] if os.path.isdir(os.path.join(local, names[0])) else os.path.splitext(names[0])[0]
    else:
        base = (os.path.basename(d.rstrip("/")) or "files") + "-выбранное"
    with ALOCK:
        out = arch_unique_out(d, base)
        j = {"id": os.urandom(5).hex(), "dir": d, "names": names, "out": out, "out_name": os.path.basename(out), "status": "в очереди",
             "files_total": 0, "files_done": 0, "bytes_total": 0, "bytes_done": 0, "size": 0, "error": "", "created": int(time.time()), "finished": 0}
        ARCH["jobs"].append(j)
        arch_save()
    AQUEUE.put(j["id"])
    log(f"архив «{j['out_name']}»: поставлен в очередь")
    return 200, arch_public(j)


def arch_fail(j, msg, tmp=None):
    if tmp:
        try:
            os.remove(tmp)
        except OSError:
            pass
    j["status"], j["error"], j["finished"] = "ошибка", msg, int(time.time())
    arch_save()
    log(f"архив «{j['out_name']}»: {msg}")


def arch_run(j):
    tmp = None
    try:
        j["status"] = "считаю"
        local = safe_local(BASE, j["dir"])
        items = []
        for n in j["names"]:
            p = os.path.join(local, n)
            if os.path.islink(p) or not os.path.exists(p):
                continue
            items += fm_walk(p, n)
        j["files_total"] = sum(1 for x in items if x[2] is not None)
        j["bytes_total"] = sum(x[2] for x in items if x[2] is not None)
        if not items:
            return arch_fail(j, "нечего архивировать")
        free = shutil.disk_usage(local).free
        if j["bytes_total"] + RESERVE > free:
            return arch_fail(j, f"не хватает места: нужно ≈{j['bytes_total']/2**30:.1f} ГБ, свободно {free/2**30:.1f} ГБ")
        outdir = os.path.dirname(safe_local(BASE, j["out"]))
        tmp = os.path.join(outdir, f"{TMP_PREFIX}{j['id']}.tmp")
        j["status"] = "собираю"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for path, arc, size in items:
                if j["id"] in ACANCEL:
                    break
                if size is None:
                    zf.writestr(zipfile.ZipInfo(arc), b"")
                    continue
                try:
                    zi = zipfile.ZipInfo.from_file(path, arc, strict_timestamps=False)
                    zi.compress_type = zipfile.ZIP_STORED
                    with open(path, "rb") as src, zf.open(zi, "w", force_zip64=True) as dst:
                        while True:
                            chunk = src.read(1 << 20)
                            if not chunk:
                                break
                            dst.write(chunk)
                            j["bytes_done"] += len(chunk)
                            if j["id"] in ACANCEL:
                                break
                except OSError:
                    pass                      # файл исчез или не читается: пропускаем
                j["files_done"] += 1
        if j["id"] in ACANCEL:
            ACANCEL.discard(j["id"])
            os.remove(tmp)
            j["status"], j["finished"] = "отменён", int(time.time())
            arch_save()
            log(f"архив «{j['out_name']}»: отменён")
            return
        final = safe_local(BASE, j["out"])
        os.replace(tmp, final)
        j["size"], j["status"], j["finished"] = os.path.getsize(final), "готов", int(time.time())
        arch_save()
        log(f"архив «{j['out_name']}» готов ({j['size']/2**20:.0f} МБ)")
    except Exception as e:
        arch_fail(j, f"{type(e).__name__}: {str(e)[:120]}", tmp)


def arch_worker():
    while True:
        jid = AQUEUE.get()
        with ALOCK:
            j = next((x for x in ARCH["jobs"] if x["id"] == jid), None)
        if j and j["status"] == "в очереди":
            arch_run(j)


def arch_load():
    try:
        ARCH.update(read_json(ARCH_FILE))
    except Exception:
        pass
    now = int(time.time())
    keep = []
    for j in ARCH["jobs"]:
        if j["status"] in ("в очереди", "считаю", "собираю"):
            j["status"], j["error"], j["finished"] = "ошибка", "прервано перезапуском сервиса", now
            try:
                os.remove(os.path.join(os.path.dirname(safe_local(BASE, j["out"])), f"{TMP_PREFIX}{j['id']}.tmp"))
            except Exception:
                pass
        if j["status"] in ("ошибка", "отменён") and now - j.get("finished", now) > 86400:
            continue
        keep.append(j)
    ARCH["jobs"] = keep
    arch_save()


def fm_delete(d, names, confirm):
    """Удаляет выбранное с диска. Согласовано с очередью загрузок: то, что лежит внутри папки ссылки, исключается из загрузки
    (иначе ydl скачал бы это заново), а удаление всей папки ссылки убирает и саму ссылку из списка."""
    if confirm is not True:
        return 400, {"error": "нужно подтверждение удаления"}
    if not isinstance(d, str) or not d.startswith("/") or not isinstance(names, list) or not names or not all(valid_name(n) for n in names):
        return 400, {"error": "некорректный запрос"}
    try:
        d = norm_rel(d)
        local_dir = safe_local(BASE, d)
    except Exception:
        return 400, {"error": "некорректный путь"}
    if not os.path.isdir(local_dir) or not inside_ok(local_dir):
        return 404, {"error": "папка не найдена"}
    deleted, errors, freed, removed_links = [], [], 0, []
    for n in names:
        if n.startswith(TMP_PREFIX):
            continue
        p = os.path.join(local_dir, n)
        if not os.path.lexists(p):
            continue
        rel = rel_join(d, n)
        parts = [x for x in rel.split("/") if x]
        size = fm_size(d, [n], limit_s=60)["bytes"] if not os.path.islink(p) else 0
        with LOCK:                                             # сначала согласуем очередь, потом удаляем файлы
            link = next((l for l in S["links"] if l.get("name") and l["name"] == parts[0]), None)
            if link and len(parts) == 1:
                S["links"].remove(link)
                removed_links.append(link["name"])
                for f in (os.path.join(LISTS, link["id"] + ".json"), os.path.join(LISTS, link["id"] + ".walk.json"), os.path.join(SESS_DIR, link["id"] + ".json")):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                log(f"«{link['name']}»: папка удалена целиком, ссылка убрана из списка")
            elif link:
                sub = "/" + "/".join(parts[1:])
                ex = link.setdefault("excluded", [])
                if not any(sub == e or sub.startswith(e + "/") for e in ex):
                    ex[:] = [e for e in ex if not e.startswith(sub + "/")] + [sub]
                if link["status"] in ("готово", "ждём Яндекс", "ждём (есть ошибки)", "ошибка"):
                    link["status"] = "в очереди"
                link["resume_at"], link["nb"], S["cooldown_until"], link["dirty"] = 0, {}, 0, True
                if os.path.exists(os.path.join(LISTS, link["id"] + ".json")):
                    recount(link)
                log(f"«{link['name']}»: удалено {sub}, исключено из загрузки")
            save()
        try:
            if os.path.islink(p) or os.path.isfile(p):
                os.remove(p)
            else:
                shutil.rmtree(p)
            deleted.append(n)
            freed += size
        except OSError as e:
            errors.append(f"{n}: {str(e)[:80]}")
        with ALOCK:                                            # убрать из реестра архивов то, что удалили вместе с папкой
            before = len(ARCH["jobs"])
            ARCH["jobs"] = [j for j in ARCH["jobs"] if not (j.get("out") == rel or j.get("out", "").startswith(rel + "/"))]
            if len(ARCH["jobs"]) != before:
                arch_save()
    log(f"удалено через менеджер: {len(deleted)} элем., освобождено ≈{freed/2**20:.0f} МБ" + (f", ошибок: {len(errors)}" if errors else ""))
    return 200, {"deleted": deleted, "freed": freed, "errors": errors, "removed_links": removed_links}


def fm_api(method, path, q, body):
    """API файлового менеджера. Возвращает (код, объект) или None, если путь не наш."""
    if method == "GET" and path == "/api/ls":
        rel = q.get("path", ["/"])[0] or "/"
        try:
            rel = norm_rel(rel)
            local = safe_local(BASE, rel)
        except Exception:
            return 400, {"error": "некорректный путь"}
        if not os.path.isdir(local) or not inside_ok(local):
            return 404, {"error": "папка не найдена"}
        try:
            ents = fm_entries(local)
        except OSError as e:
            return 500, {"error": str(e)[:100]}
        with ALOCK:
            mine = {j["out"]: j for j in ARCH["jobs"] if j["status"] == "готов"}
        for e in ents:
            j = mine.get(rel_join(rel, e["n"]))
            if j:
                e["a"] = j["id"]
        du = shutil.disk_usage(local)
        return 200, {"path": "/" + rel.strip("/") if rel.strip("/") else "/", "entries": ents, "free": du.free, "total": du.total}
    if method == "GET" and path == "/api/find":
        qs = q.get("q", [""])[0].strip().lower()
        if len(qs) < 2:
            return 200, {"results": [], "partial": False}
        t0, res, partial = time.time(), [], False
        for dp, dns, fns in os.walk(BASE, followlinks=False):
            dns[:] = [d for d in dns if not d.startswith(TMP_PREFIX)]
            for nm, isd in [(x, True) for x in dns] + [(x, False) for x in fns]:
                if qs in nm.lower() and not nm.startswith(TMP_PREFIX):
                    fp = os.path.join(dp, nm)
                    try:
                        sz = 0 if isd else os.path.getsize(fp)
                    except OSError:
                        continue
                    res.append({"p": "/" + os.path.relpath(fp, BASE).replace(os.sep, "/"), "d": isd, "s": sz})
            if len(res) >= 300 or time.time() - t0 > 8:
                partial = True
                break
        return 200, {"results": res[:300], "partial": partial}
    if method == "GET" and path == "/api/archives":
        with ALOCK:
            return 200, {"jobs": [arch_public(j) for j in ARCH["jobs"]]}
    if method == "POST" and path == "/api/size":
        d = json.loads(body or "{}")
        try:
            sdir = norm_rel(d.get("dir", "/"))
        except Exception:
            return 400, {"error": "некорректный путь"}
        return 200, fm_size(sdir, d.get("names", []) if isinstance(d.get("names"), list) else [])
    if method == "POST" and path == "/api/mkdir":
        d = json.loads(body or "{}")
        name = d.get("name", "").strip()
        if not valid_name(name):
            return 400, {"error": "недопустимое имя папки"}
        try:
            local = safe_local(BASE, rel_join(norm_rel(d.get("dir", "/")), name))
            if not inside_ok(os.path.dirname(local)):
                return 400, {"error": "некорректный путь"}
            os.mkdir(local)
        except FileExistsError:
            return 409, {"error": "такая папка уже есть"}
        except Exception as e:
            return 400, {"error": str(e)[:100]}
        return 200, {"ok": True}
    if method == "POST" and path == "/api/delete":
        d = json.loads(body or "{}")
        return fm_delete(d.get("dir"), d.get("names"), d.get("confirm"))
    if method == "POST" and path == "/api/archive":
        d = json.loads(body or "{}")
        return arch_create(d.get("dir"), d.get("names"))
    if method == "POST" and path in ("/api/archive/cancel", "/api/archive/delete"):
        jid = q.get("id", [""])[0]
        with ALOCK:
            j = next((x for x in ARCH["jobs"] if x["id"] == jid), None)
            if not j:
                return 404, {"error": "архив не найден"}
            if path.endswith("cancel"):
                if j["status"] in ("в очереди", "считаю", "собираю"):
                    if j["status"] == "в очереди":
                        j["status"], j["finished"] = "отменён", int(time.time())
                    else:
                        ACANCEL.add(jid)
                    arch_save()
                return 200, {"ok": True}
            if j["status"] in ("в очереди", "считаю", "собираю"):
                return 409, {"error": "архив ещё собирается: сначала отмените"}
            if j["status"] == "готов":
                try:
                    f = safe_local(BASE, j["out"])
                    if os.path.isfile(f) and not os.path.islink(f):
                        os.remove(f)
                except OSError as e:
                    return 500, {"error": str(e)[:100]}
            ARCH["jobs"] = [x for x in ARCH["jobs"] if x["id"] != jid]
            arch_save()
        log(f"архив «{j['out_name']}» удалён")
        return 200, {"ok": True}
    return None



# ===== добавление ссылок =====
LINK_RE = re.compile(r"^https://(disk\.360\.yandex\.ru|disk\.yandex\.(ru|com))/d/[\w\-]+/?$")


def add_links(rows):
    """rows: [{"url": ..., "pw": ...}] из формы (вызывать под LOCK). Возвращает отчёт по каждой строке в том же порядке."""
    out = []
    have = {l["url"].rstrip("/"): l for l in S["links"]}
    for r in rows:
        url, pw = str(r.get("url", "")).strip(), str(r.get("pw", "")).strip()
        if not LINK_RE.match(url):
            out.append({"ok": False, "reason": "нужна ссылка на папку вида https://disk.yandex.ru/d/…" + (" (ссылку yadi.sk откройте и скопируйте адрес из браузера)" if "yadi.sk" in url else "")})
            continue
        url = url.rstrip("/")
        if url in have:
            if pw:
                have[url]["pw"] = pw
                log(f"для ссылки {url} задан пароль (длина {len(pw)})")
            out.append({"ok": True, "status": "пароль обновлён" if pw else "уже в списке"})
            continue
        item = {"id": hashlib.md5(url.encode()).hexdigest()[:10], "url": url, "name": "", "status": "проверка", "started": False,
                "total_files": 0, "total_bytes": 0, "done_files": 0, "done_bytes": 0, "failed": []}
        if pw:
            item["pw"] = pw
        S["links"].append(item)
        have[url] = item
        log(f"добавлена ссылка {url}" + (f" с паролем (длина {len(pw)})" if pw else ""))
        out.append({"ok": True, "status": "добавлена"})
    return out


LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain", ".home")
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def host_allowed(host):
    """Защита от DNS-rebinding: принимаем запросы только с «своим» адресом в заголовке Host
    (IP, localhost, короткое имя без точки, домашние зоны .local/.lan, заданные в YDL_ALLOWED_HOSTS/YDL_HTTPS_HOST)."""
    h = (host or "").strip().lower()
    if h.startswith("["):                                   # [::1]:8085
        return True
    h = h.split(":")[0]
    if not h or IPV4_RE.match(h) or h == "localhost" or "." not in h:
        return True
    return h in ALLOWED_HOSTS or h.endswith(LOCAL_SUFFIXES)


class H(BaseHTTPRequestHandler):
    timeout = 120                                           # сокет без движения 2 минуты — соединение закрываем (защита от «медленных» клиентов)

    def log_message(self, *a):
        pass

    def _std_headers(self, page=False):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        if page:
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self._std_headers(page=ctype.startswith("text/html"))
        self.end_headers()
        self.wfile.write(b)

    def _precheck(self, post=False):
        """Общие проверки запроса. False — ответ уже отправлен."""
        if not host_allowed(self.headers.get("Host")):
            self._send(421, "Недопустимый адрес в заголовке Host. Если вы открываете ydl по другому имени, добавьте его в YDL_ALLOWED_HOSTS (см. README).", "text/plain; charset=utf-8")
            return False
        if post:
            sfs = self.headers.get("Sec-Fetch-Site")
            origin = self.headers.get("Origin")
            if (sfs and sfs not in ("same-origin", "none")) or (origin and urllib.parse.urlparse(origin).netloc != (self.headers.get("Host") or "")):
                log(f"отклонён POST {self.path}: Sec-Fetch-Site={sfs!r}, Origin={origin!r}, Host={self.headers.get('Host')!r}")
                self._send(403, json.dumps({"error": "запрос с чужого сайта отклонён"}))
                return False
            if self.path not in ("/login", "/logout") and self.headers.get("X-YDL") != "1":
                self._send(403, json.dumps({"error": "нужен заголовок X-YDL"}))
                return False
        return True

    def _body(self, limit=MAX_BODY):
        """Тело POST-запроса; None — уже ответили ошибкой."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0:
            self._send(400, json.dumps({"error": "некорректный Content-Length"}))
            return None
        if n > limit:
            self._send(413, json.dumps({"error": "слишком большой запрос"}))
            return None
        return self.rfile.read(n).decode("utf-8", "ignore") if n else ""

    def _cookie(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return ""

    def _redirect(self, where, cookie=None, code=303):
        self.send_response(code)
        self.send_header("Location", where)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self._std_headers()
        self.end_headers()

    def _authorized(self):
        cfg = auth_config()
        if cfg is None or session_valid(self._cookie(), cfg[1]):
            return True
        p = urllib.parse.urlparse(self.path).path
        if p == "/" or p.startswith("/files"):               # страницы — на вход, запросы данных — 401
            self._redirect("/login?next=" + urllib.parse.quote(self.path, safe="/"), code=302)
        else:
            self._send(401, json.dumps({"error": "auth"}))
        return False

    def _login_page(self, err="", nxt="/", code=200):
        html = read_text(LOGIN_FILE)
        html = html.replace("{{ERR}}", htmllib.escape(err)).replace("{{NEXT}}", htmllib.escape(nxt, quote=True))
        self._send(code, html, "text/html; charset=utf-8")

    def _login_post(self):
        raw = self._body(4096)
        if raw is None:
            return
        form = urllib.parse.parse_qs(raw)
        pw = form.get("password", [""])[0]
        nxt = form.get("next", ["/"])[0]
        if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/login"):
            nxt = "/"
        cfg = auth_config()
        if cfg is None:
            return self._redirect("/")
        ip = self.client_address[0]
        if BEHIND_PROXY:                                   # реальный адрес клиента, а не адрес прокси
            ip = (self.headers.get("X-Forwarded-For") or ip).split(",")[0].strip() or ip
        with _FAILS_LOCK:
            if len(_FAILS) > 2000:                         # не даём таблице расти бесконечно
                for k in [k for k, v in _FAILS.items() if v[1] < time.time() - 3600]:
                    _FAILS.pop(k, None)
            f = _FAILS.get(ip, [0, 0.0])
            wait = f[1] - time.time()
        if wait > 0:
            return self._login_page(f"Слишком много попыток. Подождите {int(wait) + 1} с.", nxt, 429)
        if cfg[0](pw):
            with _FAILS_LOCK:
                _FAILS.pop(ip, None)
            ck = f"{COOKIE}={make_session(cfg[1])}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"
            if BEHIND_PROXY and self.headers.get("X-Forwarded-Proto") == "https":
                ck += "; Secure"                              # cookie только по HTTPS
            return self._redirect(nxt, ck)
        with _FAILS_LOCK:
            f[0] += 1
            if f[0] >= 5:
                f[1] = time.time() + min(900, 30 * 2 ** (f[0] - 5))   # 30 с, 1 мин, 2 мин ... до 15 мин
            _FAILS[ip] = f
        log(f"неудачная попытка входа с {ip} ({f[0]})")
        time.sleep(1)
        self._login_page("Неверный пароль", nxt, 401)

    def _files(self):
        """Раздел «Файлы»: просмотр папок со скачанным и скачивание в браузере (с докачкой по Range)."""
        u = urllib.parse.urlparse(self.path)
        rel = urllib.parse.unquote(u.path[len("/files"):]) or "/"
        try:
            local = safe_local(BASE, rel)
        except Exception:
            return self._send(400, "bad path", "text/plain")
        if os.path.isdir(local):
            if not u.path.endswith("/"):
                self.send_response(301)
                self.send_header("Location", u.path + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._send(200, read_text(FILES_PAGE), "text/html; charset=utf-8")
        if not os.path.isfile(local):
            return self._send(404, "not found", "text/plain")
        size = os.path.getsize(local)
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        ctype = mimetypes.guess_type(local)[0] or "application/octet-stream"
        # В браузере открываем только заведомо «пассивное»: картинки (кроме SVG), аудио, видео, PDF и обычный текст.
        # HTML, SVG, XML, скрипты и всё неизвестное — только скачиванием: иначе скачанный из чужой папки файл мог бы
        # выполнить скрипт от имени сервиса (с вашей сессией).
        inline = (ctype.split("/")[0] in ("video", "audio") or ctype == "application/pdf" or ctype == "text/plain"
                  or (ctype.startswith("image/") and "svg" not in ctype))
        if not inline:
            ctype = "application/octet-stream"
        fname = urllib.parse.quote(os.path.basename(local))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if ctype != "application/pdf":                       # песочница: даже если файл как-то откроется, скрипты в нём не работают
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'")
        self.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8\'\'{fname}")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(local, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, "ok", "text/plain")
        if not self._precheck():
            return
        if self.path.split("?")[0] == "/login":
            if auth_config() is None:
                return self._redirect("/")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._login_page("", q.get("next", ["/"])[0])
        if not self._authorized():
            return
        if self.path == "/api/version":
            return self._send(200, json.dumps({"version": VERSION, "status": STATUS}))
        if self.path.startswith("/files"):
            return self._files()
        u0 = urllib.parse.urlparse(self.path)
        if u0.path.startswith("/api/") and u0.path in ("/api/ls", "/api/find", "/api/archives"):
            r = fm_api("GET", u0.path, urllib.parse.parse_qs(u0.query), None)
            return self._send(r[0], json.dumps(r[1], ensure_ascii=False))
        if self.path == "/":
            html = read_text(PAGE_FILE)  # правки интерфейса применяются без перезапуска
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path.startswith("/api/tree"):
            qq = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            lid = qq.get("id", [""])[0]
            link = next((l for l in S["links"] if l["id"] == lid), None)
            lp = os.path.join(LISTS, lid + ".json")
            if not link or not os.path.exists(lp):
                self._send(404, json.dumps({"error": "список файлов ещё не получен"}, ensure_ascii=False))
                return
            root = os.path.join(BASE, link["name"])
            out = []
            for rel, size, md5 in read_json(lp):
                try:
                    local = safe_local(root, rel)
                    done = os.path.exists(local) and os.path.getsize(local) == size
                except Exception:
                    done = False
                out.append([rel, size, done])
            self._send(200, json.dumps({"name": link["name"], "files": out, "excluded": link.get("excluded", []),
                                        "bad": list(link.get("bad", {}).keys()), "started": link.get("started", True)}, ensure_ascii=False))
        elif self.path.startswith("/api/metrics"):
            qq = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                self._send(200, json.dumps(metrics_query(qq.get("range", ["24h"])[0]), ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)[:100]}))
        elif self.path == "/api/state":
            with LOCK:
                pub = dict(S)
                pub["links"] = [{**{k: v for k, v in l.items() if k != "pw"}, "has_pw": bool(l.get("pw"))} for l in S["links"]]
                pub["stats"] = compute_stats()
                pub["auth"] = auth_config() is not None
                self._send(200, json.dumps(pub, ensure_ascii=False))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if not self._precheck(post=True):
            return
        if self.path == "/login":
            return self._login_post()
        if self.path == "/logout":
            return self._redirect("/login", f"{COOKIE}=; Path=/; HttpOnly; Max-Age=0")
        if not self._authorized():
            return
        body = self._body()
        if body is None:
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/api/size", "/api/mkdir", "/api/delete", "/api/archive", "/api/archive/cancel", "/api/archive/delete"):
            try:
                r = fm_api("POST", u.path, q, body)
            except Exception as e:
                r = (400, {"error": f"{type(e).__name__}: {str(e)[:80]}"})
            return self._send(r[0], json.dumps(r[1], ensure_ascii=False))
        res = {}
        with LOCK:
            if u.path == "/api/add":
                try:
                    rows = json.loads(body)
                    assert isinstance(rows, list) and all(isinstance(r, dict) for r in rows)
                except Exception:
                    rows = []
                res = {"results": add_links(rows)}
            elif u.path == "/api/pause":
                S["paused"] = not S["paused"]
                S["cooldown_until"] = 0
                log("пауза" if S["paused"] else "продолжаю")
            elif u.path == "/api/remove":
                S["links"] = [l for l in S["links"] if l["id"] != q.get("id", [""])[0]]
            elif u.path == "/api/exclude":
                lid = q.get("id", [""])[0]
                for l in S["links"]:
                    if l["id"] == lid:
                        try:
                            ex = [x for x in json.loads(body) if isinstance(x, str) and x.startswith("/")]
                        except Exception:
                            ex = l.get("excluded", [])
                        l["excluded"] = ex
                        if l["status"] in ("готово", "ждём Яндекс", "ждём (есть ошибки)", "ошибка"):
                            l["status"] = "в очереди"
                        l["resume_at"], l["nb"], S["cooldown_until"], l["dirty"] = 0, {}, 0, True
                        if os.path.exists(os.path.join(LISTS, lid + ".json")) and l.get("name"):
                            recount(l)
                        log(f"«{l.get('name')}»: выбор файлов сохранён, пропускаем {len(ex)} пунктов; скачать осталось {l.get('total_files',0)-l.get('done_files',0)} файлов")
            elif u.path == "/api/check":
                for l in S["links"]:
                    if l["id"] == q.get("id", [""])[0] and l.get("pw"):
                        threading.Thread(target=check_pw_now, args=(l,), daemon=True).start()
            elif u.path in ("/api/start", "/api/stop"):
                for l in S["links"]:
                    if l["id"] == q.get("id", [""])[0]:
                        if u.path == "/api/start":
                            l["started"], l["stopped"] = True, False
                            if l["status"] in ("выбор файлов", "остановлено", "ошибка"):
                                l["status"] = "в очереди"
                            l["resume_at"], l["nb"], S["cooldown_until"], l["dirty"] = 0, {}, 0, True
                            l["streak"], l["cool_n"] = 0, 0
                            log(f"«{l.get('name') or l['url']}»: запущено в работу")
                        else:
                            l["stopped"] = True
                            if l["status"] not in ("готово", "ошибка", "проверка", "выбор файлов"):
                                l["status"] = "остановлено"
                            log(f"«{l.get('name') or l['url']}»: остановлено")
            elif u.path == "/api/retry":
                for l in S["links"]:
                    if l["id"] == q.get("id", [""])[0]:
                        l["status"], l["error"], l["failed"] = ("в очереди" if l.get("started", True) else "проверка"), "", []
                        l["stopped"] = False
                        l["dirty"], l["resume_at"], l["streak"], l["cool_n"] = True, 0, 0, 0
                        S["cooldown_until"] = 0
        save()
        self._send(200, json.dumps(res, ensure_ascii=False))


def main():
    os.makedirs(LISTS, exist_ok=True)
    os.makedirs(BASE, exist_ok=True)
    if auth_config() is None:
        print("ВНИМАНИЕ: пароль на страницу не задан — страница открыта всем, кто видит порт. Задайте: ./ydl password", flush=True)
    if os.path.exists(STATE):
        try:
            S.update(read_json(STATE))
            for l in S["links"]:
                if "started" not in l:
                    l["started"] = True
                if l["status"] in ("качаю", "листинг", "ждём Яндекс", "ждём (есть ошибки)"):
                    l["status"] = "в очереди" if l["started"] else "проверка"
                if not l["started"] and l["status"] in ("в очереди",):
                    l["status"] = "проверка"
                l["resume_at"] = 0
                l["nb"] = {}
                l["active"], l["streak"], l["cool_n"], l["dirty"] = 0, 0, 0, True
            S["cooldown_until"], S["speed"] = 0, 0.0
        except Exception as e:
            log(f"не смог прочитать state: {e}")
            try:
                shutil.copy2(STATE, STATE + ".corrupt")          # не затираем проблемный файл пустым состоянием
                log(f"копия нечитаемого состояния сохранена: {STATE}.corrupt")
            except OSError:
                pass
    for l in S["links"]:
        if l.get("name"):
            l["name"] = safe_name(l["name"])
    arch_load()
    threading.Thread(target=arch_worker, daemon=True).start()
    threading.Thread(target=manager, daemon=True).start()
    threading.Thread(target=scout_loop, daemon=True).start()
    threading.Thread(target=metrics_loop, daemon=True).start()
    for i in range(SLOTS):
        threading.Thread(target=slot_worker, args=(i,), daemon=True).start()
    def _bye(*_):
        try:
            save()
        finally:
            os._exit(0)
    signal.signal(signal.SIGTERM, _bye)                         # docker stop: сохранить очередь и выйти
    log(f"ydl {VERSION}{' ' + STATUS if STATUS else ''} запущен: порт {PORT}, данные {BASE}, слотов {SLOTS}")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "passwd":
        passwd_cli(sys.argv[2:])
    else:
        main()
