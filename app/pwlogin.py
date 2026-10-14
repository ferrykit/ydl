#!/usr/bin/env python3
"""pwlogin — вход по паролю на публичную ссылку Яндекс.Диска через настоящий браузер (13.10.2026).
Запускается из ydl: python pwlogin.py <id ссылки>. Пароль берётся из $YDL_CONFIG/state.json (его ввёл
пользователь на странице ydl). Результат — $YDL_CONFIG/sessions/<id>.json (права 600): sk, passToken, cookie, хеш корня.
Дальше ydl работает обычными HTTP-запросами. Пароль и токены в журнал не пишутся."""
import json, os, re, sys, time
from playwright.sync_api import sync_playwright

CONFIG = os.environ.get("YDL_CONFIG", "/config")
STATE = os.path.join(CONFIG, "state.json")
SESS = os.path.join(CONFIG, "sessions")
PROFILE = os.path.join(CONFIG, "browser-profile")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def out(lid, d):
    os.makedirs(SESS, mode=0o700, exist_ok=True)
    p = os.path.join(SESS, lid + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def state_root(html):
    i = html.find('"resources":{')
    if i < 0:
        return None
    j = html.index("{", i)
    depth, k = 0, j
    while k < len(html):
        depth += (html[k] == "{") - (html[k] == "}")
        k += 1
        if depth == 0:
            break
    res = json.loads(html[j:k])
    return next((v for v in res.values() if v.get("parent") is None), None)


def main(lid):
    st = _load(STATE)
    link = next((l for l in st["links"] if l["id"] == lid), None)
    if not link:
        return out(lid, {"error": "ссылка не найдена"})
    pw = link.get("pw", "")
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            PROFILE, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"], locale="ru-RU", viewport={"width": 1100, "height": 800})
        try:
            page = ctx.new_page()
            page.goto(link["url"], wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector("input", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            inp = None
            for i in range(page.locator("input").count()):
                el = page.locator("input").nth(i)
                if el.is_visible() and el.get_attribute("type") in ("password", "text", None):
                    inp = el
                    break
            if inp is not None:
                if not pw:
                    return out(lid, {"error": "ссылка требует пароль, но он не задан"})
                inp.fill(pw)
                clicked = False
                for sel in ("button:text-is('Открыть')", "[role=button]:text-is('Открыть')", "button[type=submit]"):
                    loc = page.locator(sel)
                    if loc.count():
                        try:
                            loc.first.click(timeout=4000)
                            clicked = True
                            break
                        except Exception:
                            pass
                if not clicked:
                    inp.press("Enter")
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
                page.wait_for_timeout(5000)
            html = page.content()
            m = re.search(r'"sk"\s*:\s*"([A-Za-z0-9]{20,})"', html)
            pt = next((c["value"] for c in ctx.cookies() if c["name"] == "passToken"), None)
            root = state_root(html)
            if "Введите пароль" in page.inner_text("body")[:400]:
                return out(lid, {"error": "пароль не принят (страница всё ещё просит пароль)"})
            if not (m and root):
                return out(lid, {"error": "не нашёл данные страницы после входа"})
            if inp is not None and not pt:
                return out(lid, {"error": "после ввода пароля токен не выдан"})
            cookie = "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies() if "yandex" in c["domain"])
            out(lid, {"ts": time.time(), "sk": m.group(1), "passToken": pt or "", "cookie": cookie,
                      "root_hash": root["hash"], "name": root["name"]})
        finally:
            ctx.close()


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as e:
        out(sys.argv[1], {"error": "сбой входа: " + type(e).__name__})
