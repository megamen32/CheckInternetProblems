#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, time, json, traceback, datetime, subprocess, signal, shutil, argparse, logging
from threading import Thread, Event
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
PING_INTERVAL = 1  # seconds between background pings
PING_FAIL_LIMIT = 3  # how many failed pings trigger event

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

# storage for background ping results
PING_HISTORY_SIZE = 300  # keep a few minutes of history
ping_router_history = deque(maxlen=PING_HISTORY_SIZE)
ping_target_history = deque(maxlen=PING_HISTORY_SIZE)
ping_stop = Event()


def ping_worker(host, store):
    while not ping_stop.is_set():
        ok, rtt = ping(host)
        store.append({
            "time": datetime.datetime.now(),
            "ok": ok,
            "rtt": rtt,
        })
        time.sleep(PING_INTERVAL)


router_thread = Thread(target=ping_worker, args=(PING_ROUTER, ping_router_history), daemon=True)
target_thread = Thread(target=ping_worker, args=(PING_TARGET, ping_target_history), daemon=True)
router_thread.start()
target_thread.start()

log_history = deque(maxlen=KEEP_STATES + 1)

prev_uptime = None
history = []
events  = []
router_fail_seq = 0
target_fail_seq = 0

def graceful_exit(sig, frame):
    print("\n[!] Останавливаю, генерирую отчёт…")
    ping_stop.set()
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

        # Последние результаты пингов из фоновых потоков
        last_target = ping_target_history[-1] if ping_target_history else None
        last_router = ping_router_history[-1] if ping_router_history else None
        ok_ping = last_target["ok"] if last_target else False
        rtt_ping = last_target["rtt"] if last_target else None
        ok_router = last_router["ok"] if last_router else False
        rtt_router = last_router["rtt"] if last_router else None
        if ok_router:
            router_fail_seq = 0
        else:
            router_fail_seq += 1
        if ok_ping:
            target_fail_seq = 0
        else:
            target_fail_seq += 1
        logging.info(
            "ping %s: %s%s; %s: %s%s",
            PING_TARGET,
            "OK " if ok_ping else "FAIL ",
            f"{rtt_ping:.2f} ms" if rtt_ping is not None else "",
            PING_ROUTER,
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
            "ping_yandex_ms": rtt_ping,
            "ping_router": ok_router,
            "ping_router_ms": rtt_router,
        }

        # Проверяем обрыв по uptime и пингам
        cur_uptime = parse_uptime(status["uptime"])
        drop_reason = None
        if prev_uptime is not None and cur_uptime < prev_uptime:
            drop_reason = "uptime_reset"
        elif router_fail_seq == PING_FAIL_LIMIT:
            drop_reason = "router_ping"
        elif target_fail_seq == PING_FAIL_LIMIT:
            drop_reason = "target_ping"
        if drop_reason:
            idx = f"{len(events):03d}"
            evdir = EVENTS_DIR / idx
            evdir.mkdir(exist_ok=True)

            # Сохраняем скриншоты до обрыва (до KEEP_STATES штук)
            before_screens = []
            for i in range(1, KEEP_STATES + 1):
                status_src = OUT_DIR / STATUS_FILES[i]
                log_src = OUT_DIR / LOG_FILES[i]
                status_dst = log_dst = None
                if status_src.exists():
                    status_dst = evdir / f"status_before_{i}.png"
                    shutil.copy2(status_src, status_dst)
                if log_src.exists():
                    log_dst = evdir / f"log_before_{i}.png"
                    shutil.copy2(log_src, log_dst)
                if status_dst or log_dst:
                    before_screens.append({
                        "status": str(status_dst) if status_dst else None,
                        "log": str(log_dst) if log_dst else None,
                    })

            # Скриншоты после обрыва
            shutil.copy2(OUT_DIR / STATUS_FILES[0], evdir / "status_after.png")
            shutil.copy2(OUT_DIR / LOG_FILES[0],    evdir / "log_after.png")
            after_screen = {
                "status": str(evdir / "status_after.png"),
                "log":    str(evdir / "log_after.png"),
            }

            # Сохраняем предыдущие состояния в отдельный JSON (для удобства)
            if history:
                with open(evdir / "status_before.json", "w", encoding="utf-8") as f:
                    json.dump(history[-KEEP_STATES:], f, ensure_ascii=False, indent=2)

            logs_before = list(log_history)[1:KEEP_STATES+1][::-1]

            cutoff = now - datetime.timedelta(seconds=120)
            router_pings = [
                {"time": p["time"].isoformat(timespec="seconds"), "ok": p["ok"], "rtt": p["rtt"]}
                for p in ping_router_history if p["time"] >= cutoff
            ]
            target_pings = [
                {"time": p["time"].isoformat(timespec="seconds"), "ok": p["ok"], "rtt": p["rtt"]}
                for p in ping_target_history if p["time"] >= cutoff
            ]

            evt = {
                "drop_detected": now.isoformat(timespec="seconds"),
                "reason": drop_reason,
                "prev_records": history[-KEEP_STATES:] if history else None,
                "new_record": record,
                "screens": {
                    "before": before_screens,
                    "after":  after_screen,
                },
                "logs": {
                    "before": logs_before,
                    "after":  log_data,
                },
                "pings": {
                    "router": router_pings,
                    "target": target_pings,
                }
            }
            events.append(evt)
            with open(evdir / "event.json", "w", encoding="utf-8") as f:
                json.dump(evt, f, ensure_ascii=False, indent=2)
            print(f"[!] ОБРЫВ {now:%F %T} – скрины и event сохранены в {evdir}")
            router_fail_seq = 0
            target_fail_seq = 0

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
    ping_stop.set()
    router_thread.join(timeout=1)
    target_thread.join(timeout=1)
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
