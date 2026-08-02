#!/usr/bin/env python
"""Record provider responses into replayable fixtures.

    python scripts/record_cassettes.py            # add what is missing
    python scripts/record_cassettes.py --rerecord # replace everything

Needs ``ETHERSCAN_API_KEY`` in the environment or ``.env``. Nobody else needs it
afterwards --- that is the point of committing the result.

**What gets recorded is chosen, not swept.** Each entry below exists to pin one
response *shape* the parser has to survive: a complete history, an empty one, a
truncated one, a token transfer with non-18 decimals, an EOA versus a contract.
Recording "some addresses" instead would produce a large fixture that pins
nothing in particular and goes stale without anyone noticing.

Every target is a publicly documented address --- exploiter wallets named in
incident reports, a sanctioned mixer on the OFAC list, a labelled exchange hot
wallet. None of it is sensitive, and all of it stays interesting for years,
which matters for a fixture meant to outlive the session that recorded it.

Responses are scrubbed on the way out and the file is refused if any credential
survives; see :mod:`chainscope.transport.cassette`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from chainscope.config import ConfigError, Settings  # noqa: E402
from chainscope.core.chainid import ChainId  # noqa: E402
from chainscope.providers.etherscan import EtherscanProvider, ResultTruncated  # noqa: E402
from chainscope.transport.cassette import Cassette, Mode  # noqa: E402
from chainscope.transport.http import Client  # noqa: E402
from chainscope.transport.throttle import Throttle  # noqa: E402

CASSETTES = ROOT / "tests" / "cassettes"
ETHEREUM = ChainId.evm(1)

# Ronin bridge exploiter. Named in the March 2022 incident report; the account
# is long dormant, so its history is finite and will not grow under the fixture.
RONIN_EXPLOITER = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"

# The bridge was drained in block 14442835 (23 March 2022). This window opens
# just before it and closes shortly after, so the recording covers the theft
# itself and the first movements out of it.
RONIN_FROM = 14_442_000
RONIN_TO = 14_460_000

# Tornado Cash 100 ETH pool. On the OFAC SDN list, and the canonical example of
# an address where a naive traversal must stop rather than continue.
TORNADO_100 = "0xA160cdAB225685dA1d56aa342Ad8841c3b53f291"

# Binance hot wallet 14. Enormously busy: the truncation case, and the reason
# ResultTruncated exists.
BINANCE_14 = "0x28C6c06298d514Db089934071355E5743bf21d60"

# A thirty-block window, long finalised. Narrow enough that even this address
# produces a reviewable number of rows, wide enough to actually contain a token
# transfer, and fixed so the fixture does not drift.
BINANCE_FROM = 21_000_000
BINANCE_TO = 21_000_030

# An address nobody has ever transacted with. Pins the "No transactions found"
# response, which shares `status: "0"` with rate limiting and must not be
# confused with it.
UNUSED = "0x0000000000000000000000000000000000031337"

# USDC. Six decimals, not eighteen -- the case where treating decimals as a
# constant produces an answer wrong by a factor of a trillion.
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"


def record(mode: str) -> int:
    try:
        settings = Settings.load(search_from=ROOT)
        api_key = settings.require("etherscan")
    except ConfigError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    path = CASSETTES / "etherscan_mainnet.json"
    # Etherscan documents the free tier as 5 calls/second; the account used to
    # record this was cut off at 3, and the error arrives as a `status: "0"`
    # body rather than an HTTP 429, so the throttle never sees it and the
    # circuit breaker never trips -- the request simply fails. Recording is a
    # one-off, so it runs well under whichever limit is real.
    throttle = Throttle(default_rate=min(settings.rate_limit, 2.0))

    with Cassette(path, mode=mode) as cassette:
        client = Client(cache=cassette, throttle=throttle, timeout=settings.timeout)
        provider = EtherscanProvider(api_key, client=client)
        before = len(cassette)

        for label, action in _targets(provider):
            with cassette.labelling(label):
                try:
                    action()
                    print(f"  ok        {label}")
                except ResultTruncated:
                    # Expected for the busy addresses, and the response was
                    # recorded before the check fired -- which is exactly the
                    # shape the truncation test needs to replay.
                    print(f"  truncated {label}  (recorded; this is the point)")
                except Exception as exc:
                    print(f"  FAILED    {label}: {type(exc).__name__}: {exc}")

        client.close()
        added = len(cassette) - before

    size = path.stat().st_size if path.is_file() else 0
    print(
        f"\n{path.relative_to(ROOT)}: {len(cassette)} interactions "
        f"(+{added}), {size / 1024:.1f} KiB"
    )

    return _verify(path)


class _Offline(Client):
    """A client that cannot reach the network, for proving a cassette complete."""

    def _send(self, *args: object, **kw: object) -> object:
        raise RuntimeError("cassette miss --- this request was never recorded")


def _verify(path: Path) -> int:
    """Replay every target with no network and no key. A miss is a broken fixture.

    This needs its own pass because a successful-looking recording can still be
    incomplete. :meth:`EtherscanProvider.asset_transfers` deliberately swallows
    one endpoint's failure so a single dead endpoint does not lose the other
    two; during recording that turns a rate-limited call into a silent gap. The
    run still prints ``ok``, and the hole surfaces much later as a test reaching
    for the network on somebody else's machine.
    """
    print("\nverifying offline replay:")
    provider = EtherscanProvider("replay-needs-no-key", client=_Offline(cache=Cassette(path)))
    targets = _targets(provider)
    missing = 0
    for label, action in targets:
        try:
            action()
        except ResultTruncated:
            pass  # recorded; the exception is the point
        except Exception as exc:
            missing += 1
            print(f"  MISS  {label}: {exc}")
    if missing:
        print(f"\n{missing} of {len(targets)} targets cannot replay. Re-run to fill the gaps.")
        return 1
    print(f"  all {len(targets)} targets replay with no network and no key")
    return 0


def _targets(p: EtherscanProvider) -> list[tuple[str, object]]:
    """Label and thunk per recorded shape. Order is the order in the file.

    Every history query is bounded by a block range rather than trusting a row
    limit to keep it small. Two reasons, and the second is the important one:
    a bounded range yields a fixture small enough to review, and it yields the
    *same* rows in five years. An unbounded query against a live address
    records whatever that address happened to be doing on the afternoon
    somebody ran this, which is not a fixture --- it is a snapshot with no
    stated meaning.
    """
    return [
        (
            "txlist: Ronin exploiter, complete history in a bounded range",
            lambda: p.address_history(
                ETHEREUM,
                RONIN_EXPLOITER,
                start_block=RONIN_FROM,
                end_block=RONIN_TO,
                limit=100,
            ),
        ),
        (
            "txlist: unused address, empty result",
            lambda: p.address_history(ETHEREUM, UNUSED, limit=10),
        ),
        (
            "txlist: Binance 14, truncated at the requested limit",
            lambda: p.address_history(ETHEREUM, BINANCE_14, limit=5),
        ),
        (
            # Bounded to a few blocks rather than limited to a few rows. An
            # unbounded query against this address truncates on the first
            # endpoint and raises, so `tokentx` never runs and the six-decimal
            # shape -- the one that costs a factor of a trillion when handled
            # as though decimals were always 18 -- never gets recorded at all.
            "transfers: Binance 14 USDC, six decimals, bounded range",
            lambda: p.asset_transfers(
                ETHEREUM,
                BINANCE_14,
                direction="all",
                contract=USDC,
                start_block=BINANCE_FROM,
                end_block=BINANCE_TO,
                limit=100,
            ),
        ),
        (
            "transfers: Ronin exploiter, native and internal, bounded range",
            lambda: p.asset_transfers(
                ETHEREUM,
                RONIN_EXPLOITER,
                direction="all",
                start_block=RONIN_FROM,
                end_block=RONIN_TO,
                limit=100,
            ),
        ),
        (
            "account: Ronin exploiter, EOA with balance and nonce",
            lambda: p.get_account(ETHEREUM, RONIN_EXPLOITER),
        ),
        (
            "account: Tornado Cash 100 ETH, contract",
            lambda: p.get_account(ETHEREUM, TORNADO_100),
        ),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--rerecord",
        action="store_true",
        help="refetch everything, replacing existing entries. Without this, "
        "existing recordings are left alone and only gaps are filled -- so a "
        "diff shows the one thing that changed rather than every timestamp.",
    )
    args = ap.parse_args()
    return record(Mode.RECORD if args.rerecord else Mode.ONCE)


if __name__ == "__main__":
    raise SystemExit(main())
