/**
 * The views this tool produces, and the endpoints it answers on.
 *
 * This mirrors `chainscope.server.site.VIEWS` in the Python package. Two
 * descriptions of one tool drift apart, and the drift always favours the one
 * nobody reads — so `scripts/check-views-match.py` fails the build when these
 * disagree, rather than leaving it to whoever notices first.
 *
 * The `cannot` field is the reason this file exists at all. Every view here can
 * produce a picture that looks complete when it is not, and the *way* each one
 * fails differs: a graph stops at a node limit, a filter matches nothing, a
 * source could not be read. One general disclaimer at the bottom of a page
 * cannot say which, and a reader who has seen it twice stops reading it.
 */

export type View = {
  name: string;
  where: string;
  what: string;
  /** What to reach for it for. */
  use: string;
  /** What an answer from it does not settle. Never omitted. */
  cannot: string;
};

export const VIEWS: View[] = [
  {
    name: "flow graph",
    where: "the case view, or chainscope graph -f flow",
    what:
      "Addresses as cards, money as arrows, laid out left to right by how many " +
      "hops the funds travelled from the seed.",
    use:
      "Reading the shape of a path: a split into many wallets, a collection " +
      "back into one, a hop through a mixer.",
    cannot:
      "The columns are hop distance, not time — an address two columns right " +
      "is not necessarily later. A node with a dashed border is a frontier: " +
      "its counterparties were never fetched, so the picture stops there " +
      "because nobody looked, not because the money did.",
  },
  {
    name: "timeline scrubber",
    where: "bottom left of the graph",
    what: "Hides flows whose span had not yet reached the chosen instant.",
    use: "Watching an incident unfold, and seeing what was already true before it.",
    cannot:
      "An edge aggregates many transfers, so it appears once its span reaches " +
      "the cursor. Undated edges are always shown: a missing timestamp means " +
      "the provider gave none, which says nothing about when it happened.",
  },
  {
    name: "asset filter",
    where: "left panel of the case view",
    what:
      "Every asset in view, grouped by whether its symbol matches the canonical " +
      "contract for that symbol on this chain.",
    use:
      "Seeing at a glance that a transfer labelled USDC was not USDC. Forged " +
      "tokens are how a graph is made to tell a false story.",
    cannot:
      "UNLISTED means there is no canonical entry to compare against. It is " +
      "neither an accusation nor a clearance.",
  },
  {
    name: "follow the money from here",
    where: "right panel, with an address selected",
    what:
      "Fetches one hop out from the selected address and merges it in, filtered " +
      "by direction, asset, time window and value floor.",
    use:
      "Building the picture by decision rather than by depth: every address on " +
      "screen is there because you judged the one before it worth following.",
    cannot:
      "The only control that spends a rate limit. A filter narrows what is " +
      "fetched, so it narrows what you will ever see — the result states how " +
      "many flows it excluded, because a filter that matched nothing and an " +
      "address that never moved money produce the same small graph.",
  },
  {
    name: "case dashboard",
    where: "chainscope dashboard",
    what: "A case overview: what is labelled, what is open, what was asked of whom.",
    use: "Picking up a case after a week away, or handing one to somebody else.",
    cannot: "Reads the store only. It cannot know about work done outside it.",
  },
  {
    name: "report",
    where: "chainscope report",
    what:
      "The case narrative, its claims, and the provenance of each — assembled " +
      "from notes, attributions and analyzer results.",
    use: "Handing conclusions to somebody who will act on them.",
    cannot:
      "Every claim carries its confidence and source. A report whose claims are " +
      "all MEDIUM is a report of leads, not of evidence, and reads that way on " +
      "purpose.",
  },
  {
    name: "case bundle",
    where: "chainscope bundle export",
    what:
      "One directory or zip holding the case record, the query cache, the audit " +
      "log, the analyzer results and the report, under one manifest.",
    use: "Sending a case to somebody who can then replay and check it.",
    cannot:
      "A bundle without the query cache documents what was concluded but not " +
      "from what, and says so rather than looking complete.",
  },
  {
    name: "graph exports",
    where: "chainscope graph -f {cytoscape,d3,dot,gexf}",
    what: "The same graph in formats other tools read.",
    use: "Taking the case into Gephi, Cytoscape, or a d3 page of your own.",
    cannot:
      "These carry the topology and the amounts, not the provenance. A claim " +
      "rendered in another tool loses the confidence attached to it here, which " +
      "is exactly the flattening this package exists to resist.",
  },
];

