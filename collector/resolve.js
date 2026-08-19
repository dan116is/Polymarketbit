#!/usr/bin/env node
/* Fills in the outcome column for rows the collector wrote before their window
 * had settled. Safe to run any time: it only touches rows whose up_won is empty
 * and whose window has already ended.
 *
 * Note on the lookup: Gamma's /markets?slug= returns nothing for the 5-minute
 * crypto windows even after they settle — the settled market is only reachable
 * through its event. Resolving through /markets alone silently never resolves. */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const DATA = path.join(ROOT, 'data');
function csvFiles(){
  if(!fs.existsSync(DATA)) return [];
  return fs.readdirSync(DATA).filter(f => /^windows.*\.csv$/.test(f)).sort().map(f => path.join(DATA, f));
}
const GAMMA = 'https://gamma-api.polymarket.com';
const MAX_SLUGS = +(process.env.MAX_SLUGS || 400);

async function getJSON(url){
  const r = await fetch(url, {headers:{accept:'application/json'}});
  if(!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}
async function fetchMarketBySlug(slug){
  try{
    const ms = await getJSON(GAMMA + '/markets?slug=' + encodeURIComponent(slug));
    if(ms && ms.length) return ms[0];
  }catch(e){ /* fall through */ }
  try{
    const evs = await getJSON(GAMMA + '/events?slug=' + encodeURIComponent(slug));
    const ev = evs && evs[0];
    if(ev && ev.markets && ev.markets.length) return ev.markets.find(m => m.slug === slug) || ev.markets[0];
  }catch(e){ /* leave unresolved */ }
  return null;
}

(async () => {
  const files = csvFiles();
  if(!files.length){ console.log('no csv yet'); return; }

  const parsed = files.map(file => {
    const lines = fs.readFileSync(file, 'utf8').split('\n');
    const head = lines[0].split(',');
    return {file, lines, head,
            iSlug:head.indexOf('slug'), iEnd:head.indexOf('window_end'), iWon:head.indexOf('up_won')};
  }).filter(p => p.iSlug >= 0 && p.iEnd >= 0 && p.iWon >= 0);

  const pending = new Set();
  for(const p of parsed){
    for(let i = 1; i < p.lines.length; i++){
      const v = p.lines[i].split(',');
      if(v.length < p.head.length) continue;
      if(v[p.iWon] === '' && +v[p.iEnd] < Date.now() - 45000) pending.add(v[p.iSlug]);
    }
  }
  if(!pending.size){ console.log('nothing pending'); return; }

  const outcome = new Map();
  for(const slug of [...pending].slice(0, MAX_SLUGS)){
    const mk = await fetchMarketBySlug(slug);
    if(!mk || !mk.closed) continue;
    let outcomes, prices;
    try{
      outcomes = JSON.parse(mk.outcomes || '[]');
      prices = JSON.parse(mk.outcomePrices || '[]').map(Number);
    }catch(e){ continue; }
    const winner = prices.findIndex(p => p >= 0.99);
    if(winner < 0) continue;
    const upIndex = Math.max(0, outcomes.findIndex(o => /up/i.test(o)));
    outcome.set(slug, winner === upIndex ? 1 : 0);
  }
  if(!outcome.size){ console.log('pending', pending.size, 'slugs, none settled yet'); return; }

  let filled = 0;
  for(const p of parsed){
    let touched = false;
    for(let i = 1; i < p.lines.length; i++){
      const v = p.lines[i].split(',');
      if(v.length < p.head.length || v[p.iWon] !== '') continue;
      if(!outcome.has(v[p.iSlug])) continue;
      v[p.iWon] = outcome.get(v[p.iSlug]);
      p.lines[i] = v.join(',');
      filled++; touched = true;
    }
    if(touched) fs.writeFileSync(p.file, p.lines.join('\n'));
  }
  console.log('resolved', outcome.size, 'windows,', filled, 'rows');
})();
