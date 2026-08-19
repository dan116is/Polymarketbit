#!/usr/bin/env node
/* Turns the raw collector log into the verdict the page shows on open.
 * Reads data/windows.csv, writes data/summary.json.
 *
 * The rule being measured is the same one the live view applies: one trade per
 * window, at the first moment an outcome's edge clears the threshold, priced at
 * the average fill the real book would have given for SIZE shares, and skipped
 * when the model disagrees with the whole market by more than 25c. */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const DATA = path.join(ROOT, 'data');
const OUT = path.join(ROOT, 'data', 'summary.json');
const COST_C = +(process.env.COST_CENTS || 1.0);      // fees + friction assumed per share
const SANITY = 0.25;
const THRESHOLDS = [1, 2, 3, 4, 5, 6];

const files = fs.existsSync(DATA)
  ? fs.readdirSync(DATA).filter(f => /^windows.*\.csv$/.test(f)).sort().map(f => path.join(DATA, f))
  : [];
if(!files.length){ console.log('no data yet'); process.exit(0); }

const rows = [];
for(const file of files){
  const lines = fs.readFileSync(file, 'utf8').trim().split('\n');
  if(lines.length < 2) continue;
  const head = lines[0].split(',');
  for(const l of lines.slice(1)){
    if(!l) continue;
    const v = l.split(',');
    const o = {};
    head.forEach((hh,i) => { const x = v[i]; o[hh] = (x === '' || x == null) ? null : (isNaN(+x) ? x : +x); });
    if(o.up_won === 0 || o.up_won === 1) rows.push(o);
  }
}
rows.sort((a,b) => a.ts - b.ts);

const mean = a => a.length ? a.reduce((x,y) => x+y, 0) / a.length : 0;

/* ---- calibration: when the model says 80%, does it happen 80% of the time ---- */
const buckets = Array.from({length:5}, () => ({n:0, model:0, actual:0}));
for(const r of rows){
  const side = r.p_up >= 0.5 ? r.p_up : 1 - r.p_up;
  const hit = r.p_up >= 0.5 ? r.up_won === 1 : r.up_won === 0;
  const b = Math.min(4, Math.floor((side - 0.5) / 0.1));
  buckets[b].n++; buckets[b].model += side; buckets[b].actual += hit ? 1 : 0;
}
const calibration = buckets.map((b,i) => ({
  bucket: (50 + i*10) + '-' + (60 + i*10) + '%',
  n: b.n,
  model: b.n ? +(b.model / b.n * 100).toFixed(1) : null,
  actual: b.n ? +(b.actual / b.n * 100).toFixed(1) : null
})).filter(b => b.n > 0);

/* ---- what the rule would have earned, one trade per window ---- */
function backtest(threshC, filter){
  const taken = new Set(), trades = [];
  for(const r of rows){
    if(taken.has(r.slug) || (filter && !filter(r))) continue;
    const sides = [
      {name:'up',   fair:r.p_up,     ask:r.ask_up,   mid:r.mid_up,   won:r.up_won === 1},
      {name:'down', fair:1 - r.p_up, ask:r.ask_down, mid:r.mid_down, won:r.up_won === 0}
    ];
    let pick = null;
    for(const s of sides){
      if(s.ask == null) continue;
      if(s.mid != null && Math.abs(s.fair - s.mid) > SANITY) continue;
      const edge = s.fair - s.ask - COST_C/100;
      if(edge * 100 >= threshC && (!pick || edge > pick.edge)) pick = {edge, s};
    }
    if(!pick) continue;
    taken.add(r.slug);
    trades.push({edge:pick.edge, won:pick.s.won, pnl:(pick.s.won ? 1 : 0) - pick.s.ask - COST_C/100,
                 secs_left:r.secs_left, asset:r.asset});
  }
  if(!trades.length) return {trades:0};
  const pnls = trades.map(t => t.pnl);
  const m = mean(pnls);
  const sd = Math.sqrt(pnls.reduce((s,x) => s + (x-m)*(x-m), 0) / Math.max(1, pnls.length - 1));
  const se = sd / Math.sqrt(pnls.length);
  return {
    trades: trades.length,
    hit_rate: +(trades.filter(t => t.won).length / trades.length * 100).toFixed(1),
    promised_cents: +(mean(trades.map(t => t.edge)) * 100).toFixed(2),
    realised_cents: +(m * 100).toFixed(2),
    stderr_cents: +(se * 100).toFixed(2),
    t_stat: se > 0 ? +(m / se).toFixed(2) : null
  };
}

const windows = new Set(rows.map(r => r.slug));
const assets = [...new Set(rows.map(r => r.asset))];
const phases = [[240,300],[180,240],[120,180],[60,120],[0,60]];

const summary = {
  updated: new Date().toISOString(),
  rows: rows.length,
  windows: windows.size,
  first: rows.length ? new Date(rows[0].ts).toISOString() : null,
  last: rows.length ? new Date(rows[rows.length-1].ts).toISOString() : null,
  assumptions: {cost_cents_per_share: COST_C, sanity_gap: SANITY, size_note: 'edge priced on the collector SIZE'},
  calibration,
  edge_by_threshold: THRESHOLDS.map(t => Object.assign({threshold_cents:t}, backtest(t))),
  by_phase: phases.map(([a,b]) => Object.assign({secs_left: a + '-' + b}, backtest(3, r => r.secs_left >= a && r.secs_left < b))),
  by_asset: assets.map(a => Object.assign({asset:a}, backtest(3, r => r.asset === a)))
};
fs.mkdirSync(path.dirname(OUT), {recursive:true});
fs.writeFileSync(OUT, JSON.stringify(summary, null, 1));
console.log('summary:', summary.windows, 'windows,', summary.rows, 'rows ->', path.relative(ROOT, OUT));
