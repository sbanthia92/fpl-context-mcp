"""
Tests for jobs/ingest_press_content.py.

All HTTP and Pinecone calls are mocked. Tests cover fetcher logic, document
building, deduplication, and the threaded orchestration path.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
import requests

from jobs.ingest_press_content import (
    BBCSportFetcher,
    GuardianAPIFetcher,
    _cleanup_stale_player_news,
    _days_ago,
    _doc_id,
    _existing_ids,
    _fetch_player_news_docs,
    _recency_score,
    _upsert,
    main,
    run,
)

# ---------------------------------------------------------------------------
# Pure helper tests (no mocking needed)
# ---------------------------------------------------------------------------


def test_doc_id_is_deterministic():
    """Same input always produces the same MD5 digest."""
    assert _doc_id("press_BBC_http://example.com") == _doc_id("press_BBC_http://example.com")


def test_doc_id_differs_for_different_inputs():
    """Different inputs produce different IDs."""
    assert _doc_id("press_BBC_article1") != _doc_id("press_BBC_article2")


def test_days_ago_recent():
    """An RFC 2822 date from today returns < 1 day."""
    from datetime import UTC, datetime

    now_str = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    assert _days_ago(now_str) < 1


def test_days_ago_empty_returns_999():
    """Missing date string returns 999 so the article is treated as old."""
    assert _days_ago("") == 999


def test_days_ago_invalid_returns_999():
    """Unparseable date returns 999."""
    assert _days_ago("not a date") == 999


def test_recency_score_today_is_one():
    """An article published today scores 1.0."""
    from datetime import UTC, datetime

    now_str = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    assert _recency_score(now_str) == pytest.approx(1.0, abs=0.05)


def test_recency_score_old_article_is_minimum():
    """An article older than 14 days returns the minimum score (0.1)."""
    assert _recency_score("Mon, 01 Jan 2024 00:00:00 +0000") == 0.1


# ---------------------------------------------------------------------------
# BBCSportFetcher
# ---------------------------------------------------------------------------


def _make_rss_response(items: list[dict]) -> str:
    """Build a minimal RSS XML string from a list of item dicts."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items_xml = ""
    for item in items:
        items_xml += f"""
        <item>
            <title>{item.get("title", "Title")}</title>
            <description>{item.get("description", "Body text")}</description>
            <link>{item.get("link", "http://example.com/1")}</link>
            <pubDate>{item.get("pubDate", now)}</pubDate>
        </item>"""
    return f"""<?xml version="1.0"?>
    <rss><channel>{items_xml}</channel></rss>"""


@pytest.fixture
def mock_bbc_response():
    """Fixture that patches requests.get for the BBC fetcher."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    rss = _make_rss_response(
        [
            {
                "title": "Arsenal win title",
                "description": "Arsenal clinch it",
                "link": "http://bbc.com/1",
                "pubDate": now,
            },  # noqa: E501
            {
                "title": "Salah hat-trick",
                "description": "Liverpool star scores three",
                "link": "http://bbc.com/2",
                "pubDate": now,
            },  # noqa: E501
        ]
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = rss
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def test_bbc_fetcher_returns_docs(mock_bbc_response):
    """BBCSportFetcher parses RSS and returns doc tuples."""
    with patch("jobs.ingest_press_content.requests.get", return_value=mock_bbc_response):
        fetcher = BBCSportFetcher()
        docs = fetcher.fetch()

    assert len(docs) == 2
    doc_id, text, meta = docs[0]
    assert isinstance(doc_id, str) and len(doc_id) == 32  # MD5 hex
    assert "BBC Sport" in text
    assert meta["type"] == "press_article"
    assert meta["source"] == "BBC Sport"
    assert "recency_score" in meta
    assert "pub_timestamp" in meta


def test_bbc_fetcher_skips_old_articles():
    """BBCSportFetcher skips articles older than 7 days."""
    old_date = "Mon, 01 Jan 2024 00:00:00 +0000"
    rss = _make_rss_response(
        [
            {
                "title": "Old article",
                "description": "This is old",
                "link": "http://bbc.com/old",
                "pubDate": old_date,
            },  # noqa: E501
        ]
    )
    mock_resp = MagicMock()
    mock_resp.text = rss
    mock_resp.raise_for_status = MagicMock()

    with patch("jobs.ingest_press_content.requests.get", return_value=mock_resp):
        docs = BBCSportFetcher().fetch()

    assert docs == []


def test_bbc_fetcher_returns_empty_on_http_error():
    """BBCSportFetcher returns empty list when HTTP request fails."""
    with patch(
        "jobs.ingest_press_content.requests.get",
        side_effect=requests.RequestException("timeout"),
    ):
        docs = BBCSportFetcher().fetch()

    assert docs == []


def test_bbc_fetcher_skips_items_without_description(mock_bbc_response):
    """BBCSportFetcher skips RSS items that have no description text."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S +0000")
    rss = _make_rss_response(
        [
            {
                "title": "No body article",
                "description": "",
                "link": "http://bbc.com/3",
                "pubDate": now,
            },
        ]
    )
    mock_resp = MagicMock()
    mock_resp.text = rss
    mock_resp.raise_for_status = MagicMock()

    with patch("jobs.ingest_press_content.requests.get", return_value=mock_resp):
        docs = BBCSportFetcher().fetch()

    assert docs == []


