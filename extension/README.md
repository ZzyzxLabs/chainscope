# chainscope browser extension

Shows what your store knows about the addresses on an explorer page, and lets
you record a label without leaving it.

## Install

```bash
pip install -e ".[all]"
chainscope-serve --writable          # prints a token; keep the terminal open
```

Then load the extension unpacked:

- **Chrome / Edge / Brave** — `chrome://extensions` → Developer mode → *Load
  unpacked* → pick this directory.
- **Firefox** — `about:debugging` → This Firefox → *Load Temporary Add-on* →
  pick `manifest.json`.

Open the extension's options page and paste the token.

## Why there is a token

The server binds to `127.0.0.1`, and that is worth being precise about: it
keeps *other machines* out and does nothing about the one you are on. Any page
in any open tab can `fetch("http://127.0.0.1:8787/...")`, and the browser will
make the request.

So the token is the actual control, and it is required for reads as well as
writes — which addresses you have been looking at is an investigation artefact,
not public information. A fresh one is generated each run, so a token pasted
once does not outlive the session that issued it.

Writes need `--writable`, which is off by default.

## What it sends

Addresses, to `127.0.0.1`. Not URLs, not page text, and nothing to anywhere
else. Which addresses somebody is looking at is already sensitive; what they are
reading about them is more so.

## What the badge means

```
Binance 14 · HIGH
```

The confidence is never omitted, and the tooltip carries the source and the
rationale. A bare label next to an address invites the reader to treat it as
fact, and the moment a label appears on screen is the moment it starts being
quoted.

Where sources disagree, the strongest claim leads and the tooltip lists all of
them. Disagreement is usually the interesting part, not a data-quality problem.

## Recording a label

Click the toolbar icon. The rationale box appears the moment confidence drops
below medium — the store refuses a weak claim without one, and surfacing that
here means the refusal arrives while the reasoning is still in your head.

## Supported explorers

Etherscan and its family (BSC, Polygon, Arbitrum, Base, Optimism, Avalanche),
Suiscan, SuiVision, Tronscan, mempool.space, and Blockchair.

Bitcoin addresses on a page are deliberately **not** annotated: base58 and
bech32 are case-sensitive, and a greedy pattern over page text produces false
positives that are worse than no annotation. Look them up through the popup
instead.
