# Frontend Reference

A single-page chat UI lives at [`src/web/index.html`](../src/web/index.html)
and is mounted at `GET /` by the FastAPI app
([`src/main.py`](../src/main.py)). No build step, no framework, no package
manager — one HTML file with inline CSS and inline JS.

## Why a static HTML file

- The SHL evaluator scores the API only. A UI is for humans (demo,
  manual testing, interview walkthrough). It should add zero ops cost.
- A single file means cold-start cost is zero (no asset pipeline) and
  there is nothing to break separately from the backend.
- All ML/agent intelligence is server-side; the UI is a thin shell over
  `POST /chat` + `GET /version`.

## Visual design

The frontend uses a **light, off-white** theme with a **water-blur**
glassmorphism aesthetic.

| Token | Light value | Notes |
|-------|-------------|-------|
| `--bg` | `#f8f5ef` | Warm off-white canvas |
| `--paper` | `#fbf9f4` | Card highlight |
| `--glass` | `rgba(255,253,247,0.55)` | Translucent surface (backdrop-blur) |
| `--glass-strong` | `rgba(255,253,247,0.78)` | Hover/focus state |
| `--accent` | `#0891b2` (cyan-700) | Brand dot, primary button, focus ring |
| `--text` | `#2a2520` | Warm dark gray |

The "water" backdrop is a separate element (`.water`) that drifts on a
28-second `@keyframes drift` animation under all glass surfaces. A
fractal-noise SVG grain overlay reduces gradient banding.

Glassmorphism is achieved with `backdrop-filter: saturate(140%) blur(20px)`
on the header, composer, recommendation cards, and chip backgrounds.
Each glass surface has a 1-pixel inset white highlight on its top edge
to suggest light refraction.

## Layout

```
┌──────────────────────────────────────────────────────────┐
│ header (glass)  brand · model badge                      │
├──────────────────────────────────────────────────────────┤
│ main (scroll)                                            │
│   ┌─ empty state ───────────────────┐                    │
│   │ "Grounded · Catalog-only" badge │                    │
│   │ heading (gradient text)         │                    │
│   │ description                     │                    │
│   │ 4 example-prompt buttons (glass)│                    │
│   └─────────────────────────────────┘                    │
│                                                          │
│   ─ on send: messages stack, then ─                      │
│   ┌─ msg.user ──────────────────────┐                    │
│   │ avatar · You · text             │                    │
│   └─────────────────────────────────┘                    │
│   ┌─ msg.assistant ─────────────────┐                    │
│   │ avatar · Agent · text           │                    │
│   │   (recs grid: chip · name · ↗)  │                    │
│   └─────────────────────────────────┘                    │
├──────────────────────────────────────────────────────────┤
│ composer (glass)                                         │
│   [textarea] [Reset] [Send →]                            │
│   hint: Enter to send · Shift+Enter for newline          │
└──────────────────────────────────────────────────────────┘
```

## JS lifecycle

State lives in three plain variables inside the IIFE:

| State | Purpose |
|-------|---------|
| `history` | Full conversation array sent on every POST /chat |
| `messagesEl` / `inputEl` / `sendBtn` / `resetBtn` | DOM refs |
| `modelLabel` / `emptyState` | Bootstrap-time refs |

Flow:

1. `loadVersion()` runs once at page load and populates the model badge
   from `GET /version`. There's no periodic poll — the badge is static
   for the page lifetime.
2. `send()` reads the textarea, pushes `{role:"user", content}` onto
   `history`, renders the user bubble, renders the typing indicator,
   POSTs the full history to `/chat`, and on response renders the
   assistant bubble plus any recommendation cards. Network errors are
   surfaced as a fake assistant turn with the error text.
3. `reset()` clears the conversation and re-attaches the empty state.

Keyboard: <kbd>Enter</kbd> sends, <kbd>Shift+Enter</kbd> newlines, the
textarea auto-grows to a 200 px max.

## Test-type chips

Each recommendation card shows the test-type letters as small colored
chips. The color palette is in the `--tt-A` … `--tt-S` CSS variables:

| Code | Letter color (light) | Tooltip |
|------|----------------------|---------|
| A | cyan-700 `#0e7490` | Ability & Aptitude |
| B | amber-700 `#b45309` | Biodata & SJT |
| C | violet-700 `#6d28d9` | Competencies |
| D | pink-700 `#be185d` | Development & 360 |
| E | rose-700 `#be123c` | Exercises |
| K | blue-700 `#1d4ed8` | Knowledge & Skills |
| P | purple-700 `#7e22ce` | Personality & Behavior |
| S | emerald-700 `#047857` | Simulations |

The mapping comes from SHL's catalog legend; see
[docs/data.md](data.md) for the full taxonomy.

## CSP — special handling

The HTML page is served with a relaxed Content-Security-Policy
(`default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'
'unsafe-inline'; connect-src 'self'`). All other routes (JSON endpoints)
keep `default-src 'none'` — see `src/main.py::create_app`.

The page uses inline `<style>` and `<script>` tags for self-containment;
`'unsafe-inline'` is acceptable here because the page contains no
user-controlled content (no XSS surface).

## Tests

[`tests/test_web.py`](../tests/test_web.py):

- `GET /` returns 200 with `text/html` and the brand text.
- The HTML references the `/chat` and `/version` endpoints (not /ready,
  which was removed when the status pill was simplified out).
- The CSP is loosened to `default-src 'self'` on the HTML route.
- The CSP on JSON endpoints stays at `default-src 'none'`.
- The four security headers (X-Content-Type-Options, X-Frame-Options,
  Referrer-Policy, Content-Security-Policy) all apply to `/`.

## Accessibility

- All buttons have `aria-label` on icon-only actions.
- The textarea has `aria-label="Message"`.
- Tab order is left-to-right, top-to-bottom; the textarea is the
  default focus on page load.
- Color contrast meets WCAG AA on body text. Decorative gradients are
  marked `aria-hidden="true"`.

## Iterating on the UI

The page is plain HTML — open it in any editor and refresh the browser.

```bash
# 1. Start the server
uvicorn src.main:app --reload --port 8000

# 2. Open the UI
start http://127.0.0.1:8000/   # Windows
# open http://127.0.0.1:8000/  # macOS
```

`--reload` triggers on Python changes; for HTML/CSS/JS edits, just hard-
refresh (Ctrl+Shift+R) the browser tab.

If you want to add a feature, it's almost certainly:
- a new CSS rule in the `<style>` block,
- a new function in the IIFE in the `<script>` block, or
- a new element under `<main>` in the markup.

Keep the page small and self-contained — that's the whole point.
