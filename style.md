# Standard Web Theme Spec — Rob Sterba

This document is the single source of truth for the look, feel, and style of
**all** web applications (docker-update-checker, taskd, and future apps).
It defines design tokens, base element styles, universal components, app
chrome, density profiles, and reusable patterns. Copy the code blocks into
each app's stylesheet (or import a shared stylesheet built from them) and
consume the CSS variables — **never hard-code hex values in app code**.

Derived from the taskd application, generalized for reuse. Apps may differ
in information density but never in color, theming, chrome, or component
recipes.

---

## Table of contents

1. [Brand palette](#1-brand-palette)
2. [Design tokens](#2-design-tokens)
3. [Base element styles](#3-base-element-styles)
4. [Core components](#4-core-components)
5. [Data display components](#5-data-display-components)
6. [Overlays and feedback](#6-overlays-and-feedback)
7. [App chrome: header and version](#7-app-chrome-header-and-version)
8. [Density profiles](#8-density-profiles)
9. [Patterns](#9-patterns)
10. [Theme switching](#10-theme-switching)
11. [Adoption checklist](#11-adoption-checklist)

---

## 1. Brand palette

Five core colors; everything else is derived.

| Hex       | Name        | Role                                              |
|-----------|-------------|---------------------------------------------------|
| `#E7ECEF` | Platinum    | Day page background / night text                   |
| `#274C77` | Dusk Blue   | Night page background / day primary accent & text  |
| `#6096BA` | Steel Blue  | Secondary accent, borders, hover                   |
| `#A3CEF1` | Icy Blue    | Highlights, night accent fills (with dusk-blue text) |
| `#8B8C89` | Grey Olive  | Muted/secondary text (day theme)                  |

Design language: cool, calm, blue-forward. The day theme is a platinum page
with dusk-blue text and primary accents. The night theme inverts to a
dusk-blue page with platinum text and icy-blue accent fills carrying
dusk-blue text (inverted accent). The AMOLED theme keeps the brand identity
on a true-black page: same icy-blue accents and brightened status neutrals,
near-black surfaces. Status colors are desaturated neutrals — never fully
saturated web-blue/green/red.

Every app ships **three themes**: light, dark (dusk blue), and amoled
(true black). See [Theme switching](#10-theme-switching).

---

## 2. Design tokens

Usage: import these blocks into each app's global stylesheet, set
`data-theme="light"`, `"dark"`, or `"amoled"` on `<html>`. Components consume
variables only.

### Light / day theme (default)

```css
:root,
[data-theme="light"] {
  color-scheme: light;

  /* Surfaces */
  --bg-page:        #E7ECEF;  /* platinum page background */
  --bg-surface:     #FFFFFF;  /* cards, panels (near-neutral, derived) */
  --bg-surface-alt: #F5F8FA;  /* subtle raised areas, inputs */
  --bg-inset:       #D3DCE3;  /* wells, code blocks, pressed states */

  /* Text */
  --text-primary:   #274C77;
  --text-secondary: #8B8C89;
  --text-disabled:  #B4B7B3;
  --text-inverse:   #E7ECEF;  /* text on dark fills */

  /* Brand / accents */
  --accent:         #274C77;  /* primary buttons, links, active nav */
  --accent-hover:   #6096BA;
  --accent-pressed: #1F3D5E;
  --accent-border:  #6096BA;
  --on-accent:      #E7ECEF;

  /* Highlights */
  --highlight:      #A3CEF1;  /* selected rows, info chips */
  --highlight-soft: #D9EAF8;

  /* Lines & dividers */
  --border-default: #6096BA;
  --border-subtle:  #C5CDD4;

  /* Status (derived harmonized neutrals) */
  --success:        #4C6B57;
  --warning:        #8A6D3B;
  --danger:         #8C3A3A;
  --info:           #6096BA;

  /* Elevation & focus */
  --shadow-sm: 0 1px 2px rgba(39, 76, 119, 0.15);
  --shadow-md: 0 4px 12px rgba(39, 76, 119, 0.18);
  --shadow-lg: 0 12px 32px rgba(39, 76, 119, 0.22);
  --focus-ring: 0 0 0 3px rgba(96, 150, 186, 0.50);
}
```

### Dark / night theme (dusk blue)

```css
[data-theme="dark"] {
  color-scheme: dark;

  /* Surfaces */
  --bg-page:        #274C77;  /* dusk blue page background */
  --bg-surface:     #2F587F;  /* cards, panels (derived raised tone) */
  --bg-surface-alt: #37628C;  /* subtle raised areas, inputs */
  --bg-inset:       #1F3A5C;  /* wells, code blocks, pressed states */

  /* Text */
  --text-primary:   #E7ECEF;
  --text-secondary: #A3CEF1;
  --text-disabled:  #7E93AA;
  --text-inverse:   #274C77;  /* text on light fills */

  /* Brand / accents (accent inverts: light fill, dark text) */
  --accent:         #A3CEF1;
  --accent-hover:   #C6E2F7;
  --accent-pressed: #8FBDE4;
  --accent-border:  #6096BA;
  --on-accent:      #274C77;

  /* Highlights */
  --highlight:      #A3CEF1;
  --highlight-soft: #3F6D97;

  /* Lines & dividers */
  --border-default: #6096BA;
  --border-subtle:  #46688B;

  /* Status (brightened for dark backgrounds) */
  --success:        #7FA891;
  --warning:        #C9A56A;
  --danger:         #C97A7A;
  --info:           #A3CEF1;

  /* Elevation & focus */
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.30);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.35);
  --shadow-lg: 0 12px 32px rgba(0, 0, 0, 0.45);
  --focus-ring: 0 0 0 3px rgba(163, 206, 241, 0.40);
}
```

### AMOLED / true black theme

True black page so OLED pixels switch off; surfaces stay near-black to keep
card edges readable. Brand identity is preserved through the icy-blue accents
and the brightened status neutrals shared with the dusk-blue dark theme.

```css
[data-theme="amoled"] {
  color-scheme: dark;

  /* Surfaces */
  --bg-page:        #000000;
  --bg-surface:     #0A0C0E;
  --bg-surface-alt: #14181C;
  --bg-inset:       #050607;

  /* Text */
  --text-primary:   #E7ECEF;
  --text-secondary: #A3CEF1;
  --text-disabled:  #5E6B76;
  --text-inverse:   #000000;  /* text on light fills */

  /* Brand / accents (icy blue fill, black text) */
  --accent:         #A3CEF1;
  --accent-hover:   #C6E2F7;
  --accent-pressed: #8FBDE4;
  --accent-border:  #6096BA;
  --on-accent:      #000000;

  /* Highlights */
  --highlight:      #A3CEF1;
  --highlight-soft: #16222E;

  /* Lines & dividers */
  --border-default: #3D4C5A;
  --border-subtle:  #1C242C;

  /* Status (same brightened neutrals as the dusk-blue dark theme) */
  --success:        #7FA891;
  --warning:        #C9A56A;
  --danger:         #C97A7A;
  --info:           #A3CEF1;

  /* Elevation & focus */
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.60);
  --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.70);
  --shadow-lg: 0 12px 32px rgba(0, 0, 0, 0.85);
  --focus-ring: 0 0 0 3px rgba(163, 206, 241, 0.40);
}
```

---

## 3. Base element styles

Apply everywhere, before any component styles.

```css
* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg-page);
  color: var(--text-primary);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  transition: background-color 0.25s ease, color 0.25s ease;
}

h1, h2, h3, h4 { color: var(--text-primary); line-height: 1.25; }
a { color: var(--accent); text-decoration: underline; text-underline-offset: 2px; }
a:hover { color: var(--accent-hover); }

:focus-visible { outline: none; box-shadow: var(--focus-ring); }

hr, .divider { border: none; border-top: 1px solid var(--border-subtle); }

code, pre {
  background: var(--bg-inset);
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
code { padding: 0.1rem 0.35rem; }
pre { padding: 1rem; overflow-x: auto; }
```

---

## 4. Core components

Every app has these; style them identically.

### 4.1 Cards and panels

```css
.card, .panel {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
}
```

### 4.2 Buttons

Primary button (accent fill) and secondary button (outline). Interactive
buttons press down 1px on `:active`.

```css
.btn {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.55rem 1.1rem;
  border-radius: 6px;
  border: 1px solid var(--accent-border);
  background: var(--accent);
  color: var(--on-accent);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
  transition: background-color 0.15s ease, transform 0.05s ease;
}
.btn:hover  { background: var(--accent-hover); }
.btn:active { background: var(--accent-pressed); transform: translateY(1px); }

.btn-secondary {
  background: transparent;
  color: var(--text-primary);
  border: 1px solid var(--border-default);
  font-weight: 500;
}
.btn-secondary:hover { background: var(--bg-surface-alt); color: var(--text-primary); }

button:disabled { cursor: not-allowed; opacity: 0.5; }
```

Rule: default `button` elements get the secondary (outline) treatment;
reserve the accent fill for the one primary action per view.

### 4.3 Form inputs

```css
input, select, textarea {
  background: var(--bg-surface-alt);
  color: var(--text-primary);
  border: 1px solid var(--border-default);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  font: inherit;
}
```

---

## 5. Data display components

Components for showing lists, records, and user-generated content.

### 5.1 Badges

Small pill labels for status, priority, severity, or category. Use the tinted
badge recipe: `color-mix` the status color into a 15-22% transparent tint,
with the full status color as text.

```css
.badge {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 4px 10px;
  border-radius: 12px;
  font-size: 0.8rem;
  font-weight: 500;
  text-transform: uppercase;
}

/* Status tints */
.badge.todo, .badge.info, .badge.in_progress {
  background: color-mix(in srgb, var(--info) 15%, transparent);
  color: var(--info);
}
.badge.done, .badge.success, .badge.ok {
  background: color-mix(in srgb, var(--success) 15%, transparent);
  color: var(--success);
}
.badge.archived, .badge.muted {
  background: var(--bg-surface-alt);
  color: var(--text-secondary);
}

/* Severity / priority tints */
.badge.low    { background: color-mix(in srgb, var(--success) 18%, transparent); color: var(--success); }
.badge.medium { background: color-mix(in srgb, var(--warning) 18%, transparent); color: var(--warning); }
.badge.high   { background: color-mix(in srgb, var(--danger) 18%, transparent);  color: color-mix(in srgb, var(--danger) 75%, var(--text-primary)); }
.badge.urgent, .badge.critical {
  background: color-mix(in srgb, var(--danger) 22%, transparent);
  color: var(--danger);
}
```

Notes:
- Larger badges (detail views) scale up: `padding: 6px 12px;
  border-radius: 16px; font-size: 0.85rem;` and drop `text-transform`.
- Completed items may add `text-decoration: line-through`.

### 5.2 Chips and tags

Neutral pill for a keyword/tag, clickable to filter or toggle. Hover inverts
to accent fill.

```css
.chip {
  background: var(--bg-surface-alt);
  color: var(--text-primary);
  padding: 4px 10px;
  border-radius: 12px;
  font-size: 0.8rem;
  cursor: pointer;
  transition: all 0.2s ease;
}
.chip:hover { background: var(--accent); color: var(--on-accent); }

/* Non-interactive variant */
.chip-static { cursor: default; }
```

### 5.3 Meta grid (definition rows)

Label/value rows for record metadata, stacked or wrapped. Label in secondary
text, value in primary text, on a raised surface.

```css
.meta-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  padding: 16px;
  background: var(--bg-surface-alt);
  border-radius: 8px;
}

.meta-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: var(--bg-surface-alt);
  border-radius: 8px;
}
.meta-item label { font-size: 0.85rem; color: var(--text-secondary); }
.meta-item span  { font-size: 0.85rem; color: var(--text-primary); }
```

### 5.4 Tables

Tables use the inset well treatment for contrast against page and card
backgrounds.

```css
table {
  width: 100%;
  border-collapse: collapse;
  background: var(--bg-surface);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}
th, td {
  padding: 10px 16px;
  text-align: left;
  border-bottom: 1px solid var(--border-subtle);
}
th {
  font-size: 0.8rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--text-secondary);
  background: var(--bg-surface-alt);
}
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: var(--bg-surface-alt); }
```

### 5.5 Stat blocks

Compact key/number summaries; centered, secondary text, divider-attached.

```css
.stats {
  text-align: center;
  padding: 16px;
  color: var(--text-secondary);
  font-size: 0.9rem;
  border-top: 1px solid var(--border-subtle);
}
.stats strong { color: var(--text-primary); font-weight: 600; }
```

### 5.6 Markdown-rendered content

User-authored rich text (descriptions, notes, docs) inside cards.

```css
.markdown { white-space: pre-wrap; word-wrap: break-word; }
.markdown p:not(:last-child) { margin-bottom: 1rem; }
.markdown h1, .markdown h2, .markdown h3,
.markdown h4, .markdown h5, .markdown h6 {
  margin-top: 1.5rem;
  margin-bottom: 1rem;
}
.markdown ul, .markdown ol { margin-bottom: 1rem; padding-left: 2rem; }
.markdown li { margin-bottom: 0.5rem; }
.markdown a { color: var(--accent); text-decoration: underline; }
.markdown blockquote {
  border-left: 3px solid var(--border-default);
  margin: 0 0 1rem;
  padding: 0.25rem 1rem;
  color: var(--text-secondary);
}
```

### 5.7 Timestamps and dates

```css
.timestamp { color: var(--text-secondary); }
.timestamp.overdue { color: var(--danger); font-weight: 600; }
```

---

## 6. Overlays and feedback

Standard recipes for modals, dropdown menus, toasts, and progress. Used by
dashboard-class apps; adopt as needed, do not reinvent.

### 6.1 Modal

```css
.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex; align-items: center; justify-content: center;
  z-index: 500; opacity: 0; pointer-events: none;
  transition: opacity 0.2s;
}
.modal-backdrop.open { opacity: 1; pointer-events: all; }
.modal {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 1.5rem;
  max-width: 760px; width: 92%;
  max-height: 84vh; overflow-y: auto;
  transform: translateY(12px);
  transition: transform 0.2s;
}
.modal-backdrop.open .modal { transform: none; }
.modal-title { font-weight: 700; font-size: 1rem; margin-bottom: 1rem; }
```

### 6.2 Dropdown menu

```css
.dropdown { position: relative; display: inline-block; }
.dropdown-menu {
  position: absolute; top: 100%; right: 0; margin-top: 0.25rem;
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  box-shadow: var(--shadow-md);
  z-index: 50; min-width: 160px; overflow: hidden;
}
.dropdown-menu-item {
  display: block; width: 100%; text-align: left;
  padding: 0.35rem 0.65rem; font-size: 0.75rem;
  color: var(--text-primary); background: none; border: none;
  cursor: pointer; transition: background 0.12s;
  border-bottom: 1px solid var(--border-subtle);
}
.dropdown-menu-item:last-child { border-bottom: none; }
.dropdown-menu-item:hover { background: var(--bg-surface-alt); }
```

### 6.3 Toast / notification

```css
.notification {
  position: fixed; top: 12px; right: 12px;
  padding: 0.65rem 0.85rem; border-radius: 6px;
  box-shadow: var(--shadow-lg); font-size: 0.85rem;
  display: none; align-items: center; gap: 0.4rem; z-index: 900;
}
.notification.show { display: flex; }
.notification.info {
  background: var(--bg-surface); color: var(--text-primary);
  border: 1px solid var(--border-subtle);
}
.notification.success {
  background: color-mix(in srgb, var(--info) 15%, transparent);
  color: var(--info);
  border: 1px solid color-mix(in srgb, var(--info) 35%, transparent);
}
```

### 6.4 Progress and resource bars

```css
.progress {
  height: 0.45rem;
  background: var(--bg-page);
  border-radius: 999px; overflow: hidden;
  border: 1px solid var(--border-subtle);
}
.progress-bar { height: 100%; background: var(--accent); transition: width 0.2s ease; }
```

---

## 7. App chrome: header and version

Every app shares the same header grammar and version presentation.

### 7.1 Header layout

Sticky bar on the card surface, app title on the left, actions on the right.

```
header (sticky, bg-surface, border-bottom subtle, z-index 100)
├── .header-left
│   ├── logo / app title (clickable -> home)
│   └── .version-badge      v{version}, immediately right of the title
└── .header-actions (margin-left: auto)
    ├── app controls (settings, filters, status)
    └── .theme-toggle      light <-> dark toggle, inline SVG icon
```

```css
.app-header {
  background: var(--bg-surface);
  border-bottom: 1px solid var(--border-subtle);
  box-shadow: var(--shadow-sm);
  position: sticky; top: 0; z-index: 100;
  display: flex; align-items: center; justify-content: space-between;
}
.header-left { display: flex; align-items: center; gap: 12px; }
.header-actions { display: flex; align-items: center; gap: 12px; }
```

The version badge always sits in `header-left`, immediately to the right of
the app title — never buried in the actions cluster. This is taskd's
placement and is the standard for all apps.

### 7.2 Version badge styling

```css
.version-badge {
  font-size: 0.85rem;
  color: var(--text-secondary);
  padding: 4px 8px;
  background: var(--bg-surface-alt);
  border-radius: 4px;
}
```

### 7.3 Version system

All apps use the same version pipeline:

1. **Source of truth**: a single-line semver `VERSION` file at the repo root,
   no `v` prefix in the file (e.g. `1.5.0`). Git release tags use the `v`
   prefix (`v1.5.0`). Never duplicate the version string anywhere else.
2. **Backend**: reads `VERSION` at startup and exposes
   `GET /api/version` returning `{"version": "1.5.0"}`. Return a sane
   fallback (e.g. `1.0.0` or empty) if the file is missing.
3. **Frontend**: fetches the version on load and renders `v{version}` in the
   `.version-badge` next to the app title. If the fetch fails, hide the
   badge rather than show a placeholder.

---

## 8. Density profiles

Apps differ in how much information they show; they never differ in color,
radius, typography stacks, or motion. Two density profiles exist. Pick one
per app and apply it consistently.

| Property            | Comfortable (default) | Compact (dashboard)      |
|---------------------|-----------------------|-------------------------|
| Use when            | Content apps (taskd)  | Dense data screens (DUC) |
| `main` max-width    | 1200px                | 1280px                  |
| `main` padding      | 16px                  | 8px (0.5rem)            |
| Card padding        | 16px                  | 10px (0.65rem)          |
| Card/list gaps      | 12-20px               | 8px (0.5rem)            |
| Table cells         | 10px 16px             | 6px 8px (0.4rem 0.5rem) |
| Table header type   | 0.8rem                | 0.72rem                 |
| Meta/secondary text | 0.85rem               | 0.7-0.78rem             |
| Buttons             | 0.55rem 1.1rem        | 0.3rem 0.7rem (sm: 0.25rem 0.5rem) |
| Header padding      | 16px                  | 12px 24px               |
| Badge font size     | 0.8rem                | 0.7rem                  |

Rules:

- The comfortable profile is the default; adopt compact only when a screen
  must show many rows or tiles at once (monitoring, registries, logs).
- Density changes spacing and type scale only. Colors, radii, shadows,
  focus rings, and transition timings are identical across profiles.
- One app uses one profile. Do not mix profiles within an app except for
  deliberate data-dense sub-views (tables inside modals, log panels).

---

## 9. Patterns

Generalized recipes. Adapt the class names to the app; keep the structure
and token usage.

### 9.1 Color-coded item card

Any card in a list whose category is signaled by a colored left stripe. The
stripe is 4px wide, set via `border-left` on the card, using status colors.

```css
.item-card {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-left: 4px solid var(--border-default);
  border-radius: 8px;
  box-shadow: var(--shadow-sm);
  padding: 16px;
  cursor: pointer;
  transition: box-shadow 0.15s ease, transform 0.15s ease;
}
.item-card:hover { box-shadow: var(--shadow-md); }

/* Severity mapping (priority, health, urgency...) */
.item-card.urgent  { border-left-color: var(--danger); }
.item-card.high    { border-left-color: color-mix(in srgb, var(--danger) 70%, var(--text-primary)); }
.item-card.medium  { border-left-color: var(--warning); }
.item-card.low     { border-left-color: var(--success); }
```

### 9.2 Tinted status badge

Covered in [5.1](#51-badges); the recipe itself:
`background: color-mix(in srgb, var(--status-token) 15-22%, transparent);
color: var(--status-token);`. Use one consistent tint strength within an app
(15% for informational, 18-22% for severity).

### 9.3 Item structure inside cards

Cards containing a selectable/inspectable item follow this layout grammar:

```
.item-card
├── .item-header        row: checkbox or icon, name (bold), badges on the right
├── .item-meta          row: chips, timestamps — small, secondary color
└── .item-section       optional nested content, separated by
                        border-top: 1px dashed var(--border-subtle)
```

- Done/completed items: `text-decoration: line-through` on the name, value in
  `var(--text-secondary)`.
- IDs and technical values render in the monospace stack
  (`ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`) at `0.8rem`.

### 9.4 Motion

Keep animations short (0.05-0.25s ease) and functional only: hover/press
feedback, theme transition on body, and `slideIn` for newly appearing cards.

```css
@keyframes slideIn {
  from { transform: translateY(-10px); opacity: 0; }
  to   { transform: translateY(0); opacity: 1; }
}
```

---

## 10. Theme switching

Every app ships three themes and the same interaction model (taskd's model):

- `data-theme` values: `light`, `dark` (dusk blue), `amoled` (true black).
- The **header toggle flips between light and the user's preferred dark
  variant only** — it never cycles dark <-> amoled directly.
- The preferred dark variant (Blue (default) / AMOLED Black) is chosen in
  app settings and applies immediately if the app is currently in dark mode.
- Resolution order on first load: saved `theme` in localStorage, then OS
  preference (mapped to the preferred dark variant), then light.
- The theme persists in localStorage under the key `theme`. Apps served from
  a shared origin may namespace it (`<app>-theme`).

```js
// Theme state: "light" | "dark" | "amoled"
// Settings carry the preferred dark variant: "dark" | "amoled"

// Initial load
const saved = localStorage.getItem("theme");
const theme = saved
  ?? (matchMedia("(prefers-color-scheme: dark)").matches ? preferredDark : "light");
document.documentElement.dataset.theme = theme;
localStorage.setItem("theme", theme);

// Header toggle: light <-> preferred dark variant
const next = document.documentElement.dataset.theme === "light"
  ? preferredDark
  : "light";
document.documentElement.dataset.theme = next;
localStorage.setItem("theme", next);

// Changing the preferred dark variant while in a dark theme switches to it
```

Theme toggle button styling (outline button, no text weight emphasis):

```css
.theme-toggle {
  background: transparent;
  border: 1px solid var(--border-default);
  border-radius: 6px;
  padding: 6px 12px;
  font-size: 1rem;
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  transition: background-color 0.15s ease, border-color 0.15s ease;
}
.theme-toggle:hover {
  background: var(--bg-surface-alt);
  border-color: var(--accent-border);
}
```

Toggle icons are **inline SVG**, never emoji: a sun glyph while in a light
theme (offers dark), a moon glyph while in a dark theme (offers light).

```html
<!-- Sun (shown while in a light theme) -->
<svg width="16" height="16" viewBox="0 0 24 24" fill="none"
     stroke="currentColor" stroke-width="2" stroke-linecap="round">
  <circle cx="12" cy="12" r="5"/>
  <path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
</svg>

<!-- Moon (shown while in a dark or AMOLED theme) -->
<svg width="16" height="16" viewBox="0 0 24 24" fill="none"
     stroke="currentColor" stroke-width="2" stroke-linecap="round"
     stroke-linejoin="round">
  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
</svg>
```

---

## 11. Adoption checklist

For an app to conform to this spec:

- [ ] Includes all three token blocks (light + dark + amoled) verbatim.
- [ ] Includes base element styles.
- [ ] Supports all three themes with `data-theme` switching and
      `localStorage` persistence; header toggle flips light <-> preferred
      dark variant; preferred dark variant is selectable in settings.
- [ ] No hard-coded hex values outside the token blocks.
- [ ] Header follows the standard grammar: app title + version badge in
      `header-left`, actions right-aligned.
- [ ] Version follows the standard pipeline: root `VERSION` file,
      `GET /api/version`, `v{version}` badge beside the app title.
- [ ] Theme toggle uses inline SVG sun/moon icons, never emoji.
- [ ] Declares one density profile (comfortable or compact) and applies it
      consistently; density never changes colors, radii, or motion.
- [ ] Cards use 8px radius, subtle border, `--shadow-sm`.
- [ ] Buttons use 6px radius; default buttons are outline, primary action is
      accent fill; `:active` translates 1px down.
- [ ] Badges and chips use 12px radius pills with tinted `color-mix`
      backgrounds.
- [ ] Status/severity coloring maps to `--success` / `--warning` / `--danger`
      / `--info` only.
- [ ] Chips with user-selected fill colors pick text by WCAG luminance
      (black/white), never by theme.
- [ ] Page-level class names are unique or scoped — bundled SPA stylesheets
      are global, and a shared name lets one page silently restyle another.
- [ ] Monospace stack for IDs, code, and technical values.
- [ ] Interactive transitions stay within 0.05-0.25s.