# ---------------------------------------------------------------------------
# GuardianAPIFetcher
# ---------------------------------------------------------------------------


def _make_guardian_response(articles: list[dict]) -> dict:
    """Build a mock Guardian API JSON response."""
    results = []
    for a in articles:
        results.append(
            {
                "webTitle": a.get("title", "Default Title"),
                # Default to "now": the fetcher drops articles older than 14 days.
                "webPublicationDate": a.get("date", datetime.now(UTC).isoformat()),
                "webUrl": a.get("url", "https://theguardian.com/1"),
                "fields": {
                    "headline": a.get("title", "Default Title"),
                    "trailText": a.get("body", "Article summary text"),
                    "bodyText": a.get("body", ""),
                },
            }
        )
    return {"response": {"status": "ok", "results": results}}


def test_guardian_fetcher_returns_docs():
    """GuardianAPIFetcher returns doc tuples from the API response."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = _make_guardian_response(
        [
            {
                "title": "PL title race",
                "body": "City lead by two points",
                "url": "https://guardian.com/1",
            },
        ]
    )
    mock_resp.raise_for_status = MagicMock()

    with patch("jobs.ingest_press_content.requests.get", return_value=mock_resp):
        docs = GuardianAPIFetcher().fetch()

    assert len(docs) == 1
    _, text, meta = docs[0]
    assert "The Guardian" in text
    assert meta["source"] == "The Guardian"


def test_guardian_fetcher_uses_trail_text_when_body_missing():
    """Falls back to trailText when bodyText is not populated (test API key)."""
    mock_resp = MagicMock()
    payload = _make_guardian_response(
        [
            {"title": "Test article", "url": "https://guardian.com/2"},
        ]
    )
    # Simulate test key — bodyText is empty
    payload["response"]["results"][0]["fields"]["bodyText"] = ""
    payload["response"]["results"][0]["fields"]["trailText"] = "Trail text summary"
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status = MagicMock()

    with patch("jobs.ingest_press_content.requests.get", return_value=mock_resp):
        docs = GuardianAPIFetcher().fetch()

    assert len(docs) == 1
    _, text, _ = docs[0]
    assert "Trail text summary" in text


def test_guardian_fetcher_returns_empty_on_http_error():
    """GuardianAPIFetcher returns empty list when HTTP request fails."""
    with patch(
        "jobs.ingest_press_content.requests.get",
        side_effect=requests.RequestException("network error"),
    ):
        docs = GuardianAPIFetcher().fetch()

    assert docs == []


def test_guardian_fetcher_returns_empty_on_invalid_json():
    """GuardianAPIFetcher returns empty list when API returns invalid JSON."""
    mock_resp = MagicMock()
    mock_resp.json.side_effect = ValueError("not json")
    mock_resp.raise_for_status = MagicMock()

    with patch("jobs.ingest_press_content.requests.get", return_value=mock_resp):
        docs = GuardianAPIFetcher().fetch()

    assert docs == []


# ---------------------------------------------------------------------------
# Pinecone upsert helpers
# ---------------------------------------------------------------------------


def test_existing_ids_batches_in_groups_of_1000():
    """_existing_ids fetches in batches of 1000 and returns the union."""
    index = MagicMock()
    # Two batches: first returns 2 IDs, second returns 1
    results_1 = MagicMock()
    results_1.vectors = {"id1": MagicMock(), "id2": MagicMock()}
    results_2 = MagicMock()
    results_2.vectors = {"id3": MagicMock()}
    index.fetch.side_effect = [results_1, results_2]

    ids = [f"id{i}" for i in range(1, 1502)]  # 1501 IDs → 2 batches
    existing = _existing_ids(index, ids)

    assert index.fetch.call_count == 2
    assert existing == {"id1", "id2", "id3"}


def test_upsert_skips_existing_ids():
    """_upsert does not re-embed documents whose IDs are already in Pinecone."""
    pc = MagicMock()
    index = MagicMock()

    # Pretend id1 already exists
    existing_result = MagicMock()
    existing_result.vectors = {"id1": MagicMock()}
    index.fetch.return_value = existing_result

    embedding = MagicMock()
    embedding.values = [0.1] * 1024
    pc.inference.embed.return_value = [embedding]

    docs = [
        ("id1", "existing text", {"text": "existing text"}),
        ("id2", "new text", {"text": "new text"}),
    ]
    total = _upsert(pc, index, docs, always_upsert=False)

    # Only id2 should be embedded and upserted
    assert total == 1
    embedded_texts = pc.inference.embed.call_args[1]["inputs"]
    assert "new text" in embedded_texts
    assert "existing text" not in embedded_texts


def test_upsert_always_upsert_skips_id_check():
    """_upsert with always_upsert=True skips the Pinecone fetch check."""
    pc = MagicMock()
    index = MagicMock()

    embedding = MagicMock()
    embedding.values = [0.1] * 1024
    pc.inference.embed.return_value = [embedding]

    docs = [("id1", "some text", {"text": "some text"})]
    _upsert(pc, index, docs, always_upsert=True)

    index.fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Orchestration — run()
# ---------------------------------------------------------------------------


def test_run_collects_from_both_fetchers():
    """run() submits both fetchers to the thread pool and upserts combined results."""
    _meta = {"pub_timestamp": 0.0, "type": "press_article"}
    bbc_doc = ("bbc_id", "BBC text", {"text": "BBC text", **_meta})
    guardian_doc = ("g_id", "Guardian text", {"text": "Guardian text", **_meta})

    with (
        patch("jobs.ingest_press_content.Pinecone"),
        patch.object(BBCSportFetcher, "fetch", return_value=[bbc_doc]),
        patch.object(GuardianAPIFetcher, "fetch", return_value=[guardian_doc]),
        patch("jobs.ingest_press_content._fetch_player_news_docs", return_value=[]),
        patch("jobs.ingest_press_content._upsert", return_value=1) as mock_upsert,
        patch("jobs.ingest_press_content._cleanup_stale_press"),
    ):
        run()

    # _upsert should have been called once for the combined press docs
    assert mock_upsert.called
    # Both docs should appear in the combined press_docs list passed to upsert
    upserted_docs = mock_upsert.call_args_list[0][0][2]
    ids = [d[0] for d in upserted_docs]
    assert "bbc_id" in ids
    assert "g_id" in ids


def test_run_continues_if_one_fetcher_fails():
    """run() logs the error from a failing fetcher but still upserts the other's docs."""
    _meta = {"pub_timestamp": 0.0, "type": "press_article"}
    bbc_doc = ("bbc_id", "BBC text", {"text": "BBC text", **_meta})

    with (
        patch("jobs.ingest_press_content.Pinecone"),
        patch.object(BBCSportFetcher, "fetch", return_value=[bbc_doc]),
        patch.object(GuardianAPIFetcher, "fetch", side_effect=RuntimeError("Guardian API down")),
        patch("jobs.ingest_press_content._fetch_player_news_docs", return_value=[]),
        patch("jobs.ingest_press_content._upsert", return_value=1) as mock_upsert,
        patch("jobs.ingest_press_content._cleanup_stale_press"),
    ):
        # Should not raise even though Guardian fetcher fails
        run()

    # BBC doc should still be upserted
    upserted_docs = mock_upsert.call_args_list[0][0][2]
    assert upserted_docs[0][0] == "bbc_id"


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


