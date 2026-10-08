"""Simulador de consola para demos: python -m nexlynk.cli --business barberia_demo --user 593999111222"""
from __future__ import annotations

import argparse

from .app import build_engine


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default="barberia_demo")
    ap.add_argument("--user", default="593000000001", help="número simulado; usa otro para simular otro cliente")
    args = ap.parse_args()
    engine = build_engine()
    print(f"[{args.business}] escribiendo como {args.user}. Ctrl+C para salir.\n")
    while True:
        try:
            text = input("tú > ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        reply = engine.handle_message(args.business, args.user, text)
        if reply.text:
            print(f"bot> {reply.text}")
            if reply.buttons:
                print("     [" + "] [".join(reply.buttons) + "]")
        print()


if __name__ == "__main__":
    main()
