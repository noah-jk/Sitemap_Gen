"""Admin commands: python -m app.manage create-user NAME | migrate | backup DEST_DIR"""
import argparse
import getpass
import sys

from app.auth import create_user
from app.config import data_dir
from app.db import Store


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("create-user", help="add a login").add_argument("username")
    sub.add_parser("migrate", help="bring every project database up to date")
    sub.add_parser("backup", help="consistent copies of every database").add_argument("dest_dir")
    args = ap.parse_args()

    store = Store(data_dir())
    if args.cmd == "create-user":
        password = getpass.getpass("Password (12+ characters): ")
        if password != getpass.getpass("Again: "):
            sys.exit("passwords don't match")
        try:
            create_user(store, args.username, password)
        except ValueError as e:
            sys.exit(str(e))
        print(f"created user {args.username}")
    elif args.cmd == "migrate":
        store.migrate_all()
        print("all databases up to date")
    elif args.cmd == "backup":
        for path in store.backup_all(args.dest_dir):
            print(path)


if __name__ == "__main__":
    main()
