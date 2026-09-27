#!/usr/bin/env node
/*
 * Lint the JavaScript of the pages with ESLint: `no-undef` and `no-use-before-define`.
 *
 * `tools/check-inline-js.py` only checks syntax (`node --check`). Two bugs got past it
 * because they were valid syntax and failed only at run time in the browser:
 *   - a variable assigned and read without ever being declared (ReferenceError);
 *   - a `const` read a few lines before its declaration (temporal dead zone).
 * This script catches both statically — no browser, no server.
 *
 * Setup (once, or in CI):  npm install --no-save eslint@9 globals@15
 * Run:                     node tools/lint-js.mjs
 *
 * How the page scope is rebuilt: a page's inline <script> blocks are classic scripts that
 * share one global scope, so they are linted together (in order), with the browser globals
 * plus every name the shared /static/*.js files expose (top-level declarations and
 * `window.NAME = …` assignments). Errors are reported with the page's own line numbers.
 */
import { readFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { Linter } from "eslint";
import * as espree from "espree";
import globals from "globals";

const STATIC = join(dirname(fileURLToPath(import.meta.url)), "..", "app", "static");
const ECMA = 2022;
const RULES = {
  "no-undef": ["error", { typeof: false }],
  // functions:false — calling a function declared further down is fine (hoisting);
  // variables:false — a reference inside a function that runs later is fine too.
  // What remains is exactly the TDZ case: `const`/`let` read before its line in the same scope.
  "no-use-before-define": ["error", { functions: false, classes: false, variables: false }],
};

/** Names a shared script makes visible to the pages that load it. */
function exposedNames(code) {
  const names = new Set();
  const ast = espree.parse(code, { ecmaVersion: ECMA, sourceType: "script" });
  for (const node of ast.body) {
    if (node.type === "FunctionDeclaration" && node.id) names.add(node.id.name);
    if (node.type === "ClassDeclaration" && node.id) names.add(node.id.name);
    if (node.type === "VariableDeclaration")
      for (const d of node.declarations) if (d.id.type === "Identifier") names.add(d.id.name);
  }
  // window.NAME = … anywhere (IIFE exports)
  for (const m of code.matchAll(/\bwindow\.([A-Za-z_$][\w$]*)\s*=/g)) names.add(m[1]);
  return names;
}

const linter = new Linter({ configType: "flat" });
const lint = (code, extraGlobals, env = "browser") => {
  const g = { ...(env === "sw" ? globals.serviceworker : globals.browser) };
  for (const n of extraGlobals) g[n] = "readonly";
  return linter.verify(code, [{
    languageOptions: { ecmaVersion: ECMA, sourceType: "script", globals: g },
    rules: RULES,
  }]);
};

let errors = 0, blocks = 0;
const report = (file, line, col, msg) => { errors++; console.log(`!! ${file}:${line}:${col}  ${msg}`); };

// 1. Shared scripts: collect what they expose, and lint them on their own.
const sharedFiles = readdirSync(STATIC).filter(f => f.endsWith(".js")).sort();
const shared = new Set();
for (const f of sharedFiles) {
  if (f === "sw.js") continue;
  for (const n of exposedNames(readFileSync(join(STATIC, f), "utf8"))) shared.add(n);
}
for (const f of sharedFiles) {
  const code = readFileSync(join(STATIC, f), "utf8");
  for (const m of lint(code, f === "sw.js" ? [] : shared, f === "sw.js" ? "sw" : "browser"))
    report(f, m.line, m.column, `${m.message} (${m.ruleId})`);
}

// 2. Pages: all inline blocks of a page linted together, line numbers mapped back.
const SCRIPT = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
for (const page of readdirSync(STATIC).filter(f => f.endsWith(".html")).sort()) {
  const html = readFileSync(join(STATIC, page), "utf8");
  const parts = [];      // { startLine (1-based, in the html), lines }
  let code = "";
  for (const m of html.matchAll(SCRIPT)) {
    const body = m[1];
    if (body.trim().length < 5) continue;
    const bodyStart = m.index + m[0].indexOf(">") + 1;
    const startLine = html.slice(0, bodyStart).split("\n").length;
    parts.push({ startLine, offset: code.split("\n").length - 1, lines: body.split("\n").length });
    code += body + "\n;\n";
    blocks++;
  }
  if (!parts.length) continue;
  // names the page itself publishes with `window.NAME = …` are globals for the whole page
  const own = new Set(shared);
  for (const m of code.matchAll(/\bwindow\.([A-Za-z_$][\w$]*)\s*=/g)) own.add(m[1]);
  for (const m of lint(code, own)) {
    const idx = m.line - 1;
    const part = [...parts].reverse().find(p => idx >= p.offset) || parts[0];
    const line = part.startLine + (idx - part.offset);
    report(page, line, m.column, `${m.message} (${m.ruleId})`);
  }
}

console.log(`${blocks} inline blocks + ${sharedFiles.length} shared scripts linted, ${errors} error(s)`);
process.exit(errors ? 1 : 0);
