import type { NextConfig } from "next";

/**
 * The marketing and documentation pages are fully static: they describe the
 * tool, and none of them touch a case. `output: "export"` makes that structural
 * rather than a claim — a static export has no server to leak from, and can be
 * served next to the Python API or from anywhere at all.
 *
 * The case view stays where it is, served by the Python process that holds the
 * store. Moving it here would put a build step between an investigator and
 * their own data, and would mean shipping the token to a second origin.
 */
const nextConfig: NextConfig = {
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
};

export default nextConfig;