def test_run_dry_run_skips_upsert():
    """run(dry_run=True) fetches content but never calls _upsert or _cleanup."""
    bbc_doc = (
        "bbc_id",
        "BBC text",
        {"text": "BBC text", "pub_timestamp": 0.0, "type": "press_article"},
    )  # noqa: E501

    with (
        patch("jobs.ingest_press_content.Pinecone"),
        patch.object(BBCSportFetcher, "fetch", return_value=[bbc_doc]),
        patch.object(GuardianAPIFetcher, "fetch", return_value=[]),
        patch("jobs.ingest_press_content._fetch_player_news_docs", return_value=[]),
        patch("jobs.ingest_press_content._upsert") as mock_upsert,
        patch("jobs.ingest_press_content._cleanup_stale_press") as mock_cleanup,
    ):
        run(dry_run=True)

    mock_upsert.assert_not_called()
    mock_cleanup.assert_not_called()


def test_run_dry_run_still_fetches():
    """run(dry_run=True) still calls each fetcher to verify connectivity."""
    with (
        patch("jobs.ingest_press_content.Pinecone"),
        patch.object(BBCSportFetcher, "fetch", return_value=[]) as mock_bbc,
        patch.object(GuardianAPIFetcher, "fetch", return_value=[]) as mock_guardian,
        patch("jobs.ingest_press_content._fetch_player_news_docs", return_value=[]) as mock_fpl,
        patch("jobs.ingest_press_content._upsert"),
        patch("jobs.ingest_press_content._cleanup_stale_press"),
    ):
        run(dry_run=True)

    mock_bbc.assert_called_once()
    mock_guardian.assert_called_once()
    mock_fpl.assert_called_once()


