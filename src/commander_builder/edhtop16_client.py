"""edhtop16.com client — cEDH tournament results, the corpus' FOURTH source.

FP-017. **Read the scope note before using any number out of this
module** (docs/future-plans.md, FP-017):

    This is an EXPLORATORY DATA SOURCE, NOT A PREDICTOR. It describes
    what *bracket-5 cEDH tournament humans* actually registered and
    actually won with. Nothing here is claimed to transfer to B2-B4
    casual play, and nothing here has passed a Forge A/B gate. Three
    prior heuristic-scoring attempts (FP-015 whole-ordering twice,
    FP-015 per-swap, FP-002 margin regression) all failed their
    pre-registered gates; one diagnosis is that every signal we had was
    EDHREC-derived *deckbuilding preference* with no human *win* data
    to anchor it. Tournament results are the only large-scale source of
    real humans actually winning games — which is why it is worth
    importing, and exactly why it must not be quietly promoted to a
    predictor without its own gate.

WHY GraphQL AND NOT HTML
========================
edhtop16.com serves a public, unauthenticated GraphQL API at
``/api/graphql``. Probed live 2026-08-05 with the project User-Agent;
no scraping is needed or performed. Schema facts that matter here (all
introspected, not guessed):

- ``commanders(first:, sortBy: CommandersSortBy!, timePeriod:
  TimePeriod!, minEntries:, minTournamentSize:, colorId:, after:)``
  → ``CommanderConnection {edges {node {...}} pageInfo}``.
  ``CommandersSortBy`` ∈ {CONVERSION, POPULARITY, TOP_CUTS, WINRATE};
  ``TimePeriod`` ∈ {ALL_TIME, ONE_MONTH, THREE_MONTHS, SIX_MONTHS,
  ONE_YEAR, POST_BAN}.
- ``Commander.stats(filters: CommanderStatsFilters!)`` →
  ``{count, conversionRate, winRate, topCuts, metaShare}``. The
  ``filters`` argument is REQUIRED (a bare ``stats`` is a query error).
- ``Commander.entries(first:, sortBy: EntrySortBy, filters:
  EntriesFilter!)`` → ``EntryConnection``. ``EntriesFilter`` REQUIRES
  both ``timePeriod`` and ``minEventSize``.
- ``Entry`` → ``{standing, wins, losses, draws, winRate, decklist,
  maindeck {name}, tournament {name TID size tournamentDate topCut}}``.
  One POST returns a commander's stats AND ~20 full 98-card maindecks
  in ~6.5s — no N+1 detail fetch, unlike Archidekt.

Failure shapes, and why the cache guard is not optional: the site sits
behind Cloudflare and answers an unknown commander with **HTTP 200 +
``{"errors": [...], "data": {"commander": null}}``**. A 200 is
therefore NOT proof of data. Same hard-won house convention as
``edhrec_client``: an empty parse is LOUD and is **never** written to
the disk cache — caching a challenge page or a transient shape change
would zero this source for a whole TTL with no error and no retry.

Same degrade-don't-die contract as the other three clients: every
public function returns an empty result on network/shape failure rather
than raising, and ``fetch_json`` is injectable so tests stay offline.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

ENDPOINT = "https://edhtop16.com/api/graphql"
USER_AGENT = (
    "commander-builder/0.1 (+https://github.com/LlamaAdam/commander-builder)"
)

#: The ONLY bracket this data describes. cEDH tournament results are
#: bracket-5 humans; see ``bubble_analysis`` for the corpus-side gate
#: that keeps them out of B2-B4 analysis.
CEDH_BRACKET = 5

#: Tournament results only move when tournaments happen (weekends), so a
#: day is plenty — and matches the EDHREC client's TTL so the corpus'
#: sources age together.
CACHE_TTL_HOURS = 24

#: Default entries to pull per commander. One POST carries all of them,
#: so this is a payload-size knob rather than a request budget; 20 top
#: finishes is enough for a stable presence rate without dragging a
#: multi-megabyte response through a corpus build.
DEFAULT_N = 20

#: Default commanders to pull for a leaderboard query.
DEFAULT_COMMANDERS_N = 25

#: Default rolling window. Long enough to accumulate events, short
#: enough that a banning or a set release isn't averaged away.
DEFAULT_TIME_PERIOD = "SIX_MONTHS"

#: Ignore tiny events: a 12-player local is not evidence about the
#: format. Mirrors the site's own default emphasis on real events.
DEFAULT_MIN_EVENT_SIZE = 60

#: Ignore commanders with too few registrations for a conversion rate to
#: mean anything (1 entry that top-cut is not a 100% conversion rate).
DEFAULT_MIN_ENTRIES = 20

#: Below this many entries, per-card presence is statistically
#: meaningless and reads as "unknown" (an empty stats map), never as
#: "nobody plays it". Same refusal as ``bubble_analysis``'
#: MIN_REFERENCE_DECKS / ``lift_analysis``' MIN_CORPUS_DECKS.
MIN_ENTRIES_FOR_PRESENCE = 8

#: Retry budget. A corpus build issues ONE POST per commander here (vs
#: Archidekt's ~26), so we can afford EDHREC's fuller budget of 3.
MAX_RETRIES = 3
RETRY_BASE_DELAY_SEC = 1.0

# Same transient set as edhrec_client / archidekt_client: 429 is
# rate-limiting (honor Retry-After), 5xx is server-side weather. Other
# 4xx (400, 403, 404) are deterministic — retrying cannot help.
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})

TIME_PERIODS = ("ALL_TIME", "ONE_MONTH", "THREE_MONTHS", "SIX_MONTHS",
                "ONE_YEAR", "POST_BAN")
COMMANDERS_SORT_BY = ("CONVERSION", "POPULARITY", "TOP_CUTS", "WINRATE")


class EdhTop16Error(RuntimeError):
    """A 200-OK response that carried GraphQL ``errors`` / no data.

    Distinct from ``HTTPError`` on purpose: this is the "the server
    answered, but not with data" case that the no-cache-on-empty guard
    exists for.
    """


# ---------------------------------------------------------------------------
# Cache (same tree as every other client — .cache/<source>/)
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    from .edhrec_client import CACHE_DIR
    return CACHE_DIR.parent / "edhtop16"


def _cache_path(key: str) -> Path:
    safe = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")[:120] or "query"
    return _cache_dir() / f"{safe}.json"


def _is_cache_fresh(path: Path, ttl_hours: int = CACHE_TTL_HOURS) -> bool:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return False
    return (datetime.now(timezone.utc) - mtime) < timedelta(hours=ttl_hours)


def _read_cache(path: Path, ttl_hours: int):
    if not _is_cache_fresh(path, ttl_hours):
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — corrupt cache = refetch
        return None


def _write_cache(path: Path, payload) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:  # noqa: BLE001 — a cache write failure is not fatal
        pass


def _warn_empty_parse(kind: str, ident: str, reason: str = "") -> None:
    """One LOUD, grep-able line when a 200-OK fetch yielded nothing.

    Same shape as ``edhrec_client._warn_empty_parse`` so operators can
    grep ``WARNING`` across all four sources and tell "the site was
    down" apart from "the site answered but gave us nothing".
    """
    tail = f" ({reason})" if reason else ""
    print(
        f"[edhtop16] WARNING: {kind} {ident!r} fetched OK but contained no "
        f"usable data{tail} — the API returned 200 with an empty/errored "
        "payload (possibly a Cloudflare challenge, an unknown name, or a "
        "schema change). Result NOT cached; will retry next run.",
        file=sys.stderr, flush=True,
    )


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _http_post_json(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.load(resp)


def _post_json_with_retry(
    post: Callable[[str, dict], dict],
    url: str,
    payload: dict,
    max_retries: int = MAX_RETRIES,
    base_delay: float = RETRY_BASE_DELAY_SEC,
) -> dict:
    """Call ``post(url, payload)`` with backoff on 429/5xx HTTPError.

    Mirrors the PR #40 house pattern (``archidekt_client``'s
    ``_get_json_with_retry``): Retry-After honored and clamped to
    ``MAX_RETRY_AFTER_SEC``, else exponential backoff, one stderr line
    per retry, and ONLY ``HTTPError`` is retried — network-level
    failures (URLError/OSError) propagate immediately so a dead network
    doesn't multiply into minutes of sleeping inside a corpus build.
    """
    from .edhrec_client import MAX_RETRY_AFTER_SEC, _parse_retry_after

    last_exc: Optional[urllib.error.HTTPError] = None
    for attempt in range(max_retries + 1):
        try:
            return post(url, payload)
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRYABLE_HTTP_CODES:
                raise
            last_exc = exc
        if attempt >= max_retries:
            break
        hdrs = getattr(last_exc, "headers", None)
        hint = _parse_retry_after(
            hdrs.get("Retry-After") if hdrs is not None else None)
        delay = (min(hint, MAX_RETRY_AFTER_SEC) if hint is not None
                 else base_delay * (2 ** attempt))
        print(
            f"[edhtop16] retry {attempt + 1}/{max_retries} after "
            f"HTTP {last_exc.code} — sleeping {delay:.1f}s",
            file=sys.stderr, flush=True,
        )
        time.sleep(delay)
    assert last_exc is not None  # the loop only exits via return or here
    raise last_exc


def _graphql(
    query: str,
    variables: dict,
    *,
    fetch_json: Optional[Callable[[str, dict], dict]] = None,
) -> dict:
    """Run one GraphQL operation; return its ``data`` dict.

    Raises ``EdhTop16Error`` when the server answers 200 with
    ``errors`` or without a ``data`` object — the "answered, but not
    with data" case. Transport failures raise as usual; callers turn
    both into an empty degrade.
    """
    post = fetch_json or _http_post_json
    payload = {"query": query, "variables": variables}
    raw = _post_json_with_retry(post, ENDPOINT, payload)
    if not isinstance(raw, dict):
        raise EdhTop16Error("non-object GraphQL response")
    errors = raw.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        msg = first.get("message") if isinstance(first, dict) else str(first)
        raise EdhTop16Error(f"GraphQL error: {msg}")
    data = raw.get("data")
    if not isinstance(data, dict):
        raise EdhTop16Error("GraphQL response carried no data object")
    return data


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommanderStats:
    """One commander's aggregate tournament performance.

    ``conversion_rate`` = share of registrations that made a top cut;
    ``win_rate`` = game win rate across recorded rounds; ``meta_share``
    = share of all registrations. All are DESCRIPTIVE — a high
    conversion rate says strong players brought it and did well, not
    that the deck would raise YOUR win rate.
    """

    name: str
    color_id: str = ""
    entries: int = 0
    conversion_rate: Optional[float] = None
    win_rate: Optional[float] = None
    top_cuts: int = 0
    meta_share: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "color_id": self.color_id,
            "entries": self.entries,
            "conversion_rate": self.conversion_rate,
            "win_rate": self.win_rate, "top_cuts": self.top_cuts,
            "meta_share": self.meta_share,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CommanderStats":
        return cls(
            name=str(d.get("name") or ""),
            color_id=str(d.get("color_id") or ""),
            entries=int(d.get("entries") or 0),
            conversion_rate=_as_float(d.get("conversion_rate")),
            win_rate=_as_float(d.get("win_rate")),
            top_cuts=int(d.get("top_cuts") or 0),
            meta_share=_as_float(d.get("meta_share")),
        )


@dataclass(frozen=True)
class TournamentEntry:
    """One player's registered decklist and how it finished."""

    standing: Optional[int] = None
    wins: int = 0
    losses: int = 0
    draws: int = 0
    win_rate: Optional[float] = None
    decklist_url: str = ""
    maindeck: tuple[str, ...] = ()
    tournament_name: str = ""
    tournament_id: str = ""
    tournament_size: Optional[int] = None
    tournament_date: str = ""
    top_cut: Optional[int] = None

    @property
    def made_top_cut(self) -> bool:
        """True when this finish was inside the event's top cut.

        Unknown standing or unknown cut size reads False — "we can't
        tell" must never inflate a conversion number.
        """
        if self.standing is None or not self.top_cut:
            return False
        return 1 <= self.standing <= self.top_cut

    def to_dict(self) -> dict:
        return {
            "standing": self.standing, "wins": self.wins,
            "losses": self.losses, "draws": self.draws,
            "win_rate": self.win_rate, "decklist_url": self.decklist_url,
            "maindeck": list(self.maindeck),
            "tournament_name": self.tournament_name,
            "tournament_id": self.tournament_id,
            "tournament_size": self.tournament_size,
            "tournament_date": self.tournament_date,
            "top_cut": self.top_cut,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TournamentEntry":
        return cls(
            standing=_as_int(d.get("standing")),
            wins=int(d.get("wins") or 0),
            losses=int(d.get("losses") or 0),
            draws=int(d.get("draws") or 0),
            win_rate=_as_float(d.get("win_rate")),
            decklist_url=str(d.get("decklist_url") or ""),
            maindeck=tuple(str(c) for c in (d.get("maindeck") or [])),
            tournament_name=str(d.get("tournament_name") or ""),
            tournament_id=str(d.get("tournament_id") or ""),
            tournament_size=_as_int(d.get("tournament_size")),
            tournament_date=str(d.get("tournament_date") or ""),
            top_cut=_as_int(d.get("top_cut")),
        )


@dataclass(frozen=True)
class CardTournamentStats:
    """How one card shows up across a commander's tournament entries.

    ``presence`` is a play rate among *these* entries, NOT a win-rate
    attribution: entries are sampled top-finishes-first, so a high
    presence means "the successful registrations ran it", with all the
    selection bias that implies. ``mean_entry_win_rate`` is the mean
    game win rate of the entries that ran the card — again descriptive,
    and confounded by the deck it sits in.
    """

    name: str
    entries: int = 0
    total_entries: int = 0
    presence: float = 0.0
    top_cut_entries: int = 0
    top_cut_presence: Optional[float] = None
    mean_entry_win_rate: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name, "entries": self.entries,
            "total_entries": self.total_entries, "presence": self.presence,
            "top_cut_entries": self.top_cut_entries,
            "top_cut_presence": self.top_cut_presence,
            "mean_entry_win_rate": self.mean_entry_win_rate,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CardTournamentStats":
        return cls(
            name=str(d.get("name") or ""),
            entries=int(d.get("entries") or 0),
            total_entries=int(d.get("total_entries") or 0),
            presence=float(d.get("presence") or 0.0),
            top_cut_entries=int(d.get("top_cut_entries") or 0),
            top_cut_presence=_as_float(d.get("top_cut_presence")),
            mean_entry_win_rate=_as_float(d.get("mean_entry_win_rate")),
        )


def _as_float(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v) -> Optional[int]:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _key(name: str) -> str:
    return name.strip().lower()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_COMMANDERS_QUERY = """
query TopCommanders($n: Int!, $sortBy: CommandersSortBy!,
                    $tp: TimePeriod!, $minEntries: Int,
                    $minTournamentSize: Int) {
  commanders(first: $n, sortBy: $sortBy, timePeriod: $tp,
             minEntries: $minEntries,
             minTournamentSize: $minTournamentSize) {
    edges { node {
      name
      colorId
      stats(filters: {timePeriod: $tp}) {
        count conversionRate winRate topCuts metaShare
      }
    } }
  }
}
"""

_ENTRIES_QUERY = """
query CommanderEntries($name: String!, $n: Int!, $tp: TimePeriod!,
                       $minEventSize: Int!) {
  commander(name: $name) {
    name
    colorId
    stats(filters: {timePeriod: $tp}) {
      count conversionRate winRate topCuts metaShare
    }
    entries(first: $n, sortBy: TOP,
            filters: {timePeriod: $tp, minEventSize: $minEventSize}) {
      edges { node {
        standing wins losses draws winRate decklist
        maindeck { name }
        tournament { name TID size tournamentDate topCut }
      } }
    }
  }
}
"""


def _parse_commander_stats(node: dict) -> Optional[CommanderStats]:
    if not isinstance(node, dict):
        return None
    name = (node.get("name") or "").strip()
    if not name:
        return None
    stats = node.get("stats") or {}
    if not isinstance(stats, dict):
        stats = {}
    return CommanderStats(
        name=name,
        color_id=str(node.get("colorId") or ""),
        entries=int(stats.get("count") or 0),
        conversion_rate=_as_float(stats.get("conversionRate")),
        win_rate=_as_float(stats.get("winRate")),
        top_cuts=int(stats.get("topCuts") or 0),
        meta_share=_as_float(stats.get("metaShare")),
    )


def _parse_entry(node: dict) -> Optional[TournamentEntry]:
    """One ``Entry`` node -> record. None when it carries no decklist.

    An entry with no ``maindeck`` is a recorded finish with an
    unpublished list — real data for standings, useless for card
    presence, so it is dropped rather than counted as a 0-card deck
    (which would silently deflate every presence rate).
    """
    if not isinstance(node, dict):
        return None
    cards: list[str] = []
    for c in node.get("maindeck") or []:
        if isinstance(c, dict):
            nm = (c.get("name") or "").strip()
        else:
            nm = str(c).strip()
        if nm:
            cards.append(nm)
    if not cards:
        return None
    tourney = node.get("tournament") or {}
    if not isinstance(tourney, dict):
        tourney = {}
    return TournamentEntry(
        standing=_as_int(node.get("standing")),
        wins=int(node.get("wins") or 0),
        losses=int(node.get("losses") or 0),
        draws=int(node.get("draws") or 0),
        win_rate=_as_float(node.get("winRate")),
        decklist_url=str(node.get("decklist") or ""),
        maindeck=tuple(cards),
        tournament_name=str(tourney.get("name") or ""),
        tournament_id=str(tourney.get("TID") or ""),
        tournament_size=_as_int(tourney.get("size")),
        tournament_date=str(tourney.get("tournamentDate") or ""),
        top_cut=_as_int(tourney.get("topCut")),
    )


def _edge_nodes(connection) -> list[dict]:
    if not isinstance(connection, dict):
        return []
    out: list[dict] = []
    for edge in connection.get("edges") or []:
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
            out.append(edge["node"])
    return out


def fetch_top_commanders(
    n: int = DEFAULT_COMMANDERS_N,
    *,
    sort_by: str = "CONVERSION",
    time_period: str = DEFAULT_TIME_PERIOD,
    min_entries: int = DEFAULT_MIN_ENTRIES,
    min_tournament_size: int = DEFAULT_MIN_EVENT_SIZE,
    cache: bool = True,
    ttl_hours: int = CACHE_TTL_HOURS,
    fetch_json: Optional[Callable[[str, dict], dict]] = None,
) -> list[CommanderStats]:
    """Top-performing cEDH commanders with their tournament statistics.

    ``sort_by`` ∈ ``COMMANDERS_SORT_BY``; ``time_period`` ∈
    ``TIME_PERIODS``. Degrades to ``[]`` (loudly) on any failure. An
    empty result is NEVER cached — see the module docstring.
    """
    if n <= 0:
        return []
    if sort_by not in COMMANDERS_SORT_BY:
        raise ValueError(f"sort_by must be one of {COMMANDERS_SORT_BY}")
    if time_period not in TIME_PERIODS:
        raise ValueError(f"time_period must be one of {TIME_PERIODS}")

    key = (f"commanders-{sort_by}-{time_period}-n{n}-e{min_entries}"
           f"-s{min_tournament_size}")
    path = _cache_path(key)
    if cache:
        cached = _read_cache(path, ttl_hours)
        if isinstance(cached, list) and cached:
            return [CommanderStats.from_dict(d) for d in cached]

    try:
        data = _graphql(
            _COMMANDERS_QUERY,
            {"n": int(n), "sortBy": sort_by, "tp": time_period,
             "minEntries": int(min_entries),
             "minTournamentSize": int(min_tournament_size)},
            fetch_json=fetch_json,
        )
    except Exception as exc:  # noqa: BLE001 — outage = empty source
        print(f"[edhtop16] commander leaderboard unavailable ({exc}) — "
              "degrading to no tournament data",
              file=sys.stderr, flush=True)
        return []

    out: list[CommanderStats] = []
    for node in _edge_nodes(data.get("commanders")):
        rec = _parse_commander_stats(node)
        if rec is not None:
            out.append(rec)

    if not out:
        # NO-CACHE-ON-EMPTY: a 200 with zero commanders is far more
        # likely a challenge page / schema drift than a genuinely empty
        # leaderboard. Never let that squat on the TTL.
        _warn_empty_parse("commander leaderboard", key)
        return []
    if cache:
        _write_cache(path, [c.to_dict() for c in out])
    return out


def fetch_commander_entries(
    commander: str,
    n: int = DEFAULT_N,
    *,
    time_period: str = DEFAULT_TIME_PERIOD,
    min_event_size: int = DEFAULT_MIN_EVENT_SIZE,
    cache: bool = True,
    ttl_hours: int = CACHE_TTL_HOURS,
    fetch_json: Optional[Callable[[str, dict], dict]] = None,
) -> tuple[Optional[CommanderStats], list[TournamentEntry]]:
    """``(commander stats, top-finishing entries)`` for ``commander``.

    ONE POST carries both. Entries come back best-finish-first
    (``sortBy: TOP``) with full maindecks. Degrades to ``(None, [])``
    on any failure — including the unknown-commander case, which the
    API reports as HTTP 200 + GraphQL ``errors``. An empty entry list
    is NEVER cached.
    """
    if n <= 0 or not (commander or "").strip():
        return None, []
    if time_period not in TIME_PERIODS:
        raise ValueError(f"time_period must be one of {TIME_PERIODS}")

    key = f"entries-{commander}-{time_period}-n{n}-s{min_event_size}"
    path = _cache_path(key)
    if cache:
        cached = _read_cache(path, ttl_hours)
        if isinstance(cached, dict) and cached.get("entries"):
            stats_d = cached.get("stats")
            return (
                CommanderStats.from_dict(stats_d) if stats_d else None,
                [TournamentEntry.from_dict(e) for e in cached["entries"]],
            )

    try:
        data = _graphql(
            _ENTRIES_QUERY,
            {"name": commander, "n": int(n), "tp": time_period,
             "minEventSize": int(min_event_size)},
            fetch_json=fetch_json,
        )
    except Exception as exc:  # noqa: BLE001 — outage = empty source
        print(f"[edhtop16] entries for {commander!r} unavailable ({exc}) — "
              "degrading to no tournament data",
              file=sys.stderr, flush=True)
        return None, []

    node = data.get("commander")
    if not isinstance(node, dict):
        _warn_empty_parse("commander entries", commander,
                          "no commander node")
        return None, []
    stats = _parse_commander_stats(node)
    entries: list[TournamentEntry] = []
    for enode in _edge_nodes(node.get("entries")):
        rec = _parse_entry(enode)
        if rec is not None:
            entries.append(rec)

    if not entries:
        # NO-CACHE-ON-EMPTY. Note the stats object is discarded too:
        # caching "stats but no lists" would serve a source that looks
        # present and contributes nothing for a full TTL.
        _warn_empty_parse("commander entries", commander,
                          "no entries with decklists")
        return stats, []
    if cache:
        _write_cache(path, {
            "stats": stats.to_dict() if stats else None,
            "entries": [e.to_dict() for e in entries],
        })
    return stats, entries


# ---------------------------------------------------------------------------
# Derived per-card statistics (pure — no network)
# ---------------------------------------------------------------------------

def card_presence(
    entries: Iterable[TournamentEntry],
    *,
    min_entries: int = MIN_ENTRIES_FOR_PRESENCE,
) -> dict[str, CardTournamentStats]:
    """Per-card presence across tournament entries, keyed by lower name.

    Returns ``{}`` below ``min_entries`` — "we don't know" must never
    render as "nobody plays it" (the ``ReferenceCorpus.support()``
    contract, applied to the same problem).

    The commander itself is not filtered here: the API's ``maindeck``
    already excludes it.
    """
    rows = [e for e in entries or [] if e.maindeck]
    total = len(rows)
    if total < max(1, min_entries):
        return {}
    top_total = sum(1 for e in rows if e.made_top_cut)

    counts: dict[str, int] = {}
    top_counts: dict[str, int] = {}
    display: dict[str, str] = {}
    wr_sums: dict[str, float] = {}
    wr_ns: dict[str, int] = {}
    for e in rows:
        seen: set[str] = set()
        for name in e.maindeck:
            k = _key(name)
            if not k or k in seen:
                continue
            seen.add(k)
            display.setdefault(k, name)
            counts[k] = counts.get(k, 0) + 1
            if e.made_top_cut:
                top_counts[k] = top_counts.get(k, 0) + 1
            if e.win_rate is not None:
                wr_sums[k] = wr_sums.get(k, 0.0) + float(e.win_rate)
                wr_ns[k] = wr_ns.get(k, 0) + 1

    out: dict[str, CardTournamentStats] = {}
    for k, c in counts.items():
        n_wr = wr_ns.get(k, 0)
        out[k] = CardTournamentStats(
            name=display.get(k, k),
            entries=c,
            total_entries=total,
            presence=c / total,
            top_cut_entries=top_counts.get(k, 0),
            top_cut_presence=(top_counts.get(k, 0) / top_total
                              if top_total else None),
            mean_entry_win_rate=(wr_sums[k] / n_wr if n_wr else None),
        )
    return out


def fetch_card_stats(
    commander: str,
    n: int = DEFAULT_N,
    *,
    time_period: str = DEFAULT_TIME_PERIOD,
    min_event_size: int = DEFAULT_MIN_EVENT_SIZE,
    min_entries: int = MIN_ENTRIES_FOR_PRESENCE,
    cache: bool = True,
    ttl_hours: int = CACHE_TTL_HOURS,
    fetch_json: Optional[Callable[[str, dict], dict]] = None,
) -> dict[str, CardTournamentStats]:
    """Per-card tournament presence for ``commander``. ``{}`` on failure."""
    _stats, entries = fetch_commander_entries(
        commander, n, time_period=time_period, min_event_size=min_event_size,
        cache=cache, ttl_hours=ttl_hours, fetch_json=fetch_json,
    )
    return card_presence(entries, min_entries=min_entries)


def load_cached_card_stats(
    commander: str,
    n: int = DEFAULT_N,
    *,
    time_period: str = DEFAULT_TIME_PERIOD,
    min_event_size: int = DEFAULT_MIN_EVENT_SIZE,
    min_entries: int = MIN_ENTRIES_FOR_PRESENCE,
    ttl_hours: int = CACHE_TTL_HOURS,
) -> dict[str, CardTournamentStats]:
    """Cache-only per-card stats — NEVER touches the network.

    Exists for offline consumers that must not fetch inside a loop
    (``scripts/margin_analysis.py --features tournament``, mirroring how
    ``card_score_features`` passes ``corpus=None``). Returns ``{}`` when
    nothing is cached; the caller reports that as "unavailable", never
    as zero.
    """
    key = f"entries-{commander}-{time_period}-n{n}-s{min_event_size}"
    cached = _read_cache(_cache_path(key), ttl_hours)
    if not isinstance(cached, dict) or not cached.get("entries"):
        return {}
    entries = [TournamentEntry.from_dict(e) for e in cached["entries"]]
    return card_presence(entries, min_entries=min_entries)


def fetch_top_decklists(
    commander: str,
    bracket: Optional[int] = None,
    n: int = DEFAULT_N,
    *,
    time_period: str = DEFAULT_TIME_PERIOD,
    min_event_size: int = DEFAULT_MIN_EVENT_SIZE,
    cache: bool = True,
    ttl_hours: int = CACHE_TTL_HOURS,
    fetch_json: Optional[Callable[[str, dict], dict]] = None,
) -> list[list[str]]:
    """Corpus-shaped adapter: plain card-name lists, best finish first.

    **Bracket gate.** ``bracket`` must be ``CEDH_BRACKET`` (5) or this
    returns ``[]`` WITHOUT a request. cEDH tournament lists describe
    bracket-5 play; letting them leak into a B2-B4 reference corpus
    would quietly teach the advisor that a casual deck should look like
    a tournament deck. The gate lives here (not only in the caller) so
    every consumer inherits it. ``bracket=None`` is also refused: an
    unknown bracket is not a bracket-5 bracket.
    """
    if bracket != CEDH_BRACKET:
        return []
    _stats, entries = fetch_commander_entries(
        commander, n, time_period=time_period, min_event_size=min_event_size,
        cache=cache, ttl_hours=ttl_hours, fetch_json=fetch_json,
    )
    return [list(e.maindeck) for e in entries if e.maindeck]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_SCOPE_NOTE = (
    "NOTE (FP-017): cEDH tournament data describes BRACKET-5 humans "
    "only. It is an exploratory data source, NOT a validated predictor, "
    "and no claim is made that it transfers to casual brackets."
)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="commander-tournament",
        description="Import cEDH tournament results from edhtop16.com "
                    "(FP-017). " + _SCOPE_NOTE,
    )
    ap.add_argument("commander", nargs="?", default=None,
                    help="commander name; omit to list top commanders")
    ap.add_argument("-n", type=int, default=None,
                    help=f"entries per commander (default {DEFAULT_N}) or "
                         f"commanders to list (default "
                         f"{DEFAULT_COMMANDERS_N})")
    ap.add_argument("--time-period", choices=TIME_PERIODS,
                    default=DEFAULT_TIME_PERIOD)
    ap.add_argument("--sort-by", choices=COMMANDERS_SORT_BY,
                    default="CONVERSION",
                    help="leaderboard ordering (commander-list mode)")
    ap.add_argument("--min-event-size", type=int,
                    default=DEFAULT_MIN_EVENT_SIZE,
                    help="ignore events smaller than this")
    ap.add_argument("--top", type=int, default=25,
                    help="how many cards to print by presence")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    cache = not args.no_cache
    if not args.commander:
        rows = fetch_top_commanders(
            args.n or DEFAULT_COMMANDERS_N, sort_by=args.sort_by,
            time_period=args.time_period,
            min_tournament_size=args.min_event_size, cache=cache,
        )
        if args.as_json:
            print(json.dumps({"scope_note": _SCOPE_NOTE,
                              "commanders": [r.to_dict() for r in rows]},
                             indent=2))
            return 0 if rows else 1
        print(_SCOPE_NOTE)
        if not rows:
            print("no tournament data available")
            return 1
        print(f"\ntop {len(rows)} commanders by {args.sort_by.lower()} "
              f"({args.time_period}):")
        for r in rows:
            conv = ("  NA" if r.conversion_rate is None
                    else f"{r.conversion_rate * 100:5.1f}%")
            wr = ("  NA" if r.win_rate is None
                  else f"{r.win_rate * 100:5.1f}%")
            print(f"  {r.name[:48]:<48} n={r.entries:>5}  conv={conv}  "
                  f"wr={wr}")
        return 0

    n = args.n or DEFAULT_N
    stats, entries = fetch_commander_entries(
        args.commander, n, time_period=args.time_period,
        min_event_size=args.min_event_size, cache=cache,
    )
    cards = card_presence(entries)
    ranked = sorted(cards.values(),
                    key=lambda c: (-c.presence, c.name))[:max(0, args.top)]
    if args.as_json:
        print(json.dumps({
            "scope_note": _SCOPE_NOTE,
            "commander": stats.to_dict() if stats else None,
            "entries": [e.to_dict() for e in entries],
            "cards": [c.to_dict() for c in ranked],
        }, indent=2))
        return 0 if entries else 1
    print(_SCOPE_NOTE)
    if stats is not None:
        conv = ("NA" if stats.conversion_rate is None
                else f"{stats.conversion_rate * 100:.1f}%")
        wr = ("NA" if stats.win_rate is None
              else f"{stats.win_rate * 100:.1f}%")
        print(f"\n{stats.name}  [{stats.color_id}]  entries={stats.entries}  "
              f"top_cuts={stats.top_cuts}  conversion={conv}  win_rate={wr}")
    if not entries:
        print("no tournament decklists available")
        return 1
    print(f"\n{len(entries)} decklist(s) pulled ({args.time_period}, events "
          f">= {args.min_event_size} players)")
    if not cards:
        print(f"per-card presence withheld: fewer than "
              f"{MIN_ENTRIES_FOR_PRESENCE} entries is not a sample")
        return 0
    print(f"\ncard presence across those lists (top {len(ranked)}):")
    for c in ranked:
        twr = ("   NA" if c.mean_entry_win_rate is None
               else f"{c.mean_entry_win_rate * 100:5.1f}%")
        print(f"  {c.name[:44]:<44} {c.presence * 100:5.1f}%  "
              f"({c.entries}/{c.total_entries})  mean_entry_wr={twr}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
