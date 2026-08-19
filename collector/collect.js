#!/usr/bin/env node
/* Background collector — the part of the product that works while nobody is watching.
 *
 * Runs in GitHub Actions (see .github/workflows/collect.yml), samples the same
 * things the on-screen engine samples — external spot, the real Polymarket
 * order book, the model's probability — and after each window closes it writes
 * down what actually happened. The result lands in data/windows.csv and is
 * summarised into data/summary.json, which the page loads on open.
 *
 * The model itself is not reimplemented here: it is read out of index.html
 * between the MODEL-START / MODEL-END markers, so the collector and the page
 * can never disagree about what a fair price is.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const {execFileSync} = require('child_process');

const ROOT = path.join(__dirname, '..');
const GAMMA = 'https://gamma-api.polymarket.com';
const CLOB  = 'https://clob.polymarket.com';
const COINBASE = 'https://api.exchange.coinbase.com';

const DURATION_SEC = +(process.env.DURATION_SEC || 3300);
const SIZE      = +(process.env.SIZE || 100);        // shares used to price the edge
const BASIS_BP  = +(process.env.BASIS_BP || 3);
const SIGMA_DEFAULT = 7e-5;
const SPOT_MS   = 2000;
const SNAP_MS   = +(process.env.SNAP_MS || 20000);   // one snapshot per window per 20s keeps the log ~1.4MB/day
const ASSET_LIST = (process.env.ASSETS || 'BTC,ETH').split(',').map(s => s.trim()).filter(Boolean);

const ASSETS = {
  BTC: {cb:'BTC-USD', slug:'btc-updown-5m-'},
  ETH: {cb:'ETH-USD', slug:'eth-updown-5m-'},
  SOL: {cb:'SOL-USD', slug:'sol-updown-5m-'},
  XRP: {cb:'XRP-USD', slug:'xrp-updown-5m-'}
};

/* ---- load the shared model straight out of the page ---- */
const page = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
const block = page.split('/* ===== MODEL-START =====')[1];
if(!block) throw new Error('MODEL-START marker not found in index.html');
const modelSrc = block.split('/* ===== MODEL-END ===== */')[0].replace(/^[\s\S]*?\*\//, '');
const model = {};
new Function('exports', modelSrc + '\nexports.fairProbability=fairProbability;exports.walkBook=walkBook;' +
             'exports.kellyFraction=kellyFraction;exports.Phi=Phi;')(model);

/* ---- small helpers ---- */
const sleep = ms => new Promise(r => setTimeout(r, ms));
const mean = a => a.length ? a.reduce((x,y) => x+y, 0) / a.length : 0;
function log(...a){ console.log(new Date().toISOString().slice(11,19), ...a); }
async function getJSON(url, tries = 3){
  for(let i = 0; i < tries; i++){
    try{
      const r = await fetch(url, {headers:{'accept':'application/json'}});
      if(!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    }catch(e){
      if(i === tries - 1) throw e;
      await sleep(500 * (i + 1));
    }
  }
}
function normBook(raw){
  return {
    bids:(raw.bids || []).map(l => ({p:+l.price, s:+l.size})).sort((a,b) => b.p - a.p),
    asks:(raw.asks || []).map(l => ({p:+l.price, s:+l.size})).sort((a,b) => a.p - b.p)
  };
}

/* ---- state ---- */
const spot = {};        // sym -> [{t,p}]
const windows = new Map();  // slug -> {sym, startTs, endTs, T, type, tokens, outcomes, upIndex, rows, resolved}
const rows = [];
for(const s of ASSET_LIST) spot[s] = [];

function sigmaFor(sym){
  const pts = [];
  let last = null;
  for(const x of spot[sym]){ if(!last || x.t - last.t >= 8000){ pts.push(x); last = x; } }
  if(pts.length < 10) return {sigma:SIGMA_DEFAULT, q:'default'};
  const rets = [];
  for(let i = 1; i < pts.length; i++){
    const dt = (pts[i].t - pts[i-1].t) / 1000;
    if(dt > 0) rets.push(Math.log(pts[i].p / pts[i-1].p) / Math.sqrt(dt));
  }
  if(rets.length < 8) return {sigma:SIGMA_DEFAULT, q:'default'};
  const m = mean(rets);
  const v = rets.reduce((s,x) => s + (x-m)*(x-m), 0) / (rets.length - 1);
  const sd = Math.sqrt(v);
  return sd > 0 ? {sigma:sd, q:'measured'} : {sigma:SIGMA_DEFAULT, q:'default'};
}

async function sampleSpot(){
  await Promise.all(ASSET_LIST.map(async sym => {
    try{
      const d = await getJSON(COINBASE + '/products/' + ASSETS[sym].cb + '/ticker', 1);
      const p = +d.price;
      if(p > 0) spot[sym].push({t:Date.now(), p});
      const cut = Date.now() - 40 * 60 * 1000;
      while(spot[sym].length && spot[sym][0].t < cut) spot[sym].shift();
    }catch(e){ /* one missed tick is not worth a retry */ }
  }));
}

/* window opening price: the mean of the minute before the window starts,
   which is what a 60s TWAP stream reads at that moment */
function refPrice(sym, startTs){
  const pre = spot[sym].filter(x => x.t >= startTs - 62000 && x.t <= startTs + 2000);
  return pre.length >= 8 ? mean(pre.map(x => x.p)) : null;
}

async function ensureWindow(sym, base){
  const slug = ASSETS[sym].slug + base;
  if(windows.has(slug)) return windows.get(slug);
  let ev;
  try{
    ev = (await getJSON(GAMMA + '/events?slug=' + slug))[0];
  }catch(e){ return null; }
  const mk = ev && ev.markets && ev.markets[0];
  if(!mk) return null;
  const outcomes = JSON.parse(mk.outcomes || '[]');
  const w = {
    sym, slug, startTs:base * 1000, endTs:Date.parse(mk.endDate), T:300,
    type: /TWAP/i.test(mk.description || '') ? 'twap' : 'terminal',
    tokens: JSON.parse(mk.clobTokenIds || '[]'),
    outcomes,
    upIndex: Math.max(0, outcomes.findIndex(o => /up/i.test(o))),
    resolved:false
  };
  windows.set(slug, w);
  log('window', slug, w.type, 'ends', new Date(w.endTs).toISOString().slice(11,19));
  return w;
}

async function snapshot(w){
  const now = Date.now();
  const r = (w.endTs - now) / 1000;
  if(r <= 2) return;
  const K = refPrice(w.sym, w.startTs);
  if(K == null) return;                      // no trustworthy opening price: record nothing
  const series = spot[w.sym];
  if(!series.length) return;
  const S = series[series.length - 1].p;
  const inWin = series.filter(x => x.t >= w.startTs && x.t <= now).map(x => x.p);
  const A = inWin.length ? mean(inWin) : S;
  const sg = sigmaFor(w.sym);
  const f = model.fairProbability({type:w.type, K, S, A, elapsed:w.T - r, remaining:r, T:w.T,
                                   sigma:sg.sigma, basisBp:BASIS_BP});
  let books;
  try{
    books = await Promise.all(w.tokens.map(async t => {
      const [bk, md] = await Promise.all([
        getJSON(CLOB + '/book?token_id=' + t, 2),
        getJSON(CLOB + '/midpoint?token_id=' + t, 1).catch(() => null)
      ]);
      return {bk:normBook(bk), mid: md ? +md.mid : null};
    }));
  }catch(e){ return; }
  const ask = books.map(b => {
    const wl = model.walkBook(b.bk.asks, SIZE);
    return wl.unfilled > 0 ? null : +wl.avg.toFixed(4);
  });
  rows.push({
    ts:now, slug:w.slug, asset:w.sym, settlement:w.type,
    window_start:w.startTs, window_end:w.endTs, secs_left:Math.round(r),
    K:+K.toFixed(2), spot:+S.toFixed(2), avg_window:+A.toFixed(2),
    sigma:+(sg.sigma * 1e6).toFixed(1), sigma_q:sg.q, p_up:+f.pUp.toFixed(4),
    ask_up:ask[w.upIndex], ask_down:ask[1 - w.upIndex],
    bid_up:books[w.upIndex].bk.bids[0] ? books[w.upIndex].bk.bids[0].p : 0,
    bid_down:books[1 - w.upIndex].bk.bids[0] ? books[1 - w.upIndex].bk.bids[0].p : 0,
    mid_up:books[w.upIndex].mid, mid_down:books[1 - w.upIndex].mid,
    up_won:''
  });
}

/* ---- persistence ---- */
const COLS = ['ts','slug','asset','settlement','window_start','window_end','secs_left','K','spot',
              'avg_window','sigma','sigma_q','p_up','ask_up','ask_down','bid_up','bid_down',
              'mid_up','mid_down','up_won'];
/* one file per month: the raw log grows every hour, forever, and a single
   ever-growing CSV would eventually be the biggest thing in the repo */
function csvPathFor(ts){
  return path.join(ROOT, 'data', 'windows-' + new Date(ts).toISOString().slice(0, 7) + '.csv');
}
function appendCsv(){
  if(!rows.length){ log('no rows collected'); return 0; }
  const byFile = new Map();
  for(const r of rows){
    const f = csvPathFor(r.ts);
    if(!byFile.has(f)) byFile.set(f, []);
    byFile.get(f).push(r);
  }
  let total = 0;
  for(const [file, group] of byFile){
    fs.mkdirSync(path.dirname(file), {recursive:true});
    const seen = new Set();
    let out = '';
    if(fs.existsSync(file)){
      for(const line of fs.readFileSync(file, 'utf8').split('\n')){
        if(line) seen.add(line.split(',').slice(0, 2).join(','));   // ts+slug identifies a snapshot
      }
    }else{
      out += COLS.join(',') + '\n';
    }
    for(const r of group){
      const line = COLS.map(c => r[c] == null ? '' : r[c]).join(',');
      if(seen.has(line.split(',').slice(0, 2).join(','))) continue;
      out += line + '\n'; total++;
    }
    fs.appendFileSync(file, out);
  }
  log('appended', total, 'rows');
  return total;
}

function runStep(script){
  try{ execFileSync(process.execPath, [path.join(__dirname, script)], {stdio:'inherit'}); }
  catch(e){ log(script, 'failed:', e.message); }
}

(async () => {
  const stopAt = Date.now() + DURATION_SEC * 1000;
  log('collector start, assets', ASSET_LIST.join('/'), 'for', Math.round(DURATION_SEC/60), 'min');
  runStep('resolve.js');           // label whatever the previous run left open
  let lastSnap = 0;
  while(Date.now() < stopAt){
    await sampleSpot();
    const now = Date.now();
    if(now - lastSnap >= SNAP_MS){
      lastSnap = now;
      const base = Math.floor(now / 1000 / 300) * 300;
      for(const sym of ASSET_LIST){
        try{
          const w = await ensureWindow(sym, base);
          if(w) await snapshot(w);
        }catch(e){ log('snapshot error', sym, e.message); }
      }
    }
    await sleep(SPOT_MS);
  }
  if(appendCsv()){
    await sleep(60000);            // let the last window settle before asking for it
    runStep('resolve.js');
    runStep('summarize.js');
  }
  log('done');
})();
