from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def discover_env_files(env_dir: Path, pattern: str) -> list[Path]:
    return sorted(path for path in env_dir.glob(pattern) if path.is_file())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run multiple HeroBot instances.")
    parser.add_argument(
        "--env-dir",
        default="instances",
        help="Directory containing one env file per bot instance.",
    )
    parser.add_argument(
        "--pattern",
        default="*.env",
        help="Glob pattern for instance env files. Defaults to *.env.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List matching env files and exit without starting processes.",
    )
    args = parser.parse_args()

    env_dir = Path(args.env_dir)
    env_files = discover_env_files(env_dir, args.pattern)
    if not env_files:
        raise SystemExit(f"No env files found in {env_dir} matching {args.pattern}")
    if args.list:
        for env_file in env_files:
            print(env_file)
        return

    processes: list[subprocess.Popen[str]] = []
    try:
        for env_file in env_files:
            name = env_file.stem
            env = os.environ.copy()
            env["HEROBOT_INSTANCE_NAME"] = name
            process = subprocess.Popen(
                [sys.executable, "-m", "herobot.bot", "--env-file", str(env_file)],
                env=env,
                text=True,
            )
            processes.append(process)
            print(f"started {name}: pid={process.pid} env={env_file}", flush=True)

        # REVIEW: 用 time.sleep(1) 轮询检查子进程状态是比较原始的做法。
        # 问题：
        # 1. 浪费 CPU——每秒唤醒一次检查所有进程
        # 2. 一个进程异常退出会立即 SystemExit，其他正常运行的进程会被 finally 终止。
        #    在生产环境中，应该有重启策略而不是全部停掉。
        # 3. 没有日志——stdout/stderr 没有被捕获或转发，子进程的输出可能丢失
        #
        # 建议用 asyncio.create_subprocess_exec 配合 asyncio.gather，
        # 或者用 supervisor/systemd 来管理多进程。
        while processes:
            for process in list(processes):
                code = process.poll()
                if code is not None:
                    processes.remove(process)
                    print(f"process pid={process.pid} exited with code={code}", flush=True)
                    if code != 0:
                        raise SystemExit(code)
            time.sleep(1)
    except KeyboardInterrupt:
        print("stopping HeroBot instances...", flush=True)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
