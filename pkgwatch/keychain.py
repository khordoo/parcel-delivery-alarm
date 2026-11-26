"""Pushover credentials in the macOS login Keychain (env vars override)."""

from __future__ import annotations

import os
import re
import subprocess

SERVICE = "pkgwatch.pushover"
ENV = {"app_token": "PUSHOVER_APP_TOKEN", "user_key": "PUSHOVER_USER_KEY"}


def get(account: str) -> str:
    if os.environ.get(ENV[account]):
        return os.environ[ENV[account]]
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def set(account: str, value: str) -> None:
    if account not in ENV:
        raise ValueError(account)
    # Pushover tokens/keys are 30 alphanumerics; strict check also rules out shell/quote injection.
    if not re.fullmatch(r"[A-Za-z0-9]{20,40}", value or ""):
        raise ValueError(f"{account} should be the 30-character key from pushover.net")
    # Fed through `security -i` on stdin so the secret never appears in the process list.
    cmd = f'add-generic-password -U -s {SERVICE} -a {account} -l "PkgWatch Pushover {account}" -w "{value}"\n'
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=10)
    if r.returncode != 0 or "error" in (r.stderr + r.stdout).lower():
        raise RuntimeError(f"keychain write failed: {(r.stderr or r.stdout).strip()}")


def delete(account: str) -> None:
    subprocess.run(["security", "delete-generic-password", "-s", SERVICE, "-a", account],
                   capture_output=True, timeout=10)


def pushover_credentials() -> tuple[str, str]:
    return get("app_token"), get("user_key")
