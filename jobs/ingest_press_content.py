"""
Ingestion job: press content → Pinecone.

Fetches Premier League sports content from two sources concurrently using
ThreadPoolExecutor, then embeds and upserts the collected documents to the
existing Pinecone 'press' namespace.

Sources:
  Thread 1 — BBC Sport Premier League RSS feed
  Thread 2 — The Guardian open content API (content.guardianapis.com)

After the concurrent fetch phase, FPL player injury/availability news is fetched
sequentially (it is a single lightweight request and does not benefit from
parallelism).

Adding a new source requires only:
  1. Subclassing ContentFetcher and implementing fetch()
  2. Appending an instance to the FETCHERS list at the bottom of this file

No changes to the orchestration logic are needed.

Pinecone upsert pattern:
  - Embedding model: multilingual-e5-large (Pinecone built-in inference)
  - Batch size: 96 documents
  - input_type: "passage" (matches the ingest side; queries use "query")
  - Namespace: "press"
  - Stale articles (>14 days) are deleted on every run

Run from the fpl-context-mcp directory:
    python -m jobs.ingest_press_content

Cron (EC2): 0 7,19 * * * cd /path/to/fpl-context-mcp && python -m jobs.ingest_press_content
"""

import hashlib
import logging
import sys
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol, runtime_checkable

import requests
from pinecone import Pinecone

from config import cfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

# Pinecone settings — the query tool (tools/query_press_conferences.py) must use the same ones.
_NAMESPACE = "press"
_EMBED_MODEL = "multilingual-e5-large"
_UPSERT_BATCH = 96  # Pinecone recommended batch size for this model
_SCAN_BATCH = 100  # ids fetched per request when scanning for stale docs

_FPL_BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
_POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# Articles older than this are deleted from Pinecone after each run to stay
# within index quota.
_MAX_PRESS_AGE_DAYS = 14

# Documents newer than this are eligible for ingestion.
_MAX_ARTICLE_AGE_DAYS = 7

# Pinecone metadata limit is 40 960 bytes per vector. Cap stored text to keep
# total metadata well under that limit (other fields add ~200 bytes).
_MAX_METADATA_TEXT_CHARS = 35_000

# Guardian API endpoint — requires a free API key (GUARDIAN_API_KEY env var).
_GUARDIAN_API_BASE = "https://content.guardianapis.com"


# ---------------------------------------------------------------------------
# Document ID helpers — match the hashing strategy in pipeline/ingest_press.py
# ---------------------------------------------------------------------------


def _doc_id(key: str) -> str:
    """Return a stable MD5 hex digest for a document key string."""
    return hashlib.md5(key.encode()).hexdigest()


def _days_ago(pub_date_str: str) -> float:
    """
    Return how many days ago a publication date string represents.

    Accepts RFC 2822 (RSS pubDate) and ISO 8601 formats.

    Args:
        pub_date_str: Publication date string.

    Returns:
        Fractional days since publication, or 999 if parsing fails.
    """
    if not pub_date_str:
        return 999
    try:
        # Try RFC 2822 first (RSS pubDate format)
        dt = parsedate_to_datetime(pub_date_str)
    except Exception:
        try:
            # Fall back to ISO 8601 (Guardian API webPublicationDate)
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
        except Exception:
            return 999
    delta = datetime.now(UTC) - dt
    return delta.total_seconds() / 86400


def _recency_score(pub_date_str: str) -> float:
    """
    Compute a recency score for re-ranking in the RAG retrieval step.

    Returns 1.0 for today, decaying linearly to 0.1 at 14 days. This matches
    the formula used in server/rag.py so the existing retrieval weights are correct.

    Args:
        pub_date_str: Publication date string (RFC 2822 or ISO 8601).

    Returns:
        Float in [0.1, 1.0].
    """
    days = _days_ago(pub_date_str)
    return max(0.1, 1.0 - (days / 14) * 0.9)


def _pub_timestamp(pub_date_str: str) -> float:
    """
    Return the Unix timestamp of a publication date string.

    Used as a metadata field for Pinecone's age-based delete filter.

    Args:
        pub_date_str: Publication date string (RFC 2822 or ISO 8601).

    Returns:
        Unix timestamp float, or 0.0 on parse failure.
    """
    if not pub_date_str:
        return 0.0
    try:
        return parsedate_to_datetime(pub_date_str).timestamp()
    except Exception:
        try:
            dt = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0


