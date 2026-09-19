"""Start the local backend and frontend development servers together."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
FRONTEND_DIR = PROJECT_ROOT / "frontend"
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8000
FRONTEND_HOST = "127.0.0.1"
FRONTEND_PORT = 5173
REQUIRED_SERVICES = {
    "MySQL": 3306,
    "Qdrant": 6333,
    "Embedding": 8081,
    "Elasticsearch": 9200,
}


def _command_path(name: str) -> str | None:
    """Return an executable path while accounting for Windows command shims."""
    return shutil.which(name)


def _python_path() -> str:
    """Prefer the project's virtualenv even when start.py uses system Python."""
    relative_path = (
        Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    )
    project_python = PROJECT_ROOT / ".venv" / relative_path
    if project_python.exists():
        return str(project_python)
    return sys.executable


def _popen(command: list[str], *, cwd: Path) -> subprocess.Popen:
    """Start a child in its own process group so it can be cleaned up reliably."""
    kwargs: dict[str, object] = {"cwd": str(cwd)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _port_is_open(host: str, port: int, *, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_until_ready(
    process: subprocess.Popen,
    host: str,
    port: int,
    service_name: str,
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"{service_name}启动失败，退出码为 {return_code}。")
        if _port_is_open(host, port):
            return
        time.sleep(0.2)
    raise RuntimeError(f"等待{service_name}启动超时（{host}:{port}）。")


def _assert_port_available(port: int, service_name: str) -> None:
    if _port_is_open("127.0.0.1", port):
        raise RuntimeError(
            f"端口 {port} 已被占用，无法启动{service_name}。"
            "请先关闭上一次遗留的进程后重试。"
        )


def _check_infrastructure() -> None:
    unavailable = [
        f"{name}(:{port})"
        for name, port in REQUIRED_SERVICES.items()
        if not _port_is_open("127.0.0.1", port)
    ]
    if unavailable:
        services = "、".join(unavailable)
        raise RuntimeError(
            f"基础服务尚未就绪：{services}。\n"
            "请先执行 docker compose --env-file .env "
            "-f docker/docker-compose.yaml up -d。"
        )


def _install_frontend_dependencies() -> None:
    if (FRONTEND_DIR / "node_modules").exists():
        return

    pnpm = _command_path("pnpm")
    if pnpm is None:
        raise RuntimeError("未找到 pnpm，请先安装 Node.js 和 pnpm。")

    print("📥 第一次运行，正在执行 pnpm install...")
    # pnpm is commonly a .cmd shim on Windows. A shell is safe for this bounded,
    # synchronous install command; long-running servers are launched directly.
    command: list[str] | str
    if os.name == "nt":
        command = f'"{pnpm}" install'
    else:
        command = [pnpm, "install"]
    result = subprocess.run(command, cwd=FRONTEND_DIR, shell=os.name == "nt")
    if result.returncode != 0:
        raise RuntimeError(f"pnpm install 失败，退出码为 {result.returncode}。")


def run_backend() -> subprocess.Popen:
    print("🚀 正在启动后端 FastAPI 服务...")
    return _popen(
        [_python_path(), "-m", "fastapi", "dev", "main.py", "--host", BACKEND_HOST],
        cwd=PROJECT_ROOT,
    )


def run_frontend() -> subprocess.Popen:
    print("📦 正在启动前端服务...")
    if not FRONTEND_DIR.exists():
        raise RuntimeError("未找到 frontend 目录，请检查项目文件是否完整。")

    _install_frontend_dependencies()
    node = _command_path("node")
    vite = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
    if node is None:
        raise RuntimeError("未找到 node，请先安装 Node.js。")
    if not vite.exists():
        raise RuntimeError("未找到 Vite，请删除 node_modules 后重新执行 pnpm install。")

    return _popen(
        [
            node,
            str(vite),
            "--host",
            FRONTEND_HOST,
            "--port",
            str(FRONTEND_PORT),
            "--strictPort",
        ],
        cwd=FRONTEND_DIR,
    )


def _stop_process_tree(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return

    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass


def main() -> int:
    backend_process: subprocess.Popen | None = None
    frontend_process: subprocess.Popen | None = None
    exit_code = 0

    try:
        _check_infrastructure()
        _assert_port_available(BACKEND_PORT, "后端")
        _assert_port_available(FRONTEND_PORT, "前端")

        backend_process = run_backend()
        _wait_until_ready(
            backend_process,
            BACKEND_HOST,
            BACKEND_PORT,
            "后端服务",
            timeout=60,
        )

        frontend_process = run_frontend()
        _wait_until_ready(
            frontend_process,
            FRONTEND_HOST,
            FRONTEND_PORT,
            "前端服务",
            timeout=30,
        )

        print("\n=============================================")
        print("🎉 shopkeeper-agent 启动成功！")
        print(f"   - 前端页面: http://{FRONTEND_HOST}:{FRONTEND_PORT}")
        print(f"   - 后端接口: http://{BACKEND_HOST}:{BACKEND_PORT}")
        print(f"   - SSE 终点: http://{BACKEND_HOST}:{BACKEND_PORT}/api/query")
        print("提示: 按 Ctrl+C 可以同时关闭前后端服务")
        print("=============================================\n")

        while True:
            backend_code = backend_process.poll()
            frontend_code = frontend_process.poll()
            if backend_code is not None:
                raise RuntimeError(f"后端服务意外停止，退出码为 {backend_code}。")
            if frontend_code is not None:
                raise RuntimeError(f"前端服务意外停止，退出码为 {frontend_code}。")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n🛑 收到终止信号，正在关闭服务...")
    except RuntimeError as exc:
        exit_code = 1
        print(f"\n❌ 启动失败：{exc}", file=sys.stderr)
    finally:
        # Stop the frontend first so it cannot make requests during backend shutdown.
        _stop_process_tree(frontend_process)
        _stop_process_tree(backend_process)
        if backend_process is not None or frontend_process is not None:
            print("✨ 服务已安全关闭。")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
