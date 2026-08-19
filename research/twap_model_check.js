/* בדיקת המודל של מנוע היתרון — הרצה: node research/twap_model_check.js
 *
 * שווקי ה־5m/15m בפולימרקט מוכרעים לפי TWAP (ממוצע) לאורך החלון מול המחיר
 * בתחילתו — לא לפי המחיר בסוף. הסימולציה כאן מריצה עשרות אלפי מסלולי מחיר
 * ובודקת שני מודלים על אותם מסלולים בדיוק:
 *   1. מודל הממוצע (זה שבכלי) — אמור להיות מכויל.
 *   2. המודל הנאיבי "האם המחיר יהיה גבוה בסוף" — זה שרוב הבוטים מריצים.
 * העמודה "actual%" היא מה שקרה בפועל. פער בינה ל"model%" = טעות שיטתית.
 */
function erf(x){const s=x<0?-1:1;x=Math.abs(x);const t=1/(1+0.3275911*x);
 const y=1-((((1.061405429*t-1.453152027)*t+1.421413741)*t-0.284496736)*t+0.254829592)*t*Math.exp(-x*x);return s*y;}
const Phi=z=>0.5*(1+erf(z/Math.SQRT2));
function gauss(){let u=0,v=0;while(!u)u=Math.random();while(!v)v=Math.random();return Math.sqrt(-2*Math.log(u))*Math.cos(2*Math.PI*v);}

const T=300, sigma=7e-5, S0=64000, ELAPSED=[60,150,240];
const buckets={}; const naiveB={};
const N=40000;
for(const e of ELAPSED){ buckets[e]=Array.from({length:10},()=>({n:0,p:0,hit:0})); naiveB[e]=Array.from({length:10},()=>({n:0,p:0,hit:0})); }

for(let it=0; it<N; it++){
  // path of 1-second prices; K = price at window start
  let S=S0, K=S0, sum=0, path=[];
  for(let t=1;t<=T;t++){ S*=Math.exp(sigma*gauss()); sum+=S; path.push(S); }
  const finalAvg=sum/T;
  const up = finalAvg>=K;
  for(const e of ELAPSED){
    const A = path.slice(0,e).reduce((a,b)=>a+b,0)/e;
    const Snow = path[e-1];
    const r = T-e;
    const proj=(e*A+r*Snow)/T;
    const sd=Snow*sigma*Math.pow(r,1.5)/(Math.sqrt(3)*T);
    const p=Phi((proj-K)/sd);
    const b=Math.min(9,Math.max(0,Math.floor(p*10)));
    buckets[e][b].n++; buckets[e][b].p+=p; buckets[e][b].hit+=up?1:0;
    // naive: treat it as "will the final price be above K"
    const pn=Phi(Math.log(Snow/K)/(sigma*Math.sqrt(r)));
    const bn=Math.min(9,Math.max(0,Math.floor(pn*10)));
    naiveB[e][bn].n++; naiveB[e][bn].p+=pn; naiveB[e][bn].hit+=up?1:0;
  }
}
for(const e of ELAPSED){
  console.log('\n=== elapsed '+e+'s of 300 ===');
  console.log('TWAP model      bucket  n     model%  actual%');
  buckets[e].forEach((b,i)=>{ if(b.n>50) console.log('  '+(i*10)+'-'+(i*10+10)+'%\t'+b.n+'\t'+(b.p/b.n*100).toFixed(1)+'\t'+(b.hit/b.n*100).toFixed(1)); });
  console.log('naive terminal  bucket  n     model%  actual%');
  naiveB[e].forEach((b,i)=>{ if(b.n>50) console.log('  '+(i*10)+'-'+(i*10+10)+'%\t'+b.n+'\t'+(b.p/b.n*100).toFixed(1)+'\t'+(b.hit/b.n*100).toFixed(1)); });
}
