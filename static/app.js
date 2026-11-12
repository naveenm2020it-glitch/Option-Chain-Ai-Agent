async function loadStatus(){const health=document.getElementById("health");try{const [h,s]=await Promise.all([fetch("/api/health").then(r=>r.json()),fetch("/api/status").then(r=>r.json())]);health.textContent=h.status==="ok"?"API online":"API unavailable";document.getElementById("reports").textContent=s.report_files;document.getElementById("bias-note").textContent=s.message;}catch(e){health.textContent="API offline";document.getElementById("bias-note").textContent="Cannot reach the application API.";}}loadStatus();


async function loadHistory(){
  const note=document.getElementById("history-note");
  const list=document.getElementById("history-list");
  if(!note||!list)return;
  try{
    const response=await fetch("/api/history");
    const data=await response.json();
    if(!response.ok||!data.configured){note.textContent=data.message||"Historical storage is not configured on the web server.";return;}
    const items=data.collections||[];
    note.textContent=items.length+" archived collection(s) available.";
    if(!items.length){list.textContent="No archived collections found yet.";return;}
    list.innerHTML=items.slice().reverse().map(item=>{
      const row=document.createElement("div");
      row.className="history-row";
      const title=document.createElement("strong");
      title.textContent=item.collection_date;
      const detail=document.createElement("span");
      detail.textContent=(item.report_count??"—")+" reports · dates: "+((item.report_dates||[]).join(", ")||"not listed");
      const link=document.createElement("a");
      link.href="/api/history/"+encodeURIComponent(item.collection_date)+"/download";
      link.textContent="Get archive link";
      link.addEventListener("click",async event=>{
        event.preventDefault();
        try{
          const result=await fetch(link.href).then(r=>r.json());
          if(!result.download_url)throw new Error(result.detail||"Link unavailable");
          window.location.href=result.download_url;
        }catch(error){note.textContent="Download failed: "+error.message;}
      });
      row.append(title,detail,link);list.append(row);return row.outerHTML;
    }).join("");
  }catch(error){note.textContent="Cannot load historical archive: "+error.message;}
}
loadHistory();
