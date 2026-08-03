"use client";

/**
 * The bar, and the one control in it.
 *
 * The theme toggle writes to `localStorage` and flips a class on `<html>`, the
 * same class `layout.tsx` pre-paints from. One switch, one storage key, one
 * source of truth — a second place that decides the theme is how a page ends up
 * disagreeing with itself mid-navigation.
 */

import Link from "next/link";
import { useEffect, useState } from "react";

export function Nav() {
  // Starts null rather than false: on the server nobody knows the reader's
  // choice, and rendering "dark" before hydration would flash the wrong label
  // on the button even though the page itself is already correct.
  const [dark, setDark] = useState<boolean | null>(null);

  useEffect(() => {
    setDark(document.documentElement.classList.contains("theme-dark"));
  }, []);

  function toggle() {
    const next = !document.documentElement.classList.contains("theme-dark");
    document.documentElement.classList.toggle("theme-dark", next);
    try {
      localStorage.setItem("cs-theme", next ? "dark" : "light");
    } catch {
      // A reader with storage blocked still gets the toggle for this session.
      // Failing to persist is not a reason to fail to switch.
    }
    setDark(next);
  }

  return (
    <nav className="nav">
      <div className="nav-inner">
        <Link href="/" className="nav-mark">
          chainscope
        </Link>
        <div className="nav-links">
          <Link href="/docs">docs</Link>
          <Link href="/docs#endpoints">api</Link>
          <Link href="/docs#agents">agents</Link>
          <a href="https://github.com/chainscope/chainscope">source</a>
          <button
            className="nav-toggle"
            onClick={toggle}
            aria-label="switch theme"
            suppressHydrationWarning
          >
            {dark === null ? "theme" : dark ? "light" : "dark"}
          </button>
        </div>
      </div>
    </nav>
  );
}
