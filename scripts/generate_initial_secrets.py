import argparse
import os
import secrets
from pathlib import Path

SECRET_NAMES = (
    "API_TOKEN_HASH_KEY",
    "AGENT_DEVICE_CREDENTIAL_HASH_KEY",
    "AGENT_TOKEN_HASH_KEY",
    "CALLBACK_TOKEN_ENCRYPTION_KEY",
    "CALLBACK_TOKEN_FINGERPRINT_KEY",
)


def secret_value(name: str) -> str:
    if name == "CALLBACK_TOKEN_ENCRYPTION_KEY":
        return secrets.token_urlsafe(32)
    return secrets.token_urlsafe(48)


def generate(output: Path) -> None:
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = [f"{name}={secret_value(name)}\n" for name in SECRET_NAMES]
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.writelines(lines)


def append_missing(output: Path) -> int:
    stat = output.stat()
    if stat.st_mode & 0o077:
        raise PermissionError("existing Secret file permissions must be 0600")
    existing_names = {
        line.partition("=")[0]
        for line in output.read_text(encoding="ascii").splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    missing = [name for name in SECRET_NAMES if name not in existing_names]
    if not missing:
        return 0
    descriptor = os.open(output, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "a", encoding="ascii") as stream:
        stream.writelines(f"{name}={secret_value(name)}\n" for name in missing)
    return len(missing)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate initial crawler control-plane keys.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".secrets/crawler-api.keys.env"),
        help="Refuses to overwrite an existing file.",
    )
    parser.add_argument(
        "--append-missing",
        action="store_true",
        help="Append only missing managed keys to an existing mode-0600 file.",
    )
    args = parser.parse_args()
    generated = append_missing(args.output) if args.append_missing else None
    if generated is None:
        generate(args.output)
        generated = len(SECRET_NAMES)
    print(f"Generated {generated} missing secrets in {args.output}; values were not printed.")


if __name__ == "__main__":
    main()
