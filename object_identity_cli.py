"""Shell management for DBBASIC identity, in the spirit of Django manage.py.

This CLI operates directly on the runtime data directory, so an operator on
the VM can bootstrap the first admin user and manage accounts, users, and
passwords over SSH without HTTP access or tokens. Passwords are prompted
without echo (or read from stdin for scripting) and are never printed.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

import object_credentials
import object_identity

DATA_DIR_ENV = "DBBASIC_DATA_DIR"
ADMIN_ROLE = "admin"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Manage DBBASIC identity accounts, users, and passwords from the shell."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=f"runtime data directory (default: ${DATA_DIR_ENV} or ./data)",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    create_account = subcommands.add_parser("create-account", help="create an account")
    create_account.add_argument("--account-id", required=True)
    create_account.add_argument("--name", default="")

    create_user = subcommands.add_parser("create-user", help="create a user")
    create_user.add_argument("--user-id", required=True)
    create_user.add_argument("--email")
    create_user.add_argument("--display-name")
    create_user.add_argument("--account-id")
    create_user.add_argument("--roles", help="comma-separated roles")

    create_superuser = subcommands.add_parser(
        "create-superuser",
        help="create an admin-role user and prompt for its password",
    )
    create_superuser.add_argument("--user-id", required=True)
    create_superuser.add_argument("--email")
    create_superuser.add_argument("--display-name")
    create_superuser.add_argument("--account-id")
    create_superuser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )

    set_password = subcommands.add_parser("set-password", help="set or replace a user password")
    set_password.add_argument("--user-id", required=True)
    set_password.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )

    remove_password = subcommands.add_parser("remove-password", help="remove a user password")
    remove_password.add_argument("--user-id", required=True)

    list_accounts = subcommands.add_parser("list-accounts", help="list accounts")
    list_accounts.add_argument("--json", action="store_true")

    list_users = subcommands.add_parser("list-users", help="list users")
    list_users.add_argument("--account-id")
    list_users.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    base_dir = _base_dir(args.data_dir)

    try:
        if args.command == "create-account":
            account = object_identity.create_account(
                {"account_id": args.account_id, "name": args.name},
                base_dir=base_dir,
            )
            print(f"Created account {account['account_id']}")
            return 0

        if args.command == "create-user":
            user = object_identity.create_user(
                _user_payload(args, roles=_split_roles(args.roles)),
                base_dir=base_dir,
            )
            print(f"Created user {user['user_id']}")
            return 0

        if args.command == "create-superuser":
            password = _read_password(password_stdin=args.password_stdin)
            user = object_identity.create_user(
                _user_payload(args, roles=[ADMIN_ROLE]),
                base_dir=base_dir,
            )
            object_credentials.set_password(user["user_id"], password, base_dir=base_dir)
            print(f"Created superuser {user['user_id']} with role {ADMIN_ROLE} and password set")
            return 0

        if args.command == "set-password":
            object_identity.get_user(args.user_id, base_dir=base_dir)
            password = _read_password(password_stdin=args.password_stdin)
            result = object_credentials.set_password(args.user_id, password, base_dir=base_dir)
            print(f"Password {result['operation']} for user {result['user_id']}")
            return 0

        if args.command == "remove-password":
            object_identity.get_user(args.user_id, base_dir=base_dir)
            removed = object_credentials.remove_password(args.user_id, base_dir=base_dir)
            print(
                f"Password removed for user {args.user_id}"
                if removed
                else f"User {args.user_id} had no password"
            )
            return 0

        if args.command == "list-accounts":
            accounts = object_identity.list_accounts(base_dir=base_dir)
            if args.json:
                print(json.dumps(accounts, indent=2))
            else:
                for account in accounts:
                    print(f"{account['account_id']}\t{account['status']}\t{account['name']}")
                print(f"{len(accounts)} account(s)")
            return 0

        if args.command == "list-users":
            users = object_identity.list_users(account_id=args.account_id, base_dir=base_dir)
            if args.json:
                print(json.dumps(users, indent=2))
            else:
                for user in users:
                    roles = ",".join(user["roles"])
                    has_password = object_credentials.has_password(
                        user["user_id"], base_dir=base_dir
                    )
                    password_state = "password" if has_password else "no-password"
                    print(
                        f"{user['user_id']}\t{user['status']}\t{user['email'] or '-'}"
                        f"\t{roles or '-'}\t{password_state}"
                    )
                print(f"{len(users)} user(s)")
            return 0
    except (ValueError, LookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2


def _base_dir(data_dir: Path | None) -> Path:
    if data_dir is not None:
        return data_dir
    return Path(os.environ.get(DATA_DIR_ENV, "data"))


def _user_payload(args: argparse.Namespace, *, roles: list[str]) -> dict[str, object]:
    payload: dict[str, object] = {"user_id": args.user_id, "roles": roles}
    if args.email:
        payload["email"] = args.email
    if args.display_name:
        payload["display_name"] = args.display_name
    if args.account_id:
        payload["account_id"] = args.account_id
    return payload


def _split_roles(value: str | None) -> list[str]:
    if not value:
        return []
    return [role.strip() for role in value.split(",") if role.strip()]


def _read_password(*, password_stdin: bool) -> str:
    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        if not password:
            raise ValueError("password on stdin is empty")
        return password

    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Password (again): ")
    if password != confirmation:
        raise ValueError("passwords do not match")
    return password


if __name__ == "__main__":
    raise SystemExit(main())
