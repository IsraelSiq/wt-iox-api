# build.py
import subprocess
import sys
import os

EXE_NAME = "wt-iox-api"

icon_args = []
if os.path.exists("assets/icon.ico"):
    icon_args = ["--icon", "assets/icon.ico"]

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--name", EXE_NAME,
    "--console",
    "--add-data", "server;server",
    "--collect-all", "uvicorn",
    "--collect-all", "fastapi",
    "--collect-all", "starlette",
    "--collect-all", "pydantic",
    "--collect-all", "pydantic_core",
    "--collect-all", "anyio",
    "--collect-all", "h11",
    "--collect-all", "aiohttp",
    "--hidden-import", "uvicorn.logging",
    "--hidden-import", "uvicorn.loops",
    "--hidden-import", "uvicorn.loops.asyncio",
    "--hidden-import", "uvicorn.protocols",
    "--hidden-import", "uvicorn.protocols.http",
    "--hidden-import", "uvicorn.protocols.http.h11_impl",
    "--hidden-import", "uvicorn.lifespan",
    "--hidden-import", "uvicorn.lifespan.on",
    "--hidden-import", "asyncio",
    "--hidden-import", "webbrowser",
    "--hidden-import", "threading",
    "--hidden-import", "urllib.request",
    *icon_args,
    "--noconfirm",
    "--clean",
    "launcher.py",
]

print(f"[build] Gerando {EXE_NAME}.exe...")
result = subprocess.run(cmd)
if result.returncode == 0:
    print(f"\n[build] Sucesso! dist/{EXE_NAME}.exe")
else:
    print("\n[build] Falhou.")
    sys.exit(1)
