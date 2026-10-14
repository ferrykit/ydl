"""Автотесты ydl: безопасность путей и адресов, вход, защита от CSRF/DNS-rebinding, раздача файлов, удаление, архивы.
Запуск:  python -m unittest discover -s tests -v      (Python 3.12+ как в образе, без сторонних пакетов)
Тесты создают временную папку и ничего не трогают вне её."""
import http.client
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="ydl-test-")
os.makedirs(os.path.join(TMP, "data"))
os.makedirs(os.path.join(TMP, "cfg"))
os.environ.update(YDL_DATA=os.path.join(TMP, "data"), YDL_CONFIG=os.path.join(TMP, "cfg"), YDL_PASSWORD="",
                  YDL_ALLOWED_HOSTS="my.example.com", YDL_MIN_FREE_GB="0")
_spec = importlib.util.spec_from_file_location("ydl", os.path.join(ROOT, "app", "ydl.py"))
y = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(y)

DATA = y.BASE


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def reset_data():
    for n in os.listdir(DATA):
        p = os.path.join(DATA, n)
        shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.remove(p)
    y.ARCH["jobs"].clear()
    y.S["links"].clear()


class Paths(unittest.TestCase):
    def test_safe_name(self):
        self.assertEqual(y.safe_name("Фото 2019"), "Фото 2019")
        for bad in ("..", ".", "", "  ..  "):
            self.assertEqual(y.safe_name(bad), "_")
        self.assertNotIn("/", y.safe_name("../../etc/cron.d"))
        self.assertNotIn("\\", y.safe_name("a\\b"))
        self.assertNotIn("\0", y.safe_name("x\0y"))

    def test_safe_local_blocks_traversal(self):
        root = os.path.join(TMP, "data")
        self.assertTrue(y.safe_local(root, "/a/b").startswith(root))
        for bad in ("../x", "/../../etc/passwd", "a/../../x"):
            with self.assertRaises(RuntimeError):
                y.safe_local(root, bad)

    def test_norm_rel(self):
        self.assertEqual(y.norm_rel("/"), "/")
        self.assertEqual(y.norm_rel("/a/../b//c/"), "/b/c")
        with self.assertRaises(RuntimeError):
            y.norm_rel("/../..")

    def test_valid_name(self):
        for ok in ("файл.txt", "a b", "x.zip"):
            self.assertTrue(y.valid_name(ok))
        for bad in ("", ".", "..", "a/b", "a\0b", "x" * 300, 5, None):
            self.assertFalse(y.valid_name(bad))


class YandexUrls(unittest.TestCase):
    def test_allowed(self):
        for u in ("https://downloader.disk.yandex.ru/disk/abc", "https://s361klg.storage.yandex.net/rdisk/x", "https://cloud-api.yandex.net/v1/disk",
                  "https://disk.360.yandex.ru/d/x", "https://disk.yandex.com/x", "https://ya.ru/x"):
            self.assertTrue(y.yandex_url_ok(u), u)

    def test_rejected(self):
        for u in ("http://downloader.disk.yandex.ru/x", "https://evil.com/yandex.ru", "https://yandex.ru.evil.com/x", "https://evilyandex.ru/x",
                  "https://yandex.net.evil.org/x", "https://yandex.ru@evil.com/x", "https://evil.com#.yandex.ru", "https://169.254.169.254/x",
                  "https://localhost:8085/", "file:///etc/passwd", "https://ya.ru.evil.com/", "https://xyandex.ru/"):
            self.assertFalse(y.yandex_url_ok(u), u)


class Hosts(unittest.TestCase):
    def test_host_allowed(self):
        for h in ("192.168.1.5:8085", "localhost:8085", "mynas", "x.local", "nas.lan", "[::1]:8085", "my.example.com", "MY.EXAMPLE.COM:443", "", None):
            self.assertTrue(y.host_allowed(h), h)

    def test_host_rejected(self):
        for h in ("evil.example.com", "attacker.com:8085", "my.example.com.evil.org", "xmy.example.com"):
            self.assertFalse(y.host_allowed(h), h)


class AddLinks(unittest.TestCase):
    def setUp(self):
        y.S["links"].clear()

    def test_accepts_folder_links_and_rejects_others(self):
        rows = [{"url": "https://disk.yandex.ru/d/ABC123", "pw": ""}, {"url": "https://disk.360.yandex.ru/d/DEF-456/", "pw": "secret"},
                {"url": "просто текст"}, {"url": "https://evil.com/d/abc"}, {"url": "https://yadi.sk/d/abc"}, {"url": "https://disk.yandex.ru/i/FILE"}]
        res = y.add_links(rows)
        self.assertEqual([r["ok"] for r in res], [True, True, False, False, False, False])
        self.assertEqual(len(y.S["links"]), 2)
        self.assertEqual(y.S["links"][1].get("pw"), "secret")
        self.assertIn("yadi.sk", res[4]["reason"])

    def test_duplicates_and_password_update(self):
        y.add_links([{"url": "https://disk.yandex.ru/d/ABC", "pw": ""}])
        res = y.add_links([{"url": "https://disk.yandex.ru/d/ABC", "pw": ""}, {"url": "https://disk.yandex.ru/d/ABC/", "pw": "newpw"}])
        self.assertEqual(len(y.S["links"]), 1)
        self.assertEqual(res[0]["status"], "уже в списке")
        self.assertEqual(res[1]["status"], "пароль обновлён")
        self.assertEqual(y.S["links"][0]["pw"], "newpw")