# ---------------------------------------------------------------------------
# Fetcher protocol + base class
# ---------------------------------------------------------------------------


@runtime_checkable
class ContentFetcher(Protocol):
    """
    Protocol that every content source fetcher must satisfy.

    Implementing this protocol (rather than inheriting from a base class) means
    any class with a ``source_name`` attribute and a ``fetch`` method is a valid
    fetcher — no import of this file is required by third-party sources.
    """

    source_name: str

    def fetch(self) -> list[tuple[str, str, dict]]:
        """
        Fetch content from this source and return it as a list of document tuples.

        Returns:
            List of (doc_id, text, metadata) tuples ready for Pinecone upsert.
            Returns an empty list on failure (errors are logged internally).
        """
        ...


class _BaseFetcher(ABC):
    """
    Abstract base for concrete fetcher implementations.

    Provides shared helpers (_get, _build_doc) so subclasses only need to
    implement ``fetch()``.
    """

    #: Human-readable source label stored as metadata on each Pinecone document.
    source_name: str = ""

    def _get(self, url: str, **kwargs) -> requests.Response:
        """
        Perform a synchronous GET request with a 15-second timeout.

        Args:
            url:    URL to fetch.
            **kwargs: Additional keyword arguments forwarded to requests.get().

        Returns:
            requests.Response object.

        Raises:
            requests.RequestException: On network or HTTP errors.
        """
        kwargs.setdefault("timeout", 15)
        kwargs.setdefault("headers", {"User-Agent": "fpl-context-mcp/0.1"})
        return requests.get(url, **kwargs)

    def _build_press_doc(
        self,
        title: str,
        body: str,
        url: str,
        pub_date_str: str,
    ) -> tuple[str, str, dict] | None:
        """
        Build a (doc_id, text, metadata) tuple for a press article.

        Returns None if the article is older than _MAX_ARTICLE_AGE_DAYS or is
        missing essential fields.

        Args:
            title:       Article headline.
            body:        Article summary or body text.
            url:         Canonical URL of the article.
            pub_date_str: Publication date (RFC 2822 or ISO 8601).

        Returns:
            (doc_id, text, metadata) tuple, or None if the article should be skipped.
        """
        if not title or not body:
            return None
        if _days_ago(pub_date_str) > _MAX_ARTICLE_AGE_DAYS:
            return None

        text = f"Source: {self.source_name}\nHeadline: {title}\n{body}"
        meta = {
            "text": text,
            "type": "press_article",
            "source": self.source_name,
            "date": pub_date_str,
            "pub_timestamp": _pub_timestamp(pub_date_str),
            "url": url,
            "recency_score": _recency_score(pub_date_str),
        }
        # Use URL as the uniqueness key; fall back to title if no URL.
        doc_id = _doc_id(f"press_{self.source_name}_{url or title}")
        return doc_id, text, meta

    @abstractmethod
    def fetch(self) -> list[tuple[str, str, dict]]:
        """Subclasses must implement this to return document tuples."""
        ...


# ---------------------------------------------------------------------------
# Concrete fetchers
# ---------------------------------------------------------------------------


