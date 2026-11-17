// Fails the build if banned styling reaches the source. See the spec, section 4.
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../src", import.meta.url).pathname;
const RULES = [
  [/bg-gradient|linear-gradient|radial-gradient|conic-gradient/, "gradients are banned"],
  [/backdrop-blur|backdrop-filter|\bblur-/, "blur and glass effects are banned"],
  [/rounded-(xl|2xl|3xl|full)/, "radius above 6px is banned (use rounded-dot for status dots only)"],
  [/shadow-(lg|xl|2xl)|box-shadow|drop-shadow/, "heavy shadows are banned; use 1px borders"],
  [/\b(indigo|violet|purple|fuchsia)-\d/, "default Tailwind indigo/violet/purple/fuchsia are banned"],
  [/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}]/u, "emoji are banned"],
  [/\[(#|\d+px|\d+rem)[^\]]*\]/, "arbitrary Tailwind values are banned; add a token instead"],
  [/animate-(bounce|ping|spin)|duration-(300|500|700|1000)/, "long or bouncy animation is banned"],
];
const HEX = /#[0-9a-fA-F]{3,8}\b/;

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* files(path);
    else if (/\.(tsx?|css)$/.test(name)) yield path;
  }
}

const problems = [];
for (const path of files(ROOT)) {
  const rel = relative(ROOT, path);
  readFileSync(path, "utf8").split("\n").forEach((line, index) => {
    for (const [pattern, message] of RULES) {
      if (pattern.test(line)) problems.push(`${rel}:${index + 1} ${message}`);
    }
    // Raw colours belong in tokens.css only (tests may assert on them).
    if (HEX.test(line) && rel !== "tokens.css" && !rel.endsWith(".test.ts") && !rel.endsWith(".test.tsx")) {
      problems.push(`${rel}:${index + 1} raw hex colour outside tokens.css`);
    }
  });
}
if (problems.length) {
  console.error(problems.join("\n"));
  console.error(`\ndesign lint: ${problems.length} problem(s)`);
  process.exit(1);
}
console.log("design lint: clean");
