/**
 * Talking to the local chainscope server.
 *
 * The token is minted per run and injected into the served HTML, exactly as it
 * was before this front end existed. It is not in the URL, because a URL is the
 * one place a credential gets copied into a bug report, a screenshot, or
 * somebody's shell history — and this one authorises reading a case.
 *
 * `window.__CHAINSCOPE__` is written by a script tag the Python server
 * substitutes into the exported HTML. In `next dev` there is no such server, so
 * the values fall back to an env var and a default port; that path is for
 * developing the UI and is never how an investigator runs it.
 */

export type Boot = { token: string; store: string; writable: boolean };

declare global {
  interface Window {
    __CHAINSCOPE__?: Boot;
  }
}

export function boot(): Boot {
  if (typeof window !== "undefined" && window.__CHAINSCOPE__) {
    return window.__CHAINSCOPE__;
  }
  return {
    token: process.env.NEXT_PUBLIC_CHAINSCOPE_TOKEN ?? "",
    store: "",
    writable: false,
  };
}

/** Where the API lives. Same origin when the Python server serves this. */
function base(): string {
  if (typeof window === "undefined") return "";
  return process.env.NEXT_PUBLIC_CHAINSCOPE_API ?? window.location.origin;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/**
 * One request.
 *
 * Failures carry the server's own message rather than a generic one. Those
 * messages are written to say what the tool could not do and why — "no store
 * at …", "this source covers Ethereum only" — and replacing them with "request
 * failed" throws away the only part a reader can act on.
 */
export async function api<T>(
  path: string,
  params: Record<string, string | number | undefined> = {},
): Promise<T> {
  const url = new URL(path, base() || "http://127.0.0.1:8899");
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  const reply = await fetch(url, {
    headers: {
      accept: "application/json",
      authorization: `Bearer ${boot().token}`,
    },
  });
  const body = await reply.json().catch(() => ({}) as Record<string, unknown>);
  if (!reply.ok) {
    throw new ApiError(
      (body as { error?: string }).error ?? `HTTP ${reply.status}`,
      reply.status,
    );
  }
  return body as T;
}

export async function post<T>(path: string, body: unknown): Promise<T> {
  const reply = await fetch(new URL(path, base() || "http://127.0.0.1:8899"), {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${boot().token}`,
    },
    body: JSON.stringify(body),
  });
  const parsed = await reply.json().catch(() => ({}) as Record<string, unknown>);
  if (!reply.ok) {
    throw new ApiError(
      (parsed as { error?: string }).error ?? `HTTP ${reply.status}`,
      reply.status,
    );
  }
  return parsed as T;
}

// ---------------------------------------------------------------- shapes
//
// Typed against what the server actually returns. The fields that describe the
// *completeness* of an answer are not optional decoration — `frontier`,
// `truncated`, `reliable` and `filtered_out` are the difference between a
// result and an absence, so they are on the types and the components are
// required to deal with them.

export type GraphNode = {
  address: string;
  as_written: string;
  depth: number;
  seed: boolean;
  /** Its counterparties were never fetched. Not the end of the money. */
  frontier: boolean;
  label: string;
  category: string;
};

export type GraphEdge = {
  source: string;
  target: string;
  symbol: string;
  asset: string;
  decimals: number;
  /** A string. Wei-scale values exceed what a JSON number holds exactly. */
  total_raw: string;
  transfers: number;
  first_seen: number | null;
  last_seen: number | null;
};

export type Asset = {
  symbol: string;
  asset: string;
  transfers: number;
  verdict: string;
  why: string;
};

export type GraphReply = {
  seed: string;
  chain: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  assets: Asset[];
  /** A limit stopped the walk. The picture is a prefix. */
  truncated: boolean;
  fetched?: number;
};

export type Claim = {
  label: string;
  category: string;
  confidence: string;
  confidence_value: number;
  method: string;
  source: string;
  rationale: string;
  chain: string | null;
};

export type ResolveReply = {
  address: string;
  key: string;
  chain: string | null;
  claims: Claim[];
  /** Named sources that could not be consulted. Empty is not "nothing known". */
  unreachable_sources: string[];
  reliable: boolean;
  note: string;
};

export type ExpandReply = {
  address: string;
  directions: string[];
  fetched: number;
  complete: boolean;
  new_addresses: string[];
  kept: number;
  filtered_out: number;
  truncated: boolean;
  applied: Record<string, unknown>;
};

export type AskReply = {
  question: string;
  endpoint: string;
  params: Record<string, string>;
  reading: string;
  caveat: string;
  ignored: string[];
};
