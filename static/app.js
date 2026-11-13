const byId=id=>document.getElementById(id);
async function loadStatus(){
 const health=byId("health"),api=byId("api-state");
 try{
  const [h,s]=await Promise.all([fetch("/api/health",{cache:"no-store"}).then(r=>r.json()),fetch("/api/status",{cache:"no-store"}).then(r=>r.json())]);
  const online=h.status==="ok";health.textContent=online?"API connected":"API unavailable";api.textContent=online?"Online":"Offline";
  byId("reports").textContent=s.report_files??"—";
  byId("bias-note").textContent=s.message||"Waiting for validated market data.";
  byId("bias").textContent=s.prediction?.market_bias||"WAIT";
  byId("decision").textContent=s.prediction?.decision||"NO SIGNAL";
  if(s.prediction?.ce_seller_strength!==undefined)byId("ce-strength").textContent=s.prediction.ce_seller_strength;
  if(s.prediction?.pe_seller_strength!==undefined)byId("pe-strength").textContent=s.prediction.pe_seller_strength;
 }catch(e){health.textContent="API offline";api.textContent="Offline";byId("bias-note").textContent="Cannot reach the application API. No market signal is available.";}
}
async function loadHistory(){
 const note=byId("history-note"),list=byId("history-list");if(!note||!list)return;
 try{
  const response=await fetch("/api/history",{cache:"no-store"}),data=await response.json();
  if(!response.ok||!data.configured){note.textContent=data.message||"Historical storage is not configured.";return;}
  const items=data.collections||[];note.textContent=items.length+" archived collection(s) available.";
  if(!items.length){list.textContent="No archived collections found.";return;}
  list.replaceChildren();
  items.slice().reverse().slice(0,5).forEach(item=>{
   const row=document.createElement("div");row.className="history-row";
   const title=document.createElement("strong");title.textContent=item.collection_date;
   const detail=document.createElement("span");detail.textContent=(item.report_count??"—")+" reports · "+((item.report_dates||[]).join(", ")||"dates not listed");
   const link=document.createElement("a");link.href="/api/history/"+encodeURIComponent(item.collection_date)+"/download";link.textContent="Get archive link";
   link.addEventListener("click",async event=>{event.preventDefault();try{const result=await fetch(link.href).then(r=>r.json());if(!result.download_url)throw new Error(result.detail||"Link unavailable");window.location.href=result.download_url;}catch(error){note.textContent="Download failed: "+error.message;}});
   row.append(title,detail,link);list.append(row);
  });
 }catch(error){note.textContent="Cannot load archive: "+error.message;}
}
function refresh(){loadStatus();loadHistory();}
byId("refresh")?.addEventListener("click",refresh);byId("quick-refresh")?.addEventListener("click",refresh);
document.querySelectorAll(".bottom-nav a").forEach(link=>link.addEventListener("click",()=>{document.querySelectorAll(".bottom-nav a").forEach(a=>a.classList.remove("active"));link.classList.add("active");}));
refresh();
