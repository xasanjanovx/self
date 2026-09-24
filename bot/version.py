"""Версия Джарвиса: номер выпуска + коммит и время обновления сервера (из .git внутри образа)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.6"          # выпуск Джарвиса (бот + приложение на телефоне); меняется вместе с APK
APP_VERSION = "1.6"      # последняя версия приложения jarvis-android

_ROOT = Path(__file__).resolve().parent.parent


def _commit() -> tuple[str, datetime | None]:
    git = _ROOT / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_file = git / ref
            if ref_file.exists():
                return ref_file.read_text(encoding="utf-8").strip()[:7], datetime.fromtimestamp(ref_file.stat().st_mtime, timezone.utc)
            for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                if line.endswith(ref):
                    return line[:7], None
            return "", None
        return head[:7], datetime.fromtimestamp((git / "HEAD").stat().st_mtime, timezone.utc)
    except OSError:
        return "", None


def info() -> dict[str, str]:
    commit, at = _commit()
    out = {"version": VERSION, "app_latest": APP_VERSION}
    if commit:
        out["commit"] = commit
    if at:
        out["server_updated"] = at.astimezone(timezone(timedelta(hours=5))).strftime("%d.%m.%Y %H:%M")
    return out


__all__ = ["VERSION", "APP_VERSION", "info"]
