"""Small, cache-first client for the private Xueqiu timeline endpoints.

This module is intentionally independent from the production application.  It
does not import the database or any runtime provider code.  The endpoint is a
private research input and should not become a runtime dependency.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import requests

TIMELINE_URL = "https://api.xueqiu.com/v4/statuses/user_timeline.json"
DETAIL_URL = "https://api.xueqiu.com/statuses/show.json"
DEFAULT_USER_ID = "8374048440"
DEFAULT_COUNT = 20
DEFAULT_TYPE = 10
DEFAULT_REQUEST_INTERVAL = 2.0
REQUEST_ATTEMPTS = 3
RETRY_BACKOFFS = (2.0, 5.0)
DEFAULT_TIMEOUT = 30.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class XueqiuClientError(RuntimeError):
    """Base class for safe, actionable client failures."""


class CookieError(XueqiuClientError):
    """The online client could not load a usable cookie file."""


class CacheError(XueqiuClientError):
    """A raw cache could not be read or written."""


class CacheMissError(CacheError):
    """Offline mode was asked for a raw response that is not cached."""


class MalformedResponseError(XueqiuClientError):
    """The provider returned invalid JSON or an unusable response shape."""


class RequestError(XueqiuClientError):
    """A request failed after the bounded retry policy."""


class NotFoundError(RequestError):
    """A requested Xueqiu post no longer exists."""


@dataclass(frozen=True, slots=True)
class TimelineResult:
    """Raw timeline statuses and the reason pagination stopped."""

    pages: int
    statuses: list[dict[str, Any]]
    stop_reason: str
    raw_post_count: int = 0

    @property
    def raw_statuses(self) -> list[dict[str, Any]]:
        """Compatibility spelling for callers that use the source term."""

        return self.statuses


class XueqiuClient:
    """Sequential, cache-first access to one Xueqiu user's public archive.

    ``cache_dir`` is the account-specific directory for provider payloads.  A
    timeline page is stored as ``timeline-page-NNNN.json`` and a detail
    response as ``post-ID.json``.  Deleted detail posts also leave a separate
    ``post-ID.statuses-show.not-found`` marker, never a fake provider response.
    """

    def __init__(
        self,
        cookie_file: str | os.PathLike[str] | None = None,
        cache_dir: str | os.PathLike[str] = "research/cache/xueqiu",
        *,
        user_id: str = DEFAULT_USER_ID,
        count: int = DEFAULT_COUNT,
        request_interval: float = DEFAULT_REQUEST_INTERVAL,
        offline: bool = False,
        refresh: bool = False,
        session: Any | None = None,
        sleep: Callable[[float], Any] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        try:
            interval = float(request_interval)
        except (TypeError, ValueError) as exc:
            raise ValueError("request_interval must be a finite number >= 1 second") from exc
        if not math.isfinite(interval) or interval < 1.0:
            raise ValueError("request_interval must be a finite number >= 1 second")
        if int(count) != count or int(count) < 1:
            raise ValueError("count must be a positive integer")
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError("timeout must be a positive finite number") from exc
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("timeout must be a positive finite number")

        self.user_id = str(user_id)
        if not self.user_id:
            raise ValueError("user_id must not be empty")
        self.count = int(count)
        self.request_interval = interval
        self.offline = bool(offline)
        self.refresh = bool(refresh)
        self.timeout = timeout_value
        self.cache_dir = Path(cache_dir)
        self.session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._last_request_at: float | None = None

        # Offline replay deliberately does not read a cookie at all.
        cookie = None if self.offline else self._read_cookie(cookie_file)
        headers = {
            "Referer": f"https://xueqiu.com/u/{self.user_id}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if cookie is not None:
            headers["Cookie"] = cookie
        self._headers = headers
        try:
            self.session.headers.update(headers)
        except AttributeError:
            # A tiny injected test session need only implement get().
            pass

    @staticmethod
    def _read_cookie(cookie_file: str | os.PathLike[str] | None) -> str:
        if cookie_file is None:
            raise CookieError("cookie_file is required for online requests")
        try:
            cookie = Path(cookie_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            # Do not include the path or file content in a user-visible error.
            raise CookieError("cookie file could not be read") from exc
        if not cookie:
            raise CookieError("cookie file is empty")
        return cookie

    @staticmethod
    def _post_id(value: Any) -> str:
        post_id = str(value).strip()
        if not post_id or not re.fullmatch(r"[A-Za-z0-9_-]+", post_id):
            raise ValueError("post_id must be a simple Xueqiu identifier")
        return post_id

    def _timeline_path(self, page: int) -> Path:
        return self.cache_dir / f"timeline-page-{page:04d}.json"

    def _detail_path(self, post_id: str) -> Path:
        return self.cache_dir / f"post-{post_id}.json"

    def _detail_not_found_path(self, post_id: str) -> Path:
        # Include the endpoint identity so a corrected endpoint never trusts
        # negative results cached by an older, incorrect route.
        return self.cache_dir / f"post-{post_id}.statuses-show.not-found"

    def _read_cache(self, path: Path) -> Any:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError as exc:
            raise CacheMissError(f"missing cached response: {path.name}") from exc
        except (OSError, UnicodeError) as exc:
            raise CacheError(f"could not read cached response: {path.name}") from exc
        except json.JSONDecodeError as exc:
            raise MalformedResponseError(f"cached response is not valid JSON: {path.name}") from exc

    def _write_cache(self, path: Path, payload: Any) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CacheError(f"could not write raw response: {path.name}") from exc

    def _raise_if_not_found_marker(self, path: Path, *, refresh: bool) -> None:
        if refresh:
            return
        try:
            path.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise CacheError(f"could not read not-found marker: {path.name}") from exc
        raise NotFoundError("cached Xueqiu post was not found")

    def _write_not_found_marker(self, path: Path) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with temporary.open("wb") as handle:
                handle.write(b"not-found\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CacheError(f"could not write not-found marker: {path.name}") from exc

    @staticmethod
    def _remove_not_found_marker(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise CacheError(f"could not remove not-found marker: {path.name}") from exc

    def _wait_for_request(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.request_interval - (self._clock() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    def _wait_for_retry(self, backoff: float) -> None:
        elapsed = 0.0 if self._last_request_at is None else self._clock() - self._last_request_at
        self._sleep(max(backoff, self.request_interval - elapsed, 0.0))

    @staticmethod
    def _status_code(response: Any) -> int:
        try:
            return int(response.status_code)
        except (AttributeError, TypeError, ValueError) as exc:
            raise MalformedResponseError("response did not include an HTTP status") from exc

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        return status == 403 or status == 429 or 500 <= status <= 599

    def _request_json(
        self,
        url: str,
        params: Mapping[str, Any],
        *,
        permanent_not_found_statuses: frozenset[int] = frozenset(),
    ) -> Any:
        for attempt in range(REQUEST_ATTEMPTS):
            if attempt == 0:
                self._wait_for_request()
            else:
                self._wait_for_retry(RETRY_BACKOFFS[attempt - 1])
            self._last_request_at = self._clock()
            try:
                response = self.session.get(url, params=dict(params), timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt < REQUEST_ATTEMPTS - 1:
                    continue
                raise RequestError("request failed after 3 attempts") from exc

            status = self._status_code(response)
            if self._is_retryable_status(status):
                if attempt < REQUEST_ATTEMPTS - 1:
                    continue
                raise RequestError(f"request failed after 3 attempts (HTTP {status})")
            if status in permanent_not_found_statuses:
                raise NotFoundError(
                    f"requested Xueqiu post detail is permanently unavailable (HTTP {status})"
                )
            if not 200 <= status < 300:
                raise RequestError(f"request failed with HTTP {status}")

            try:
                payload = response.json()
            except (ValueError, TypeError) as exc:
                raise MalformedResponseError("provider response was not valid JSON") from exc
            try:
                json.dumps(payload)
            except (TypeError, ValueError) as exc:
                raise MalformedResponseError("provider response was not JSON-serializable") from exc
            return payload
        raise AssertionError("unreachable retry state")

    def _read_or_fetch(
        self,
        path: Path,
        url: str,
        params: Mapping[str, Any],
        *,
        offline: bool,
        refresh: bool,
        not_found_path: Path | None = None,
        permanent_not_found_statuses: frozenset[int] = frozenset(),
    ) -> Any:
        if not refresh:
            try:
                return self._read_cache(path)
            except CacheMissError:
                pass
            if not_found_path is not None:
                self._raise_if_not_found_marker(not_found_path, refresh=False)
        if offline:
            return self._read_cache(path)
        try:
            payload = self._request_json(
                url,
                params,
                permanent_not_found_statuses=permanent_not_found_statuses,
            )
        except NotFoundError:
            if not_found_path is not None:
                self._write_not_found_marker(not_found_path)
            raise
        # The cache write is deliberately before returning to the parser.
        self._write_cache(path, payload)
        if not_found_path is not None:
            self._remove_not_found_marker(not_found_path)
        return payload

    @staticmethod
    def _timeline_statuses(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, Mapping) or "statuses" not in payload:
            raise MalformedResponseError("timeline response did not contain statuses")
        raw_statuses = payload["statuses"]
        if raw_statuses is None:
            return []
        if not isinstance(raw_statuses, (list, tuple)):
            raise MalformedResponseError("timeline statuses were not a list")
        statuses: list[dict[str, Any]] = []
        for status in raw_statuses:
            if not isinstance(status, Mapping):
                raise MalformedResponseError("timeline contained a non-object status")
            statuses.append(dict(status))
        return statuses

    @staticmethod
    def _status_id(status: Mapping[str, Any]) -> str | None:
        for key in ("id", "status_id", "statusId"):
            value = status.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    def fetch_timeline(
        self,
        *,
        start_page: int = 1,
        max_pages: int | None = None,
        count: int | None = None,
        offline: bool | None = None,
        refresh: bool | None = None,
    ) -> TimelineResult:
        """Fetch or replay sequential timeline pages until a terminal condition."""

        if int(start_page) != start_page or int(start_page) < 1:
            raise ValueError("start_page must be a positive integer")
        if max_pages is not None and (int(max_pages) != max_pages or int(max_pages) < 1):
            raise ValueError("max_pages must be a positive integer")
        page_count = self.count if count is None else int(count)
        if page_count < 1:
            raise ValueError("count must be a positive integer")
        use_offline = self.offline if offline is None else bool(offline)
        use_refresh = self.refresh if refresh is None else bool(refresh)
        page = int(start_page)
        pages = 0
        raw_post_count = 0
        statuses: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        while max_pages is None or pages < max_pages:
            payload = self._read_or_fetch(
                self._timeline_path(page),
                TIMELINE_URL,
                {
                    "user_id": self.user_id,
                    "page": page,
                    "count": page_count,
                    "type": DEFAULT_TYPE,
                },
                offline=use_offline,
                refresh=use_refresh,
            )
            pages += 1
            page_statuses = self._timeline_statuses(payload)
            raw_post_count += len(page_statuses)
            if not page_statuses:
                return TimelineResult(pages, statuses, "empty_statuses", raw_post_count)

            page_ids = [self._status_id(status) for status in page_statuses]
            known_ids = [post_id for post_id in page_ids if post_id is not None]
            if known_ids and all(post_id in seen_ids for post_id in known_ids):
                return TimelineResult(pages, statuses, "repeated_post_ids", raw_post_count)

            for status, post_id in zip(page_statuses, page_ids):
                if post_id is None or post_id not in seen_ids:
                    statuses.append(status)
                if post_id is not None:
                    seen_ids.add(post_id)

            if max_pages is not None and pages >= max_pages:
                return TimelineResult(pages, statuses, "max_pages", raw_post_count)
            page += 1

        return TimelineResult(pages, statuses, "max_pages", raw_post_count)

    def fetch_detail(
        self,
        post_id: Any,
        *,
        offline: bool | None = None,
        refresh: bool | None = None,
    ) -> Any:
        """Fetch one post detail on demand; timeline pagination never calls this."""

        normalized_id = self._post_id(post_id)
        use_offline = self.offline if offline is None else bool(offline)
        use_refresh = self.refresh if refresh is None else bool(refresh)
        return self._read_or_fetch(
            self._detail_path(normalized_id),
            DETAIL_URL,
            {"id": normalized_id},
            offline=use_offline,
            refresh=use_refresh,
            not_found_path=self._detail_not_found_path(normalized_id),
            permanent_not_found_statuses=frozenset({400, 404, 405, 410}),
        )

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


__all__ = [
    "DEFAULT_COUNT",
    "DEFAULT_REQUEST_INTERVAL",
    "DEFAULT_TYPE",
    "DEFAULT_USER_ID",
    "DETAIL_URL",
    "RETRY_BACKOFFS",
    "TIMELINE_URL",
    "CacheError",
    "CacheMissError",
    "CookieError",
    "MalformedResponseError",
    "NotFoundError",
    "RequestError",
    "TimelineResult",
    "XueqiuClient",
    "XueqiuClientError",
]
