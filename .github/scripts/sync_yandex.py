#!/usr/bin/env python3
"""
Синхронизация расписаний с публичных папок Яндекс.Диска в этот репозиторий.

Теперь синхронизируются ДВА независимых источника:
  - СПО (существующее расписание)  -> папка репозитория "spo/"
  - ВО, высшее образование          -> папка репозитория "vo/"

Как это работает (для каждого источника отдельно):
  1. Спрашиваем у Яндекс.Диска список файлов в публичной папке (без токена —
     публичные ресурсы можно читать анонимно).
  2. Для каждого .xlsx/.xls файла получаем прямую ссылку на скачивание и
     сохраняем файл в соответствующую подпапку репозитория под тем же именем.
  3. Если содержимое файла не изменилось (совпадает хэш) — просто пропускаем,
     чтобы не создавать пустые коммиты.
  4. Если что-то изменилось хотя бы в одном источнике — печатаем
     "changed=true" в GITHUB_OUTPUT, и workflow сам сделает commit + push.

Меняется на ходу может только PUBLIC_KEY у каждого источника (если ссылку на
папку когда-нибудь пересоздадут).
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

META_URL = "https://cloud-api.yandex.net/v1/disk/public/resources"
DOWNLOAD_URL = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
ALLOWED_EXT = (".xlsx", ".xls")

# Источники: публичная ссылка на Яндекс.Диске -> подпапка в репозитории,
# откуда фронтенд (index.html) их потом читает через GitHub API.
SOURCES = [
    {
        "name": "СПО",
        "public_key": "https://disk.yandex.ru/d/JuK8aJ2gV8XCjA",
        "target_dir": "spo",
    },
    {
        "name": "ВО",
        "public_key": "https://disk.yandex.ru/d/TWvx0IMCKRqhwg",
        "target_dir": "vo",
    },
]


def api_get(url, params):
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_bytes(url):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def list_public_items(public_key):
    """Returns a flat list of file entries (dicts with at least name/path),
    recursing one level into subfolders if the public link is itself a folder
    that contains subfolders (harmless no-op if it's already flat)."""
    data = api_get(META_URL, {"public_key": public_key, "limit": 200})

    if data.get("type") == "file":
        return [data]

    items = data.get("_embedded", {}).get("items", [])
    flat = []
    for item in items:
        if item.get("type") == "dir":
            try:
                sub = api_get(META_URL, {
                    "public_key": public_key,
                    "path": item.get("path", ""),
                    "limit": 200,
                })
                flat.extend(sub.get("_embedded", {}).get("items", []))
            except urllib.error.HTTPError as e:
                print(f"  (пропускаю подпапку {item.get('name')}: {e})")
        else:
            flat.append(item)
    return flat


def sha256_of(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def sync_one(source):
    name, public_key, target_dir = source["name"], source["public_key"], source["target_dir"]
    print(f"[{name}] Проверяю папку на Яндекс.Диске…")
    os.makedirs(target_dir, exist_ok=True)

    try:
        items = list_public_items(public_key)
    except urllib.error.HTTPError as e:
        print(f"[{name}] Не удалось получить список файлов с Яндекс.Диска: {e}")
        return False
    except urllib.error.URLError as e:
        print(f"[{name}] Сетевая ошибка при обращении к Яндекс.Диску: {e}")
        return False

    changed = False
    for item in items:
        fname = item.get("name") or ""
        if not fname.lower().endswith(ALLOWED_EXT):
            continue

        path = item.get("path", "")
        try:
            href_data = api_get(DOWNLOAD_URL, {"public_key": public_key, "path": path})
            content = fetch_bytes(href_data["href"])
        except Exception as e:
            print(f"  [{name}] Не удалось скачать {fname}: {e}")
            continue

        local_path = os.path.join(target_dir, fname)
        new_hash = hashlib.sha256(content).hexdigest()
        old_hash = sha256_of(local_path)

        if new_hash == old_hash:
            print(f"  [{name}] Без изменений: {fname}")
            continue

        with open(local_path, "wb") as f:
            f.write(content)
        print(f"  [{name}] Обновлено: {fname}")
        changed = True

    if not changed:
        print(f"[{name}] Новых файлов и изменений не найдено.")
    return changed


def main():
    any_changed = False
    for source in SOURCES:
        try:
            if sync_one(source):
                any_changed = True
        except Exception as e:
            # Один упавший источник не должен останавливать синхронизацию другого.
            print(f"[{source['name']}] Непредвиденная ошибка: {e}")

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a") as f:
            f.write(f"changed={'true' if any_changed else 'false'}\n")

    if not any_changed:
        print("Итого: изменений нет ни в одном источнике.")


if __name__ == "__main__":
    main()