export type Endpoint = {
  http: string;
  name: string;
  description: string;
};

export const ENDPOINTS: Endpoint[] = [
  {
    http: "GET /resolve?address=&chain=",
    name: "Say what is known about an address",
    description:
      "Every attribution held for an address, each with its source, confidence " +
      "and rationale. Reports which sources could not be consulted, so an empty " +
      "answer is never mistaken for a clean one.",
  },
  {
    http: "GET /flows?address=&chain=&direction=out|in",
    name: "List the money in or out of an address",
    description:
      "Aggregated flows by counterparty and asset, largest first. Amounts are " +
      "strings: JSON numbers are doubles and would round them.",
  },
  {
    http: "GET /expand?address=&chain=&direction=out,in&asset=&since=&min_raw=",
    name: "Follow the money one hop",
    description:
      "Fetch a single address's counterparties from a chain and merge them into " +
      "the case. The one call that spends a rate limit. Returns what it excluded " +
      "alongside what it kept.",
  },
  {
    http: "GET /graph?address=&chain=&depth=&max_nodes=",
    name: "Get the flow graph around an address",
    description:
      "Nodes, edges, hop depths and assets, built from the store. Marks frontier " +
      "nodes, so a boundary is never read as an ending.",
  },
  {
    http: "GET /analyze?name=&address=&chain=",
    name: "Run an analyzer",
    description:
      "impersonation, poisoning, contributors or route. Returns findings " +
      "(observations) and hypotheses (inferences) separately, with the factors " +
      "behind each score exposed rather than a single number.",
  },
  {
    http: "GET /notes, GET /leads, POST /note, POST /tag",
    name: "Read and add to the case record",
    description:
      "Append-only, authored and timed: an agent's suggestion stays " +
      "distinguishable from a person's judgement six months later, which cannot " +
      "be recovered after the fact.",
  },
];

/**
 * The rules a caller has to honour to read a response honestly.
 *
 * Stated on the page as well as in the agent card, because a person reading the
 * docs and a model reading `/llms.txt` make the same mistake: reporting an empty
 * result as a finding.
 */
export const CONTRACT: { term: string; meaning: string }[] = [
  {
    term: "reliable / unreachable_sources",
    meaning:
      "A source that could not be read produces the same empty claim list as an " +
      "address nobody has labelled. Check this before saying nothing is known.",
  },
  {
    term: "complete / truncated",
    meaning:
      "There was more. Reporting the rows you got as the whole answer turns a " +
      "stopping point into a conclusion.",
  },
  {
    term: "filtered_out",
    meaning:
      "How many flows your own filter removed. Zero results with 526 excluded " +
      "is a statement about the filter, not about the address.",
  },
  {
    term: "frontier",
    meaning: "Nobody looked past this node. It is not where the money stopped.",
  },
  {
    term: "findings vs hypotheses",
    meaning:
      "Findings are observed. Hypotheses are inferred and capped at MEDIUM " +
      "confidence. Merging them in a summary invents certainty.",
  },
  {
    term: "amounts are strings",
    meaning:
      "Wei-scale. Parsing them as JSON numbers rounds them, and the rounding " +
      "survives all the way into a report.",
  },
];

export type Analysis = { name: string; what: string; cannot: string };

/**
 * The registered analyses.
 *
 * The server serves this list from the entry-point registry at `/analyses`, so
 * the case view never hard-codes it. This copy exists because the landing page
 * is statically exported and has no server to ask — and it carries the `cannot`
 * line, which the registry has no field for and which is the part a reader most
 * needs before deciding what a result means.
 *
 * `scripts/check_views_match.py` fails the build if these names drift from what
 * is actually installed.
 */
