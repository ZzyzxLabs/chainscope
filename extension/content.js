/* Annotate addresses on an explorer page with what the local store knows.
 *
 * Two design choices worth stating, because both are easy to get wrong in a
 * way that looks fine.
 *
 * ONLY THE PAGE'S OWN TEXT IS READ. The extension never sends page content
 * anywhere except to 127.0.0.1, and it sends addresses rather than URLs or
 * page text. Which addresses somebody is looking at is already sensitive; what
 * they are reading about them is more so.
 *
 * A LABEL IS RENDERED WITH ITS CONFIDENCE, ALWAYS. A badge saying "Binance 14"
 * next to an address invites the reader to treat it as fact. The badge says
 * "Binance 14 · HIGH" and its tooltip carries the source and rationale, because
 * the moment a label appears on screen is the moment it starts being quoted.
 */

const CHAIN_BY_HOST = {
  "etherscan.io": "eip155:1",
  "optimistic.etherscan.io": "eip155:10",
  "bscscan.com": "eip155:56",
  "polygonscan.com": "eip155:137",
  "basescan.org": "eip155:8453",
  "arbiscan.io": "eip155:42161",
  "snowtrace.io": "eip155:43114",
  "suiscan.xyz": "sui:mainnet",
  "suivision.xyz": "sui:mainnet",
  "tronscan.org": "tron:mainnet",
  "mempool.space": "bip122:000000000019d6689c085ae165831e93",
};

// EVM (20 byte), Sui (32 byte), and Tron. Bitcoin is left alone deliberately:
// base58 and bech32 are case-sensitive and a greedy pattern over page text
// produces false positives that are worse than no annotation.
const ADDRESS_RE = /\b(0x[0-9a-fA-F]{40}|0x[0-9a-fA-F]{64}|T[1-9A-HJ-NP-Za-km-z]{33})\b/g;

const CATEGORY_COLOUR = {
  sanctioned: "#c62828",
  mixer: "#ad1457",
  cex: "#1565c0",
  dex: "#2e7d32",
  bridge: "#6a1b9a",
  illicit: "#e65100",
};

let config = null;
const cache = new Map();
const pending = new Set();

async function settings() {
  if (config) return config;
  config = await chrome.storage.local.get(["endpoint", "token"]);
  config.endpoint = config.endpoint || "http://127.0.0.1:8787";
  return config;
}

async function resolve(address, chain) {
  const key = `${chain}:${address.toLowerCase()}`;
  if (cache.has(key)) return cache.get(key);
  if (pending.has(key)) return null;
  pending.add(key);

  const { endpoint, token } = await settings();
  if (!token) return null;

  try {
    const url = `${endpoint}/resolve?address=${encodeURIComponent(address)}` +
                `&chain=${encodeURIComponent(chain)}`;
    const response = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) return null;
    const data = await response.json();
    cache.set(key, data);
    return data;
  } catch {
    // The server is simply not running. That is the normal state for most of
    // the day and is not worth a console error on every address.
    return null;
  } finally {
    pending.delete(key);
  }
}

function badgeFor(data) {
  if (!data || !data.claims || !data.claims.length) return null;

  // Strongest claim leads. The rest go in the tooltip rather than being
  // dropped: disagreement between sources is usually the interesting part.
  const claims = [...data.claims].sort((a, b) => b.confidence_value - a.confidence_value);
  const best = claims[0];

  const badge = document.createElement("span");
  badge.className = "chainscope-badge";
  badge.style.setProperty("--cs-colour", CATEGORY_COLOUR[best.category] || "#546e7a");

  const name = document.createElement("span");
  name.className = "chainscope-name";
  name.textContent = best.label;

  const confidence = document.createElement("span");
  confidence.className = "chainscope-confidence";
  // Never omitted. A bare label is read as a fact.
  confidence.textContent = best.confidence;

  badge.append(name, confidence);
  badge.title = claims
    .map((c) => {
      const parts = [`${c.label} [${c.category}] ${c.confidence}`, `source: ${c.source}`];
      if (c.rationale) parts.push(`why: ${c.rationale}`);
      return parts.join("\n");
    })
    .join("\n\n") + (claims.length > 1 ? `\n\n${claims.length} claims; sources disagree.` : "");

  return badge;
}

function annotate(node, address, data) {
  const badge = badgeFor(data);
  if (!badge) return;
  // Marked so a re-scan does not stack badges as the page mutates.
  node.dataset.chainscopeDone = address.toLowerCase();
  node.after(badge);
}

function candidates() {
  // Links and short text nodes only. Walking the whole DOM text on a busy
  // explorer page is slow enough to be noticeable and finds addresses inside
  // scripts, which are not on screen anyway.
  return document.querySelectorAll(
    'a[href*="/address/"], a[href*="/account"], a[href*="/token/"], ' +
    "span[data-highlight-target], .hash-tag, .text-truncate"
  );
}

async function scan() {
  const chain = CHAIN_BY_HOST[location.hostname.replace(/^www\./, "")];
  if (!chain) return;

  for (const node of candidates()) {
    const text = (node.textContent || "") + " " + (node.getAttribute("href") || "");
    const match = text.match(ADDRESS_RE);
    if (!match) continue;
    const address = match[0];
    if (node.dataset.chainscopeDone === address.toLowerCase()) continue;

    const data = await resolve(address, chain);
    if (data) annotate(node, address, data);
  }
}

let scheduled = null;
function schedule() {
  // Explorer pages mutate constantly. Coalescing keeps this from re-scanning
  // on every row that renders.
  if (scheduled) clearTimeout(scheduled);
  scheduled = setTimeout(scan, 400);
}

schedule();
new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