class Auth(unittest.TestCase):
    def test_password_hash(self):
        salt, h = y.hash_password("пароль-123")
        self.assertEqual(y.hash_password("пароль-123", salt)[1], h)
        self.assertNotEqual(y.hash_password("другой", salt)[1], h)

    def test_session_token(self):
        t = y.make_session("pwid-1")
        self.assertTrue(y.session_valid(t, "pwid-1"))
        self.assertFalse(y.session_valid(t, "pwid-2"), "смена пароля должна разлогинивать")
        exp, sig = t.split(".")
        self.assertFalse(y.session_valid(f"{int(exp) + 1}.{sig}", "pwid-1"), "подделка срока")
        self.assertFalse(y.session_valid(f"{int(time.time()) - 5}.{y._sign('pwid-1', int(time.time()) - 5)}", "pwid-1"), "истёкшая сессия")
        for junk in ("", "abc", "1.2.3", None):
            self.assertFalse(y.session_valid(junk or "", "pwid-1"))

    def test_env_password_non_ascii(self):
        old = y.AUTH_PASSWORD
        try:
            y.AUTH_PASSWORD = "пароль-123"
            check, _ = y.auth_config()
            self.assertTrue(check("пароль-123"))
            self.assertFalse(check("неверный"))
            self.assertFalse(check("abc"))
        finally:
            y.AUTH_PASSWORD = old


