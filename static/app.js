const byId=id=>document.getElementById(id);
async function loadStatus(){
 const health=byId("health"),api=byId("api-state");
 try{
  const [h,s]=await Promise.all([fetch("/api/health",{cache:"no-store"}).then(r=>r.json()),fetch("/api/status",{cache:"no-store"}).then(r=>r.json())]);
  const online=h.status==="ok";health.textContent=online?"API connected":"API unavailable";api.textContent=online?"Online":"Offline";
  byId("reports-count").textContent=s.report_files??"—";byId("bias-note").textContent=s.message||"Waiting for validated market data.";
  byId("bias").textContent=s.prediction?.market_bias||"WAIT";byId("decision").textContent=s.prediction?.decision||"NO SIGNAL";
  if(s.prediction?.ce_seller_strength!==undefined)byId("ce-strength").textContent=s.prediction.ce_seller_strength;
  if(s.prediction?.pe_seller_strength!==undefined)byId("pe-strength").textContent=s.prediction.pe_seller_strength;
 }catch(e){health.textContent="API offline";api.textContent="Offline";byId("bias-note").textContent="Cannot reach the application API. No market signal is available.";}
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
 try{const response=await fetch("/api/performance?from="+encodeURIComponent(from)+"&to="+encodeURIComponent(to),{cache:"no-store"});const data=await response.json();
  renderHeatmap(data.records||[],from,to);note.textContent=data.available?(data.message||"Green = correct next-day call; red = incorrect; grey = no evaluated result."):(data.message||"Prediction outcome history is not connected yet. Grey squares mean no evaluated result, not a failed prediction.");
 }catch(e){note.textContent="Could not load evaluated prediction history. No results are being fabricated.";}
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
function refresh(){loadStatus();loadHistory();}
byId("refresh")?.addEventListener("click",refresh);byId("quick-refresh")?.addEventListener("click",refresh);
refresh();initHeatmap();initMotionAndNavigation();