# ---------------------------------------------------------------------------
# Failure signalling (exit codes)
# ---------------------------------------------------------------------------


def test_run_returns_false_when_pinecone_key_missing(monkeypatch):
    """run() reports failure instead of silently succeeding without credentials."""
    monkeypatch.setenv("PINECONE_API_KEY", "")
    assert run() is False


def test_main_exits_nonzero_when_pinecone_key_missing(monkeypatch):
    """The CLI entry point exits 1 so schedulers/CI mark the run as failed."""
    monkeypatch.setenv("PINECONE_API_KEY", "")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Guardian key handling
# ---------------------------------------------------------------------------


def test_guardian_fetcher_skips_without_key(monkeypatch):
    """No GUARDIAN_API_KEY -> no HTTP call, empty result (the 'test' key is rejected)."""
    monkeypatch.setenv("GUARDIAN_API_KEY", "")
    with patch("jobs.ingest_press_content.requests.get") as mock_get:
        docs = GuardianAPIFetcher().fetch()

    assert docs == []
    mock_get.assert_not_called()


def test_guardian_fetcher_does_not_log_api_key(monkeypatch, caplog):
    """The API key must never appear in log output."""
    monkeypatch.setenv("GUARDIAN_API_KEY", "super-secret-key-123")
    mock_resp = MagicMock()
    mock_resp.json.return_value = _make_guardian_response([])
    mock_resp.raise_for_status = MagicMock()

    with (
        caplog.at_level("DEBUG"),
        patch("jobs.ingest_press_content.requests.get", return_value=mock_resp),
    ):
        GuardianAPIFetcher().fetch()

    assert "super-secret-key-123" not in caplog.text


# ---------------------------------------------------------------------------
# Player news: stable IDs and stale cleanup
# ---------------------------------------------------------------------------


def _fpl_bootstrap(news_by_player: dict[int, str]) -> dict:
    return {
        "teams": [{"id": 1, "name": "Arsenal"}],
        "elements": [
            {
                "id": pid,
                "first_name": "First",
                "second_name": f"Player{pid}",
                "team": 1,
                "element_type": 3,
                "news": news,
                "news_added": "2026-04-10T14:30:09Z",
                "chance_of_playing_next_round": 50,
            }
            for pid, news in news_by_player.items()
        ],
    }