class BBCSportFetcher(_BaseFetcher):
    """
    Fetches Premier League content from the BBC Sport RSS feed.

    The BBC feed provides article titles and description snippets. No API key
    required — the feed is publicly available.
    """

    source_name = "BBC Sport"
    _RSS_URL = "https://feeds.bbci.co.uk/sport/football/premier-league/rss.xml"

    def fetch(self) -> list[tuple[str, str, dict]]:
        """
        Fetch and parse the BBC Sport Premier League RSS feed.

        Returns:
            List of (doc_id, text, metadata) tuples for articles published within
            the last _MAX_ARTICLE_AGE_DAYS days. Returns empty list on any error.
        """
        log.info("[%s] fetching RSS: %s", self.source_name, self._RSS_URL)
        try:
            resp = self._get(self._RSS_URL)
            resp.raise_for_status()
        except Exception as exc:
            log.error(
                "[%s] failed to fetch RSS feed: %s",
                self.source_name,
                exc,
                exc_info=True,
            )
            return []

        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            log.error("[%s] failed to parse RSS XML: %s", self.source_name, exc, exc_info=True)
            return []

        channel = root.find("channel")
        if channel is None:
            log.warning("[%s] RSS feed has no <channel> element.", self.source_name)
            return []

        docs: list[tuple[str, str, dict]] = []
        for item in channel.findall("item"):
            title = (item.findtext("title") or "").strip()
            description = (item.findtext("description") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_date = (item.findtext("pubDate") or "").strip()

            doc = self._build_press_doc(title, description, link, pub_date)
            if doc:
                docs.append(doc)

        log.info("[%s] fetched %d articles.", self.source_name, len(docs))
        return docs


class GuardianAPIFetcher(_BaseFetcher):
    """
    Fetches Premier League content from The Guardian open content API.

    Requires a registered API key in GUARDIAN_API_KEY (free at
    https://open-platform.theguardian.com/access/). The old public 'test' key is
    rejected by the API with HTTP 401, so without a key this fetcher is skipped.

    Endpoint: https://content.guardianapis.com/search
    """

    source_name = "The Guardian"
    _SEARCH_PATH = "/search"

    def fetch(self) -> list[tuple[str, str, dict]]:
        """
        Query the Guardian API for recent Premier League articles.

        Requests ``headline``, ``trailText`` and ``bodyText``, falling back to the
        trail text if a key tier does not return the full body.

        Returns:
            List of (doc_id, text, metadata) tuples. Returns empty list if no key is
            configured or the request fails.
        """
        api_key = cfg.guardian_api_key
        if not api_key:
            log.warning(
                "[%s] GUARDIAN_API_KEY is not set — skipping Guardian (BBC Sport only). "
                "Register a free key at https://open-platform.theguardian.com/access/",
                self.source_name,
            )
            return []
        log.info("[%s] querying Guardian API", self.source_name)

        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=_MAX_ARTICLE_AGE_DAYS)).strftime("%Y-%m-%d")

        params = {
            "q": "premier league",
            "section": "football",
            "from-date": cutoff,
            "show-fields": "headline,trailText,bodyText",
            "order-by": "newest",
            "page-size": 50,
            "api-key": api_key,
        }

        try:
            resp = self._get(f"{_GUARDIAN_API_BASE}{self._SEARCH_PATH}", params=params)
            resp.raise_for_status()
        except Exception as exc:
            log.error(
                "[%s] failed to call Guardian API: %s",
                self.source_name,
                exc,
                exc_info=True,
            )
            return []

        try:
            data = resp.json()
        except ValueError as exc:
            log.error(
                "[%s] invalid JSON from Guardian API: %s", self.source_name, exc, exc_info=True
            )
            return []

        results = data.get("response", {}).get("results", [])
        docs: list[tuple[str, str, dict]] = []

        for article in results:
            fields = article.get("fields", {})

            # Prefer full body text; fall back to trail text (summary snippet)
            body = (fields.get("bodyText") or fields.get("trailText") or "").strip()
            title = (fields.get("headline") or article.get("webTitle") or "").strip()
            url = article.get("webUrl", "")
            pub_date = article.get("webPublicationDate", "")  # ISO 8601

            doc = self._build_press_doc(title, body, url, pub_date)
            if doc:
                docs.append(doc)

        log.info("[%s] fetched %d articles.", self.source_name, len(docs))
        return docs


# ---------------------------------------------------------------------------
# Register fetchers here — adding a new source is one line.
# ---------------------------------------------------------------------------

FETCHERS: list[_BaseFetcher] = [
    BBCSportFetcher(),
    GuardianAPIFetcher(),
]


# ---------------------------------------------------------------------------
# FPL player news (sequential — single lightweight request)
# ---------------------------------------------------------------------------


