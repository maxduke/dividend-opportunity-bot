import json

import pytest
import requests

from research.csi_dp2.xueqiu_client import (
    CacheMissError,
    MalformedResponseError,
    NotFoundError,
    RequestError,
    XueqiuClient,
)


class Response:
    def __init__(self, status_code=200, payload=None, error=None):
        self.status_code = status_code
        self.payload = payload
        self.error = error

    def json(self):
        if self.error:
            raise self.error
        return self.payload


class Session:
    def __init__(self, responses=()):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def client(tmp_path, session, *, offline=False, refresh=False, sleeps=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cookie = tmp_path / "xueqiu.cookie"
    cookie.write_text("xq_a_token=secret-cookie", encoding="utf-8")
    sleeps = [] if sleeps is None else sleeps
    return XueqiuClient(
        cookie_file=cookie,
        cache_dir=tmp_path / "cache",
        session=session,
        request_interval=1,
        offline=offline,
        refresh=refresh,
        sleep=sleeps.append,
        clock=lambda: 0,
    ), sleeps


def test_timeline_endpoint_params_headers_and_raw_cache(tmp_path):
    session = Session(
        [
            Response(200, {"statuses": [{"id": 101, "text": "one"}]}),
            Response(200, {"statuses": []}),
        ]
    )
    xueqiu, _ = client(tmp_path, session)

    result = xueqiu.fetch_timeline()

    assert result.pages == 2
    assert result.raw_post_count == 1
    assert result.stop_reason == "empty_statuses"
    assert result.raw_statuses == [{"id": 101, "text": "one"}]
    assert [call[0] for call in session.calls] == [
        "https://api.xueqiu.com/v4/statuses/user_timeline.json",
        "https://api.xueqiu.com/v4/statuses/user_timeline.json",
    ]
    assert session.calls[0][1]["params"] == {
        "user_id": "8374048440",
        "page": 1,
        "count": 20,
        "type": 10,
    }
    assert session.headers["Cookie"] == "xq_a_token=secret-cookie"
    assert session.headers["Referer"] == "https://xueqiu.com/u/8374048440"
    assert session.headers["Accept"] == "application/json"
    cached = json.loads((tmp_path / "cache/timeline-page-0001.json").read_text())
    assert cached == {"statuses": [{"id": 101, "text": "one"}]}
    assert "secret-cookie" not in (tmp_path / "cache/timeline-page-0001.json").read_text()


def test_cache_hit_avoids_network_and_refresh_ignores_cache(tmp_path):
    first, _ = client(tmp_path, Session([Response(200, {"statuses": []})]))
    first.fetch_timeline()

    cached_session = Session()
    cached, _ = client(tmp_path, cached_session)
    assert cached.fetch_timeline().stop_reason == "empty_statuses"
    assert cached_session.calls == []

    refreshed_session = Session([Response(200, {"statuses": [{"id": 9}]})])
    refreshed, _ = client(tmp_path, refreshed_session, refresh=True)
    assert refreshed.fetch_timeline(max_pages=1).statuses == [{"id": 9}]
    assert len(refreshed_session.calls) == 1


def test_offline_replay_makes_zero_network_calls_and_reports_miss(tmp_path):
    online, _ = client(
        tmp_path,
        Session([Response(200, {"statuses": [{"id": 1}]}), Response(200, {"statuses": []})]),
    )
    online.fetch_timeline()

    offline_session = Session()
    offline, _ = client(tmp_path, offline_session, offline=True)
    assert offline.fetch_timeline().pages == 2
    assert offline_session.calls == []

    missing_session = Session()
    missing, _ = client(tmp_path / "different", missing_session, offline=True)
    with pytest.raises(CacheMissError, match="missing cached response"):
        missing.fetch_timeline()
    assert missing_session.calls == []


@pytest.mark.parametrize("failure", [429, 403, 500, 502])
def test_transient_status_retries_with_bounded_backoff(tmp_path, failure):
    sleeps = []
    session = Session([Response(failure), Response(200, {"statuses": []})])
    xueqiu, sleeps = client(tmp_path, session, sleeps=sleeps)

    result = xueqiu.fetch_timeline(max_pages=1)

    assert result.stop_reason == "empty_statuses"
    assert len(session.calls) == 2
    assert sleeps == [2.0]


def test_timeout_and_connection_failure_retry_then_fail(tmp_path):
    sleeps = []
    session = Session([requests.Timeout(), requests.ConnectionError(), requests.Timeout()])
    xueqiu, sleeps = client(tmp_path, session, sleeps=sleeps)

    with pytest.raises(RequestError, match="3 attempts"):
        xueqiu.fetch_detail("42")

    assert len(session.calls) == 3
    assert sleeps == [2.0, 5.0]


def test_not_found_detail_is_permanent_and_not_retried(tmp_path):
    session = Session([Response(404)])
    xueqiu, sleeps = client(tmp_path, session)

    with pytest.raises(NotFoundError):
        xueqiu.fetch_detail("deleted-post")

    assert len(session.calls) == 1
    assert sleeps == []


def test_not_found_detail_is_negative_cached_across_clients_and_offline(tmp_path):
    session = Session([Response(404)])
    xueqiu, _ = client(tmp_path, session)

    with pytest.raises(NotFoundError):
        xueqiu.fetch_detail("deleted-post")

    marker = tmp_path / "cache/post-deleted-post.not-found"
    assert marker.read_bytes() == b"not-found\n"
    assert not (tmp_path / "cache/post-deleted-post.json").exists()

    cached_session = Session([Response(200, {"id": "deleted-post"})])
    cached, _ = client(tmp_path, cached_session)
    with pytest.raises(NotFoundError):
        cached.fetch_detail("deleted-post")
    assert cached_session.calls == []

    offline_session = Session([Response(200, {"id": "deleted-post"})])
    offline, _ = client(tmp_path, offline_session, offline=True)
    with pytest.raises(NotFoundError):
        offline.fetch_detail("deleted-post")
    assert offline_session.calls == []


def test_refresh_retries_negative_cached_detail_and_removes_marker(tmp_path):
    first, _ = client(tmp_path, Session([Response(404)]))
    with pytest.raises(NotFoundError):
        first.fetch_detail("deleted-post")

    session = Session([Response(200, {"id": "deleted-post", "text": "restored"})])
    refreshed, _ = client(tmp_path, session, refresh=True)
    assert refreshed.fetch_detail("deleted-post") == {
        "id": "deleted-post",
        "text": "restored",
    }
    assert not (tmp_path / "cache/post-deleted-post.not-found").exists()
    assert (tmp_path / "cache/post-deleted-post.json").exists()


def test_successful_detail_cache_wins_over_stale_not_found_marker(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "post-restored.json").write_text(
        '{"id": "restored", "text": "available"}', encoding="utf-8"
    )
    (cache_dir / "post-restored.not-found").write_text("not-found\n", encoding="utf-8")
    session = Session()
    cached, _ = client(tmp_path, session)

    assert cached.fetch_detail("restored")["text"] == "available"
    assert session.calls == []


def test_malformed_json_is_clear_and_not_cached(tmp_path):
    session = Session([Response(200, error=ValueError("not json"))])
    xueqiu, _ = client(tmp_path, session)

    with pytest.raises(MalformedResponseError, match="valid JSON"):
        xueqiu.fetch_timeline(max_pages=1)

    assert not (tmp_path / "cache/timeline-page-0001.json").exists()


def test_empty_and_repeated_id_pagination_stop_without_duplicate_statuses(tmp_path):
    empty_session = Session([Response(200, {"statuses": []})])
    empty, _ = client(tmp_path, empty_session)
    assert empty.fetch_timeline().stop_reason == "empty_statuses"

    repeated_session = Session(
        [
            Response(200, {"statuses": [{"id": "a"}]}),
            Response(200, {"statuses": [{"id": "a"}]}),
        ]
    )
    repeated, _ = client(tmp_path / "repeated", repeated_session)
    result = repeated.fetch_timeline()
    assert result.stop_reason == "repeated_post_ids"
    assert result.pages == 2
    assert result.raw_post_count == 2
    assert result.statuses == [{"id": "a"}]


def test_detail_is_on_demand_and_cached(tmp_path):
    session = Session([Response(200, {"id": "42", "text": "detail"})])
    xueqiu, _ = client(tmp_path, session)

    assert xueqiu.fetch_detail(42) == {"id": "42", "text": "detail"}
    assert session.calls[0][0] == "https://api.xueqiu.com/v4/statuses/show.json"
    assert session.calls[0][1]["params"] == {"id": "42"}
    assert (tmp_path / "cache/post-42.json").exists()

    cached_session = Session()
    cached, _ = client(tmp_path, cached_session)
    assert cached.fetch_detail("42")["text"] == "detail"
    assert cached_session.calls == []


def test_online_client_requires_cookie_but_offline_does_not(tmp_path):
    with pytest.raises(Exception, match="cookie_file"):
        XueqiuClient(cache_dir=tmp_path / "cache", session=Session())

    (tmp_path / "cache/timeline-page-0001.json").parent.mkdir()
    (tmp_path / "cache/timeline-page-0001.json").write_text('{"statuses": []}')
    offline = XueqiuClient(cache_dir=tmp_path / "cache", offline=True, session=Session())
    assert offline.fetch_timeline().stop_reason == "empty_statuses"


def test_request_interval_must_be_at_least_one_second(tmp_path):
    with pytest.raises(ValueError, match="1 second"):
        XueqiuClient(
            cookie_file=tmp_path / "cookie",
            cache_dir=tmp_path / "cache",
            request_interval=0,
            session=Session(),
        )


def test_cookie_is_never_exposed_by_failure_or_logs(tmp_path, caplog):
    session = Session([Response(403), Response(403), Response(403)])
    xueqiu, _ = client(tmp_path, session)

    with pytest.raises(RequestError) as raised:
        xueqiu.fetch_timeline(max_pages=1)

    assert "secret-cookie" not in str(raised.value)
    assert "secret-cookie" not in caplog.text