def _bootstrap_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    return resp


def test_player_news_id_is_stable_when_news_changes():
    """A player's doc keeps the same ID when their news text changes, so it overwrites."""
    with patch(
        "jobs.ingest_press_content.requests.get",
        return_value=_bootstrap_response(_fpl_bootstrap({7: "Knee injury"})),
    ):
        first = _fetch_player_news_docs(100.0)
    with patch(
        "jobs.ingest_press_content.requests.get",
        return_value=_bootstrap_response(_fpl_bootstrap({7: "Hamstring injury"})),
    ):
        second = _fetch_player_news_docs(200.0)

    assert first[0][0] == second[0][0]
    assert "Knee" in first[0][1] and "Hamstring" in second[0][1]


def test_player_news_docs_carry_refreshed_at_and_skip_players_without_news():
    """Docs are stamped with the run time; players with empty news get no doc."""
    payload = _fpl_bootstrap({1: "Ankle knock", 2: ""})
    with patch("jobs.ingest_press_content.requests.get", return_value=_bootstrap_response(payload)):
        docs = _fetch_player_news_docs(123.0)

    assert len(docs) == 1
    assert docs[0][2]["refreshed_at"] == 123.0
    assert docs[0][2]["type"] == "player_news"


def _match(doc_id: str, refreshed_at: float | None) -> MagicMock:
    m = MagicMock()
    m.id = doc_id
    m.metadata = {} if refreshed_at is None else {"refreshed_at": refreshed_at}
    return m


def test_cleanup_stale_player_news_deletes_only_unrefreshed():
    """Docs older than the run (or lacking refreshed_at) are deleted; fresh ones kept."""
    index = MagicMock()
    index.query.return_value.matches = [
        _match("fresh", 500.0),
        _match("stale", 100.0),
        _match("legacy_no_field", None),
    ]

    deleted = _cleanup_stale_player_news(index, run_started=400.0)

    assert deleted == 2
    index.delete.assert_called_once()
    assert sorted(index.delete.call_args.kwargs["ids"]) == ["legacy_no_field", "stale"]
    assert index.query.call_args.kwargs["filter"] == {"type": {"$eq": "player_news"}}


def test_cleanup_stale_player_news_is_best_effort():
    """A Pinecone error is swallowed (logged), not raised."""
    index = MagicMock()
    index.query.side_effect = RuntimeError("pinecone down")

    assert _cleanup_stale_player_news(index, run_started=1.0) == 0
    index.delete.assert_not_called()


def test_run_prunes_player_news_after_upsert():
    """run() overwrites current player news, then prunes docs it did not refresh."""
    news_doc = ("p1", "news", {"text": "news", "type": "player_news", "refreshed_at": 1.0})

    with (
        patch("jobs.ingest_press_content.Pinecone"),
        patch.object(BBCSportFetcher, "fetch", return_value=[]),
        patch.object(GuardianAPIFetcher, "fetch", return_value=[]),
        patch("jobs.ingest_press_content._fetch_player_news_docs", return_value=[news_doc]),
        patch("jobs.ingest_press_content._upsert", return_value=1) as mock_upsert,
        patch("jobs.ingest_press_content._cleanup_stale_press"),
        patch("jobs.ingest_press_content._cleanup_stale_player_news") as mock_prune,
    ):
        assert run() is True

    assert mock_upsert.call_args.kwargs["always_upsert"] is True
    mock_prune.assert_called_once()


def test_run_skips_player_news_prune_when_fetch_empty():
    """An empty player-news fetch (API outage) must not wipe existing docs."""
    with (
        patch("jobs.ingest_press_content.Pinecone"),
        patch.object(BBCSportFetcher, "fetch", return_value=[]),
        patch.object(GuardianAPIFetcher, "fetch", return_value=[]),
        patch("jobs.ingest_press_content._fetch_player_news_docs", return_value=[]),
        patch("jobs.ingest_press_content._upsert", return_value=0),
        patch("jobs.ingest_press_content._cleanup_stale_press"),
        patch("jobs.ingest_press_content._cleanup_stale_player_news") as mock_prune,
    ):
        run()

    mock_prune.assert_not_called()
