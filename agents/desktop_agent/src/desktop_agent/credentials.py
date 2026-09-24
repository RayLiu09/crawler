import argparse
import getpass
import sys
from collections.abc import Callable, Sequence
from typing import Protocol

import keyring
from keyring.errors import KeyringError


class CredentialStore(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


SecretReader = Callable[[str], str]


def run_credential_command(
    arguments: Sequence[str],
    *,
    store: CredentialStore = keyring,
    secret_reader: SecretReader = getpass.getpass,
) -> int:
    parser = argparse.ArgumentParser(
        prog="desktop-collection-agent-credential",
        description="Manage one desktop Agent device credential in the OS credential store.",
    )
    parser.add_argument("action", choices=("set", "delete", "status"))
    parser.add_argument("--service", required=True)
    parser.add_argument("--username", required=True)
    options = parser.parse_args(arguments)

    try:
        if options.action == "status":
            exists = store.get_password(options.service, options.username) is not None
            print("configured" if exists else "missing")
            return 0 if exists else 1
        if options.action == "delete":
            store.delete_password(options.service, options.username)
            print("deleted")
            return 0

        secret = secret_reader("Agent device secret: ")
        confirmation = secret_reader("Confirm Agent device secret: ")
        if secret != confirmation:
            print("credential confirmation does not match", file=sys.stderr)
            return 2
        if len(secret) < 32 or len(secret) > 4096:
            print("credential length must be between 32 and 4096 characters", file=sys.stderr)
            return 2
        store.set_password(options.service, options.username, secret)
        print("stored")
        return 0
    except KeyringError:
        print("OS credential store operation failed", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run_credential_command(sys.argv[1:]))
