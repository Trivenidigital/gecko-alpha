import sqlite3, json, os, bisect, statistics as st, datetime as dt, collections
con=sqlite3.connect("file:/root/gecko-alpha/scout.db?mode=ro",uri=True)
D="/root/xs_study"
def ts(s): return dt.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()*1000
btc=json.load(open(D+"/btc.json"))["prices"]; bt=[p[0] for p in btc]
def at(pr,t,tol=36*3600e3):
    times=[p[0] for p in pr]; i=bisect.bisect_left(times,t)
    cand=[j for j in (i-1,i) if 0<=j<len(pr)]
    j=min(cand,key=lambda j:abs(times[j]-t))
    return pr[j][1] if abs(times[j]-t)<=tol and pr[j][1]>0 else None
pc={r[0]:(r[1],r[2]) for r in con.execute("select coin_id,current_price,updated_at from price_cache")}
rows=con.execute("select source_channel_handle, token_id, created_at, resolution_snapshot_json from tg_social_signals where resolution_state='RESOLVED' order by created_at").fetchall()
first={}
for ch,tok,c,js in rows:
    k=(ch,tok)
    if k in first: continue
    p=None
    try: p=json.loads(js or "{}").get("price_usd")
    except Exception: pass
    first[k]=(c,p)
H=[7,30,90]
res=collections.defaultdict(lambda: collections.defaultdict(list))
cnt=collections.defaultdict(collections.Counter)
for (ch,tok),(c,p_snap) in first.items():
    cnt[ch]["first_calls"]+=1
    t=ts(c)
    chart=None
    if not tok.startswith("dex:"):
        f=f"{D}/charts/{tok}.json"
        if os.path.exists(f):
            d=json.load(open(f)); chart=d.get("prices") or []
            if len(chart)<2: chart=None
    p0=None
    if chart: p0=at(chart,t); src="chart"
    if p0 is None and p_snap: p0=p_snap; src="snap"
    if p0 is None: cnt[ch]["no_p0"]+=1; continue
    cnt[ch]["p0_"+src]+=1
    if chart and src=="chart":
        # sanity: chart vs snapshot
        if p_snap and not (0.5<=p0/p_snap<=2): cnt[ch]["chart_snap_disagree"]+=1; continue
        b0=at(btc,t)
        for h in H:
            p=at(chart,t+h*86400e3); 
            if p is not None and t+h*86400e3<=chart[-1][0]: res[ch][h].append(p/p0)
        res[ch]["to_0918"].append(chart[-1][1]/p0)
        if b0: res[ch]["btc_to_0918"].append(btc[-1][1]/b0)
    lp=pc.get(tok)
    if lp and lp[0] and lp[0]>0:
        days=(ts(lp[1])-t)/86400e3
        if days>=1:
            res[ch]["cache_latest"].append(lp[0]/p0); res[ch]["cache_days"].append(days)
def s(v):
    if not v: return "n=0"
    v=sorted(v); return f"n={len(v)} med={st.median(v):.2f} mean={st.mean(v):.2f} >=2x={100*sum(a>=2 for a in v)/len(v):.0f}% >=7x={100*sum(a>=7 for a in v)/len(v):.0f}% <=0.5x={100*sum(a<=.5 for a in v)/len(v):.0f}%"
for ch in sorted(cnt,key=lambda k:-cnt[k]["first_calls"]):
    print("##",ch,dict(cnt[ch]))
    for h in H: print(f"   +{h}d chart:",s(res[ch][h]))
    print("   call->2026-09-18 chart:",s(res[ch]["to_0918"]),"| BTC same:",f"{st.mean(res[ch]['btc_to_0918']):.2f}" if res[ch]["btc_to_0918"] else "-")
    cd=res[ch]["cache_days"]
    print("   call->latest price_cache:",s(res[ch]["cache_latest"]),f"| horizon days med={st.median(cd):.0f} min={min(cd):.0f} max={max(cd):.0f}" if cd else "")
# also overall + top multiples
allc=[(ch,tok) for (ch,tok) in first]
print("total first calls",len(first),"slug",sum(not t.startswith('dex:') for _,t in first),"dex",sum(t.startswith('dex:') for _,t in first))
print("slug calls with chart",sum((not t.startswith('dex:')) and os.path.exists(f"{D}/charts/{t}.json") for _,t in first))
