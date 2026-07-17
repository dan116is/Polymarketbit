// Math extracted VERBATIM from web/index.html (app) — also matches the cloud
// watcher ticker.server.ts (same erf/phi/fee; stake ladder equivalent when
// gate2 is red). Emits decisions for scenarios read from stdin as JSON.
function erf(x){const s=Math.sign(x);x=Math.abs(x);
  const t=1/(1+0.3275911*x);
  const y=1-((((1.061405429*t-1.453152027)*t+1.421413741)*t-0.284496736)*t+0.254829592)*t*Math.exp(-x*x);
  return s*y;}
const phi = x => 0.5*(1+erf(x/Math.SQRT2));
const feePerShare = (p, bps) => (p>0 && p<1) ? (bps/10000)*Math.min(p,1-p) : 0;

class EwmaVol {
  constructor(hl, floor){this.hl=hl; this.floor=floor; this.var=null; this.lp=null; this.lt=null; this.n=0;}
  update(p, ts){
    if(p<=0) return;
    if(this.lp!==null){
      const dt=ts-this.lt; if(dt<=0) return;
      const r=Math.log(p/this.lp), r2s=r*r/dt, d=Math.pow(0.5, dt/this.hl);
      this.var = this.var===null ? r2s : d*this.var+(1-d)*r2s; this.n++;
    }
    this.lp=p; this.lt=ts;
  }
  get sigma1s(){return this.var===null?this.floor:Math.max(Math.sqrt(this.var),this.floor);}
}

function stakeFromEdge(net, step){ if(net<=0) return 0; if(net<=step) return 1; return 2; }

function evaluate(sc){
  const {sT, sOpen, sigma, tau, askUp, askDown, feeBps, theta, buffer, band, step} = sc;
  const pf = phi(Math.log(sT/sOpen)/(sigma*Math.sqrt(tau)));
  if(tau < band[0] || tau > band[1]) return {side:"PASS", stake:0, pf, reason:"band"};
  const cands=[];
  if(askUp!=null && askUp>0 && askUp<1)
    cands.push(["UP", pf-askUp-feePerShare(askUp,feeBps)-buffer, askUp]);
  if(askDown!=null && askDown>0 && askDown<1)
    cands.push(["DOWN", (1-pf)-askDown-feePerShare(askDown,feeBps)-buffer, askDown]);
  if(!cands.length) return {side:"PASS", stake:0, pf, reason:"no_quotes"};
  cands.sort((a,b)=>b[1]-a[1]);
  const [side, edge] = cands[0];
  if(edge<=theta) return {side:"PASS", stake:0, pf, edge, reason:"theta"};
  const stake = stakeFromEdge(edge-theta, step);
  return {side, stake, pf, edge, reason:"go"};
}

import { readFileSync } from 'fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
const out = { decisions: input.scenarios.map(evaluate), ewma: [] };
for(const series of input.ewma_series){
  const v = new EwmaVol(90, 1e-6);
  for(const [p, ts] of series) v.update(p, ts);
  out.ewma.push(v.sigma1s);
}
// phi accuracy sweep vs nothing (Python compares against math.erf)
out.phi = input.phi_xs.map(phi);
console.log(JSON.stringify(out));
