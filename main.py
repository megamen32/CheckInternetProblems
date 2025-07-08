#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys, time, json, traceback, datetime, subprocess, signal, shutil
from pathlib import Path
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
CHECK_PERIOD = 5
PING_TARGET = "77.88.8.8"
PING_ROUTER = "192.168.2.1"

OUT_DIR    = Path("router_monitor")
EVENTS_DIR = OUT_DIR / "events"
OUT_DIR.mkdir(exist_ok=True)
EVENTS_DIR.mkdir(exist_ok=True)

# УТИЛИТЫ
def mk_driver():
    opts = webdriver.FirefoxOptions() if sys.platform.startswith("linux") else webdriver.ChromeOptions()
    #opts.add_argument("--headless")
    return (webdriver.Firefox if sys.platform.startswith("linux") else webdriver.Chrome)(options=opts)

def safe_txt(elem):
    return elem.text.replace('\xa0', ' ').strip()

def login(driver):
    driver.get(ROUTER_URL_LOGIN)
    Wait(driver, 10).until(EC.presence_of_element_located((By.NAME, "f_username")))
    driver.find_element(By.NAME, "f_username").send_keys(USERNAME)
    driver.find_element(By.NAME, "pwd").send_keys(PASSWORD)
    driver.find_element(By.XPATH, '//input[@value="Войти"]').click()
    time.sleep(1)

def scrape_status(driver):
    driver.get(ROUTER_URL_INFO)
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
    table = driver.find_element(By.ID, "newtablelist")
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

last_status_png = "status_last.png"
last_log_png    = "log_last.png"
prev_status_png = "status_prev.png"
prev_log_png    = "log_prev.png"
prev_log_data   = None

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

        # Переносим предыдущие скриншоты, чтобы иметь "до" состояния
        if (OUT_DIR / last_status_png).exists():
            (OUT_DIR / last_status_png).replace(OUT_DIR / prev_status_png)
        if (OUT_DIR / last_log_png).exists():
            (OUT_DIR / last_log_png).replace(OUT_DIR / prev_log_png)

        # Сохраняем свежий статус и скрин status
        status = scrape_status(driver)
        screenshot(driver, last_status_png)
        # Сохраняем скрин логов и сам лог
        driver.get(ROUTER_URL_LOG)
        screenshot(driver, last_log_png)
        log_data = scrape_full_log(driver)

        # Пинг
        ok_ping, rtt_ping = ping(PING_TARGET)
        ok_router, _ = ping(PING_ROUTER)

        record = {
            "timestamp": now.isoformat(timespec="seconds"),
            "status": status,
            "ping_ok": ok_ping,
            "ping_rtt": rtt_ping,
            "router_ping": ok_router
        }

        # Проверяем обрыв по uptime
        cur_uptime = parse_uptime(status["uptime"])
        if prev_uptime is not None and cur_uptime < prev_uptime:
            idx = f"{len(events):03d}"
            evdir = EVENTS_DIR / idx
            evdir.mkdir(exist_ok=True)

            # Копируем скрины до и после
            if (OUT_DIR / prev_status_png).exists():
                shutil.copy2(OUT_DIR / prev_status_png, evdir / "status_before.png")
            if (OUT_DIR / prev_log_png).exists():
                shutil.copy2(OUT_DIR / prev_log_png,    evdir / "log_before.png")
            shutil.copy2(OUT_DIR / last_status_png, evdir / "status_after.png")
            shutil.copy2(OUT_DIR / last_log_png,    evdir / "log_after.png")

            # Можно (по желанию) также сохранить предыдущий статус в JSON, если хочешь "до"
            if len(history) > 0:
                with open(evdir / "status_before.json", "w", encoding="utf-8") as f:
                    json.dump(history[-1], f, ensure_ascii=False, indent=2)

            evt = {
                "drop_detected": now.isoformat(timespec="seconds"),
                "prev_state": history[-1] if history else None,
                "new_state": record,
                "screens": {
                    "status_before": str(evdir / "status_before.png"),
                    "log_before":    str(evdir / "log_before.png"),
                    "status_after":  str(evdir / "status_after.png"),
                    "log_after":     str(evdir / "log_after.png"),
                },
                "logs": {
                    "before": prev_log_data,
                    "after":  log_data,
                }
            }
            events.append(evt)
            with open(evdir / "event.json", "w", encoding="utf-8") as f:
                json.dump(evt, f, ensure_ascii=False, indent=2)
            print(f"[!] ОБРЫВ {now:%F %T} – скрины и event сохранены в {evdir}")

        prev_uptime = cur_uptime
        prev_log_data = log_data
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
