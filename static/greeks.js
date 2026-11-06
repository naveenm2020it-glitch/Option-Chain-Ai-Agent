(() => {
 const $ = id => document.getElementById(id);
 if(typeof titles!=="undefined") titles.greeks="Greeks & Premium Intelligence";
 const n = v => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v)) ? Number(v) : null;
 const fmt = (v,d=3) => v===null || !Number.isFinite(v) ? "—" : v.toFixed(d);
 const erf = x => { const s=x<0?-1:1; x=Math.abs(x); const t=1/(1+0.3275911*x); const y=1-(((((1.061405429*t-1.453152027)*t)+1.421413741)*t-0.284496736)*t+0.254829592)*t*Math.exp(-x*x); return s*y; };
 const cdf = x => 0.5*(1+erf(x/Math.SQRT2));
 const pdf = x => Math.exp(-0.5*x*x)/Math.sqrt(2*Math.PI);
 function calc(S,K,iv,days,isCall) {
  if(!(S>0&&K>0&&iv>0&&days>0)) return null;
  const T=days/365,r=0.065,sigma=iv>1?iv/100:iv,root=Math.sqrt(T);
  const d1=(Math.log(S/K)+(r+0.5*sigma*sigma)*T)/(sigma*root),d2=d1-sigma*root,disc=Math.exp(-r*T),density=pdf(d1);
  return {delta:isCall?cdf(d1):cdf(d1)-1,gamma:density/(S*sigma*root),
   theta:(isCall?(-S*density*sigma/(2*root)-r*K*disc*cdf(d2)):(-S*density*sigma/(2*root)+r*K*disc*cdf(-d2)))/365,
   vega:S*density*root/100};
 }
 function daysLeft(expiry) {
  if(!expiry) return null;
  let d=new Date(expiry);
  if(Number.isNaN(d.getTime())) {
   const m=String(expiry).match(/^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/);
   if(!m) return null;
   d=new Date(m[2]+" "+m[1]+", "+m[3]+" 15:30:00 GMT+0530");
  }
  return Number.isNaN(d.getTime())?null:Math.max(0,(d.getTime()-Date.now())/86400000);
 }
 function render() {
  if(typeof analysisData==="undefined"||!analysisData||analysisData.status!=="LIVE") return;
  const S=n(analysisData.market&&analysisData.market.spot),days=daysLeft(analysisData.expiry),rows=analysisData.rows||[],atm=n(analysisData.metrics&&analysisData.metrics.atm);
  if(!S||days===null||atm===null) return;
  $("gSpot").textContent=S.toLocaleString("en-IN",{maximumFractionDigits:2});
  $("gDays").textContent=days.toFixed(1)+" days";
  let atmRow=null;
  rows.forEach(row=>{if(!atmRow||Math.abs(Number(row.strike)-atm)<Math.abs(Number(atmRow.strike)-atm))atmRow=row;});
  const atmIv=n(atmRow&&atmRow.ce&&atmRow.ce.impliedVolatility);
  $("gIv").textContent=atmIv===null?"—":fmt(atmIv,2)+"%";
  const range=$("greekRange")?$("greekRange").value:"5";
  const selected=rows.filter(row=>range==="all"||Math.abs(Number(row.strike)-atm)<=Number(range)*50);
  $("greekRows").innerHTML=selected.map(row=>{
   const K=n(row.strike),ceIv=n(row.ce&&row.ce.impliedVolatility),peIv=n(row.pe&&row.pe.impliedVolatility);
   const ce=calc(S,K,ceIv,days,true),pe=calc(S,K,peIv,days,false);
   const c=(g,k)=>g?fmt(g[k],4):"—";
   return '<tr class="'+(K===atm?"atm-row":"")+'"><td class="positive-text">'+fmt(n(row.ce&&row.ce.lastPrice),2)+'</td><td>'+fmt(ceIv,2)+'</td><td class="greek-positive">'+c(ce,"delta")+'</td><td>'+c(ce,"gamma")+'</td><td class="greek-negative">'+c(ce,"theta")+'</td><td>'+c(ce,"vega")+'</td><td class="strike">'+fmt(K,0)+'</td><td class="negative-text">'+fmt(n(row.pe&&row.pe.lastPrice),2)+'</td><td>'+fmt(peIv,2)+'</td><td class="greek-negative">'+c(pe,"delta")+'</td><td>'+c(pe,"gamma")+'</td><td class="greek-negative">'+c(pe,"theta")+'</td><td>'+c(pe,"vega")+'</td></tr>';
  }).join("")||'<tr><td colspan="13" class="empty">No contracts available in this strike window.</td></tr>';
 }
 $("greekRange")&&$("greekRange").addEventListener("change",render);
 document.querySelectorAll('[data-page="greeks"],[data-go="greeks"]').forEach(el=>el.addEventListener("click",()=>setTimeout(render,0)));
 setInterval(render,2000);
})();