// Post-build step for the static export. On client-side navigation Next 16 fetches a page's segment payload as
// `<route>/__next.<segment>.__PAGE__.txt`, but `output: "export"` writes it as `<route>/__next.<segment>/__PAGE__.txt`.
// A static host then answers 404 (the page still loads via a full fetch, but the browser logs an error). Copy each
// nested payload to the flat name the client requests.
import { copyFileSync, readdirSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

const OUT = fileURLToPath(new URL("../out/", import.meta.url));

function walk(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) walk(path);
    else if (entry.name.endsWith(".txt") && dir.split(sep).at(-1).startsWith("__next.")) {
      const flat = `${dir}.${entry.name}`;
      copyFileSync(path, flat);
      console.log(`segment payload: ${relative(OUT, path)} -> ${relative(OUT, flat)}`);
    }
  }
}

walk(OUT);