def _fetch_player_news_docs(
    refreshed_at: float | None = None,
) -> list[tuple[str, str, dict]]:
    """
    Fetch player injury/availability news from the FPL bootstrap-static endpoint.

    Only players with a non-empty 'news' field are included. Each player has ONE
    document with a stable ID (derived from the FPL player id), so a changed
    injury overwrites the previous text in place. Every doc is stamped with
    ``refreshed_at`` so _cleanup_stale_docs can delete docs for players whose
    news FPL has since cleared.

    Args:
        refreshed_at: Unix timestamp of the current run. Defaults to now.

    Returns:
        List of (doc_id, text, metadata) tuples, one per player with news.
        Returns empty list on any HTTP or parse error.
    """
    refreshed_at = time.time() if refreshed_at is None else refreshed_at
    log.info("Fetching FPL player news from bootstrap-static...")
    try:
        resp = requests.get(_FPL_BOOTSTRAP_URL, timeout=30)
        resp.raise_for_status()
        bootstrap = resp.json()
    except Exception as exc:
        log.error("Failed to fetch FPL bootstrap: %s", exc, exc_info=True)
        return []

    teams_by_id = {t["id"]: t["name"] for t in bootstrap.get("teams", [])}
    docs: list[tuple[str, str, dict]] = []

    for p in bootstrap.get("elements", []):
        news = (p.get("news") or "").strip()
        if not news:
            continue

        name = f"{p['first_name']} {p['second_name']}"
        team = teams_by_id.get(p["team"], "Unknown")
        position = _POSITION_MAP.get(p["element_type"], "UNK")
        news_date = (p.get("news_added") or "").strip()
        chance = p.get("chance_of_playing_next_round")
        chance_str = f"{chance}%" if chance is not None else "unknown"

        text = (
            f"Player availability update\n"
            f"Player: {name} | Team: {team} | Position: {position}\n"
            f"Chance of playing next round: {chance_str}\n"
            f"News: {news}"
        )
        if news_date:
            text += f"\nUpdated: {news_date}"

        meta = {
            "text": text,
            "type": "player_news",
            "player_name": name,
            "team": team,
            "position": position,
            "date": news_date,
            "chance_of_playing": chance,
            # Player news is always treated as fresh — FPL updates it live.
            "recency_score": 1.0,
            "refreshed_at": refreshed_at,
        }
        # One stable ID per player: new news overwrites the old doc instead of
        # leaving the outdated one behind.
        doc_id = _doc_id(f"player_news_{p['id']}")
        docs.append((doc_id, text, meta))

    log.info("FPL player news: %d players with news.", len(docs))
    return docs


# ---------------------------------------------------------------------------
# Pinecone helpers — identical pattern to pipeline/ingest_press.py
# ---------------------------------------------------------------------------


def _existing_ids(index, ids: list[str]) -> set[str]:
    """
    Fetch which of the given document IDs already exist in Pinecone.

    Batches the fetch in groups of 1000 to respect Pinecone's fetch limit.

    Args:
        index: Pinecone Index object.
        ids:   List of document IDs to check.

    Returns:
        Set of IDs that already exist in the index.
    """
    existing: set[str] = set()
    for start in range(0, len(ids), 1000):
        batch = ids[start : start + 1000]
        result = index.fetch(ids=batch, namespace=_NAMESPACE)
        existing.update(result.vectors.keys())
    return existing


