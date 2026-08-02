# Running it

Four ways in, in ascending order of commitment.

## uvx — nothing installed

```bash
uvx --from 'chainscope[all]' chainscope doctor
```

Downloads, runs, leaves nothing behind. Good for "does this do what I want"
before deciding anything.

`doctor` is the right first command and it **exits non-zero** when
`ADDRESS_HISTORY` is unreachable — which is the state of a fresh install with
no key. That is deliberate: without it, "what did this address do" has no
answer, and an empty result is indistinguishable from an address that never did
anything.

## pip — the ordinary way

```bash
pip install -e ".[all]"
cp .env.example .env      # fill in what you have
chainscope doctor
```

## Docker — no Python on your machine

```bash
docker build -t chainscope .
docker run --rm -v "$PWD/case:/case" chainscope doctor
```

Runs as uid 1000, not root. A forensics tool reads untrusted data all day —
label files from strangers, JSON from explorers, HTML it renders — and mounts
your case directory. Root for that is a bad trade for nothing.

With compose, sharing one case directory:

```bash
docker compose run --rm cli doctor
docker compose run --rm cli tag 0xABC… -l "eXch deposit" -t cex -C high -s "analysis"
docker compose --profile serve up            # the browser extension's backend
docker compose run --rm agent                # MCP over stdio
```

The `serve` port is published to `127.0.0.1` explicitly rather than `0.0.0.0`.
A token guards the API, but not listening on the network in the first place is
the stronger control.

The `agent` service runs with `network_mode: none`. It reads a local store and
answers over stdio; there is nothing for it to reach.

## Nix — the same build in two years

```bash
nix run github:ZzyzxLabs/chainscope -- doctor
nix develop            # the dev shell, with graphviz and uv
nix run .#mcp          # the agent surface
```

The lockfile is committed, which is the only reason to offer a flake: it pins
the transitive C libraries that a requirements file does not, and those are what
break a reproduction long before any Python package does.

## Which to use

| | |
|---|---|
| Trying it out | `uvx` |
| Working on a case | `pip install -e` |
| No Python, or an isolated case | Docker |
| Reproducing a case later | Nix |
| Giving an agent access | `docker compose run --rm agent`, or `chainscope-mcp` |

## The one key worth having

`ETHERSCAN_API_KEY` — free, covers 60+ EVM chains, and is the only way to answer
"what did this address do" on EVM. No JSON-RPC method lists an address's
transactions.

Sui needs no key at all: `suix_queryTransactionBlocks` filters by address
directly.
