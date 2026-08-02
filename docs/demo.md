# Showing it to someone

Eight minutes, four acts. Every act demonstrates the same thing from a
different angle: **the tool refuses to give an answer it does not have.** That
is the difference worth showing, because everything else here has a commercial
equivalent.

Run it from a fresh directory. The cold start is part of the point.

---

## Act 1 — thirty seconds to a first result (1 min)

```bash
docker compose run --rm cli doctor
docker compose run --rm cli tag 0x28C6c06298d514Db089934071355E5743bf21d60 \
    -l "Binance 14" -t cex -C high -s "etherscan public tag"
docker compose run --rm cli label 0x28C6c06298d514Db089934071355E5743bf21d60
```

No configuration, no API key. `doctor` reports what is reachable and what is
not, and on a fresh install the answer is "quite a lot" --- Blockscout needs no
credential.

The third command exists in this script because it did not work until recently.
`label` only consulted external sources and answered "no sources configured"
while the user's own label sat in the store. Show it finding what `tag` just
wrote.

---

## Act 2 — the refusals (3 min, the important one)

```bash
docker compose run --rm cli analyze taint -p source=0xTHIEF
docker compose run --rm cli analyze probing -p address=0xOPERATOR
```

Pick a **sparse** address for the temporal run, so it prints the refusal:

```bash
docker compose run --rm cli analyze temporal -p address=0xQUIET
```

> That window is too wide to place anyone: the plausible band spans more of the
> clock than it excludes, so no offset is reported. The activity is real; the
> location is not inferable from it.

Say: *another tool gives you a timezone here. This one tells you the data does
not support one.* Before the fix it printed "operating hours consistent with
UTC-14 to UTC+6" --- twenty hours wide, covering most of the inhabited world,
with a bullet point beside it.

Then show `taint`'s two separate fields:

- `addresses` — currently **holds** value traceable to the source
- `passed_through_but_holds_none` — it **went through** here

Say: *reporting the second as the first is how a payment processor gets
described as a launderer.*

---

## Act 3 — the picture (2 min)

```bash
docker compose run --rm cli graph 0xSEED -f flow --visible-depth 2 \
    --out /case/flow.html
```

Open it. Three things to do, in this order:

**Click a downstream address.** Every route from the seed lights up --- not the
shortest, every one. A split that rejoins shows both legs, because somebody
split the funds for a reason and a shortest-path search hides that.

**Click a node showing `+n`.** One ring opens. There is always either another
`+n` or a frontier marker, never a picture that merely stops.

**Drag the time slider.** The case as it stood at that moment. An edge whose
window straddles the cursor is drawn --- the money had begun moving.

Point at a dashed box: *that is the frontier. Nobody looked past it. It is not
a leaf.*

---

## Act 4 — somebody else can build on it (2 min)

A third-party analyzer, in its own package, touching none of this source:

```bash
pip install -e ./myplugin
chainscope analyze --list      # it is there
```

Then the agent:

```bash
docker compose run --rm agent
```

Ask it in plain language: has this address been labelled, mark it as a mixer,
where did this balance come from. Ten tools, including writes.

---

## Closing slide

Open `docs/needs.md` and point at the **Observed / Inferred** line.

> Every item says where the evidence came from. *Observed* was hit while doing
> the work. *Inferred* is reasoning from those and stays a hypothesis until
> somebody hits it.

Then the sentence to end on:

**Every threshold in this tool was measured, and three of those measurements
overturned what I had guessed.** The mixer's precision decays geometrically,
not linearly --- so the anonymity-set limit is 5 and not 20. Probing's `1/n!`
null model fired on 38% of ordinary accumulation, so the gate is growth and not
length. The revenue-split tolerance had to be relative, because 25 basis points
on a 20% cut and on a 0.1% rebate are not the same claim.

None of those were reasoned out. The validation harnesses corrected them.

---

## If you only have two minutes

Act 2's temporal refusal, and the frontier in Act 3. Both take fifteen seconds
and both show the same property: **the tool distinguishes "no" from "I don't
know", and says which one it means.**
