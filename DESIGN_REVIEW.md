# Design review

Screenshots in `docs/screenshots/` are taken by Playwright (`make e2e`) from the
real API after a real 20-seed benchmark (360 stored runs). No fixture data.
Each screen was captured in dark and light and reviewed against spec section 4.

## Round 1 findings and fixes

| # | Screen | Finding | Fix |
|---|---|---|---|
| 1 | Benchmark | Charts encoded outcome by colour only; no legend. | Added a legend row using the same icon + label badges as the tables. |
| 2 | Scenarios | Detail pane was a single narrow column with a large empty area. | Added a second column: the latest benchmark batch for the selected fault (real counts, each row links to its runs) and the workflow steps the fault can hit. |
| 3 | All | Page titles were bare nouns ("Runs", "Benchmark"). | Titles now say what the screen holds, e.g. "Benchmark: fault type × controls config". |
| 4 | Run detail | "Run started" row dumped the order as raw JSON and wrapped badly. | Rendered as a sentence: quantity × SKU at price for customer. |
| 5 | Benchmark | Chart y-axis reserved 230px, squeezing the bars. | Reduced to 200px. |
| 6 | Tokens | Dark-theme violation red was below 4.5:1 on the raised surface (caught by the contrast test). | Lightened `--violation` in the dark theme. |
| 7 | Run detail | Timeline grid used an arbitrary Tailwind value (caught by the design lint). | Moved to a named grid token in `tailwind.config.ts`. |
| 8 | Scenarios | Evidence table overflowed its column at 1440px. | Fixed table layout with wrapping outcome labels. |

## Checks that are automated

- `web/scripts/lint-design.mjs` fails the build on gradients, blur, radius above
  6px, heavy shadows, indigo/violet/purple/fuchsia classes, emoji, arbitrary
  Tailwind values, long or bouncy animation, and raw hex outside `tokens.css`.
- `web/src/lib/tokens.test.ts` computes WCAG contrast for every text and outcome
  colour on every background, in both themes, and requires 4.5:1.
- `OutcomeBadge.test.tsx` requires every outcome to have its own icon and label.
- The Tailwind theme is replaced rather than extended, so default palettes,
  large radii and shadows do not exist as classes.

## Per-screen verdict: would a senior SRE trust this?

- **Run detail (hero):** yes. Three lanes share one time axis; the fault row is
  marked and says what the agent was told and what the databases show; the
  verdict states what the agent reported against what is true.
- **Benchmark:** yes. A plain matrix of counts with the denominator in every
  cell, a delta table, and charts whose titles are the question they answer.
- **Runs:** yes. Dense 32px rows, sortable, filter state in the URL.
- **Scenarios:** yes after fix 2.

## Known gaps (not fixed)

- Keyboard shortcuts (j/k, enter, r, /, ?) are P2 and not built. Focus rings and
  tab order work.
- Nav has three items. Invariants, Providers and Settings are P2 screens.
- On the Scenarios evidence table the longest config name (`idempotency_client_only`)
  touches the next column at 1440px.
- Screens are designed for 1280px and wider only, as specified.
