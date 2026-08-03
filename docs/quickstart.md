# Quickstart

Every command below was run in order, from an empty directory, while writing
this. Where the output is quoted it is what actually appeared — including the
parts that say something is missing, because those are most of what a new user
sees and a quickstart that hides them is lying about the first ten minutes.

## 1. Install

```bash
pip install -e '.[all]'      # from a clone; not yet on PyPI
chainscope --help
```

## 2. Get the naming data

```bash
chainscope labels            # what is present, and what each is worth
chainscope labels fetch      # ~270,000 named addresses
```

Skip this and every address reads `unlabelled`, which is **not** the same as an
address being unknown — it means nothing was consulted. `labels` exits non-zero
when nothing is present so a script notices.

The terms are printed beside each dataset. Two of them may not be
redistributed and one repackages Etherscan's data, so they are fetched into the
case directory and gitignored. Nothing is bundled.

## 3. Start from one address

```bash
chainscope investigate 0xdAC17F958D2ee523a2206206994597C13D831ec7
```

On an empty directory this prints:

```
  — already labelled        no store at .chainscope/store.db yet
  — somewhere to look next  no store yet
  — when it acts            too active for one page --- narrow it with -p start_block
```

Three different absences, each named. `investigate` runs what applies and
**prints the next command with its arguments filled in** — it is the step that
produces the parameters the specific analyzers need.

It exits non-zero when nothing is found, so silence is never readable as a
clean bill of health.

## 4. Look at it

```bash
chainscope serve --writable --analyst you@lab
```

Opens a browser on loopback. Type an address; if the case has never seen it,
it is fetched and the status line says how many transfers came from a provider.

Click a card to select it — every path from the seed lights up, not just the
shortest, because a split that rejoins is the structure worth seeing. The `+`
handles on each card grow the picture in the direction the money went.

Writing is off without `--writable`: recording a label should be a decision,
not the default for a command somebody ran to look at something.

## 5. Check what is a forgery before quoting any total

```bash
chainscope analyze impersonation -p address=0xYOURSEED
```

Measured on a real case: **42 of 55** ERC-20 transfers belonged to tokens
imitating USDC and ETH. A total grouped by symbol over that data is mostly
forgery and looks exactly like a real number.

The web page does this automatically and hides forgeries by default — visibly,
with a count in the status line.

## 6. Record what you find

```bash
chainscope tag 0xADDR -l "Binance hot wallet" -t cex -C high -s "etherscan"
chainscope note observation "the relay pays out within an hour"
chainscope lead add 0xADDR -k twitter -v alice -s "ENS text record"
```

Three different things, kept apart on purpose:

- a **tag** is a claim about what an address *is*, and cannot be recorded
  without a source;
- a **note** is reasoning, append-only and authored;
- a **lead** is somewhere to look, carrying the step that would confirm it —
  never an attribution.

## 7. Hand it to somebody else

```bash
chainscope bundle export --out case.zip
```

See [handover.md](./handover.md) — a case is more than one file, and which
files travel is a decision about what you are asserting.

---

## What to read next

| | |
|---|---|
| Why a technique works and when it fails | [`docs/methods/`](./methods/) |
| What each label dataset is worth | [`docs/data-sources.md`](./data-sources.md) |
| Writing your own analyzer | [`docs/extending.md`](./extending.md) |
| Why this is Python | [`docs/why-python.md`](./why-python.md) |
