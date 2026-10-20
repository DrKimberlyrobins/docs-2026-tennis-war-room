#!/usr/bin/env python3
"""Refresh verified ATP/WTA player intelligence for fixtures, never guess missing values.
Requires GitHub Actions secret RAPIDAPI_KEY for tennis-api-atp-wta-itf.
Provider docs: https://docs.tennis-api.com/players
"""
import json, os, sys, time, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/"data"
HOST="tennis-api-atp-wta-itf.p.rapidapi.com"
KEY="".join(os.getenv("RAPIDAPI_KEY","").split())  # GitHub secrets may contain copied line breaks
BUDGET=int(os.getenv("TENNIS_PLAYER_BUDGET","40"))
REQUEST_LIMIT=int(os.getenv("TENNIS_REQUEST_LIMIT","45"))
REQUEST_COUNT=0
MAX_AGE=int(os.getenv("TENNIS_CACHE_MAX_HOURS","96"))
def read(path,default):
    try:return json.loads((DATA/path).read_text(encoding="utf-8"))
    except (FileNotFoundError,json.JSONDecodeError):return default
def fetch(path,query=None):
    global REQUEST_COUNT
    if REQUEST_COUNT>=REQUEST_LIMIT:raise RuntimeError("Daily request safety budget exhausted")
    REQUEST_COUNT+=1
    if not KEY:raise RuntimeError("Missing RAPIDAPI_KEY secret; no fabricated player data.")
    url="https://"+HOST+"/tennis/v2/"+path
    if query:url+="?"+urllib.parse.urlencode(query)
    req=urllib.request.Request(url,headers={"X-RapidAPI-Host":HOST,"X-RapidAPI-Key":KEY,"Accept":"application/json","User-Agent":"DocsTennisWarRoom/2"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req,timeout=25) as resp:
                obj=json.load(resp)
            if isinstance(obj,dict) and obj.get("err"):raise ValueError(str(obj["err"]))
            return obj.get("data") if isinstance(obj,dict) else obj
        except urllib.error.HTTPError as e:
            if e.code in (401,403,429):raise RuntimeError("API authorization/quota failure HTTP "+str(e.code)) from e
            if attempt==2:raise
        except (TimeoutError,urllib.error.URLError):
            if attempt==2:raise
        time.sleep(2**attempt)
def valid(x):return isinstance(x,dict) and bool(x)
def number(x):
    try:
        if x is None or x=="":return None
        return float(x)
    except (ValueError,TypeError):return None
def percent(n,d):
    n,d=number(n),number(d)
    return {"value":n,"count":d} if n is not None and d is not None and d>0 and 0<=n<=d else None
def normalize_stats(s):
    if not valid(s):return None
    service=s.get("serviceStats") or {}
    bps=s.get("breakPointsServeStats") or {}
    bpr=s.get("breakPointsRtnStats") or {}
    first=percent(service.get("winningOnFirstServeGm"),service.get("winningOnFirstServeOfGm"))
    second=percent(service.get("winningOnSecondServeGm"),service.get("winningOnSecondServeOfGm"))
    saved=percent(bps.get("breakPointSavedGm"),bps.get("breakPointFacedGm"))
    converted=percent(bpr.get("breakPointWonGm"),bpr.get("breakPointChanceGm"))
    return {"serviceStats":{"winningOnFirstServe":first,"winningOnSecondServe":second},
            "breakPointsServe":{"breakPointSave":saved},
            "breakPointsRtn":{"breakPointWon":converted}}
def recent(rows,pid):
    clean=[]
    for m in rows if isinstance(rows,list) else []:
        a=str(m.get("player1Id") or (m.get("player1") or {}).get("id") or "")
        b=str(m.get("player2Id") or (m.get("player2") or {}).get("id") or "")
        if str(pid) not in (a,b):continue
        if str(m.get("result_type","")).lower() in ("walkover","default") or "w/o" in str(m.get("result","")).lower():continue
        winner=str(m.get("match_winner") or a)
        if not winner:continue
        clean.append({"date":m.get("date"),"win":winner==str(pid),
          "opponent":(m.get("player2") if a==str(pid) else m.get("player1") or {}).get("name") if isinstance(m.get("player2") if a==str(pid) else m.get("player1"),dict) else None,
          "score":m.get("result"),"surfaceId":(m.get("tournament") or {}).get("courtId"),
          "tournament":(m.get("tournament") or {}).get("name"),"round":(m.get("round") or {}).get("name") if isinstance(m.get("round"),dict) else None})
    clean.sort(key=lambda x:x.get("date") or "",reverse=True)
    return clean[:25]
def summary(matches,n):
    subset=matches[:n]
    return {"wins":sum(m["win"] for m in subset),"losses":sum(not m["win"] for m in subset),"n":len(subset)} if subset else None
def surface(rows):
    out={}
    for year in rows if isinstance(rows,list) else []:
        if not isinstance(year,dict):continue
        y=str(year.get("year") or "")
        if y!="2026":continue
        for s in year.get("surfaces") or []:
            if not isinstance(s,dict):continue
            court=str(s.get("courtId"))
            out[court]={"name":s.get("court"),"wins":s.get("courtWins"),"losses":s.get("courtLosses"),"year":2026}
    return out
def fresh(item):
    try:return datetime.fromisoformat(item.get("updatedAt","").replace("Z","+00:00"))>datetime.now(timezone.utc)-timedelta(hours=MAX_AGE)
    except (TypeError,ValueError):return False
def main():
    if not KEY:raise RuntimeError("Set RAPIDAPI_KEY as a repository Actions secret before running.")
    cache=read("player-cache.json",{})
    fixtures=[]
    fixture_errors=[]
    for tour in ("atp","wta"):
        old=read(tour+"-today.json",{})
        try:
            rows=fetch(tour+"/fixtures",{"include":"round,tournament,tournament.court,tournament.rank,h2h","filter":"PlayerGroup:singles","pageSize":500,"pageNo":1})
            if not isinstance(rows,list):raise ValueError("Unexpected fixtures response")
            # An empty valid schedule is not an API outage.
            payload={"data":rows,"updatedAt":now if "now" in locals() else datetime.now(timezone.utc).isoformat(),"source":"RapidAPI tennis v2"}
            (DATA/(tour+"-today.json")).write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        except Exception as e:
            fixture_errors.append(tour+": "+str(e)[:100])
            payload=old
        fixtures += [(tour,m) for m in payload.get("data",[]) if isinstance(m,dict)]
    players={}
    for tour,m in fixtures:
        for side in (1,2):
            p=m.get("player"+str(side)) or {}
            pid=p.get("id") or m.get("player"+str(side)+"Id")
            if pid:players[tour+":"+str(pid)]=(tour,str(pid),p.get("name"))
    now=datetime.now(timezone.utc).isoformat()
    # Prioritize complete matchups rather than scattering profiles across the entire calendar.
    # Fixtures are ordered by event date; already cached players get preference.
    ordered=[]
    seen=set()
    for tour,m in sorted(fixtures,key=lambda item:str(item[1].get("date") or "9999")):
        ids=[]
        for side in (1,2):
            obj=m.get("player"+str(side)) or {}
            pid=obj.get("id") or m.get("player"+str(side)+"Id")
            if pid:ids.append(tour+":"+str(pid))
        if len(ids)!=2:continue
        for key in ids:
            if key in players and key not in seen and not fresh(cache.get(key,{})):
                ordered.append((key,*players[key]))
                seen.add(key)
    for key,value in players.items():
        if key not in seen and not fresh(cache.get(key,{})):
            ordered.append((key,*value))
    pending=ordered
    updated=0;errors=fixture_errors[:]
    for k,tour,pid,name in pending[:min(BUDGET,max(0,REQUEST_LIMIT-REQUEST_COUNT))]:
        old=cache.get(k,{})
        try:
            profile=fetch(f"{tour}/player/profile/{pid}")
            if not valid(profile) or str(profile.get("id"))!=pid:raise ValueError("Profile ID mismatch")
            stats={} # optional detailed stats deferred for wider profile coverage
            past=[] # optional match history deferred for wider profile coverage
            surfaces=[] # optional surface stats deferred for wider profile coverage
            matches=recent(past,pid)
            profile["form"]=["w" if m["win"] else "l" for m in reversed(matches[:10])]
            season=normalize_stats(stats)
            cache[k]={"id":pid,"name":profile.get("name") or name,"tour":tour.upper(),
                "profile":profile,"season2026":old.get("season2026",{}), "careerStats":old.get("careerStats",{}),
                "recent5":old.get("recent5"),"recent10":old.get("recent10"),
                "recentMatches":old.get("recentMatches",[]),"surface2026":old.get("surface2026",{}),
                "rankingPoints":profile.get("points"),"rankingDate":profile.get("rankingDate") or (profile.get("curRank") or {}).get("date"),
                "sources":{"profile":"provider-profile","details":"previously cached if present; otherwise pending"},
                "updatedAt":now}
            updated+=1
        except Exception as e:
            errors.append(k+": "+str(e)[:140])
            if "authorization/quota" in str(e) or "budget exhausted" in str(e):break
            if old:cache[k]=old
        time.sleep(0.15)
    if updated:
        (DATA/"player-cache.json").write_text(json.dumps(cache,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    h2h=read("h2h-cache.json",{})
    h2h_budget=int(os.getenv("TENNIS_H2H_BUDGET","0"))
    h2h_updated=0
    for tour,m in fixtures:
        a=str(m.get("player1Id") or (m.get("player1") or {}).get("id") or "")
        b=str(m.get("player2Id") or (m.get("player2") or {}).get("id") or "")
        if not a or not b or h2h_updated>=h2h_budget:continue
        key=tour+":"+a+":"+b
        if fresh(h2h.get(key,{})):continue
        try:
            info=fetch(f"{tour}/h2h/info/{a}/{b}",{"includeAll":"true"})
            meetings=fetch(f"{tour}/h2h/matches/{a}/{b}",{"includeAll":"true","include":"round,tournament.court"})
            if not isinstance(info,list):info=[]
            if not isinstance(meetings,list):meetings=[]
            h2h[key]={"updatedAt":now,"player1Id":a,"player2Id":b,"tour":tour,"surfaces":info,"meetings":meetings[:20],"scope":"All levels including ITF and Challenger"}
            h2h_updated+=1
        except Exception as e:
            errors.append(key+" H2H: "+str(e)[:100])
            if "authorization/quota" in str(e):break
    if h2h_updated:(DATA/"h2h-cache.json").write_text(json.dumps(h2h,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    report={"updatedAt":now,"fixturePlayers":len(players),"cached":sum(k in cache for k in players),
      "refreshedThisRun":updated,"h2hRefreshed":h2h_updated,"requestsUsed":REQUEST_COUNT,"fixtures":len(fixtures),"fixtureErrors":fixture_errors,"requestLimit":REQUEST_LIMIT,"remaining":len(pending)-updated,"coveragePercent":round(100*sum(k in cache for k in players)/max(1,len(players)),2),"errors":errors[:20],
      "note":"No guessed rankings or percentages. API match-stats endpoint is not necessarily current-season only."}
    (DATA/"player-pipeline-status.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report))
    if errors and not updated and not h2h_updated:sys.exit(1)
if __name__=="__main__":main()
