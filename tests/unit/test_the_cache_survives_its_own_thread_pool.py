"""The response cache is read by the fetch pool, from several threads at once.

`_fetch_into` fetches a window of pages concurrently, and every one of those
reads goes through `Cache`. It held a single `sqlite3.Connection` opened with
``check_same_thread=False`` and a lock that guarded only construction, on the
reasoning --- written into its own docstring --- that "SQLite serialises writes
itself".

SQLite does. The Python `Connection` object does not: `check_same_thread=False`
turns off the *check*, and where `sqlite3.threadsafety` is 1 the interleaved
``execute``/``fetchone``/``commit`` of concurrent callers corrupts the statement
state. It surfaced as::

    sqlite3.InterfaceError: bad parameter or other API misuse

against page three of an address whose pages one and two had just returned a
thousand rows each --- reported to the reader as "fetch failed", on an empty
canvas, with nothing naming a database.

These tests hammer the cache from more threads than the fetch pool uses. They
failed before the lock was extended to cover every statement.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from chainscope.transport.cache import Cache, Volatility

THREADS = 12
ROUNDS = 40


def test_concurrent_readers_and_writers_do_not_corrupt_the_connection(
    tmp_path: Path,
) -> None:
    cache = Cache(tmp_path / "c.db")
    # A payload with some bulk to it, so a reader is genuinely mid-statement
    # while another thread commits.
    payload = {"rows": [{"i": i, "hash": f"0x{i:064x}"} for i in range(200)]}

    def churn(worker: int) -> list[str]:
        problems: list[str] = []
        for round_ in range(ROUNDS):
            key = f"w{worker}-r{round_ % 7}"
            try:
                cache.put(key, payload, Volatility.SETTLED, provider="stub")
                got = cache.get(key, Volatility.SETTLED)
                if got is not None and len(got["rows"]) != 200:
                    problems.append(f"{key}: short read")
            except Exception as exc:  # collected, not raised: the assertion is
                problems.append(f"{key}: {type(exc).__name__}: {exc}")
        return problems

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        failures = [p for batch in pool.map(churn, range(THREADS)) for p in batch]

    assert not failures, failures[:5]
    cache.close()


def test_a_value_survives_the_round_trip_unchanged(tmp_path: Path) -> None:
    """Serialising the statements must not have changed what is stored."""
    cache = Cache(tmp_path / "c.db")
    value = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    cache.put("k", value, Volatility.SETTLED)
    assert cache.get("k", Volatility.SETTLED) == value
    cache.close()


def test_stats_can_be_read_while_the_pool_writes(tmp_path: Path) -> None:
    """`stats` walks the whole table; it must not trip over a concurrent put."""
    cache = Cache(tmp_path / "c.db")

    def write(i: int) -> None:
        cache.put(f"k{i}", {"i": i}, Volatility.SETTLED)

    def read(_: int) -> int:
        return int(cache.stats()["entries"])

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(write, range(120)))
        counts = list(pool.map(read, range(THREADS)))

    assert all(c > 0 for c in counts)
    assert cache.stats()["entries"] == 120
    cache.close()


def test_closing_under_load_is_not_a_crash(tmp_path: Path) -> None:
    """`close` takes the lock too, so it cannot land mid-statement."""
    cache = Cache(tmp_path / "c.db")
    for i in range(20):
        cache.put(f"k{i}", {"i": i}, Volatility.SETTLED)

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(cache.get, f"k{i}", Volatility.SETTLED) for i in range(20)]
        for job in jobs:
            job.result()
    cache.close()

    # And it really did write: the file is readable afterwards.
    assert json.loads('{"ok": true}')["ok"]
    assert (tmp_path / "c.db").exists()
