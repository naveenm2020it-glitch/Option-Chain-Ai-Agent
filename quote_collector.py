"""One-shot adapter for a user-selected broker/vendor normalized quote REST feed.

This is deliberately not tied to an invented NSE/broker endpoint. Configure a
provider URL only after confirming its API permits real-time quote access.
Expected response: {"quotes": [normalized_quote, ...]} or a JSON list.
"""
from __future__ import annotations
import os
import sys
import requests

REQUIRED=("symbol","expiry","strike","option_type","bid","ask","timestamp","lot_size")

def fetch_quotes(session=requests):
    url=os.getenv("QUOTE_PROVIDER_URL")
    if not url:
        raise RuntimeError("QUOTE_PROVIDER_URL is not configured; choose a permitted live market-data provider")
    token=os.getenv("QUOTE_PROVIDER_TOKEN","")
    headers={"Accept":"application/json"}
    if token:
        headers["Authorization"]="Bearer "+token
    response=session.get(url,headers=headers,timeout=(5,15))
    response.raise_for_status()
    payload=response.json()
    quotes=payload.get("quotes") if isinstance(payload,dict) else payload
    if not isinstance(quotes,list):
        raise ValueError("provider response must be a JSON list or an object with a quotes list")
    normalized=[]
    for index,item in enumerate(quotes):
        if not isinstance(item,dict) or any(k not in item for k in REQUIRED):
            raise ValueError(f"quote at index {index} is missing required normalized fields")
        normalized.append({**item,"source":str(item.get("source") or "configured_provider")})
    return normalized

def ingest_quotes(quotes, session=requests):
    base=os.getenv("PAPER_API_BASE_URL","").rstrip("/")
    token=os.getenv("PAPER_INGEST_TOKEN")
    if not base or not token:
        raise RuntimeError("PAPER_API_BASE_URL and PAPER_INGEST_TOKEN are required")
    accepted=0
    rejected=[]
    for q in quotes:
        try:
            r=session.post(base+"/api/paper/quotes",json=q,
                headers={"Authorization":"Bearer "+token},timeout=(5,15))
            if r.status_code==400:
                rejected.append({"contract":q.get("symbol"),"reason":r.text[:300]})
                continue
            r.raise_for_status()
            accepted+=1
        except requests.RequestException as exc:
            rejected.append({"contract":q.get("symbol"),"reason":str(exc)[:300]})
    return {"received":len(quotes),"accepted":accepted,"rejected":rejected}

def main():
    quotes=fetch_quotes()
    result=ingest_quotes(quotes)
    print(result)
    if result["accepted"]==0:
        sys.exit(2)

if __name__=="__main__":
    main()
