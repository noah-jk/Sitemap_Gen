"""Admin commands: python -m app.manage create-user NAME | set-password NAME | list-users | migrate | backup DIR"""
import argparse
import getpass
import sys

from app.auth import create_user, list_usernames, set_password
from app.config import data_dir
from app.db import Store


def ask_password() -> str:
    password = getpass.getpass("New password (12+ characters, nothing shows while you type): ")
    if password != getpass.getpass("Same password again: "):
        sys.exit("The two passwords didn't match. Nothing was changed; run the command again.")
    return password


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-user", help="add a login").add_argument("username")
    sub.add_parser("set-password", help="change a login's password").add_argument("username")
    sub.add_parser("list-users", help="show the usernames that exist")
    sub.add_parser("migrate", help="bring every project database up to date")
    sub.add_parser("backup", help="consistent copies of every database").add_argument("dest_dir")
    args = ap.parse_args()

    store = Store(data_dir())
    try:
        if args.cmd == "create-user":
            create_user(store, args.username, ask_password())
            print(f"Created user {args.username}. You can log in now.")
        elif args.cmd == "set-password":
            if args.username not in list_usernames(store):
                sys.exit(f"There's no user called {args.username}. Run list-users to see who exists.")
            set_password(store, args.username, ask_password())
            print(f"Password changed for {args.username}. Anyone logged in as them has to log in again.")
        elif args.cmd == "list-users":
            print("\n".join(list_usernames(store)) or "No users yet.")
        elif args.cmd == "migrate":
            store.migrate_all()
            print("All databases are up to date.")
        elif args.cmd == "backup":
            for path in store.backup_all(args.dest_dir):
                print(path)
    except ValueError as e:
        sys.exit(f"Nothing was changed: {e}")
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nCancelled. Nothing was changed.")


if __name__ == "__main__":
    main()
