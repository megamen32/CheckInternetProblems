#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, time, json, traceback, datetime, subprocess, signal, shutil, argparse, logging
from pathlib import Path
from collections import deque
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait as Wait
from selenium.webdriver.support import expected_conditions as EC

# НАСТРОЙКИ
ROUTER_URL_LOGIN = "http://192.168.2.1/old_login.htm"
ROUTER_URL_INFO  = "http://192.168.2.1/status/st_deviceinfo_tl.htm"
ROUTER_URL_LOG   = "http://192.168.2.1/status/st_log_tl.htm"
USERNAME = "admin"
PASSWORD = "admin"
CHECK_PERIOD = 60
PING_TARGET = "77.88.8.8"
PING_ROUTER = "192.168.2.1"
PAGE_TIMEOUT = 180  # wait up to 3 minutes for pages/elements

OUT_DIR    = Path("router_monitor")
EVENTS_DIR = OUT_DIR / "events"
OUT_DIR.mkdir(exist_ok=True)
EVENTS_DIR.mkdir(exist_ok=True)

parser = argparse.ArgumentParser()
parser.add_argument(
    "--keep-states",
    type=int,
    default=2,
    help="how many previous states to keep (screenshots/logs)"
)
args = parser.parse_args()
KEEP_STATES = max(1, args.keep_states)

STATUS_FILES = [f"status_{i}.png" for i in range(KEEP_STATES + 1)]
LOG_FILES = [f"log_{i}.png" for i in range(KEEP_STATES + 1)]
LOG_FILE = OUT_DIR / "monitor.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()]
)

# УТИЛИТЫ
def mk_driver():
    opts = webdriver.FirefoxOptions() if sys.platform.startswith("linux") else webdriver.ChromeOptions()
    #opts.add_argument("--headless")
    drv = (webdriver.Firefox if sys.platform.startswith("linux") else webdriver.Chrome)(options=opts)
    drv.set_page_load_timeout(PAGE_TIMEOUT)
    return drv
def safe_txt(elem):
    return elem.text.replace('\xa0', ' ').strip()

def login(driver):
    driver.get(ROUTER_URL_LOGIN)
    Wait(driver, PAGE_TIMEOUT).until(EC.presence_of_element_located((By.NAME, "f_username")))
    driver.find_element(By.NAME, "f_username").send_keys(USERNAME)
    driver.find_element(By.NAME, "pwd").send_keys(PASSWORD)
    driver.find_element(By.XPATH, '//input[@value="Войти"]').click()
    Wait(driver, PAGE_TIMEOUT).until(EC.presence_of_element_located((By.TAG_NAME, "title")))

def scrape_status(driver):
    driver.get(ROUTER_URL_INFO)
    Wait(driver, PAGE_TIMEOUT).until(
        EC.presence_of_element_located((By.XPATH, '//td[contains(text(),"Время подключения")]'))
    )
    def get(x):
        try: return safe_txt(driver.find_element(By.XPATH, x))
        except: return ""
    return {
        "uptime": get('//td[contains(text(),"Время подключения")]/following-sibling::td'),
        "iface_uptime": get('//td[contains(text(),"Время работы интерфейса")]/following-sibling::td'),
        "cpu": get('//td[contains(text(),"Загрузка процессора")]/following-sibling::td'),
        "cpu_temp": get('//td[contains(text(),"Температура CPU")]/following-sibling::td'),
        "optical_power": get('//td[contains(text(),"Оптическая мощность")]/following-sibling::td'),
        "optical_temp": get('//td[contains(text(),"Температура опт. модуля")]/following-sibling::td'),
        "mac": get('//td[contains(text(),"MAC-адрес устройства")]/following-sibling::td'),
        "ip": get('//td[contains(text(),"IP-адрес")]/following-sibling::td'),
        "fw_version": get('//td[contains(text(),"Версия ПО:")]/following-sibling::td')
    }

def scrape_full_log(driver, max_rows=None):
    """Return router log entries as list of dicts."""
    table = Wait(driver, PAGE_TIMEOUT).until(
        EC.presence_of_element_located((By.ID, "newtablelist"))
    )
    rows = table.find_elements(By.TAG_NAME, "tr")[1:]
    if max_rows:
        rows = rows[:max_rows]
    log = []
    for row in rows:
        cols = row.find_elements(By.TAG_NAME, "td")
        if len(cols) >= 3:
            log.append({
                "number": cols[0].text.strip(),
                "datetime": cols[1].text.strip(),
                "event": cols[2].text.strip(),
            })
    return log

def ping(host, count=1, timeout=1):
    param = "-n" if sys.platform.startswith("win") else "-c"
    try:
        out = subprocess.check_output(
            ["ping", param, str(count), "-w", str(timeout), host],
            stderr=subprocess.DEVNULL
        )
        encoding = "cp866" if sys.platform.startswith("win") else "utf-8"
        out = out.decode(encoding, errors="ignore")
        ok = "ttl" in out.lower()
        rtt = None
        if ok:
            import re
            m = re.search(r'время[=<]\s*([\d\.,]+)\s*мс', out, re.I)
            if not m:
                m = re.search(r'time[=<]\s*([\d\.,]+)\s*ms', out, re.I)
            if m:
                rtt = float(m.group(1).replace(',', '.'))
        return ok, rtt
    except subprocess.CalledProcessError:
        return False, None

