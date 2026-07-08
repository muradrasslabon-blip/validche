# checker.py — парсинг данных из astroSharedData (страница настроек)
import os
import sys
import json
import glob
import random
import logging
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

# --- Настройки ---
THREADS = int(os.getenv("THREADS", "3"))
TIMEOUT = int(os.getenv("TIMEOUT", "20"))
REPORT_FILE = os.getenv("REPORT_FILE", "report.json")
ACCOUNTS_DIR = os.getenv("ACCOUNTS_DIR", "accounts")
PROXY_FILE = os.getenv("PROXY_FILE", "proxies.txt")
DELAY = float(os.getenv("DELAY", "1.0"))
RETRY_COUNT = int(os.getenv("RETRY_COUNT", "2"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("checker.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("checker")

# --- Браузерные заголовки ---
BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Referer": "https://www.kleinanzeigen.de/",
    "Sec-Ch-UA": '"Google Chrome";v="126", "Chromium";v="126", "Not?A_Brand";v="99"',
    "Sec-Ch-UA-Mobile": "?0",
    "Sec-Ch-UA-Platform": '"Windows"',
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]

def get_random_ua() -> str:
    return random.choice(USER_AGENTS)

def parse_proxy_line(line: str) -> dict:
    line = line.strip()
    if not line:
        return None
    if line.startswith("socks5://") or line.startswith("http://") or line.startswith("https://"):
        if "@" in line:
            return {"http": line, "https": line}
        else:
            if "://" in line:
                protocol, rest = line.split("://", 1)
            else:
                protocol = "http"
                rest = line
            parts = rest.split(":")
            if len(parts) == 4:
                host, port, user, password = parts
                if protocol == "socks5":
                    proxy_str = f"socks5://{user}:{password}@{host}:{port}"
                else:
                    proxy_str = f"http://{user}:{password}@{host}:{port}"
                return {"http": proxy_str, "https": proxy_str}
            else:
                log.warning("Не удалось разобрать прокси: %s", line)
                return None
    else:
        parts = line.split(":")
        if len(parts) == 4:
            ip, port, user, password = parts
            proxy_str = f"http://{user}:{password}@{ip}:{port}"
            return {"http": proxy_str, "https": proxy_str}
        else:
            log.warning("Неизвестный формат прокси: %s", line)
            return None

def load_proxies(file_path: str) -> list:
    proxies = []
    if not os.path.exists(file_path):
        log.warning("Файл прокси %s не найден.", file_path)
        return proxies
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            proxy_dict = parse_proxy_line(line)
            if proxy_dict:
                proxies.append(proxy_dict)
            else:
                log.warning("Пропускаю строку: %s", line.strip())
    return proxies

def get_proxy_for_account(index: int, proxies: list) -> dict:
    if not proxies:
        return None
    return proxies[index % len(proxies)]

def build_session(cookies: list, proxy_dict: dict = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(BASE_HEADERS)
    session.headers.update({"User-Agent": get_random_ua()})
    if proxy_dict:
        session.proxies.update({"http": proxy_dict["http"], "https": proxy_dict["https"]})
    for c in cookies:
        rest = {"HttpOnly": c["httpOnly"]} if c.get("httpOnly") else {}
        session.cookies.set(
            name=c["name"],
            value=c["value"],
            domain=c.get("domain", ".kleinanzeigen.de"),
            path=c.get("path", "/"),
            secure=c.get("secure", False),
            rest=rest,
        )
    return session

def check_session(session: requests.Session, acc_id: str, retry: int = 0) -> dict:
    """Проверяет аккаунт через страницу настроек и парсит JSON."""
    url = "https://www.kleinanzeigen.de/m-einstellungen.html"
    try:
        time.sleep(random.uniform(0.5, 1.0) + retry * 0.5)
        resp = session.get(url, timeout=TIMEOUT, allow_redirects=False)
    except Exception as e:
        return {"status": "error", "detail": str(e)}

    status_code = resp.status_code
    html = resp.text
    debug_file = f"debug_{acc_id}.html"
    with open(debug_file, "w", encoding="utf-8") as f:
        f.write(html)

    html_lower = html.lower()
    if "ip banned" in html_lower or "access denied" in html_lower or status_code == 403:
        if retry < RETRY_COUNT:
            time.sleep(2 + retry * 2)
            return check_session(session, acc_id, retry+1)
        else:
            return {"status": "blocked"}

    if status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("Location", "").lower()
        if "login" in location or "einloggen" in location:
            return {"status": "invalid"}
        return {"status": "error", "detail": "редирект на неизвестный URL"}

    if status_code == 200:
        # Ищем тег script с id="astroSharedData"
        soup = BeautifulSoup(html, 'html.parser')
        script_tag = soup.find('script', {'id': 'astroSharedData'})
        if script_tag:
            try:
                data = json.loads(script_tag.string)
                user = data.get('user', {})
                user_profile = data.get('userProfile', {})
                email = user.get('email', '—')
                name = user.get('contactName', '—')
                user_id = str(user.get('userId', '—'))
                user_since = user_profile.get('userSince', '—')
                rating = '—'  # рейтинг отдельно не найден, но можно проверить
                # Проверяем, есть ли рейтинг в других данных (не видно)
                acc_type = "selfreg"  # по умолчанию, можно определить позже
                if rating != '—' and rating != '0':
                    acc_type = "brute"
                return {
                    "status": "valid",
                    "email": email,
                    "name": name,
                    "userid": user_id,
                    "rating": rating,
                    "reg_date": user_since,
                    "type": acc_type
                }
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                log.warning("Ошибка парсинга astroSharedData: %s", e)
                # fallback на поиск в HTML
        else:
            # Если тег не найден, пробуем найти данные в HTML как запасной вариант
            # Email
            email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', html)
            email = email_match.group(0) if email_match else '—'
            # Имя (в h2 "Profil von")
            name_match = re.search(r'Profil von\s*"?([^"]+)"?', html, re.IGNORECASE)
            name = name_match.group(1).strip().strip('"') if name_match else '—'
            # Дата
            date_match = re.search(r'Aktiv seit\s*([\d.]+)', html, re.IGNORECASE)
            reg_date = date_match.group(1) if date_match else '—'
            # UserID
            uid_match = re.search(r'userId:\s*(\d+)', html)
            user_id = uid_match.group(1) if uid_match else '—'
            return {
                "status": "valid",
                "email": email,
                "name": name,
                "userid": user_id,
                "rating": '—',
                "reg_date": reg_date,
                "type": "selfreg"
            }

        # Если ничего не нашли, проверяем, залогинен ли пользователь
        if "angemeldet als" in html_lower or "ausloggen" in html_lower:
            # Значит, залогинен, но данные не распарсились — пробуем вытянуть хоть email
            email_match = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', html)
            email = email_match.group(0) if email_match else '—'
            return {
                "status": "valid",
                "email": email,
                "name": '—',
                "userid": '—',
                "rating": '—',
                "reg_date": '—',
                "type": "selfreg"
            }
        elif "anmelden" in html_lower or "login" in html_lower or "einloggen" in html_lower:
            return {"status": "invalid"}
        else:
            return {"status": "error", "detail": "неизвестный ответ"}
    return {"status": "error", "detail": f"статус {status_code}"}

# --- Функции для локального использования (если нужно) ---

def load_accounts_from_dir(directory: str) -> list:
    if not os.path.isdir(directory):
        log.error("Папка не найдена: %s", directory)
        sys.exit(1)
    paths = sorted(glob.glob(os.path.join(directory, "*.json")))
    if not paths:
        log.error("В папке %s нет .json файлов.", directory)
        sys.exit(1)
    accounts = []
    for path in paths:
        acc_id = os.path.splitext(os.path.basename(path))[0]
        try:
            with open(path, "r", encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception as e:
            log.warning("Пропущен %s: ошибка чтения (%s)", path, e)
            continue
        if not isinstance(cookies, list):
            log.warning("Пропущен %s: не массив кук.", path)
            continue
        accounts.append({"id": acc_id, "cookies": cookies})
    return accounts

def _check_one(account: dict, proxy_dict: dict) -> dict:
    acc_id = account.get("id", "?")
    try:
        session = build_session(account["cookies"], proxy_dict)
        result = check_session(session, acc_id)
        status = result.get("status", "error")
    except Exception as e:
        log.warning("Аккаунт %s: ошибка — %s", acc_id, e)
        return {"id": acc_id, "status": "error", "detail": str(e)}
    proxy_str = proxy_dict.get("http", "none") if proxy_dict else "none"
    masked = proxy_str[:30] + "..." if len(proxy_str) > 30 else proxy_str
    log.info("Аккаунт %s: %s (прокси %s)", acc_id, status, masked)
    return {"id": acc_id, **result}

def check_accounts(accounts: list, proxies: list, threads: int = THREADS) -> list:
    results = []
    max_workers = min(threads, len(accounts)) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for idx, acc in enumerate(accounts):
            proxy = get_proxy_for_account(idx, proxies)
            future = executor.submit(_check_one, acc, proxy)
            futures[future] = acc
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                acc = futures[future]
                results.append({"id": acc.get("id", "?"), "status": "error", "detail": str(e)})
    try:
        results.sort(key=lambda r: str(r["id"]))
    except TypeError:
        pass
    return results

def print_summary(results: list):
    summary = {}
    for r in results:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    print("\n=== Результат ===")
    for status in ("valid", "invalid", "blocked", "error"):
        if status in summary:
            print(f"  {status:8}: {summary[status]}")
    print(f"  {'всего':8}: {len(results)}")
    valid_ids = [r["id"] for r in results if r["status"] == "valid"]
    if valid_ids:
        print(f"\nВалидные: {', '.join(map(str, valid_ids))}")

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("dir", nargs="?", default=ACCOUNTS_DIR)
    parser.add_argument("--threads", type=int, default=THREADS)
    args = parser.parse_args()

    proxies = load_proxies(PROXY_FILE)
    if not proxies:
        log.warning("Прокси не загружены. Будут ошибки соединения.")
    else:
        log.info("Загружено прокси: %d", len(proxies))

    accounts = load_accounts_from_dir(args.dir)
    if not accounts:
        log.error("Нет аккаунтов для проверки.")
        sys.exit(1)

    log.info("Папка: %s | аккаунтов: %d | потоков: %d", args.dir, len(accounts), args.threads)

    results = check_accounts(accounts, proxies, threads=args.threads)

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    log.info("Отчёт сохранён: %s", REPORT_FILE)
    print_summary(results)

if __name__ == "__main__":
    main()