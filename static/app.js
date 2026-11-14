const byId=id=>document.getElementById(id);
function setText(id,value){
 const node=byId(id);if(node)node.textContent=value;
}
function setFeedState(label,title,detail,tone="unavailable"){
 const chip=byId("option-chain-state"),message=byId("option-chain-message");
 if(chip){chip.textContent=label;chip.dataset.tone=tone;}
 if(message){
  message.replaceChildren();
  const icon=document.createElement("span");icon.className="chain-state-icon";
  icon.textContent=tone==="error"?"!":tone==="stale"?"◷":tone==="ready"?"✓":"⌁";
  const copy=document.createElement("div");
  const heading=document.createElement("strong");heading.textContent=title;
  const note=document.createElement("small");note.textContent=detail;
  copy.append(heading,note);message.append(icon,copy);
 }
}
function applyFeedStatus(status){
 const snapshot=status?.market_snapshot;
 const live=status?.seller_engine?.live_data_connected===true;
 if(snapshot?.as_of){
  const asOf=new Date(snapshot.as_of);
  if(Number.isNaN(asOf.getTime())){
   setFeedState("Feed status error","Snapshot timestamp is invalid","No option-chain values are displayed until the timestamp can be verified.","error");
  }else if(Date.now()-asOf.getTime()>15*60*1000){
   setFeedState("Stale data","Last reported market snapshot is stale","Last snapshot: "+asOf.toLocaleString()+". No stale option-chain values are displayed.","stale");
  }else if(live){
   setFeedState("Snapshot reported","Market snapshot metadata received","As of "+asOf.toLocaleString()+". Strike rows remain hidden unless the validated chain payload is supplied.","ready");
  }else{
   setFeedState("Unavailable","Snapshot metadata is not backed by a connected feed","No option-chain values are displayed.","unavailable");
  }
 }else if(live){
  setFeedState("Awaiting snapshot","Feed is connected, but no current snapshot is available","The option-chain table remains empty until a validated snapshot arrives.","pending");
 }else{
  setFeedState("Unavailable","Option-chain feed is not connected","The current API has no validated live option-chain snapshot. No market values are fabricated.","unavailable");
 }
}
async function loadStatus(){
 const health=byId("health"),api=byId("api-state");
 try{
  const [hr,sr]=await Promise.all([fetch("/api/health",{cache:"no-store"}),fetch("/api/status",{cache:"no-store"})]);
  if(!hr.ok||!sr.ok)throw new Error("Status endpoint returned HTTP "+(!hr.ok?hr.status:sr.status));
  const [h,s]=await Promise.all([hr.json(),sr.json()]);
  const online=h.status==="ok";
  health.textContent=online?"API service online":"API unavailable";
  api.textContent=online?"Online":"Offline";
  setText("reports-count",s.report_files??"—");
  setText("bias-note",s.message||"Waiting for validated market data.");
  setText("bias",s.prediction?.market_bias||"WAIT");
  setText("decision",s.prediction?.decision||s.prediction?.final_decision||"NO SIGNAL");
  if(s.prediction?.ce_seller_strength!==undefined)setText("ce-strength",s.prediction.ce_seller_strength);
  if(s.prediction?.pe_seller_strength!==undefined)setText("pe-strength",s.prediction.pe_seller_strength);
  const live=s.seller_engine?.live_data_connected===true;
  setText("nse-feed-state",live?"Connected — awaiting snapshot":"Not connected");
  setText("training-state","OFF — locked");
  setText("archive-state",s.historical_storage==="backblaze_b2_configured"?"Configured":"Not configured");
  const quality=s.data_quality||{};
  setText("data-quality-state",quality.aligned===true?"Aligned":quality.status?("Blocked — "+quality.status):"Unavailable");
  setText("system-status-note",s.message||"System status received.");
  setText("status-checked-at",new Date().toLocaleTimeString());
  applyFeedStatus(s);
 }catch(e){
  health.textContent="API unavailable";api.textContent="Offline";
  setText("bias-note","Cannot reach the application API. No market signal is available.");
  setText("nse-feed-state","Status unavailable");
  setText("archive-state","Status unavailable");
  setText("data-quality-state","Status unavailable");
  setText("system-status-note","Error: "+e.message+". No market values are shown.");
  setText("status-checked-at","Check failed");
  setFeedState("API error","Could not retrieve option-chain status","The application API could not be reached. No market values are shown.","error");
 }
}
async function loadModels(){
 const grid=byId("engine-grid");if(!grid)return;
 grid.replaceChildren();
 const loading=document.createElement("article");loading.className="model-card model-card-loading";
 const loadingTitle=document.createElement("strong");loadingTitle.textContent="Loading engine catalogue…";
 const loadingNote=document.createElement("small");loadingNote.textContent="Checking the available analysis modules.";
 loading.append(loadingTitle,loadingNote);grid.append(loading);
 const descriptions={
  "CE seller":"Call-side OI build-up, premium movement and squeeze-risk evidence.",
  "PE seller":"Put-side OI build-up, premium movement and downside-risk evidence.",
  "OI/PCR":"Aggregate and change-in-OI put/call positioning.",
  "seller strength":"Relative CE and PE writing evidence across strikes.",
  "futures confirmation":"Futures price and open-interest confirmation.",
  "participant positioning":"FII / participant positioning, subject to adapter sign validation.",
  "volatility risk":"ATM IV, IV expansion and expiry proximity.",
  "seller trap risk":"OI addition with rising premiums as a squeeze-risk warning."
 };
 try{
  const response=await fetch("/api/engine/models",{cache:"no-store"});
  if(!response.ok)throw new Error("Engine catalogue returned HTTP "+response.status);
  const data=await response.json();
  const names=Array.isArray(data.parallel_subprocesses)?data.parallel_subprocesses:[];
  grid.replaceChildren();
  if(!names.length)throw new Error("No analysis modules were returned");
  names.forEach((name,index)=>{
   const card=document.createElement("article");card.className="model-card";
   const top=document.createElement("div");top.className="model-card-top";
   const number=document.createElement("span");number.className="model-index";number.textContent=String(index+1).padStart(2,"0");
   const status=document.createElement("span");status.className="model-state";
   status.textContent=data.live_data_connected===true?"Awaiting validated output":"Feed not connected";
   top.append(number,status);
   const title=document.createElement("strong");title.textContent=name;
   const note=document.createElement("small");note.textContent=descriptions[name]||"Independent seller-analysis input; awaiting validated data.";
   const footer=document.createElement("p");footer.textContent=data.live_data_connected===true?"No signal until a validated snapshot is supplied.":"Logic catalogue available; live analysis is unavailable.";
   card.append(top,title,note,footer);grid.append(card);
  });
 }catch(e){
  grid.replaceChildren();
  const error=document.createElement("article");error.className="model-card model-card-error";
  const title=document.createElement("strong");title.textContent="Engine catalogue unavailable";
  const note=document.createElement("small");note.textContent=e.message+". No analysis output is being generated.";
  error.append(title,note);grid.append(error);
 }
}
async function loadHistory(){
 const note=byId("history-note"),list=byId("history-list");if(!note||!list)return;
 try{const response=await fetch("/api/history",{cache:"no-store"}),data=await response.json();
  if(!response.ok||!data.configured){note.textContent=data.message||"Historical storage is not configured.";return;}
  const items=data.collections||[];note.textContent=items.length+" archived collection(s) available.";
  if(!items.length){list.textContent="No archived collections found.";return;}list.replaceChildren();
  items.slice().reverse().slice(0,5).forEach(item=>{const row=document.createElement("div");row.className="history-row";const title=document.createElement("strong");title.textContent=item.collection_date;const detail=document.createElement("span");detail.textContent=(item.report_count??"—")+" reports · "+((item.report_dates||[]).join(", ")||"dates not listed");const link=document.createElement("a");link.href="/api/history/"+encodeURIComponent(item.collection_date)+"/download";link.textContent="Get archive link";link.addEventListener("click",async event=>{event.preventDefault();try{const result=await fetch(link.href).then(r=>r.json());if(!result.download_url)throw new Error(result.detail||"Link unavailable");window.location.href=result.download_url;}catch(error){note.textContent="Download failed: "+error.message;}});row.append(title,detail,link);list.append(row);});
 }catch(error){note.textContent="Cannot load archive: "+error.message;}
}
const isoDate=d=>{const x=new Date(d);return x.getFullYear()+"-"+String(x.getMonth()+1).padStart(2,"0")+"-"+String(x.getDate()).padStart(2,"0")};
function renderHeatmap(records,from,to){
 const grid=byId("heatmap-grid"),months=byId("heatmap-months");grid.replaceChildren();months.replaceChildren();
 const start=new Date(from+"T00:00:00"),end=new Date(to+"T00:00:00");if(Number.isNaN(+start)||Number.isNaN(+end)||start>end){byId("heatmap-note").textContent="Choose a valid date range.";return;}
 const first=new Date(start);first.setDate(first.getDate()-((first.getDay()+6)%7));
 const last=new Date(end);last.setDate(last.getDate()+(6-((last.getDay()+6)%7)));
 const byDate=new Map((records||[]).map(r=>[r.date,r]));
 let current=new Date(first),lastMonth="",week=0,correct=0,incorrect=0,evaluated=0;
 while(current<=last){
  const monthKey=current.getFullYear()+"-"+current.getMonth();
  if(monthKey!==lastMonth){const label=document.createElement("span");label.style.gridColumn=String(week+1);label.textContent=current.toLocaleString("en",{month:"short"});months.append(label);lastMonth=monthKey;}
  for(let day=0;day<7;day++){
   const d=new Date(current);d.setDate(d.getDate()+day);const date=isoDate(d);const record=byDate.get(date);const cell=document.createElement("i");cell.className="heat-cell";
   if(d<start||d>end)cell.classList.add("future");
   else if(record?.result==="correct"){cell.classList.add("pass");correct++;evaluated++;}
   else if(record?.result==="incorrect"){cell.classList.add("fail");incorrect++;evaluated++;}
   else cell.classList.add("no-data");
   cell.title=date+(record?.result?": "+record.result:" : no evaluated result");cell.setAttribute("aria-label",cell.title);grid.append(cell);
  }
  current.setDate(current.getDate()+7);week++;
 }
 byId("evaluated-count").textContent=evaluated;byId("correct-count").textContent=correct;byId("incorrect-count").textContent=incorrect;byId("accuracy-rate").textContent=evaluated?((correct/evaluated)*100).toFixed(1)+"%":"—";
}
async function loadPerformance(){
 const from=byId("heat-from").value,to=byId("heat-to").value,note=byId("heatmap-note");
 renderHeatmap([],from,to);
 try{const response=await fetch("/api/performance?from="+encodeURIComponent(from)+"&to="+encodeURIComponent(to),{cache:"no-store"});if(!response.ok)throw new Error("Performance endpoint returned HTTP "+response.status);const data=await response.json();
  setText("evaluator-state",data.available?"Available":"Not connected");renderHeatmap(data.records||[],from,to);note.textContent=data.available?(data.message||"Green = correct next-day call; red = incorrect; grey = no evaluated result."):(data.message||"Prediction outcome history is not connected yet. Grey squares mean no evaluated result, not a failed prediction.");
 }catch(e){setText("evaluator-state","Error");note.textContent="Could not load evaluated prediction history. No results are being fabricated.";}
}
function initHeatmap(){const today=new Date(),from=new Date(today);from.setDate(from.getDate()-364);byId("heat-from").value=isoDate(from);byId("heat-to").value=isoDate(today);byId("heat-apply").addEventListener("click",loadPerformance);loadPerformance();}
function initMotionAndNavigation(){
 const navLinks=[...document.querySelectorAll(".bottom-nav a")];
 const targets=navLinks.map(link=>document.querySelector(link.getAttribute("href"))).filter(Boolean);
 const activate=id=>navLinks.forEach(link=>link.classList.toggle("active",link.getAttribute("href")==="#"+id));
 navLinks.forEach(link=>link.addEventListener("click",event=>{
  const target=document.querySelector(link.getAttribute("href"));if(!target)return;
  event.preventDefault();
  const reduce=window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({behavior:reduce?"auto":"smooth",block:"start"});
  activate(target.id||"top");
  if(!reduce){
   document.querySelectorAll(".tab-transition").forEach(node=>node.classList.remove("tab-transition"));
   void target.offsetWidth;
   target.classList.add("tab-transition");
   link.classList.remove("tab-tapped");void link.offsetWidth;link.classList.add("tab-tapped");
   window.setTimeout(()=>target.classList.remove("tab-transition"),700);
   window.setTimeout(()=>link.classList.remove("tab-tapped"),500);
  }
 }));
 if("IntersectionObserver" in window){
  const motionObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add("is-visible");motionObserver.unobserve(entry.target);}}),{threshold:.08,rootMargin:"0px 0px -35px 0px"});
  document.querySelectorAll(".section-block").forEach(section=>{section.classList.add("reveal-ready");motionObserver.observe(section);});
  const navObserver=new IntersectionObserver(entries=>{const visible=entries.filter(entry=>entry.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];if(visible)activate(visible.target.id||"top");},{rootMargin:"-25% 0px -60% 0px",threshold:[0,.1,.25,.5]});
  targets.forEach(target=>navObserver.observe(target));
 }
}
function refresh(){loadStatus();loadHistory();loadModels();}
byId("refresh")?.addEventListener("click",refresh);byId("quick-refresh")?.addEventListener("click",refresh);
refresh();initHeatmap();initMotionAndNavigation();
