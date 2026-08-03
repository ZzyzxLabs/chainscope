/**
 * A four-character spinner for inside a button.
 *
 * Text rather than an SVG or a CSS ring: it inherits the button's colour and
 * font without a second thing to keep in sync, and it survives a copied
 * screenshot, which is how these get reported.
 */
export function Spinner() {
  return <span className="spin" aria-hidden="true" />;
}
