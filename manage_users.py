from __future__ import annotations

import argparse
from getpass import getpass
from pathlib import Path

from sqlalchemy import select
from werkzeug.security import generate_password_hash

from db import User, create_session_factory


BASE_DIR = Path(__file__).resolve().parent
DB_URL = f"sqlite:///{(BASE_DIR / 'cdr_reporting.db').as_posix()}"
SessionFactory = create_session_factory(DB_URL)


def cmd_add(username: str, password: str) -> None:
    with SessionFactory() as session:
        existing = session.scalar(select(User).where(User.username == username))
        if existing:
            raise SystemExit(f"User '{username}' already exists")
        session.add(
            User(
                username=username,
                password_hash=generate_password_hash(password),
                is_active=True,
            )
        )
        session.commit()
    print(f"User '{username}' created")


def cmd_set_password(username: str, password: str) -> None:
    with SessionFactory() as session:
        user = session.scalar(select(User).where(User.username == username))
        if not user:
            raise SystemExit(f"User '{username}' not found")
        user.password_hash = generate_password_hash(password)
        session.commit()
    print(f"Password updated for '{username}'")


def cmd_list() -> None:
    with SessionFactory() as session:
        rows = session.scalars(select(User).order_by(User.username)).all()
    if not rows:
        print("No users")
        return
    for row in rows:
        print(f"{row.username}\tactive={row.is_active}\tcreated_at={row.created_at}")


def cmd_set_active(username: str, is_active: bool) -> None:
    with SessionFactory() as session:
        user = session.scalar(select(User).where(User.username == username))
        if not user:
            raise SystemExit(f"User '{username}' not found")
        user.is_active = is_active
        session.commit()
    state = "activated" if is_active else "disabled"
    print(f"User '{username}' {state}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage SMSC reporting users")
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="Add new user")
    add_p.add_argument("username")
    add_p.add_argument("--password")

    set_pass_p = sub.add_parser("set-password", help="Set user password")
    set_pass_p.add_argument("username")
    set_pass_p.add_argument("--password")

    disable_p = sub.add_parser("disable", help="Disable user")
    disable_p.add_argument("username")

    enable_p = sub.add_parser("enable", help="Enable user")
    enable_p.add_argument("username")

    sub.add_parser("list", help="List users")

    args = parser.parse_args()

    if args.command == "add":
        password = args.password or getpass("Password: ")
        cmd_add(args.username, password)
    elif args.command == "set-password":
        password = args.password or getpass("New password: ")
        cmd_set_password(args.username, password)
    elif args.command == "disable":
        cmd_set_active(args.username, is_active=False)
    elif args.command == "enable":
        cmd_set_active(args.username, is_active=True)
    elif args.command == "list":
        cmd_list()


if __name__ == "__main__":
    main()
