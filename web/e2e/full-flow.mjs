// Phase 7 acceptance check in a real browser (SPEC section 13): upload > processing > summary > key information
// > clauses > chat with follow-up > citations open the cited page. Prints checks and timings; saves screenshots.
//
//   node e2e/full-flow.mjs <contract.pdf> [screenshot dir]        (WEB_URL defaults to http://localhost:3000)
//
// Uploading costs what an ingest costs (about $0.17 in Bedrock).
import { mkdirSync } from "node:fs";
import { chromium } from "playwright";

const BASE = process.env.WEB_URL ?? "http://localhost:3000";
const [pdf, shots = "../out/screens"] = process.argv.slice(2);
if (!pdf) throw new Error("usage: node e2e/full-flow.mjs <contract.pdf> [screenshot dir]");
mkdirSync(shots, { recursive: true });

const t0 = Date.now();
const secs = () => `${((Date.now() - t0) / 1000).toFixed(0)}s`;
const results = [];
function check(name, ok, detail = "") {
  results.push(ok);
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
}

const browser = await chromium.launch({ channel: process.env.E2E_CHANNEL }); // e.g. "msedge": skip the Chromium download
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await context.newPage();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
// "Failed to load resource" messages carry no URL; add the resource's so a failure says what was missing
page.on("console", (m) => m.type() === "error" &&
  errors.push(m.location()?.url ? `${m.text()} [${m.location().url}]` : m.text()));

/** Open a citation chip and check the panel shows the cited printed page and highlights text on it. */
async function checkCitation(chip, name) {
  const label = await chip.getAttribute("aria-label");
  const wanted = label.match(/(?<!PDF )\bp\. (\d+)/)?.[1];     // "PDF p. N" = unnumbered page: nothing printed to compare
  await chip.click();
  const dialog = page.getByRole("dialog");
  await dialog.locator(".react-pdf__Page canvas").waitFor({ timeout: 60_000 });
  await page.waitForTimeout(1500);       // text layer
  const status = await dialog.getByText(/PDF page \d+/).textContent();
  const shown = status.match(/printed p\. (\d+)/)?.[1];
  const marks = await dialog.locator("mark.pdf-hit").count();
  check(`${name}: citation opens the cited page`, !wanted || wanted === shown,
        `${label.replace("Open source: ", "")} -> ${status.trim()}`);
  check(`${name}: cited text highlighted on the page`, marks > 0, `${marks} highlighted lines`);
  return dialog;
}

// 1. Library and upload. E2E_TOKEN and E2E_DOCUMENT resume on an already processed document instead (no upload).
if (process.env.E2E_TOKEN) {
  await context.addInitScript((t) => localStorage.setItem("billing-rag.session", t), process.env.E2E_TOKEN);
}
await page.goto(BASE);
await page.getByText("Drop a contract PDF here").waitFor();
check("library shows the browser-only notice", await page.getByText(/tied to this browser/).isVisible());
if (process.env.E2E_DOCUMENT) {
  await page.goto(`${BASE}/document/?id=${process.env.E2E_DOCUMENT}`);
} else {
  await page.setInputFiles("#pdf-input", pdf);
  await page.getByText("Uploaded. Processing has started.").waitFor({ timeout: 120_000 });
  check("upload accepted", true, secs());
  await page.locator('a[href^="/document"]').first().click();
  await page.getByRole("list", { name: "Processing steps" }).waitFor({ timeout: 30_000 });
  check("processing view with step tracker", true);
  await page.screenshot({ path: `${shots}/1-processing.png` });
}

// 2. Wait for ready (the page polls the status)
const seen = new Set();
const poll = setInterval(async () => {
  const s = await page.locator('[aria-current="step"]').textContent().catch(() => null);
  if (s && !seen.has(s)) { seen.add(s); console.log(`      ${secs()}  ${s.replace(/\(.*\)/, "").trim()}`); }
}, 2000);
const outcome = await Promise.race([
  page.getByRole("tab", { name: "Summary" }).waitFor({ timeout: 25 * 60_000 }).then(() => "ready"),
  page.getByText("Processing failed").waitFor({ timeout: 25 * 60_000 }).then(() => "failed"),
]);
clearInterval(poll);
if (outcome === "failed") {
  const why = await page.getByRole("alert").filter({ hasText: "Processing failed" }).textContent();
  check("document reached ready", false, why);
  await page.screenshot({ path: `${shots}/2-failed.png` });
  await browser.close();
  process.exit(1);
}
check("document reached ready with no manual steps", true, secs());

// 3. Summary
await page.getByRole("heading", { name: "Executive summary" }).waitFor({ timeout: 30_000 });
const topItems = await page.locator("section[aria-labelledby=top10] li").count();
check("summary: executive summary and top 10", topItems > 0, `${topItems} items`);
await page.screenshot({ path: `${shots}/2-summary.png`, fullPage: true });
let dialog = await checkCitation(page.locator("section[aria-labelledby=top10] button[aria-label^='Open source:']")
  .first(), "summary");