export const ANALYSES: Analysis[] = [
  {
    name: "linked_holders",
    what:
      "Takes a list of addresses and groups them into the parties that appear " +
      "to control them, with the link that formed each group attached.",
    cannot:
      "That a group is one person. It shows observed linkage, and an address " +
      "left alone is unlinked by these signals rather than independent — if " +
      "its history was never fetched it could not have been linked, so the " +
      "top holder's real share is a floor, never a ceiling.",
  },
  {
    name: "co_spend_cluster",
    what:
      "Groups addresses controlled by one party, from the fact that spending " +
      "several inputs in one transaction requires signing for all of them.",
    cannot:
      "Who that party is. A cluster shows shared control, not identity, and " +
      "cannot separate a person from a custodian holding funds for thousands. " +
      "CoinJoin inverts the heuristic entirely, so suspected ones are excluded " +
      "rather than down-weighted — one of them can poison a whole result.",
  },
  {
    name: "common_funder",
    what: "Groups account-model addresses by who first funded them.",
    cannot:
      "That they share an owner. A shared funder is often an exchange paying " +
      "out to thousands of unrelated people.",
  },
  {
    name: "consolidation",
    what:
      "Finds where a seed's counterparties send their funds onward, which is " +
      "how custodial services show up in a graph.",
    cannot:
      "Whether the consolidation point is a service or one person collecting. " +
      "Walking into an exchange usually ends a trace rather than advancing it.",
  },
  {
    name: "contributors",
    what: "Splits an address's inflow by who sent it and how each is linked.",
    cannot:
      "Ownership. Links are graded — same wallet, reachable, co-funded — and " +
      "the weakest of those is barely a lead.",
  },
  {
    name: "cross_chain",
    what: "Matches a deposit on one chain to a payout on another.",
    cannot:
      "Certainty that the two are the same money. It pairs by timing and " +
      "amount, and a busy bridge produces coincidences.",
  },
  {
    name: "impersonation",
    what: "Finds tokens whose symbol imitates a real one.",
    cannot:
      "Anything about a symbol with no canonical entry to compare against. " +
      "UNLISTED is neither an accusation nor a clearance.",
  },
  {
    name: "mixer",
    what: "Pairs mixer deposits with withdrawals by timing, with the anonymity set.",
    cannot:
      "Which withdrawal is yours. The anonymity set size is reported because a " +
      "pairing inside a set of 500 is not evidence.",
  },
  {
    name: "peel_chain",
    what: "Follows a UTXO peel chain, identifying the payment shed at each hop.",
    cannot:
      "Whether the peels are payments or self-transfers. Both look the same on " +
      "chain.",
  },
  {
    name: "poisoning",
    what: "Finds addresses generated to be mistaken for a real counterparty.",
    cannot:
      "That anyone was deceived. It reports the probability the resemblance " +
      "was chance — read that number before acting.",
  },
  {
    name: "probing",
    what: "Finds test-then-commit and escalating transfer sequences.",
    cannot:
      "Intent. A small transfer before a large one is also what a careful " +
      "person does.",
  },
  {
    name: "route",
    what: "Finds time-respecting routes between two addresses.",
    cannot:
      "That the money took that route. A path that respects time is possible, " +
      "not actual — 62% of naive graph paths are causally impossible, and " +
      "these are the ones that survive that filter.",
  },
  {
    name: "taint",
    what: "Traces how much of each address's holdings came from a given source.",
    cannot:
      "Which specific coins. Taint is an accounting convention, and different " +
      "conventions give different answers on the same data.",
  },
  {
    name: "temporal",
    what: "Profiles an address's operating hours from its own outbound activity.",
    cannot:
      "A location. It gives a plausible UTC offset band, which many places " +
      "share, and automation defeats it entirely.",
  },
];
