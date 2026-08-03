/**
 * The shell every page sits in.
 *
 * Fonts are self-hosted by `next/font`, not fetched from Google at runtime.
 * That is not a performance choice — a forensics tool that pulls a font from a
 * third party on load tells that third party that somebody opened it, and the
 * referrer says which case. The rest of this package refuses to phone out; the
 * web front end does not get an exemption for looking nicer.
 */

import type { Metadata } from "next";
import { Archivo, JetBrains_Mono, Space_Grotesk } from "next/font/google";

import "./globals.css";
import { Nav } from "@/components/nav";

const sans = Space_Grotesk({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-sans",
});
const disp = Archivo({
  subsets: ["latin"],
  display: "swap",
  weight: ["400", "700"],
  variable: "--font-disp",
});
const mono = JetBrains_Mono({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "chainscope — blockchain forensics that keeps its uncertainty",
  description:
    "Every claim carries where it came from and how sure it is. Every absence " +
    "says whether it is an absence of evidence or an absence of looking.",
};

/**
 * Paint the theme before first paint.
 *
 * Inline and blocking on purpose: a `useEffect` toggle runs after the first
 * frame, so a reader who chose dark gets a white flash on every navigation.
 * Wrapped in try/catch because a browser with storage disabled must still
 * render the page rather than throwing on load.
 */
const THEME_BOOT = `
try {
  var saved = localStorage.getItem("cs-theme");
  var dark = saved ? saved === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  if (dark) document.documentElement.classList.add("theme-dark");
} catch (e) {}
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${disp.variable} ${mono.variable}`}>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT }} />
      </head>
      <body>
        <Nav />
        {children}
      </body>
    </html>
  );
}
