/* Run the generated page in a DOM and report what happened.
 *
 * Written for node 6.10: no padStart/padEnd (node 8), no async/await (node 7.6),
 * no object spread (node 8.3). The page's own script is held to the same bar --
 * it has to parse under `node --check` here.
 *
 * The machine that builds this file has no javascript engine on PATH, and three
 * bugs shipped because of it: a real newline inside a string literal, a `return`
 * inside a `for..of`, and an assignment above the `const` it assigns to. All
 * three were found by the reader opening the page. This finds them instead.
 *
 * Node 6.10 (module load cdt2/node) plus jsdom 9 is what is available; both are
 * old, and neither is needed to *use* the page -- it stays one self-contained
 * file with no libraries. This is a test harness, not a dependency.
 */
"use strict";

const fs = require("fs");
const jsdom = require("jsdom");

const file = process.argv[2];
if (!file) {
  console.error("usage: node run_page.js <page.html> [view]");
  process.exit(2);
}

const errors = [];
// Below this there is nothing left over to dim, so the count would prove nothing.
// Kept low: the overview only carries the artifacts two turns shared, so a small
// synthetic session has few nodes there however many actions it has.
const MIN_TO_DIM = 4;
const virtualConsole = jsdom.createVirtualConsole();
virtualConsole.on("jsdomError", err => errors.push(String(err.stack || err)));
virtualConsole.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

