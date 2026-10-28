// Phase 0 probe: count Kalshi CPI events/markets in the live and historical tiers.
// Throwaway feasibility script (Node, because Python isn't installed yet).
// Caches every raw response to data/raw/phase0/kalshi/ and prints summaries only.
// Usage: node phase0/probe_kalshi.mjs KXCPI CPI KXCPIYOY ...
import fs from "node:fs";
import path from "node:path";

const BASE = "https://api.elections.kalshi.com/trade-api/v2";
const CACHE = path.join("data", "raw", "phase0", "kalshi");
fs.mkdirSync(CACHE, { recursive: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getJson(url, cacheName) {
  const file = path.join(CACHE, cacheName + ".json");
  if (fs.existsSync(file)) return JSON.parse(fs.readFileSync(file, "utf8"));
  for (let attempt = 0; attempt < 5; attempt++) {
    const res = await fetch(url);
    if (res.status === 429) { await sleep(1000 * 2 ** attempt); continue; }
    if (!res.ok) return { __error: res.status, __body: (await res.text()).slice(0, 300) };
    const data = await res.json();
    fs.writeFileSync(file, JSON.stringify(data));
    await sleep(150);
    return data;
  }
  return { __error: 429 };
}

async function paginate(endpoint, params, key, tag) {
  const out = [];
  let cursor = "";
  for (let page = 0; page < 200; page++) {
    const q = new URLSearchParams({ ...params, limit: "1000" });
    if (cursor) q.set("cursor", cursor);
    const data = await getJson(`${BASE}${endpoint}?${q}`, `${tag}_p${page}`);
    if (data.__error) return { error: data.__error, body: data.__body, items: out };
    out.push(...(data[key] || []));
    cursor = data.cursor;
    if (!cursor) break;
  }
  return { items: out };
}

function summarize(label, markets) {
  if (!markets.length) { console.log(`  ${label}: 0 markets`); return; }
  const byEvent = {};
  for (const m of markets) (byEvent[m.event_ticker] ||= []).push(m);
  const events = Object.keys(byEvent).sort();
  const counts = events.map((e) => byEvent[e].length).sort((a, b) => a - b);
  const closes = markets.map((m) => m.close_time).filter(Boolean).sort();
  const vol = markets.reduce((s, m) => s + Number(m.volume ?? m.volume_fp ?? 0), 0);
  console.log(`  ${label}: ${markets.length} markets across ${events.length} events`);
  console.log(`    close_time range: ${closes[0]} -> ${closes.at(-1)}`);
  console.log(`    brackets/event: min ${counts[0]}, median ${counts[Math.floor(counts.length / 2)]}, max ${counts.at(-1)}`);
  console.log(`    total volume field sum: ${vol}`);
  console.log(`    first/last events: ${events.slice(0, 3).join(", ")} ... ${events.slice(-3).join(", ")}`);
}

const series = process.argv.slice(2);
for (const s of series) {
  console.log(`\n=== ${s}`);
  const live = await paginate("/markets", { series_ticker: s }, "markets", `${s}_live_markets`);
  if (live.error) console.log(`  live error ${live.error}: ${live.body}`);
  summarize("live tier", live.items);
  const hist = await paginate("/historical/markets", { series_ticker: s }, "markets", `${s}_hist_markets`);
  if (hist.error) console.log(`  historical error ${hist.error}: ${hist.body}`);
  summarize("historical tier", hist.items);
  const sample = hist.items[0] || live.items[0];
  if (sample) console.log(`    market fields: ${Object.keys(sample).join(",")}`);
}
