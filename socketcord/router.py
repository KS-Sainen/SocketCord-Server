"""Routes a complete RequestCord to the function registered for (method, URL).

URL patterns are slash-separated; `{name}` captures a segment and
`{name:uuid}` / `{name:int}` also converts it. A request whose method/URL
matches nothing gets 253 Not Found.
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .protocol import METHOD_NAMES, ReplyCord, RequestCord, Status


class ApiError(Exception):
    """Raise from a handler to answer with an error status."""

    def __init__(self, status: int, code: str, detail: str = "", **extra: Any) -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail, self.extra = status, code, detail, extra

    def reply(self) -> ReplyCord:
        data = {"error": self.code, "detail": self.detail}
        data.update(self.extra)
        return ReplyCord.from_dict(self.status, data)


def bad_request(code: str, detail: str, **extra: Any) -> ApiError:
    return ApiError(Status.BAD_REQUEST, code, detail, **extra)


def unauthorized(code: str, detail: str, **extra: Any) -> ApiError:
    return ApiError(Status.UNAUTHORIZED, code, detail, **extra)


def forbidden(code: str, detail: str, **extra: Any) -> ApiError:
    return ApiError(Status.FORBIDDEN, code, detail, **extra)


def not_found(code: str = "NOT_FOUND", detail: str = "not found", **extra: Any) -> ApiError:
    return ApiError(Status.NOT_FOUND, code, detail, **extra)


def ok(data: Optional[dict] = None, **extra: Any) -> ReplyCord:
    return ReplyCord.from_dict(Status.OK, data, **extra)


def normalize_url(url: str) -> str:
    return url.strip().strip("/")


# ---------------------------------------------------------------------- routes

_CONVERTERS: Dict[str, Callable[[str], Any]] = {"str": str, "uuid": uuid.UUID, "int": int}


@dataclass
class Route:
    method: int
    pattern: str
    handler: Callable[..., ReplyCord]
    auth: bool
    parts: List[Tuple[str, Optional[str], Optional[str]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for seg in normalize_url(self.pattern).split("/"):
            if seg.startswith("{") and seg.endswith("}"):
                name, _, kind = seg[1:-1].partition(":")
                self.parts.append(("param", name, kind or "str"))
            else:
                self.parts.append(("literal", seg.lower(), None))

    def match(self, segments: List[str]) -> Optional[Dict[str, Any]]:
        if len(segments) != len(self.parts):
            return None
        params: Dict[str, Any] = {}
        for seg, (kind, value, conv) in zip(segments, self.parts):
            if kind == "literal":
                if seg.lower() != value:
                    return None
            else:
                try:
                    params[value] = _CONVERTERS[conv](seg)
                except (ValueError, TypeError):
                    return None
        return params

    @property
    def literal_count(self) -> int:
        return sum(1 for kind, _, _ in self.parts if kind == "literal")

    def describe(self) -> str:
        return f"{METHOD_NAMES.get(self.method, self.method):5} {self.pattern}"


class Router:
    def __init__(self) -> None:
        self.routes: List[Route] = []

    def route(self, method: int, pattern: str, auth: bool = True) -> Callable:
        def register(fn: Callable[..., ReplyCord]) -> Callable[..., ReplyCord]:
            self.routes.append(Route(method, pattern, fn, auth))
            # Literal segments win over parameters, e.g. users/online before users/{user_id}.
            self.routes.sort(key=lambda r: -r.literal_count)
            return fn
        return register

    def match(self, method: int, url: str) -> Optional[Tuple[Route, Dict[str, Any]]]:
        url = normalize_url(url)
        if not url or "//" in url:
            return None
        segments = url.split("/")
        for route in self.routes:
            if route.method != method:
                continue
            params = route.match(segments)
            if params is not None:
                return route, params
        return None


# ---------------------------------------------------------------------- request context

@dataclass
class RequestContext:
    app: Any                      # socketcord.app.Application
    peer: Any                     # socketcord.server.Peer
    request: RequestCord
    body: dict
    user: Any = None              # db.base.User, for authenticated routes
    session: Any = None           # db.base.Session

    @property
    def repo(self):
        return self.app.repo

    @property
    def hub(self):
        return self.app.hub

    @property
    def config(self):
        return self.app.config

    # ---- body field helpers; each raises 250 with a precise message
    def has(self, key: str) -> bool:
        return self.body.get(key) is not None

    def str_field(self, key: str, required: bool = True, min_len: int = 1, max_len: int = 4000,
                  strip: bool = True, default: Optional[str] = None, multiline: bool = False) -> Optional[str]:
        """A text field. Control characters are refused; `multiline` text may contain
        newlines and tabs (message content, descriptions, bios), names may not."""
        value = self.body.get(key)
        if value is None:
            if required:
                raise bad_request("MISSING_FIELD", f"'{key}' is required", field=key)
            return default
        if not isinstance(value, str):
            raise bad_request("INVALID_FIELD", f"'{key}' must be a string", field=key)
        if strip:
            value = value.strip()
        if not min_len <= len(value) <= max_len:
            raise bad_request("INVALID_FIELD", f"'{key}' must be {min_len}-{max_len} characters", field=key)
        allowed = _MULTILINE_CONTROLS if multiline else ()
        if any(unicodedata.category(ch) == "Cc" and ch not in allowed for ch in value):
            raise bad_request("INVALID_FIELD", f"'{key}' contains control characters", field=key)
        return value

    def int_field(self, key: str, required: bool = True, minimum: Optional[int] = None,
                  maximum: Optional[int] = None, default: Optional[int] = None) -> Optional[int]:
        value = self.body.get(key)
        if value is None:
            if required:
                raise bad_request("MISSING_FIELD", f"'{key}' is required", field=key)
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise bad_request("INVALID_FIELD", f"'{key}' must be an integer", field=key)
        low = INT64_MIN if minimum is None else minimum
        high = INT64_MAX if maximum is None else maximum   # fits a PostgreSQL bigint
        if not low <= value <= high:
            raise bad_request("INVALID_FIELD", f"'{key}' must be in {low}..{high}", field=key)
        return value

    def bool_field(self, key: str, default: bool = False) -> bool:
        value = self.body.get(key)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise bad_request("INVALID_FIELD", f"'{key}' must be true or false", field=key)
        return value

    def uuid_field(self, key: str, required: bool = True) -> Optional[uuid.UUID]:
        value = self.body.get(key)
        if value is None:
            if required:
                raise bad_request("MISSING_FIELD", f"'{key}' is required", field=key)
            return None
        try:
            return uuid.UUID(str(value))
        except ValueError:
            raise bad_request("INVALID_FIELD", f"'{key}' must be a UUID", field=key) from None

    def b64_field(self, key: str, length: Optional[int] = None) -> bytes:
        value = self.body.get(key)
        if value is None:
            raise bad_request("MISSING_FIELD", f"'{key}' is required", field=key)
        try:
            raw = base64.b64decode(str(value).encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError):
            raise bad_request("INVALID_FIELD", f"'{key}' must be base64", field=key) from None
        if length is not None and len(raw) != length:
            raise bad_request("INVALID_FIELD", f"'{key}' must decode to {length} bytes", field=key)
        return raw

    def time_field(self, value: Any, key: str) -> datetime:
        """Accepts ISO 8601 ("2026-11-05T10:00:00Z", "...+07:00", date only) or epoch seconds."""
        if isinstance(value, bool):
            raise bad_request("INVALID_FIELD", f"'{key}' must be a timestamp", field=key)
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                raise bad_request("INVALID_FIELD", f"'{key}' is out of range", field=key) from None
        if isinstance(value, str):
            dt = parse_iso8601(value)
            if dt is None:
                raise bad_request("INVALID_FIELD", f"'{key}' must be ISO 8601 or epoch seconds", field=key)
            try:
                # In UTC from here on, so every backend compares the same instant (and an
                # offset that pushes the date past year 1 or 9999 is refused, not a crash).
                return dt.astimezone(timezone.utc)
            except (OverflowError, ValueError):
                raise bad_request("INVALID_FIELD", f"'{key}' is out of range", field=key) from None
        raise bad_request("INVALID_FIELD", f"'{key}' must be a timestamp", field=key)


INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1
_MULTILINE_CONTROLS = ("\n", "\r", "\t")

_ISO8601 = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,9}))?)?)?"
    r"\s*(?:([Zz])|([+-])(\d{2}):?(\d{2})?)?$")


def parse_iso8601(text: str) -> Optional[datetime]:
    """ISO 8601 parsing that behaves the same on Python 3.9 and 3.12 (no timezone = UTC)."""
    m = _ISO8601.match(text.strip())
    if not m:
        return None
    year, month, day, hh, mm, ss, frac, zulu, sign, off_h, off_m = m.groups()
    try:
        tz = timezone.utc
        if sign:
            offset = timedelta(hours=int(off_h), minutes=int(off_m or 0))
            tz = timezone(offset if sign == "+" else -offset)
        micro = int((frac or "0").ljust(6, "0")[:6])
        return datetime(int(year), int(month), int(day), int(hh or 0), int(mm or 0), int(ss or 0), micro, tzinfo=tz)
    except ValueError:  # e.g. month 13, or an offset of 24 hours or more
        return None
