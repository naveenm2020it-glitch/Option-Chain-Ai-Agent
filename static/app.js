const $=id=>document.getElementById(id);
const fmt=value=>value===null||value===undefined||value===""?"—":Number(value).toLocaleString("en-IN",{maximumFractionDigits:2});
const compact=value=>value===null||value===undefined?"—":Intl.NumberFormat("en-IN",{notation:"compact",maximumFractionDigits:1}).format(Number(value));
const safe=value=>String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let analysisData=null,marketData=null,activePage="overview",selectedExpiry="",lastRefresh=null;
const titles={overview:"Market Overview",chain:"Live Option Chain",oi:"OI Intelligence",maxpain:"Max Pain Analysis",analyzer:"AI Strike Analyzer",alerts:"Entry Alerts",performance:"Accuracy & Records",export:"Data Export"};
async function api(url){const response=await fetch(url,{cache:"no-store"});const data=await response.json();if(!response.ok)throw Error(data.detail||"Request failed");return data}
function setPage(name){activePage=name;if(window.__niftySetPage){window.__niftySetPage([...document.querySelectorAll("#pageTrack .page")].findIndex(el=>el.id===name));return;}document.querySelectorAll(".page").forEach(el=>el.classList.toggle("active",el.id===name));$("pageTitle").textContent=titles[name]||"NIFTY LAB";if(name==="export")updateExportState()}
document.querySelectorAll("[data-page]").forEach(button=>button.addEventListener("click",()=>setPage(button.dataset.page)));
/* Vertical screen navigation: one screen at a time, up/down gestures and tab controls. */
(function setupVerticalScreens(){
 const sidebar=$("sidebar"),topbar=document.querySelector(".topbar"),main=document.querySelector(".main");
 if(!sidebar||!topbar||!main)return;
 const nav=document.createElement("nav");nav.id="screenSwitcher";nav.setAttribute("aria-label","Trading screens");
 const buttons=[...sidebar.querySelectorAll("[data-page]")];
 buttons.forEach((button,i)=>{button.classList.add("screen-tab");button.innerHTML='<span class="tab-index">'+String(i+1).padStart(2,"0")+'</span><span>'+button.textContent.trim()+'</span>';nav.appendChild(button);});
 topbar.insertAdjacentElement("afterend",nav);sidebar.remove();
 const pages=[...main.querySelectorAll(":scope > section.page")];
 const viewport=document.createElement("div");viewport.id="pageViewport";
 const track=document.createElement("div");track.id="pageTrack";
 pages.forEach(page=>track.appendChild(page));viewport.appendChild(track);nav.insertAdjacentElement("afterend",viewport);
 document.body.classList.add("dark");
 $("themeBtn").textContent="☀";
 let current=0,startX=0,startY=0,gesture=false;
 const show=index=>{
  current=Math.max(0,Math.min(pages.length-1,index));
  track.style.transform='translate3d(0,-'+(current*100)+'%,0)';
  buttons.forEach((button,i)=>{button.classList.toggle("active",i===current);button.setAttribute("aria-current",i===current?"page":"false")});
  $("pageTitle").textContent=titles[pages[current]?.id]||"NIFTY LAB";
  if(pages[current]?.id==="export")updateExportState();
  viewport.setAttribute("aria-label",(titles[pages[current]?.id]||"")+" screen");
 };
 window.__niftySetPage=show;
 buttons.forEach((button,i)=>button.addEventListener("click",()=>show(i)));
 viewport.addEventListener("touchstart",event=>{if(event.touches.length!==1)return;startX=event.touches[0].clientX;startY=event.touches[0].clientY;gesture=true},{passive:true});
 viewport.addEventListener("touchend",event=>{if(!gesture)return;gesture=false;const dx=event.changedTouches[0].clientX-startX,dy=event.changedTouches[0].clientY-startY;if(Math.abs(dy)>65&&Math.abs(dy)>Math.abs(dx)*1.2)show(current+(dy<0?1:-1));},{passive:true});
 show(0);
})();document.querySelectorAll("[data-go]").forEach(button=>button.addEventListener("click",()=>setPage(button.dataset.go)));
$("themeBtn").addEventListener("click",()=>{document.body.classList.toggle("dark");$("themeBtn").textContent=document.body.classList.contains("dark")?"☀":"◐";});
$("menuBtn").addEventListener("click",()=> $("sidebar").classList.toggle("open"));
$("refreshBtn").addEventListener("click",()=>refresh(true));
$("analyzeBtn").addEventListener("click",()=>refresh(true));
$("rangeSelect").addEventListener("change",renderFullChain);
$("expirySelect").addEventListener("change",()=>{selectedExpiry=$("expirySelect").value;refresh(true)});
function setStatus(status,stamp){const state=$("marketState");state.innerHTML="<i></i> "+safe(status||"UNAVAILABLE");state.dataset.state=status||"UNAVAILABLE";$("side-status").textContent=status||"UNAVAILABLE";$("sourceStamp").textContent=stamp||"No verified source timestamp";$("qualityStatus").textContent=status||"UNAVAILABLE";$("qualityTime").textContent=stamp||"—";}
function expiryMenu(){const select=$("expirySelect");if(!marketData)return;const current=selectedExpiry||analysisData?.expiry||marketData.expiry_dates?.[0]||"";select.innerHTML=(marketData.expiry_dates||[]).map(x=>'<option value="'+safe(x)+'" '+(x===current?"selected":"")+'>'+safe(x)+'</option>').join("");selectedExpiry=current;}
function updateOverview(){const m=marketData,met=analysisData?.metrics;if(!m)return;renderSignalEngine();$("spot").textContent=fmt(m.spot);$("spotTime").textContent=m.source_timestamp?"Exchange: "+m.source_timestamp:"No source timestamp";$("atm").textContent=fmt(met?.atm);$("maxPain").textContent=fmt(met?.max_pain);$("maxPainGap").textContent=met?.max_pain!=null&&m.spot!=null?((Number(m.spot)-Number(met.max_pain))>0?"+":"")+fmt(Number(m.spot)-Number(met.max_pain))+" pts from spot":"Selected expiry";$("pcr").textContent=fmt(met?.pcr);$("mpValue").textContent=fmt(met?.max_pain);$("mpSpot").textContent=fmt(m.spot);$("mpDistance").textContent=met?.max_pain!=null&&m.spot!=null?fmt(Number(m.spot)-Number(met.max_pain))+" pts":"—";$("ceOi").textContent=compact(met?.total_ce_oi);$("peOi").textContent=compact(met?.total_pe_oi);$("oiPcr").textContent=fmt(met?.pcr);$("scanCount").textContent=fmt(analysisData?.scan_count);$("alertCandidates").textContent=fmt(analysisData?.signals?.length||0);$("qualitySource").textContent=m.source||"No approved provider configured";renderBias();renderLevels();renderOiRanks();renderMaxPain();renderOverviewChain();renderFullChain();renderWatchlist();}
function renderBias(){const rawPcr=analysisData?.metrics?.pcr;const pcr=rawPcr==null?NaN:Number(rawPcr);let score=Number.isFinite(pcr)?Math.max(0,Math.min(100,Math.round(50+(pcr-1)*55))):null;$("biasScore").textContent=score??"—";const tag=$("biasTag");if(!Number.isFinite(pcr)){tag.textContent="WAITING";$("biasTitle").textContent="Waiting for live market data";$("biasText").textContent="Market interpretation will appear when verified option-chain data is received.";return}if(pcr>=1.2){tag.textContent="PUT OI HEAVY";$("biasTitle").textContent="Put-side OI concentration";$("biasText").textContent="Put open interest exceeds call open interest. Treat this as a positioning observation, not a bullish forecast.";}else if(pcr<=.8){tag.textContent="CALL OI HEAVY";$("biasTitle").textContent="Call-side OI concentration";$("biasText").textContent="Call open interest exceeds put open interest. Treat this as a positioning observation, not a bearish forecast.";}else{tag.textContent="BALANCED";$("biasTitle").textContent="Balanced OI distribution";$("biasText").textContent="Call and put open interest are relatively balanced for the selected expiry.";}}
function levelMarkup(items,side){if(!items?.length)return '<div class="empty">No OI levels returned</div>';return items.slice(0,3).map((x,i)=>'<div class="level-row"><span><b>'+fmt(x.strike)+'</b><small> #'+(i+1)+'</small></span><span><b>'+compact(x.oi)+'</b><small> ΔOI '+compact(x.change_oi)+'</small></span></div>').join("")}
function renderLevels(){const x=analysisData?.metrics;$("supportLevels").innerHTML=levelMarkup(x?.support,"put");$("resistanceLevels").innerHTML=levelMarkup(x?.resistance,"call");}
function rankMarkup(items,side){if(!items?.length)return '<div class="empty">No verified OI values</div>';return items.map((x,i)=>'<div class="rank-row"><div class="rank-num">'+(i+1)+'</div><div class="rank-info"><b>'+fmt(x.strike)+' · '+(side==="PE"?"Put OI support":"Call OI resistance")+'</b><small>Change in OI: '+fmt(x.change_oi)+'</small></div><div class="rank-value"><b>'+compact(x.oi)+'</b><small>Open interest</small></div></div>').join("")}
function renderOiRanks(){const x=analysisData?.metrics;$("supportTable").innerHTML=rankMarkup(x?.support,"PE");$("resistanceTable").innerHTML=rankMarkup(x?.resistance,"CE");}
function renderMaxPain(){const x=analysisData?.metrics;if(!x){$("maxPainSummary").textContent="Max Pain is unavailable until a valid option-chain snapshot is received.";return}$("maxPainSummary").textContent="Selected expiry: "+(analysisData.expiry||"—")+" · Max Pain: "+fmt(x.max_pain)+" · PCR: "+fmt(x.pcr)+". The calculation uses current OI and intrinsic payout across the selected expiry.";}
function chainRows(rows){return rows.map(r=>'<tr class="'+(Number(r.strike)===Number(analysisData?.metrics?.atm)?"atm-row":"")+'"><td>'+compact(r.ce.openInterest)+'</td><td class="'+(Number(r.ce.changeinOpenInterest)>=0?"positive-text":"negative-text")+'">'+compact(r.ce.changeinOpenInterest)+'</td><td>'+compact(r.ce.totalTradedVolume)+'</td><td class="positive-text">'+fmt(r.ce.lastPrice)+'</td><td class="strike">'+fmt(r.strike)+'</td><td class="negative-text">'+fmt(r.pe.lastPrice)+'</td><td>'+compact(r.pe.totalTradedVolume)+'</td><td class="'+(Number(r.pe.changeinOpenInterest)>=0?"positive-text":"negative-text")+'">'+compact(r.pe.changeinOpenInterest)+'</td><td>'+compact(r.pe.openInterest)+'</td></tr>').join("")}
function renderFullChain(){const rows=analysisData?.rows||[];const atm=Number(analysisData?.metrics?.atm);const range=$("rangeSelect")?.value||"500";const filtered=rows.filter(r=>range==="all"||Math.abs(Number(r.strike)-atm)<=Number(range));$("fullChain").innerHTML=filtered.length?chainRows(filtered):'<tr><td colspan="9" class="empty">No contracts available for this range.</td></tr>';}
function renderOverviewChain(){const rows=analysisData?.near_rows||[];const atm=Number(analysisData?.metrics?.atm);const filtered=rows.filter(r=>Math.abs(Number(r.strike)-atm)<=100);$("overviewChain").innerHTML=filtered.length?filtered.map(r=>'<tr class="'+(Number(r.strike)===atm?"atm-row":"")+'"><td>'+compact(r.ce.openInterest)+'</td><td>'+compact(r.ce.changeinOpenInterest)+'</td><td class="positive-text">'+fmt(r.ce.lastPrice)+'</td><td class="strike">'+fmt(r.strike)+'</td><td class="negative-text">'+fmt(r.pe.lastPrice)+'</td><td>'+compact(r.pe.changeinOpenInterest)+'</td><td>'+compact(r.pe.openInterest)+'</td></tr>').join(""):'<tr><td colspan="7" class="empty">No contracts available.</td></tr>';}
function renderSignalEngine(){const e=analysisData?.signal_engine||{};const c=e.selected_candidate||null;if($("engineDirection"))$("engineDirection").textContent=(e.direction||"WAIT")+" · "+(e.decision||"NO_TRADE");if($("engineReason"))$("engineReason").textContent=e.note||((e.missing_inputs||[]).join(", ")||"No entry setup has passed the snapshot confluence threshold.");if($("engineContract"))$("engineContract").textContent=c?c.strike+" "+c.side+" · "+c.expiry:"—";if($("engineEntry"))$("engineEntry").textContent=fmt(c?.entry_price);if($("engineTarget"))$("engineTarget").textContent=fmt(c?.target_price);if($("engineStop"))$("engineStop").textContent=fmt(c?.stop_price);if($("engineEta"))$("engineEta").textContent=c?fmt(c.estimated_minutes_to_target)+" min":"—";const g=c?.greeks||{};if($("engineDelta"))$("engineDelta").textContent=fmt(g.delta);if($("engineGamma"))$("engineGamma").textContent=fmt(g.gamma);if($("engineTheta"))$("engineTheta").textContent=fmt(g.theta_per_calendar_day);if($("engineVega"))$("engineVega").textContent=fmt(g.vega_per_1_vol_point);}
function renderWatchlist(){const xs=analysisData?.signals||[];$("topScore").textContent=xs.length?fmt(xs[0].score):"—";$("watchlist").innerHTML=xs.length?xs.map(x=>'<tr><td><b>'+safe(x.direction)+'</b><br><small>'+fmt(x.strike)+' · '+safe(x.expiry)+'</small></td><td>'+fmt(x.ltp)+'</td><td class="'+(x.premium_change_pct>=0?"positive-text":"negative-text")+'">'+fmt(x.premium_change_pct)+'%</td><td class="'+(x.change_oi>=0?"positive-text":"negative-text")+'">'+compact(x.change_oi)+'</td><td>'+compact(x.volume)+'</td><td>'+fmt(x.spread_pct)+'%</td><td>'+fmt(x.iv)+'%</td><td class="watch-score">'+fmt(x.score)+'</td><td><span class="watch-badge">WATCH</span></td></tr>').join(""):'<tr><td colspan="9" class="empty">No qualified contracts in this snapshot.</td></tr>';}
async function refresh(force=false){try{const url="/api/analysis"+(selectedExpiry?"?expiry="+encodeURIComponent(selectedExpiry):"");const data=await api(url);analysisData=data;marketData=data.market;if(marketData.status==="LIVE"&&data.status==="LIVE"){setStatus("LIVE",marketData.source_timestamp||marketData.received_at);expiryMenu();updateOverview();$("alertStatus").textContent="MONITORING";$("alertTitle").textContent=(data.active_signals||[]).length?"Active option-chain signal":"No entry setup confirmed";const se=data.signal_engine||{};$("alertMessage").textContent=se.note||((se.direction||"WAIT")+" · "+(se.decision||"NO_TRADE")+". Signals require successive verified snapshots; premium candles are not used.");renderSignalEngine();}else{setStatus(marketData.status||"UNAVAILABLE",marketData.source_timestamp||marketData.received_at);expiryMenu();$("spot").textContent=fmt(marketData.spot);$("biasTitle").textContent="Live data unavailable";$("biasText").textContent=data.message||marketData.error||"Waiting for verified NSE response";$("alertStatus").textContent="PAUSED";$("alertTitle").textContent="Signal monitoring paused";$("alertMessage").textContent=data.message||marketData.error||"No verified exchange snapshot."; $("fullChain").innerHTML='<tr><td colspan="9" class="empty">No verified live option chain.</td></tr>';$("overviewChain").innerHTML='<tr><td colspan="7" class="empty">No verified live option chain.</td></tr>';}$("qualityTime").textContent=marketData.received_at||"—";lastRefresh=new Date();if(marketData.status==="LIVE")loadGreeks();}catch(error){setStatus("UNAVAILABLE","No response received");$("biasTitle").textContent="Unable to load market data";$("biasText").textContent=error.message;}}
function updateExportState(){const ok=analysisData?.status==="LIVE";["exportChain","exportOI","exportSignals"].forEach(id=>$(id).disabled=!ok);}
function downloadCsv(filename,rows){if(!rows?.length){alert("No verified data is available to export.");return}const keys=[...new Set(rows.flatMap(x=>Object.keys(x)))];const csv=[keys.join(","),...rows.map(row=>keys.map(k=>{let v=row[k];if(v&&typeof v==="object")v=JSON.stringify(v);return '"'+String(v??"").replace(/"/g,'""')+'"';}).join(","))].join("\r\n");const blob=new Blob(["\ufeff"+csv],{type:"text/csv;charset=utf-8;"});const url=URL.createObjectURL(blob);const a=document.createElement("a");a.href=url;a.download=filename;a.click();URL.revokeObjectURL(url);}
$("exportChain").addEventListener("click",()=>downloadCsv("nifty-option-chain.csv",(analysisData?.rows||[]).map(r=>({strike:r.strike,expiry:r.expiry,ce_oi:r.ce.openInterest,ce_change_oi:r.ce.changeinOpenInterest,ce_volume:r.ce.totalTradedVolume,ce_ltp:r.ce.lastPrice,ce_iv:r.ce.impliedVolatility,pe_ltp:r.pe.lastPrice,pe_volume:r.pe.totalTradedVolume,pe_change_oi:r.pe.changeinOpenInterest,pe_oi:r.pe.openInterest,pe_iv:r.pe.impliedVolatility}))));
$("exportOI").addEventListener("click",()=>{const x=analysisData?.metrics;downloadCsv("nifty-oi-levels.csv",[...(x?.support||[]).map(r=>({type:"PUT OI SUPPORT",...r})),...(x?.resistance||[]).map(r=>({type:"CALL OI RESISTANCE",...r}))]);});
$("exportSignals").addEventListener("click",()=>downloadCsv("nifty-ai-watchlist.csv",analysisData?.signals||[]));
refresh();setInterval(()=>refresh(false),7000);

async function loadJournal(){
 try{
  const [signalData,perf]=await Promise.all([api("/api/signals?limit=100"),api("/api/performance")]);
  const items=signalData.items||[];
  const active=items.filter(x=>["ACTIVE","TRIGGERED"].includes(x.state));
  const rows=items.map(x=>'<tr><td>'+safe(x.id)+'</td><td><b>'+safe(x.contract_key)+'</b></td><td>'+fmt(x.actual_entry||x.entry_trigger)+'</td><td>'+fmt(x.target_price)+'</td><td>'+fmt(x.stop_price)+'</td><td>'+safe(x.state)+'</td><td>'+safe(x.outcome||"—")+'</td><td>'+fmt(x.net_points??x.gross_points)+'</td><td>'+safe(x.triggered_at||"—")+'</td><td>'+safe(x.closed_at||"—")+'</td></tr>').join("");
  if($("signalJournal"))$("signalJournal").innerHTML=rows||'<tr><td colspan="10" class="empty">No signal records yet. Signals are created only from verified live snapshots.</td></tr>';
  if($("performanceJournal"))$("performanceJournal").innerHTML=items.map(x=>'<tr><td>'+safe(x.id)+'</td><td>'+safe(x.contract_key)+'</td><td>'+safe(x.state)+'</td><td>'+safe(x.outcome||"—")+'</td><td>'+fmt(x.net_points??x.gross_points)+'</td><td>'+safe(x.created_at)+'</td></tr>').join("")||'<tr><td colspan="6" class="empty">No records yet.</td></tr>';
  if($("activeSignalCount"))$("activeSignalCount").textContent=active.length;
  if($("recordedSignalCount"))$("recordedSignalCount").textContent=signalData.count||items.length;
  if($("perfTargets"))$("perfTargets").textContent=perf.targets||0;
  if($("perfStops"))$("perfStops").textContent=perf.stop_losses||0;
  if($("perfAccuracy"))$("perfAccuracy").textContent=perf.accuracy==null?"—":fmt(perf.accuracy)+"%";
  if($("perfSample"))$("perfSample").textContent=(perf.closed_signals||0)+" resolved · "+(perf.unresolved||0)+" unresolved · "+(perf.time_exits||0)+" time exits · Net "+fmt(perf.net_points)+" points";
  if($("performanceBars")){
   const resolved=(perf.targets||0)+(perf.stop_losses||0);
   const targetPct=resolved?perf.targets/resolved*100:0;
   if($("performanceDonut"))$("performanceDonut").style.background=resolved?"conic-gradient(#16a36a 0 "+targetPct+"%, #dc6262 "+targetPct+"% 100%)":"conic-gradient(#aab4c2 0 100%)";
   if($("performanceDonutLabel"))$("performanceDonutLabel").textContent=resolved?fmt(targetPct)+"%":"—";
   if($("performanceLegend"))$("performanceLegend").innerHTML="<span>Target "+(perf.targets||0)+" · Stop "+(perf.stop_losses||0)+"</span><small>"+resolved+" resolved outcomes; unresolved/time exits excluded from accuracy.</small>";
   $("performanceBars").innerHTML='<div class="guide-item"><b>Target hits · '+(perf.targets||0)+'</b><span><i style="display:block;height:10px;width:'+targetPct+'%;background:#16a36a;border-radius:8px"></i></span></div><div class="guide-item"><b>Stop losses · '+(perf.stop_losses||0)+'</b><span><i style="display:block;height:10px;width:'+(100-targetPct)+'%;background:#dc6262;border-radius:8px"></i></span></div>';
  }
 }catch(e){if($("signalJournal"))$("signalJournal").innerHTML='<tr><td colspan="10" class="empty">Journal unavailable: '+safe(e.message)+'</td></tr>';}
}
loadJournal();
setInterval(loadJournal,10000);

async function loadGreeks(){
 try{
  const q=selectedExpiry?"?expiry="+encodeURIComponent(selectedExpiry):"";
  const data=await api("/api/greeks"+q);
  const items=data.items||[];
  if($("greeksMatrix"))$("greeksMatrix").innerHTML=items.length?items.map(x=>'<tr><td>'+fmt(x.strike)+'</td><td><b>'+safe(x.side)+'</b></td><td>'+fmt(x.ltp)+'</td><td>'+fmt(x.iv_percent)+'</td><td>'+fmt(x.delta)+'</td><td>'+fmt(x.gamma)+'</td><td>'+fmt(x.theta_per_calendar_day)+'</td><td>'+fmt(x.vega_per_1_vol_point)+'</td><td>'+fmt(x.rho_per_1_rate_point)+'</td></tr>').join(""):'<tr><td colspan="9" class="empty">'+safe(data.message||"No valid IV values for Greeks.")+'</td></tr>';
 }catch(e){if($("greeksMatrix"))$("greeksMatrix").innerHTML='<tr><td colspan="9" class="empty">'+safe(e.message)+'</td></tr>';}
}