class FileManager(unittest.TestCase):
    def setUp(self):
        reset_data()

    def mk(self, rel, data=b"x"):
        p = os.path.join(DATA, rel.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def test_delete_requires_confirm_and_valid_names(self):
        self.mk("Папка/a.txt")
        self.assertEqual(y.fm_delete("/Папка", ["a.txt"], None)[0], 400)
        self.assertEqual(y.fm_delete("/Папка", ["a.txt"], "yes")[0], 400)
        self.assertEqual(y.fm_delete("/Папка", ["../x"], True)[0], 400)
        self.assertEqual(y.fm_delete("/../..", ["x"], True)[0], 400)
        self.assertTrue(os.path.exists(os.path.join(DATA, "Папка", "a.txt")))

    def test_delete_inside_link_excludes_from_queue(self):
        self.mk("Папка1/sub/b.txt", b"12345678")
        self.mk("Папка1/a.txt", b"123456")
        os.makedirs(y.LISTS, exist_ok=True)
        with open(os.path.join(y.LISTS, "abc123.json"), "w") as f:
            json.dump([["/a.txt", 6, ""], ["/sub/b.txt", 8, ""]], f)
        y.S["links"].append({"id": "abc123", "name": "Папка1", "url": "https://disk.yandex.ru/d/X", "status": "готово", "started": True,
                             "total_files": 2, "total_bytes": 14, "done_files": 2, "done_bytes": 14, "failed": []})
        # путь с «..»: должен привести к тому же результату, что и обычный
        code, res = y.fm_delete("/Папка1/../Папка1", ["sub"], True)
        self.assertEqual(code, 200, res)
        self.assertFalse(os.path.exists(os.path.join(DATA, "Папка1", "sub")))
        self.assertEqual(y.S["links"][0]["excluded"], ["/sub"])
        self.assertEqual(y.S["links"][0]["done_files"], 1)
        # удаление корня ссылки убирает и саму ссылку
        code, res = y.fm_delete("/", ["Папка1"], True)
        self.assertEqual(res["removed_links"], ["Папка1"])
        self.assertEqual(y.S["links"], [])

    def test_delete_symlink_keeps_target(self):
        outside = tempfile.mkdtemp(prefix="ydl-outside-")
        try:
            with open(os.path.join(outside, "precious.txt"), "w") as f:
                f.write("keep")
            os.symlink(outside, os.path.join(DATA, "ссылка"))
            self.assertEqual(y.fm_delete("/", ["ссылка"], True)[0], 200)
            self.assertFalse(os.path.lexists(os.path.join(DATA, "ссылка")))
            self.assertTrue(os.path.exists(os.path.join(outside, "precious.txt")))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_listing_hides_outside_symlinks(self):
        outside = tempfile.mkdtemp(prefix="ydl-outside-")
        try:
            os.symlink(outside, os.path.join(DATA, "evil_link"))
            self.mk("ok.txt")
            names = [e["n"] for e in y.fm_entries(DATA)]
            self.assertIn("ok.txt", names)
            self.assertNotIn("evil_link", names)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_archive_contains_files_skips_part_and_symlinks(self):
        self.mk("Папка/a.txt", b"AAA")
        self.mk("Папка/sub/b.bin", b"B" * 1000)
        self.mk("Папка/video.mp4.part", b"partial")
        os.makedirs(os.path.join(DATA, "Папка", "пустая"))
        outside = tempfile.mkdtemp(prefix="ydl-outside-")
        try:
            os.symlink(outside, os.path.join(DATA, "Папка", "link"))
            code, job = y.arch_create("/", ["Папка"])
            self.assertEqual(code, 200, job)
            j = next(x for x in y.ARCH["jobs"] if x["id"] == job["id"])
            y.arch_run(j)
            self.assertEqual(j["status"], "готов", j.get("error"))
            with zipfile.ZipFile(os.path.join(DATA, "Папка.zip")) as z:
                self.assertIsNone(z.testzip())
                names = sorted(z.namelist())
            self.assertEqual(names, ["Папка/a.txt", "Папка/sub/b.bin", "Папка/пустая/"])
            # повторный архив получает другое имя, удалить можно только архив из реестра
            code, job2 = y.arch_create("/", ["Папка"])
            self.assertEqual(job2["out_name"], "Папка-2.zip")
            self.mk("чужой.zip")
            self.assertEqual(y.fm_api("POST", "/api/archive/delete", {"id": ["../../чужой"]}, "")[0], 404)
            self.assertTrue(os.path.exists(os.path.join(DATA, "чужой.zip")))
            self.assertEqual(y.fm_api("POST", "/api/archive/delete", {"id": [j["id"]]}, "")[0], 200)
            self.assertFalse(os.path.exists(os.path.join(DATA, "Папка.zip")))
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class Http(unittest.TestCase):
    """Поднимает настоящий HTTP-сервер ydl на свободном порту и проверяет вход, CSRF, Host и раздачу файлов."""

    @classmethod
    def setUpClass(cls):
        cls.old_pw = y.AUTH_PASSWORD
        y.AUTH_PASSWORD = "Test-Pass-123"
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), y.H)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        y.AUTH_PASSWORD = cls.old_pw

    def setUp(self):
        reset_data()
        y._FAILS.clear()
        self.host = f"127.0.0.1:{self.port}"

    def req(self, method, path, body=None, headers=None, cookie=None):
        h = {"Host": self.host}
        h.update(headers or {})
        if cookie:
            h["Cookie"] = cookie
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        c.close()
        return r.status, hdrs, data

    def login(self):
        st, h, _ = self.req("POST", "/login", "password=Test-Pass-123&next=/", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 303)
        return h["set-cookie"].split(";")[0]

    def test_unauthenticated_access(self):
        self.assertEqual(self.req("GET", "/healthz")[0], 200)
        st, h, _ = self.req("GET", "/")
        self.assertEqual((st, h["location"].startswith("/login")), (302, True))
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("GET", "/api/ls?path=/")[0], 401)
        st, h, _ = self.req("GET", "/files/x.zip")
        self.assertEqual(st, 302)
        self.assertEqual(self.req("POST", "/api/delete", "{}", {"X-YDL": "1"})[0], 401)

    def test_login_flow_and_cookie_flags(self):
        st, h, _ = self.req("POST", "/login", "password=wrong", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 401)
        st, h, _ = self.req("POST", "/login", "password=Test-Pass-123", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 303)
        ck = h["set-cookie"]
        self.assertIn("HttpOnly", ck)
        self.assertIn("SameSite=Lax", ck)
        self.assertEqual(self.req("GET", "/api/state", cookie=ck.split(";")[0])[0], 200)
        self.assertEqual(self.req("GET", "/api/state", cookie="ydl_session=9999999999.deadbeef")[0], 401)

    def test_bruteforce_lockout(self):
        codes = [self.req("POST", "/login", "password=bad", {"Content-Type": "application/x-www-form-urlencoded"})[0] for _ in range(6)]
        self.assertEqual(codes, [401, 401, 401, 401, 401, 429])

    def test_version_and_status_in_api(self):
        ck = self.login()
        st, _, body = self.req("GET", "/api/version", cookie=ck)
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertEqual(d["version"], y.VERSION)
        self.assertEqual(d["status"], y.STATUS)
        self.assertIn(d["status"], ("alpha", "beta", "stable", ""))

    def test_open_redirect_blocked(self):
        st, h, _ = self.req("POST", "/login", "password=Test-Pass-123&next=//evil.com", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(h["location"], "/")

    def test_csrf_protection(self):
        ck = self.login()
        self.assertEqual(self.req("POST", "/api/pause", "", cookie=ck)[0], 403, "без X-YDL")
        self.assertEqual(self.req("POST", "/api/pause", "", {"X-YDL": "1", "Sec-Fetch-Site": "cross-site"}, ck)[0], 403)
        self.assertEqual(self.req("POST", "/api/pause", "", {"X-YDL": "1", "Origin": "http://evil.com"}, ck)[0], 403)
        self.assertEqual(self.req("POST", "/login", "password=Test-Pass-123", {"Origin": "http://evil.com", "Content-Type": "application/x-www-form-urlencoded"})[0], 403)
        st = self.req("POST", "/api/pause", "", {"X-YDL": "1", "Origin": f"http://{self.host}"}, ck)[0]
        self.assertEqual(st, 200)
        self.req("POST", "/api/pause", "", {"X-YDL": "1"}, ck)   # вернуть паузу назад

    def test_csrf_without_password_too(self):
        y.AUTH_PASSWORD = ""
        try:
            self.assertEqual(self.req("POST", "/api/pause", "{}", {"Content-Type": "text/plain"})[0], 403, "чужая страница не должна управлять сервисом даже без пароля")
        finally:
            y.AUTH_PASSWORD = "Test-Pass-123"

    def test_dns_rebinding_host_check(self):
        st, _, body = self.req("GET", "/login", headers={"Host": "evil.example.com"})
        self.assertEqual(st, 421)
        self.assertEqual(self.req("GET", "/login", headers={"Host": "my.example.com"})[0], 200)
        self.assertEqual(self.req("GET", "/healthz", headers={"Host": "evil.example.com"})[0], 200, "мониторинг не ломаем")

    def test_request_size_limits(self):
        ck = self.login()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/api/add", skip_host=True)
        c.putheader("Host", self.host)
        c.putheader("X-YDL", "1")
        c.putheader("Cookie", ck)
        c.putheader("Content-Length", str(20 * 2**20))
        c.endheaders()
        self.assertEqual(c.getresponse().status, 413)
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/api/add", skip_host=True)
        c.putheader("Host", self.host)
        c.putheader("X-YDL", "1")
        c.putheader("Cookie", ck)
        c.putheader("Content-Length", "abc")
        c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_security_headers(self):
        ck = self.login()
        st, h, _ = self.req("GET", "/", cookie=ck)
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
        self.assertEqual(h["referrer-policy"], "same-origin", "no-referrer ломает Origin у форм")

    def test_file_serving_is_safe(self):
        ck = self.login()
        d = os.path.join(DATA, "Папка")
        os.makedirs(d)
        for name, data in (("evil.html", b"<script>alert(1)</script>"), ("evil.svg", b"<svg onload=alert(1)/>"), ("note.txt", "текст".encode()),
                           ("pic.png", b"\x89PNG\r\n\x1a\n"), ("doc.pdf", b"%PDF-1.4"), ("x.json", b"{}"), ("unknown.xyz", b"x")):
            with open(os.path.join(d, name), "wb") as f:
                f.write(data)

        def get(name, hdr=None):
            return self.req("GET", "/files/" + urllib.parse.quote("Папка/" + name), headers=hdr, cookie=ck)

        for active in ("evil.html", "evil.svg", "x.json", "unknown.xyz"):
            st, h, _ = get(active)
            self.assertEqual(st, 200, active)
            self.assertTrue(h["content-disposition"].startswith("attachment"), active)
            self.assertEqual(h["content-type"], "application/octet-stream", active)
            self.assertIn("sandbox", h["content-security-policy"], active)
            self.assertEqual(h["x-content-type-options"], "nosniff")
        for passive, ctype in (("note.txt", "text/plain"), ("pic.png", "image/png"), ("doc.pdf", "application/pdf")):
            st, h, _ = get(passive)
            self.assertTrue(h["content-disposition"].startswith("inline"), passive)
            self.assertEqual(h["content-type"], ctype)
        st, h, body = get("note.txt", {"Range": "bytes=0-1"})
        self.assertEqual(st, 206)
        self.assertEqual(len(body), 2)

    def test_files_path_traversal(self):
        ck = self.login()
        secret = os.path.join(TMP, "cfg", "state.json")
        with open(secret, "w") as f:
            f.write("secret")
        for path in ("/files/../cfg/state.json", "/files/%2e%2e/cfg/state.json", "/files/Папка/../../cfg/state.json"):
            st, _, body = self.req("GET", path.replace("Папка", urllib.parse.quote("Папка")), cookie=ck)
            self.assertNotIn(b"secret", body, path)
            self.assertIn(st, (400, 404), path)


if __name__ == "__main__":
    unittest.main()
