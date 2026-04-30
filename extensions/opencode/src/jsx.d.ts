/**
 * Augment the JSX namespace with @opentui/solid's intrinsic elements.
 *
 * tsconfig sets `jsxImportSource: "solid-js"` — we use solid-js's JSX
 * runtime (the real implementation) but render through @opentui/solid's
 * element bindings (box / text / span / ascii_font / etc., not DOM).
 * solid-js's default JSX.IntrinsicElements is DOM-shaped, which makes
 * `<box>` and `<text fg="…">` type-error. This module augmentation
 * pulls @opentui/solid's intrinsic-element shape into solid-js's
 * namespace so TS resolves the right props.
 *
 * The runtime is unaffected: opencode's TUI host wires opentui's
 * SolidJS renderer at TUI launch, so the JSX nodes are interpreted as
 * opentui Renderables when opencode invokes our slot functions.
 */

import type { JSX as OpenTuiJSX } from "@opentui/solid/jsx-runtime";

declare module "solid-js" {
  namespace JSX {
    interface IntrinsicElements extends OpenTuiJSX.IntrinsicElements {}
  }
}
