# launcher.py
import sys
import os
import asyncio
import threading
import webbrowser
import time
import urllib.request

if getattr(sys, 'frozen', False):
    base = sys._MEIPASS
    if base not in sys.path:
        sys.path.insert(0, base)
    os.chdir(base)

RADAR_URL  = "http://127.0.0.1:8000/radar"
HEALTH_URL = "http://127.0.0.1:8000/health"


def wait_and_open():
    deadline = time.time() + 15.0
    while time.time() < deadline:
        try:
            urllib.request.urlopen(HEALTH_URL, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    webbrowser.open(RADAR_URL)
    print(f"[launcher] Browser aberto em {RADAR_URL}")


def main():
    print("============================================")
    print("  WT IOX API")
    print("  Radar:     http://127.0.0.1:8000/radar")
    print("  Dashboard: http://127.0.0.1:8000/dashboard")
    print("  Pressione Ctrl+C para encerrar.")
    print("============================================\n")
    threading.Thread(target=wait_and_open, daemon=True).start()
    from server.main import main as server_main
    try:
        asyncio.run(server_main())
    except KeyboardInterrupt:
        print("\n[launcher] Encerrado.")


if __name__ == "__main__":
    main()
