import os
import subprocess
import time


def run_backend():
    print("🚀 正在启动后端 FastAPI 服务...")
    # 使用 uv run 启动 FastAPI
    # dev 模式默认包含热重载，如果是生产环境请改为 run fastapi run main.py
    return subprocess.Popen(["uv", "run", "fastapi", "dev", "main.py"])


def run_frontend():
    print("📦 正在安装前端依赖并启动前端服务...")
    frontend_dir = os.path.join(os.path.dirname(__file__), "frontend")

    # 检查前端目录是否存在
    if not os.path.exists(frontend_dir):
        print("❌ 未找到 frontend 目录，请检查路径。")
        return None

    # 如果没有 node_modules，先自动跑一遍 pnpm install
    if not os.path.exists(os.path.join(frontend_dir, "node_modules")):
        print("📥 第一次运行，正在执行 pnpm install...")
        subprocess.run(["pnpm", "install"], cwd=frontend_dir, shell=True)

    # 启动前端开发服务器
    return subprocess.Popen(["pnpm", "dev"], cwd=frontend_dir, shell=True)


def main():
    backend_process = None
    frontend_process = None

    try:
        # 1. 启动后端
        backend_process = run_backend()

        # 稍微等后端初始化一下
        time.sleep(1.5)

        # 2. 启动前端
        frontend_process = run_frontend()

        print("\n=============================================")
        print("🎉 shopkeeper-agent 启动成功！")
        print("   - 后端接口: http://127.0.0.1:8000")
        print("   - SSE 终点: http://127.0.0.1:8000/api/query")
        print("提示: 按 Ctrl+C 可以同时关闭前后端服务")
        print("=============================================\n")

        # 保持主进程运行，并监控子进程
        while True:
            if backend_process.poll() is not None:
                print("⚠️ 后端服务意外停止。")
                break
            if frontend_process and frontend_process.poll() is not None:
                print("⚠️ 前端服务意外停止。")
                break
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n🛑 收到终止信号，正在关闭服务...")
    finally:
        # 优雅清理子进程，防止端口占用
        if backend_process:
            backend_process.terminate()
        if frontend_process:
            frontend_process.terminate()
        print("✨ 服务已安全关闭。")


if __name__ == "__main__":
    main()