def _doc_timestamp(meta: dict) -> float | None:
    """
    Best-effort publication time (Unix seconds) for a stored press article.

    Prefers ``pub_timestamp``; falls back to parsing ``date`` (RFC 2822 or ISO 8601) for
    older documents that were stored without it. Returns None if neither is usable.
    """
    ts = meta.get("pub_timestamp")
    if isinstance(ts, int | float) and ts > 0:
        return float(ts)
    date = meta.get("date")
    if not isinstance(date, str) or not date:
        return None
    for parse in (
        parsedate_to_datetime,
        lambda d: datetime.fromisoformat(d.replace("Z", "+00:00")),
    ):
        try:
            dt = parse(date)
        except (TypeError, ValueError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.timestamp()
    return None


def _find_stale_ids(
    index, cutoff: float, run_started: float, prune_player_news: bool
) -> tuple[list[str], list[str]]:
    """
    Scan every document in the namespace and return (stale_press_ids, stale_news_ids).

    Reads all documents' metadata (ids are listed page by page, then fetched), rather
    than relying on a metadata filter, so documents written by older versions — which may
    lack ``pub_timestamp`` or ``refreshed_at`` — are still recognised as stale.

    * press_article: stale if older than ``cutoff`` (or its age can't be determined).
    * player_news:   stale if not refreshed by this run (only when ``prune_player_news``).
    * any other type is left alone.

    Note: ``Index.list`` requires a serverless Pinecone index.
    """
    stale_press: list[str] = []
    stale_news: list[str] = []
    for page in index.list(namespace=_NAMESPACE):
        # Depending on the SDK version a page is a list of id strings or of ListItem objects.
        ids = [getattr(item, "id", item) for item in page]
        for start in range(0, len(ids), _SCAN_BATCH):
            batch = ids[start : start + _SCAN_BATCH]
            for doc_id, vec in index.fetch(ids=batch, namespace=_NAMESPACE).vectors.items():
                meta = getattr(vec, "metadata", None) or {}
                doc_type = meta.get("type")
                if doc_type == "press_article":
                    ts = _doc_timestamp(meta)
                    if ts is None or ts < cutoff:
                        stale_press.append(doc_id)
                elif doc_type == "player_news" and prune_player_news:
                    if meta.get("refreshed_at", 0) < run_started:
                        stale_news.append(doc_id)
    return stale_press, stale_news


def _cleanup_stale_docs(index, run_started: float, prune_player_news: bool) -> int:
    """
    Delete outdated documents from the namespace. Best-effort: failures are logged.

    Removes press articles older than _MAX_PRESS_AGE_DAYS and, when ``prune_player_news``
    is True, player-news docs that this run did not refresh (news FPL has cleared).

    Args:
        index:             Pinecone Index object.
        run_started:       Unix timestamp taken at the start of the current run.
        prune_player_news: False when the FPL news fetch returned nothing, so an API outage
                           can't wipe every injury doc.

    Returns:
        Number of documents deleted (0 on failure).
    """
    cutoff = time.time() - _MAX_PRESS_AGE_DAYS * 86400
    try:
        stale_press, stale_news = _find_stale_ids(index, cutoff, run_started, prune_player_news)
        stale = stale_press + stale_news
        for start in range(0, len(stale), 1000):
            index.delete(ids=stale[start : start + 1000], namespace=_NAMESPACE)
        log.info(
            "Deleted %d press articles older than %d days and %d outdated player news docs.",
            len(stale_press),
            _MAX_PRESS_AGE_DAYS,
            len(stale_news),
        )
        return len(stale)
    except Exception as exc:
        log.warning("Stale document cleanup skipped: %s", exc)
        return 0


def _upsert(
    pc: Pinecone,
    index,
    docs: list[tuple[str, str, dict]],
    always_upsert: bool = False,
) -> int:
    """
    Embed and upsert a batch of documents into Pinecone.

    Skips documents whose IDs are already present in the index unless
    ``always_upsert`` is True (used for player news where content-hash IDs already
    encode whether re-embedding is needed).

    Follows the exact same pattern as pipeline/ingest_press.py:
      - Embed model: multilingual-e5-large
      - input_type: "passage"
      - Batch size: 96
      - 8-second sleep between batches to respect inference rate limits

    Args:
        pc:           Pinecone client instance.
        index:        Pinecone Index object.
        docs:         List of (doc_id, text, metadata) tuples.
        always_upsert: If True, skip the existing-ID check and upsert everything.

    Returns:
        Number of documents actually upserted.
    """
    if not docs:
        return 0

    if always_upsert:
        new_docs = docs
        log.info("  %d to upsert (overwrite mode).", len(new_docs))
    else:
        all_ids = [doc_id for doc_id, _, _ in docs]
        existing = _existing_ids(index, all_ids)
        new_docs = [(doc_id, text, meta) for doc_id, text, meta in docs if doc_id not in existing]
        log.info(
            "  %d already in Pinecone, %d new to embed.",
            len(existing),
            len(new_docs),
        )

    if not new_docs:
        return 0

    total = 0
    for start in range(0, len(new_docs), _UPSERT_BATCH):
        batch = new_docs[start : start + _UPSERT_BATCH]
        texts = [text for _, text, _ in batch]

        embeddings = pc.inference.embed(
            model=_EMBED_MODEL,
            inputs=texts,
            parameters={"input_type": "passage"},
        )

        vectors = [
            {
                "id": doc_id,
                "values": emb.values,
                "metadata": {
                    **meta,
                    "text": meta.get("text", "")[:_MAX_METADATA_TEXT_CHARS],
                },
            }
            for (doc_id, _, meta), emb in zip(batch, embeddings)
        ]
        index.upsert(vectors=vectors, namespace=_NAMESPACE)
        total += len(vectors)
        log.info("  upserted %d / %d.", total, len(new_docs))

        # Avoid Pinecone inference rate-limit errors between batches.
        if start + _UPSERT_BATCH < len(new_docs):
            time.sleep(8)

    return total


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(dry_run: bool = False) -> bool:
    """
    Run the full press content ingestion pipeline.

    Execution order:
      1. Fetch BBC Sport and Guardian content concurrently (ThreadPoolExecutor).
         Each fetcher runs in its own thread. Failures in one thread do not abort
         the other — whatever is successfully retrieved is passed to the upsert step.
      2. Fetch FPL player news sequentially (single lightweight request).
      3. Embed and upsert all collected press articles to Pinecone.
      4. Embed and upsert all player news documents to Pinecone.
      5. Delete press articles older than _MAX_PRESS_AGE_DAYS.

    Args:
        dry_run: When True, fetches are performed normally but all Pinecone writes
                 and deletes are skipped. Logs what would have been written so you can
                 verify connectivity and document counts before committing to a live run.
                 Can also be enabled via the DRY_RUN=true environment variable.

    Returns:
        True on success, False if the job could not run (e.g. missing PINECONE_API_KEY).
    """
    if not cfg.pinecone_api_key:
        log.error("PINECONE_API_KEY is not set — aborting press content ingestion.")
        return False

    run_started = time.time()

    if dry_run:
        log.info("[dry run] press ingestion — fetches will run; Pinecone writes are skipped.")

    pc = Pinecone(api_key=cfg.pinecone_api_key)
    index = pc.Index(cfg.pinecone_index_name)

    # Step 1: Run all registered fetchers concurrently, one thread per fetcher.
    press_docs: list[tuple[str, str, dict]] = []
    with ThreadPoolExecutor(max_workers=len(FETCHERS)) as executor:
        # Map each Future back to its fetcher so we can log the source name.
        future_to_fetcher = {executor.submit(f.fetch): f for f in FETCHERS}

        for future in as_completed(future_to_fetcher):
            fetcher = future_to_fetcher[future]
            try:
                docs = future.result()
                press_docs.extend(docs)
            except Exception as exc:
                # Log with full traceback but continue — partial data is better than nothing.
                log.error(
                    "[%s] fetch thread raised an exception: %s",
                    fetcher.source_name,
                    exc,
                    exc_info=True,
                )

    log.info("Concurrent fetch complete: %d press articles collected.", len(press_docs))

    # Step 2: FPL player news (sequential — one request, no parallelism needed).
    player_news_docs = _fetch_player_news_docs(run_started)

    if dry_run:
        log.info(
            "[dry run] would upsert %d press articles and %d player news docs to namespace '%s'. "
            "No Pinecone writes made.",
            len(press_docs),
            len(player_news_docs),
            _NAMESPACE,
        )
        return True

    # Step 3: Upsert press articles (skip already-ingested documents).
    total = 0
    if press_docs:
        log.info("Upserting %d press articles (skip existing)...", len(press_docs))
        total += _upsert(pc, index, press_docs, always_upsert=False)

    # Step 4: Upsert player news, overwriting each player's single doc. Stable IDs
    # mean a changed injury replaces the old text; always_upsert also refreshes
    # ``refreshed_at`` so still-current news survives the cleanup below.
    if player_news_docs:
        log.info("Upserting %d player news docs (overwrite)...", len(player_news_docs))
        total += _upsert(pc, index, player_news_docs, always_upsert=True)

    # Step 5: Delete outdated docs. Player news is only pruned after a successful fetch +
    # upsert: an empty result means the FPL fetch failed (or nobody has news), so keep
    # what we have rather than wiping every injury doc during an API outage.
    if not player_news_docs:
        log.warning("No player news fetched — skipping player news cleanup.")
    log.info("Cleaning up outdated documents...")
    _cleanup_stale_docs(index, run_started, prune_player_news=bool(player_news_docs))

    log.info(
        "Press content ingestion complete. %d documents upserted to namespace '%s'.",
        total,
        _NAMESPACE,
    )
    return True


def main() -> None:
    """CLI entry point: exit non-zero on failure so schedulers/CI flag the run as failed."""
    if not run(dry_run=cfg.dry_run):
        sys.exit(1)


if __name__ == "__main__":
    main()
