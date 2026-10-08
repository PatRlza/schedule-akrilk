#!/usr/bin/env python3
"""
Забирает расписание с официального сайта колледжа (raspisanie.sphti.ru) и сохраняет
компактный файл data/sfti.json.

Сайт открывается только из России, а GitHub Actions работает из США, поэтому скрипт
ищет рабочий российский прокси в открытом списке (proxifly/free-proxy-list), проверяет
кандидатов и качает данные через первый рабочий. Соединение с сайтом идёт по HTTPS с
проверкой сертификата — прокси не может подменить данные незаметно; кроме того, ответ
проверяется на разумность (структура, число пар). Никаких секретов через прокси не
передаётся: токен GitHub используется только самим git при коммите.

Если рабочий прокси не нашёлся — ничего не меняется (файл остаётся прежним).
Нужен пакет requests[socks].
"""
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

SRC = "https://raspisanie.sphti.ru/"
PROXY_LIST_URL = "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/countries/RU/data.txt"
OUT_PATH = "data/sfti.json"
MIN_ROWS = 100
PROBE_TIMEOUT = 8       # секунд на проверку одного прокси
FETCH_TIMEOUT = 60      # секунд на скачивание полного расписания
MAX_CANDIDATES = 400    # сколько прокси максимум проверять за запуск
WORKERS = 40
WANT_WORKING = 5        # остановиться, когда нашли столько рабочих
ALLOWED_SCHEMES = ("http://", "https://", "socks5://", "socks4://")

KEEP = [
    "group_id", "group_name", "education_level_id", "course",
    "subject_name", "lesson_type", "public_lesson_type",
    "teacher_id", "teacher_name", "room_number",
    "day_of_week", "pair_number", "time_start", "time_end",
    "week_type", "half_pair", "stream_number", "stream_label",
]
HEADERS = {"User-Agent": "Mozilla/5.0 (schedule-akrilk sync bot)", "Accept": "application/json"}


def log(*a):
    print(*a, flush=True)


def load_candidates():
    resp = requests.get(PROXY_LIST_URL, timeout=30)
    resp.raise_for_status()
    out = []
    for line in resp.text.splitlines():
        line = line.strip()
        if line.startswith(ALLOWED_SCHEMES):
            out.append(line)
    # http/https-прокси чаще работают и быстрее — сначала они, потом socks
    out.sort(key=lambda p: 0 if p.startswith("http") else 1)
    return out[:MAX_CANDIDATES]


def proxies_for(p):
    # socks5h — чтобы имя сайта разрешалось на стороне прокси
    u = p.replace("socks5://", "socks5h://").replace("socks4://", "socks4a://")
    return {"http": u, "https": u}


def get_json(url, proxy, timeout):
    r = requests.get(url, headers=HEADERS, proxies=proxies_for(proxy), timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or not data.get("success"):
        raise ValueError("ответ не похож на ответ сайта")
    return data


def probe(proxy):
    """Лёгкий запрос: список курсов (десятки байт)."""
    d = get_json(SRC + "?route=schedule/filters&type=courses", proxy, PROBE_TIMEOUT)
    if "items" not in d:
        raise ValueError("нет поля items")
    return proxy


def find_working(candidates):
    working = []
    tested = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(probe, p): p for p in candidates}
        for f in as_completed(futs):
            tested += 1
            try:
                working.append(f.result())
            except Exception:
                pass
            if len(working) >= WANT_WORKING:
                for other in futs:
                    other.cancel()
                break
    log("проверено прокси: %d, рабочих найдено: %d" % (tested, len(working)))
    return working


def fetch_week(mode, proxy):
    url = SRC + "?route=schedule/data&schedule_type=semester&week_mode=" + mode
    d = get_json(url, proxy, FETCH_TIMEOUT)["data"]
    if len(d.get("rows", [])) < MIN_ROWS:
        raise ValueError("слишком мало пар: %d" % len(d.get("rows", [])))
    return d


def compact(row):
    out = {}
    for k in KEEP:
        v = row.get(k)
        if v in (None, "", 0) and k in ("stream_number", "stream_label", "public_lesson_type", "room_number"):
            continue
        if k in ("time_start", "time_end") and isinstance(v, str):
            v = v[:5]
        out[k] = v
    return out


def build_body(cur, nxt):
    seen = {}
    for flag, rows in (("c", cur["rows"]), ("n", nxt["rows"])):
        for r in rows:
            c = compact(r)
            key = json.dumps(c, ensure_ascii=False, sort_keys=True)
            seen.setdefault(key, [c, set()])[1].add(flag)
    rows = []
    for key in sorted(seen):
        c, flags = seen[key]
        c["w"] = "".join(f for f in "cn" if f in flags)
        rows.append(c)

    def week_info(d):
        a = d.get("activeWeek") or {}
        return {k: a.get(k) for k in ("weekNumber", "weekType", "label", "rangeStart", "rangeEnd", "datesByDay")}

    return {
        "weeks": {"current": week_info(cur), "next": week_info(nxt)},
        "levels": cur.get("educationLevels", []),
        "groups": cur.get("groups", []),
        "bells": cur.get("bells", []),
        "rows": rows,
    }


def body_hash(body):
    s = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def write_gh_output(changed):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write("changed=%s\n" % ("true" if changed else "false"))


def main():
    try:
        candidates = load_candidates()
    except Exception as e:
        log("Не удалось получить список прокси:", e)
        write_gh_output(False)
        return
    log("кандидатов в списке: %d" % len(candidates))

    cur = nxt = None
    for proxy in find_working(candidates):
        try:
            cur = fetch_week("current", proxy)
            nxt = fetch_week("next", proxy)
            log("данные получены через", proxy)
            break
        except Exception as e:
            log("прокси %s не справился: %s" % (proxy, e))
            cur = nxt = None
    if not cur or not nxt:
        log("Рабочий прокси не найден — файл не меняю.")
        write_gh_output(False)
        return

    body = build_body(cur, nxt)
    h = body_hash(body)
    old_hash = None
    if os.path.exists(OUT_PATH):
        try:
            with open(OUT_PATH, encoding="utf-8") as f:
                old_hash = json.load(f).get("hash")
        except Exception:
            pass
    if old_hash == h:
        log("Без изменений.")
        write_gh_output(False)
        return

    out = dict(body)
    out["hash"] = h
    out["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    log("Обновлено: %d строк." % len(body["rows"]))
    write_gh_output(True)


if __name__ == "__main__":
    main()
