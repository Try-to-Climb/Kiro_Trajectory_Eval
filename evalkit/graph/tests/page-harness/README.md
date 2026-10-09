# page-harness - run the generated page in a real DOM

`graph/html.py` emits a self-contained single-file page with zero
dependencies. **This directory does not change that** -- it is only a sandbox
for testing.

## Why it exists

The machine that builds this page has no JS engine on PATH, which let three
bugs ship undetected; each one was first spotted by a user opening the page:

| Bug | Symptom |
|---|---|
| Python's `"\n"` written as a literal newline | Template string broke -> script syntax error -> blank page |
| `return` inside a `for..of` | Exited at the first action -> every subsequent node went undrawn |
| Assignment placed above its own `const` | Temporal-dead-zone reference error -> blank page |

`graph/tests/test_html.py::TestScriptSanity` covers these three patterns with
static checks, but static checks only cover known shapes. This harness
actually executes the page.

## Install

```bash
module load cdt2/node/2.6.4      # the local node binary is 6.10 -- see limitations below
cd evalkit/graph/tests/page-harness && npm install
```

## Run

```bash
python3 -m graph.cli <session> --official-dir <dir> --format html > /tmp/p.html
node run_page.js /tmp/p.html
```

Or just run unittest (runs when installed, skipped otherwise):

```bash
python3 -m unittest graph.tests.test_page_runtime
```

## What it checks

1. The page loads with no script errors.
2. The first view actually draws nodes and edges.
3. **Switch through every view** -- changing views redraws the whole thing, so
   "view 1 is fine" says nothing about view 37.
4. Clicking a turn card navigates (breadcrumb gains `>`).
5. Clicking a file populates the right-hand panel.

## Limitations (honest record)

- **node 6.10.2** (2017), what `module load cdt2/node` provides. No
  `padStart`/`padEnd` (node 8), no `async/await` (node 7.6), no object
  spread (node 8.3). The harness itself observes this line, and so must the
  page script -- it has to pass `node --check`.
- **jsdom 9.12** (2016); no `scrollIntoView`, stubbed inside the harness.
  jsdom also does no layout, so `getBoundingClientRect()` always returns 0 --
  `fitView()` short-circuits early, so **the zoom-and-centre path is not tested**.
- Visuals are not tested. Whether the graph looks right still requires a
  human opening it.
