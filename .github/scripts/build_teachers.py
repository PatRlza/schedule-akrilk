#!/usr/bin/env python3
"""
Пересобирает словарь ФИО преподавателей с сайта колледжа
(https://www.sphti.ru/sveden/employees/pps/index.html) и сохраняет результат
в файл teachers-data.js в репозитории.

Зачем сохранять в репозиторий, а не ходить на сайт колледжа прямо из
приложения при каждом открытии расписания:
  - если сайт колледжа когда-нибудь закроют/переедет — старые данные всё
    равно останутся доступны студентам, потому что лежат у нас же;
  - открытие расписания не зависит от скорости/доступности стороннего сайта;
  - меньше шансов, что сайт колледжа заблокирует частые запросы из браузеров
    студентов.

Формат вывода — как у teachers-data.js: обфусцированный (base64) блок,
который index.html сам декодирует в словарь "Фамилия И.О." -> полное ФИО.
Файл предназначен для запуска в GitHub Actions (см. sync-teachers.yml), но
работает и локально: `python3 build_teachers.py`.
"""
import base64
import html
import json
import os
import re
import urllib.error
import urllib.request

SOURCE_URL = "https://www.sphti.ru/sveden/employees/pps/index.html"
OUTPUT_PATH = "teachers-data.js"
MIN_EXPECTED_NAMES = 10  # если нашли меньше — разметка сайта, видимо, изменилась


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (schedule-akrilk sync bot)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


NAME_RE = re.compile(r"^[А-ЯЁ][а-яёA-Za-z-]+\s+[А-ЯЁ][а-яёA-Za-z-]+\s+[А-ЯЁ][а-яёA-Za-z-]+")


def extract_names(page_html):
    """Тянет колонку "Фамилия, имя, отчество" из главной таблицы страницы."""
    m = re.search(
        r'<table[^>]*class="[^"]*table-scroll-thead[^"]*"[^>]*>(.*?)</table>',
        page_html, re.S,
    )
    if not m:
        raise RuntimeError("не нашёл основную таблицу на странице (изменилась вёрстка сайта?)")
    table_html = m.group(1)
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S)

    names = []
    for row in rows[1:]:  # первая строка — заголовок таблицы
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 2:
            continue
        name = strip_tags(cells[1])
        if name and NAME_RE.match(name):
            names.append(name)
    return names


def short_form(full_name):
    parts = full_name.split()
    if len(parts) < 3:
        return None
    fam, im, ot = parts[0], parts[1], parts[2]
    return f"{fam} {im[0]}.{ot[0]}."


def build_dict(names):
    d = {}
    for n in names:
        k = short_form(n)
        if k:
            d[k] = n
    return d


def encode_payload(d):
    payload = json.dumps(d, ensure_ascii=False)
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    chunks = [b64[i:i + 120] for i in range(0, len(b64), 120)]
    js = (
        "// Автоматически пересобирается workflow'ом sync-teachers.yml из\n"
        "// " + SOURCE_URL + " — не редактировать руками, правки будут перезаписаны.\n"
        "// index.html декодирует TEACHERS_B64 в TEACHERS_FULLNAMES (\"Фамилия И.О.\" -> полное ФИО).\n"
        "window.TEACHERS_B64 = [\n"
    )
    for c in chunks:
        js += '  "' + c + '",\n'
    js += "].join('');\n"
    return js


def write_gh_output(changed):
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")


def main():
    try:
        page_html = fetch_html(SOURCE_URL)
        names = extract_names(page_html)
    except Exception as e:
        print(f"Не удалось получить страницу сайта колледжа: {e}")
        print("Оставляю текущий teachers-data.js без изменений.")
        write_gh_output(False)
        return

    if len(names) < MIN_EXPECTED_NAMES:
        print(f"Нашёл подозрительно мало преподавателей ({len(names)}) — "
              f"похоже, разметка сайта изменилась. Файл не трогаю, чтобы не затереть рабочие данные.")
        write_gh_output(False)
        return

    new_js = encode_payload(build_dict(names))

    old_js = None
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            old_js = f.read()

    changed = new_js != old_js
    if changed:
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            f.write(new_js)
        print(f"Обновлено: {len(names)} преподавателей найдено на сайте.")
    else:
        print("Без изменений — на сайте всё то же самое.")

    write_gh_output(changed)


if __name__ == "__main__":
    main()
