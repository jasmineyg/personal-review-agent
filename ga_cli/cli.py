"""Command-line launcher for the personal review agent."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import textwrap


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)


def launch(cmd_parts: list[str], args: list[str] | None = None) -> None:
    full_cmd = [part.replace("{PROJECT_DIR}", PROJECT_DIR) for part in cmd_parts]
    if args:
        full_cmd.extend(args)
    print("Starting:", " ".join(full_cmd))
    sys.stdout.flush()
    os.chdir(PROJECT_DIR)
    proc = subprocess.Popen(full_cmd)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        sys.exit(0)


COMMANDS = {
    "desktop": {
        "help": "start the local desktop bridge web UI",
        "cmd": [sys.executable, "{PROJECT_DIR}/launch.pyw"],
    },
    "tui": {
        "help": "start the terminal UI",
        "cmd": [sys.executable, "{PROJECT_DIR}/frontends/tui_v3.py"],
    },
    "cli": {
        "help": "start the lightweight CLI chat",
        "cmd": [sys.executable, "{PROJECT_DIR}/agentmain.py"],
    },
    "configure": {
        "help": "run the local key/model configuration helper",
        "cmd": [sys.executable, "{PROJECT_DIR}/assets/configure_mykey.py"],
    },
    "hub": {
        "help": "start the service launcher",
        "cmd": [sys.executable, "{PROJECT_DIR}/hub.pyw"],
    },
}


def cmd_list() -> None:
    print()
    print(f"  {'command':20s}  description")
    print(f"  {'-' * 20}  {'-' * 40}")
    for name, info in sorted(COMMANDS.items()):
        print(f"  {name:20s}  {info['help']}")
    print("  status                show running agent processes")
    print()


def cmd_status() -> None:
    try:
        import psutil
    except Exception:
        print("psutil is not installed; cannot inspect running processes")
        return
    running = [
        p for p in psutil.process_iter(["pid", "name", "cmdline"])
        if p.info["cmdline"]
        and any("agentmain" in c or "desktop_bridge" in c for c in p.info["cmdline"])
    ]
    if not running:
        print("No Personal-review-agent process found.")
        return
    for p in running:
        print(f"PID {p.info['pid']}: {' '.join(p.info['cmdline'][:4])}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ga",
        description="Personal-review-agent launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              ga desktop
              ga tui
              ga cli
              ga hub
            """
        ),
    )
    parser.add_argument("command", nargs="?", help="command name")
    parser.add_argument("args", nargs="*", help="arguments passed to the command")
    parser.add_argument("-v", "--version", action="store_true", help="show version")
    args, unknown = parser.parse_known_args()

    if args.version:
        print("personal-review-agent 0.1.0")
        return

    if not args.command or args.command == "help":
        parser.print_help()
        print("\ncommands:")
        cmd_list()
        return

    if args.command == "list":
        cmd_list()
        return
    if args.command == "status":
        cmd_status()
        return

    info = COMMANDS.get(args.command)
    if not info:
        print(f"Unknown command: {args.command}")
        print("Use 'ga list' to see available commands.")
        sys.exit(1)

    launch(list(info["cmd"]), list(args.args) + unknown)


if __name__ == "__main__":
    main()
