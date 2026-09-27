"""Launch the supplied CRE app and the modified local Open Reel editor."""
import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen


def free_port(start):
    for port in range(start, start + 50):
        occupied = False
        for host in ("127.0.0.1", "::1"):
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    occupied = True
                    break
            except OSError:
                pass
        if occupied:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                pass
    raise RuntimeError("No available local port")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--editor-dir", type=Path, default=Path.home() / "Downloads/openreel-video-main/openreel-video-main/apps/web")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    node = shutil.which("node")
    vite = args.editor_dir / "node_modules/vite/bin/vite.js"
    if not node or not vite.exists():
        raise SystemExit("Open Reel's installed Node/Vite runtime was not found. Check --editor-dir.")
    editor_port, app_port = free_port(5173), free_port(8502)
    environment = os.environ.copy()
    environment["CRE_EDITOR_URL"] = f"http://localhost:{editor_port}"
    environment["CRE_APP_URL"] = f"http://localhost:{app_port}"
    if args.data_dir:
        environment["CRE_DATA_DIR"] = str(args.data_dir.resolve())
    logs = root / "logs"
    logs.mkdir(exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    processes = []
    try:
        for name, command, cwd, port in (
            ("editor", [node, str(vite), "--host", "127.0.0.1", "--port", str(editor_port), "--strictPort"], args.editor_dir, editor_port),
            ("cre", [sys.executable, "-m", "streamlit", "run", str(root / "app.py"), "--server.address=127.0.0.1", f"--server.port={app_port}", "--server.headless=true"], root, app_port),
        ):
            with (logs / f"{name}.log").open("wb") as log:
                process = subprocess.Popen(command, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            processes.append(process)
            for _ in range(120):
                if process.poll() is not None:
                    raise RuntimeError(f"{name} stopped; see {logs / (name + '.log')}")
                try:
                    with urlopen(f"http://127.0.0.1:{port}", timeout=1):
                        break
                except OSError:
                    time.sleep(0.25)
            else:
                raise RuntimeError(f"{name} did not start in time")
        print(f"CRE: http://localhost:{app_port}")
        print(f"Open Reel: http://localhost:{editor_port}")
        print("Process IDs: " + ", ".join(str(p.pid) for p in processes))
    except Exception:
        for process in processes:
            process.terminate()
        raise


if __name__ == "__main__":
    main()