const doc = jsdom.jsdom(fs.readFileSync(file, "utf8"), {
  virtualConsole: virtualConsole,
  features: {ProcessExternalResources: ["script"], FetchExternalResources: false},
});
const win = doc.defaultView;
// `const DATA` does not become a window property, so the payload is read from the
// document the same way the page reads it.
win.PAYLOAD_FOR_TEST = JSON.parse(
    doc.getElementById("data").textContent.replace(/<\\\//g, "</"));

// jsdom 9 has no scrollIntoView on any element; browsers do, including on SVG.
// Stubbed rather than worked around, so the page is exercised as written.
if (win.Element && !win.Element.prototype.scrollIntoView) {
  win.Element.prototype.scrollIntoView = function () {};
}
if (win.SVGElement && !win.SVGElement.prototype.scrollIntoView) {
  win.SVGElement.prototype.scrollIntoView = function () {};
}

function click(el) {
  const ev = el.ownerDocument.createEvent("MouseEvents");
  ev.initEvent("click", true, true);
  el.dispatchEvent(ev);
}

function clickType(el, type) {
  const ev = el.ownerDocument.createEvent("MouseEvents");
  ev.initEvent(type, true, true);
  el.dispatchEvent(ev);
}

function pointer(target, type, x, y) {
  // jsdom 9 has no PointerEvent, and the page only reads clientX/clientY and the
  // target, so a plain event carrying those is enough to exercise the handlers.
  const ev = target.ownerDocument
      ? target.ownerDocument.createEvent("Event")
      : target.document.createEvent("Event");
  ev.initEvent(type, true, true);
  ev.clientX = x;
  ev.clientY = y;
  target.dispatchEvent(ev);
}

function pad(s, n) { s = String(s); while (s.length < n) s += " "; return s; }
function lpad(s, n) { s = String(s); while (s.length < n) s = " " + s; return s; }

function report(label) {
  const nodes = doc.querySelectorAll("#nodes .nd").length;
  const links = doc.querySelectorAll("#links path").length;
  const seq = doc.querySelectorAll("#seq path").length;
  const stats = (doc.getElementById("stats") || {}).textContent || "";
  const crumbs = (doc.getElementById("crumbs") || {}).textContent || "";
  const legend = doc.querySelectorAll("#lgtypes .lgrow").length;
  console.log(
    "  " + pad(label, 16) +
    " nodes " + lpad(nodes, 4) +
    "  links " + lpad(links, 4) +
    "  seq " + lpad(seq, 4) +
    "  legend " + legend +
    "  stats [" + stats.trim().replace(/\s+/g, " ") + "]" +
    "  crumbs [" + crumbs.trim().replace(/\s+/g, " ") + "]");
  return nodes;
}

let failed = 0;
if (errors.length) {
  console.log("\n  SCRIPT ERRORS:");
  errors.forEach(e => console.log("    " + e.split("\n").slice(0, 4).join("\n    ")));
  failed = 1;
} else {
  const first = report("first view");
  if (!first) {
    console.log("    (no nodes drawn -- the script ran but produced nothing)");
    failed = 1;
  }

  // Every view, because a view switch rebuilds the drawing from scratch and the
  // first one being fine says nothing about the other thirty-six.
  // Read from the document, not from the window: the page declares `const VIEWS`,
  // and const at the top level of a script does not become a window property.
  // `go` is a function declaration, so that one does.
  const raw = doc.getElementById("views").textContent.replace(/<\\\//g, "</");
  const views = JSON.parse(raw);
  let empty = [];
  for (const v of views) {
    win.go(v.name);
    if (errors.length) break;
    if (!doc.querySelectorAll("#nodes .nd").length) empty.push(v.name);
  }
  if (errors.length) {
    console.log("\n  SCRIPT ERRORS after switching view:");
    errors.forEach(e => console.log("    " + e.split("\n").slice(0, 4).join("\n    ")));
    failed = 1;
  } else {
    console.log("  switched through " + views.length + " views, "
                + (empty.length ? "empty: " + empty.join(", ") : "all drew"));
    if (empty.length) failed = 1;
  }

  // Two different clicks, because a turn card and a file do different things and
  // an earlier version of this harness clicked a turn card and then complained
  // that the panel had not filled -- which is the turn card working.
  if (!failed) {
    win.go("overview");
    // The OPEN button, not the card: clicking the box focuses the turn's files
    // and stays put, which is the point of having two targets.
    const card = doc.querySelector('#nodes .nd[data-kind="turn"]');
    click(card);
    const stayed = doc.getElementById("crumbs").textContent || "";
    console.log("  click a turn box  -> " + stayed.trim().replace(/\s+/g, " ")
                + "  (should not have moved)");
    if (/\u203a/.test(stayed)) {
      console.log("    (the box navigated -- it should only focus)");
      failed = 1;
    }
    const open = card.querySelector(".open");
    if (!open) {
      console.log("    (no OPEN button on the turn card)");
      failed = 1;
    } else {
      click(open);
    }
    const crumbs = doc.getElementById("crumbs").textContent || "";
    console.log("  click OPEN        -> " + crumbs.trim().replace(/\s+/g, " "));
    if (!/\u203a/.test(crumbs)) {
      console.log("    (still on the overview -- the card did not open a turn)");
      failed = 1;
    }

    const ent = doc.querySelector('#nodes .nd[data-kind="ent"]');
    if (ent) {
      click(ent);
      const body = doc.getElementById("body").textContent || "";
      console.log("  click a file      -> " + body.trim().slice(0, 72)
                  .replace(/\s+/g, " "));
      if (/Click a node/.test(body)) {
        console.log("    (panel unchanged -- the click handler did nothing)");
        failed = 1;
      }
      if (!/trail/i.test(body)) {
        console.log("    (no trail in the panel)");
      }
    }
    // Dimming, checked on the busiest view: on a two-node turn there is nothing
    // left to dim and the count proves nothing.
    let busiest = views[0], most = 0;
    for (const v of views) {
      const n = v.geometry.nodes.length;
      if (n > most) { most = n; busiest = v; }
    }
    win.go(busiest.name);
    const anyEnt = doc.querySelector('#nodes .nd[data-kind="ent"]');
    if (!anyEnt || most <= MIN_TO_DIM) {
      // Said out loud. Skipped silently, this check stopped running when a fixture
      // shrank and nobody noticed for a release.
      console.log("  dimming check     -> SKIPPED, busiest view has only "
                  + most + " nodes");
    }
    if (anyEnt && most > MIN_TO_DIM) {
      click(anyEnt);
      const dim = doc.querySelectorAll("#nodes .nd.dimmed").length;
      const lit = doc.querySelectorAll("#nodes .nd.lit").length;
      console.log("  click a file      -> " + busiest.name + ": lit " + lit
                  + ", dimmed " + dim + " of " + most);
      if (!dim || !lit) {
        console.log("    (selecting a file did not dim the rest)");
        failed = 1;
      }
      // Hovering must not dim. The selection has to be cleared first: the hover
      // handler only previews when nothing is selected, so testing it with a node
      // still selected exercised nothing -- which is how a broken version of this
      // passed.
      pointer(doc.getElementById("graph"), "pointerdown", 400, 300);
      pointer(win, "pointerup", 400, 300);
      click(doc.getElementById("svg"));
      clickType(anyEnt, "mouseenter");
      const hoverDim = doc.querySelectorAll("#nodes .nd.dimmed").length;
      const hoverBody = (doc.getElementById("body").textContent || "").slice(0, 30);
      console.log("  hover a file      -> dimmed " + hoverDim
                  + ", panel previews " + (/Click a node/.test(hoverBody)
                      ? "no" : "yes"));
      if (hoverDim) {
        console.log("    (hover dimmed " + hoverDim + " nodes; only a click should)");
        failed = 1;
      }
    }

    // Select a file, then drag the canvas and let go. The click that ends a drag
    // used to clear the selection, so looking around the highlighted file undid
    // the highlight.
    if (ent && !failed) {
      click(ent);
      const before = (doc.getElementById("body").textContent || "").slice(0, 40);
      const svg = doc.getElementById("svg");
      pointer(doc.getElementById("graph"), "pointerdown", 400, 300);
      pointer(win, "pointermove", 520, 360);
      pointer(win, "pointerup", 520, 360);
      click(svg);
      const after = (doc.getElementById("body").textContent || "").slice(0, 40);
      console.log("  drag the canvas   -> panel " + (after === before
                  ? "kept" : "LOST"));
      if (after !== before) {
        console.log("    (panning cleared the selection)");
        failed = 1;
      }
      // And a real click on empty space still clears it.
      pointer(doc.getElementById("graph"), "pointerdown", 400, 300);
      pointer(win, "pointerup", 400, 300);
      click(svg);
      const cleared = doc.getElementById("body").textContent || "";
      console.log("  click empty space -> " + (/Click a node/.test(cleared)
                  ? "cleared" : "STILL SELECTED"));
      if (!/Click a node/.test(cleared)) {
        console.log("    (a click on empty space no longer clears)");
        failed = 1;
      }
    }
    // The suggestion list: a hit, a miss, cleared, and picking one.
    if (!failed) {
      win.go(busiest.name);
      const box = doc.getElementById("find");
      const sug = doc.getElementById("sug");
      const type = text => {
        box.value = text;
        const ev = doc.createEvent("Event");
        ev.initEvent("input", true, true);
        box.dispatchEvent(ev);
      };
      let hitTerm = "";
      for (const n of busiest.geometry.nodes) {
        const d = (win.PAYLOAD_FOR_TEST || {})[n.id];
        if (d && d.key) { hitTerm = d.key.split("/").pop().toLowerCase(); break; }
      }
      if (!hitTerm) hitTerm = "log";

      type(hitTerm);
      const rows = sug.querySelectorAll(".sg");
      const dimmedWhileTyping = doc.querySelectorAll("#nodes .nd.dimmed").length;
      console.log("  type '" + hitTerm + "' -> " + rows.length
                  + " suggestions, drawing dimmed " + dimmedWhileTyping);
      if (!rows.length) {
        console.log("    (a term that is on this view produced no suggestions)");
        failed = 1;
      }
      if (dimmedWhileTyping) {
        console.log("    (typing dimmed the drawing; it should only list)");
        failed = 1;
      }
      if (rows.length) {
        const first = rows[0];
        const hasKind = first.querySelector(".sgk");
        const hasWhere = first.querySelector(".sgv");
        console.log("  first row         -> " + (hasKind ? hasKind.textContent : "?")
                    + " | " + (first.querySelector(".sgl") || {}).textContent
                    + " | in " + (hasWhere ? hasWhere.textContent : "?"));
        if (!hasKind || !hasWhere) {
          console.log("    (a row does not say what it is or where it lives)");
          failed = 1;
        }
      }

      type("zzzznotpresentzzzz");
      const none = sug.querySelectorAll(".sgnone").length;
      console.log("  type a miss       -> " + (none ? "'no match' shown" : "NOTHING")
                  + ", suggestions " + sug.querySelectorAll(".sg").length);
      if (!none) {
        console.log("    (an empty result did not say so)");
        failed = 1;
      }

      type("");
      console.log("  clear the box     -> list " + (sug.style.display === "none"
                  ? "closed" : "STILL OPEN"));
      if (sug.style.display !== "none") {
        console.log("    (clearing the box left the list open)");
        failed = 1;
      }

      // Picking one has to navigate and highlight.
      type(hitTerm);
      const pick = sug.querySelectorAll(".sg")[0];
      if (pick) {
        const wanted = (pick.querySelector(".sgv") || {}).textContent;
        const ev = doc.createEvent("MouseEvents");
        ev.initEvent("mousedown", true, true);
        pick.dispatchEvent(ev);
        const crumbs = (doc.getElementById("crumbs").textContent || "").trim();
        const panel = (doc.getElementById("body").textContent || "").slice(0, 24);
        const dim = doc.querySelectorAll("#nodes .nd.dimmed").length;
        const lit = doc.querySelectorAll("#nodes .nd.lit").length;
        console.log("  pick a suggestion -> now in '" + crumbs.replace(/\s+/g, " ")
                    + "', panel " + (/Click a node/.test(panel) ? "EMPTY" : "filled")
                    + ", lit " + lit + " dimmed " + dim);
        if (/Click a node/.test(panel)) {
          console.log("    (picking a suggestion did not select anything)");
          failed = 1;
        }
        if (!dim && !lit) {
          console.log("    (picking a suggestion did not highlight it)");
          failed = 1;
        }
      }
    }
    if (errors.length) {
      console.log("  SCRIPT ERRORS on click:");
      errors.forEach(e => console.log("    " + e.split("\n").slice(0, 3).join("\n    ")));
      failed = 1;
    }
  }
}

console.log(failed ? "\n  FAILED" : "\n  OK");
process.exit(failed);