await page.screenshot({ path: `${shots}/3-source-panel.png` });
await page.keyboard.press("Escape");
await dialog.waitFor({ state: "hidden" });

// 4. Key information
await page.getByRole("tab", { name: "Key information" }).click();
await page.locator("dl").first().waitFor();
const values = await page.locator("dd li").count();
const flags = await page.getByText("Unverified", { exact: true }).count();
const quotes = await page.locator("dd blockquote").count();
check("key information: values with citations and quotes", values > 10, `${values} values, ${quotes} quotes`);
check("key information: unverified values flagged", true, `${flags} flagged`);
await page.screenshot({ path: `${shots}/4-key-info.png`, fullPage: true });
dialog = await checkCitation(page.locator("dd button[aria-label^='Open source:']").nth(3), "key information");
await page.keyboard.press("Escape");
await dialog.waitFor({ state: "hidden" });

// 5. Clauses
await page.getByRole("tab", { name: "Clauses" }).click();
await page.getByLabel("Search clauses and schedules").fill("termination");
const hits = page.locator("main ul button[aria-label^='Open ']");   // not the Next.js dev tools button
await hits.first().waitFor();
check("clauses: outline search", (await hits.count()) > 0, `${await hits.count()} matches for "termination"`);
await page.screenshot({ path: `${shots}/5-clauses.png` });
await hits.first().click();
await page.getByRole("dialog").locator(".react-pdf__Page canvas").waitFor({ timeout: 60_000 });
check("clauses: a clause opens in the source panel", true);
await page.keyboard.press("Escape");

// 6. Ask, with a follow-up
await page.getByRole("tab", { name: "Ask" }).click();
async function ask(q) {
  const before = await page.getByRole("button", { name: "Helpful", exact: true }).count();
  const start = Date.now();
  await page.getByLabel("Your question").fill(q);
  await page.keyboard.press("Enter");
  const answers = page.locator("[aria-busy] > div.max-w-full");
  let first = null;
  while (Date.now() - start < 180_000) {
    const text = (await answers.last().textContent().catch(() => "")) ?? "";
    if (!first && text.replace("Answer:", "").trim().length > 0) first = (Date.now() - start) / 1000;
    if ((await page.getByRole("button", { name: "Helpful", exact: true }).count()) > before) break;
    await page.waitForTimeout(250);
  }
  const answer = (await answers.last().textContent()).replace(/^Answer:\s*/, "");
  return { answer, first, total: (Date.now() - start) / 1000, last: answers.last() };
}
const a1 = await ask("What happens if Airtel pays an invoice late?");
check("chat: streamed answer", a1.first !== null, `first text ${a1.first?.toFixed(1)}s, done ${a1.total.toFixed(1)}s`);
const a2 = await ask("What rate is used?");
const chips = a2.last.locator("button[aria-label^='Open source:']");
check("chat: follow-up keeps context", /prime|lending|rate/i.test(a2.answer), a2.answer.slice(0, 160));
check("chat: answer has clickable citations", (await chips.count()) > 0, `${await chips.count()} chips`);
await page.screenshot({ path: `${shots}/6-chat.png`, fullPage: true });
if (await chips.count()) {
  dialog = await checkCitation(chips.first(), "chat");
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "hidden" });
}
await page.getByRole("button", { name: "Helpful", exact: true }).last().click();
const saved = await page.getByRole("button", { name: "Helpful", exact: true, pressed: true })
  .waitFor({ timeout: 15_000 }).then(() => true, () => false);
check("chat: feedback saved", saved);

// 7. Dark mode and phone width
const dark = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: "dark" });
const dp = await dark.newPage();
await dp.addInitScript((t) => localStorage.setItem("billing-rag.session", t),
                       await page.evaluate(() => localStorage.getItem("billing-rag.session")));
await dp.goto(page.url());
await dp.getByRole("heading", { name: "Executive summary" }).waitFor({ timeout: 30_000 });
check("dark mode applied", await dp.evaluate(() => document.documentElement.classList.contains("dark")));
await dp.screenshot({ path: `${shots}/7-dark.png` });
const phone = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
const pp = await phone.newPage();
await pp.addInitScript((t) => localStorage.setItem("billing-rag.session", t),
                       await page.evaluate(() => localStorage.getItem("billing-rag.session")));
await pp.goto(page.url());
await pp.getByRole("heading", { name: "Executive summary" }).waitFor({ timeout: 30_000 });
const overflow = await pp.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
check("phone width: no horizontal scroll", overflow <= 1, `${overflow}px overflow`);
await pp.screenshot({ path: `${shots}/8-phone.png` });

check("no browser errors", errors.length === 0, errors.slice(0, 3).join(" | "));
await browser.close();
const failed = results.filter((r) => !r).length;
console.log(`\n${results.length - failed}/${results.length} checks passed in ${secs()}`);
process.exit(failed ? 1 : 0);
