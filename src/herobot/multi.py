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
    parser.add_argument(
        "--config-dir",
        default="instances",
        help="Directory containing optional per-instance TOML config files.",
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
            command = [sys.executable, "-m", "herobot.bot", "--env-file", str(env_file)]
            config_file = Path(args.config_dir) / f"{name}.toml"
            if config_file.exists():
                command.extend(["--config", str(config_file)])
            process = subprocess.Popen(
                command,
                env=env,
                text=True,
            )
            processes.append(process)
            print(f"started {name}: pid={process.pid} env={env_file}", flush=True)

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
