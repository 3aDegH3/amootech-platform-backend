import os

import httpx
from django.conf import settings


class BotControlError(Exception):
    pass


class PermissionSnapshotMissing(BotControlError):
    pass


class BotControlUnavailable(BotControlError):
    pass


def _base_url() -> str:
    return (getattr(settings, "TELEGRAM_BOT_CONTROL_BASE_URL", "") or "").rstrip("/")


def _token() -> str:
    return (getattr(settings, "TELEGRAM_BOT_CONTROL_TOKEN", "") or "").strip()


def _headers() -> dict[str, str]:
    t = _token()
    if not t:
        return {}
    return {"Authorization": f"Bearer {t}"}


async def bot_get(path: str, params: dict | None = None) -> dict:
    base = _base_url()
    if not base:
        raise BotControlUnavailable("bot control not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{base}{path}", params=params, headers=_headers())
        except httpx.RequestError as exc:
            raise BotControlUnavailable(str(exc)) from exc
        if resp.status_code >= 500:
            raise BotControlUnavailable(f"Bot error {resp.status_code}")
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 400:
            raise BotControlError(f"Bot error {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}


async def bot_post(path: str, json: dict | None = None) -> dict:
    base = _base_url()
    if not base:
        raise BotControlUnavailable("bot control not configured")
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{base}{path}", json=json, headers=_headers())
        except httpx.RequestError as exc:
            raise BotControlUnavailable(str(exc)) from exc
        if resp.status_code >= 500:
            raise BotControlUnavailable(f"Bot error {resp.status_code}: {resp.text[:500]}")
        if resp.status_code >= 400:
            raise BotControlError(f"Bot error {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}


# Sync wrappers for sync Django views (avoid async in sync views by using httpx sync)
def bot_get_sync(path: str, params: dict | None = None) -> dict:
    base = _base_url()
    if not base:
        raise BotControlUnavailable("bot control not configured")
    with httpx.Client(timeout=10.0) as client:
        try:
            resp = client.get(f"{base}{path}", params=params, headers=_headers())
        except httpx.RequestError as exc:
            raise BotControlUnavailable(str(exc)) from exc
        if resp.status_code >= 500:
            raise BotControlUnavailable(f"Bot error {resp.status_code}")
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 400:
            raise BotControlError(f"Bot error {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}


def bot_post_sync(path: str, json: dict | None = None) -> dict:
    base = _base_url()
    if not base:
        raise BotControlUnavailable("bot control not configured")
    with httpx.Client(timeout=10.0) as client:
        try:
            resp = client.post(f"{base}{path}", json=json, headers=_headers())
        except httpx.RequestError as exc:
            raise BotControlUnavailable(str(exc)) from exc
        if resp.status_code == 409 and resp.json().get("detail") == "permission_snapshot_missing":
            raise PermissionSnapshotMissing("permission_snapshot_missing")
        if resp.status_code >= 500:
            raise BotControlUnavailable(f"Bot error {resp.status_code}: {resp.text[:500]}")
        if resp.status_code >= 400:
            raise BotControlError(f"Bot error {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}


def telemetry_sync(telegram_chat_id: int, telegram_user_id: int | None = None) -> dict:
    params: dict[str, str] = {"telegram_chat_id": str(telegram_chat_id)}
    if telegram_user_id is not None:
        params["telegram_user_id"] = str(telegram_user_id)
    try:
        return bot_get_sync("/internal/v1/telemetry", params=params)
    except (BotControlError, BotControlUnavailable):
        return {}


def bot_reachable_sync() -> bool:
    base = _base_url()
    if not base:
        return False
    try:
        bot_get_sync("/internal/v1/status")
        return True
    except Exception:
        return False