def screenshot(driver, name):
    path = OUT_DIR / name
    driver.save_screenshot(str(path))
    return path

def parse_uptime(text):
    import re
    d, h, m, s = 0, 0, 0, 0
    m1 = re.search(r'(\d+)\s*сут', text)
    m2 = re.search(r'(\d+)\s*ч', text)
    m3 = re.search(r'(\d+)\s*мин', text)
    m4 = re.search(r'(\d+)\s*сек', text)
    if m1: d = int(m1.group(1))
    if m2: h = int(m2.group(1))
    if m3: m = int(m3.group(1))
    if m4: s = int(m4.group(1))
    return d*86400 + h*3600 + m*60 + s

# ОСНОВНОЙ ЦИКЛ
driver = mk_driver()
login(driver)

log_history = deque(maxlen=KEEP_STATES + 1)

prev_uptime = None
history = []
events  = []

def graceful_exit(sig, frame):
    print("\n[!] Останавливаю, генерирую отчёт…")
    raise KeyboardInterrupt
signal.signal(signal.SIGINT, graceful_exit)

try:
    while True:
        now = datetime.datetime.now()

        # Переносим предыдущие скриншоты, чтобы иметь N прошлых состояний
        for i in range(KEEP_STATES, 0, -1):
            src = OUT_DIR / STATUS_FILES[i-1]
            dst = OUT_DIR / STATUS_FILES[i]
            if src.exists():
                src.replace(dst)
            src = OUT_DIR / LOG_FILES[i-1]
            dst = OUT_DIR / LOG_FILES[i]
            if src.exists():
                src.replace(dst)

        # Пинг
        ok_ping, rtt_ping = ping(PING_TARGET)
        ok_router, rtt_router = ping(PING_ROUTER)
        logging.info(
            "ping %s: %s%s; router: %s%s",
            PING_TARGET,
            "OK " if ok_ping else "FAIL ",
            f"{rtt_ping:.2f} ms" if rtt_ping is not None else "",
            "OK " if ok_router else "FAIL ",
            f"{rtt_router:.2f} ms" if rtt_router is not None else "",
        )

        # Сохраняем свежий статус и скрин status
        status = scrape_status(driver)
        screenshot(driver, STATUS_FILES[0])
        # Сохраняем скрин логов и сам лог
        driver.get(ROUTER_URL_LOG)
        Wait(driver, PAGE_TIMEOUT).until(
            EC.presence_of_element_located((By.ID, "newtablelist"))
        )
        screenshot(driver, LOG_FILES[0])
        log_data = scrape_full_log(driver)
        log_history.appendleft(log_data)



        record = {
            "timestamp": now.isoformat(timespec="seconds"),
            "status": status,
            "ping_yandex": ok_ping,
            "ping_uandex_ms": rtt_ping,
            "ping_router": ok_router,
            "ping_router_ms": rtt_router,
        }

        # Проверяем обрыв по uptime
        cur_uptime = parse_uptime(status["uptime"])
        if prev_uptime is not None and cur_uptime < prev_uptime:
            idx = f"{len(events):03d}"
            evdir = EVENTS_DIR / idx
            evdir.mkdir(exist_ok=True)

            # Копируем скрины до и после
            if (OUT_DIR / STATUS_FILES[1]).exists():
                shutil.copy2(OUT_DIR / STATUS_FILES[1], evdir / "status_before.png")
            if (OUT_DIR / LOG_FILES[1]).exists():
                shutil.copy2(OUT_DIR / LOG_FILES[1],    evdir / "log_before.png")
            shutil.copy2(OUT_DIR / STATUS_FILES[0], evdir / "status_after.png")
            shutil.copy2(OUT_DIR / LOG_FILES[0],    evdir / "log_after.png")

            # Можно (по желанию) также сохранить предыдущий статус в JSON, если хочешь "до"
            if len(history) > 0:
                with open(evdir / "status_before.json", "w", encoding="utf-8") as f:
                    json.dump( history[-KEEP_STATES:], f, ensure_ascii=False, indent=2)

            evt = {
                "drop_detected": now.isoformat(timespec="seconds"),
                "prev_states": history[-KEEP_STATES:] if history else None,
                "new_state": record,
                "screens": {
                    "status_before": str(evdir / "status_before.png"),
                    "log_before":    str(evdir / "log_before.png"),
                    "status_after":  str(evdir / "status_after.png"),
                    "log_after":     str(evdir / "log_after.png"),
                },
                "logs": {
                    "before": log_history[-1] if len(log_history) > 1 else None,
                    "after":  log_data,
                }
            }
            events.append(evt)
            with open(evdir / "event.json", "w", encoding="utf-8") as f:
                json.dump(evt, f, ensure_ascii=False, indent=2)
            print(f"[!] ОБРЫВ {now:%F %T} – скрины и event сохранены в {evdir}")

        prev_uptime = cur_uptime
        history.append(record)
        if len(history) > 100:
            history = history[-100:]  # Чтобы не раздувать память

        time.sleep(CHECK_PERIOD)

except KeyboardInterrupt:
    pass
except Exception:
    traceback.print_exc()
finally:
    driver.quit()
    report = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "check_period": CHECK_PERIOD,
        "pings": {"target": PING_TARGET, "router": PING_ROUTER},
        "events": len(events)
    }
    out = OUT_DIR / "report.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[✓] Отчёт сохранён: {out}")
