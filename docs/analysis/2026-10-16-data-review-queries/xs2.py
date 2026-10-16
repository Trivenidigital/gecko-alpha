src=open("/tmp/data-review/xs/analyze2.py").read().split('print("rule | n | mean')[0]
exec(src)
import random
random.seed(7)
miss={c["cid"] for c in live if c["cid"] not in stored}
bad_all=[x for x in coins if not x["dead"] and x["cid"] in stored and not (0.5<=x["p0"]/stored[x["cid"]][1]<=2)]
print("stored-price coins:",len(stored),"| bad ratio (excluded now):",len(bad_all) if False else "see below")
# recompute: coins list here is already post-exclusion; recount from audit
def r(lab,v):
    if len(v)<5: print(lab,"n",len(v)); return
    xs=sorted(c["x"] for c in v); b=m(v,"bx")
    boots=sorted(st.mean(random.choices(xs,k=len(xs))) for _ in range(4000))
    print(f"{lab:38} n={len(v):4} mean={st.mean(xs):.2f} med={st.median(xs):.2f} btc={b:.2f} ex3={st.mean(xs[:-3]):.2f} >=2x={100*sum(a>=2 for a in xs)/len(xs):.0f}% <=0.5x={100*sum(a<=.5 for a in xs)/len(xs):.0f}% 7x+={sum(a>=7 for a in xs)} CI90={boots[200]:.2f}-{boots[3800]:.2f}")
print("unverifiable (no stored price) coins:",len(miss))
um=[c for c in live if c["cid"] in miss]
print("unverifiable top:",sorted([(c["cid"],round(c["x"],1),round(c["m0"]/1e6,3),sorted(c["surf"])) for c in um],key=lambda t:-t[1])[:12])
print("unverifiable first-flag surfaces:",collections.Counter(s for c in um for s in c["surf"]).most_common())
V=[c for c in live if c["cid"] not in miss]
r("ALL verifiable (ex 69 unverifiable)",V)
r("ALL incl unverifiable",live)
for mo in sorted({c["t0"][:7] for c in V}): r(f"month {mo}",[c for c in V if c["t0"][:7]==mo])
for ch in ("solana","robinhood","binance-smart-chain","multi","ethereum","base","none"): r(f"chain {ch}",[c for c in V if c.get("chain")==ch])
for s in ["prediction","acceleration","slow_burn","paper_trade","volume_spike","momentum_7d","velocity"]:
    r(f"surface {s}",[c for c in V if s in c["surf"]])
    r(f"surface {s} ex-robinhood",[c for c in V if s in c["surf"] and c.get("chain")!="robinhood"])
F4=lambda c:F1(c) and F3(c)
r("F4 verifiable",[c for c in V if F4(c)])
r("F4 & m0>=5M",[c for c in V if F4(c) and c["m0"]>=5e6])
r("F4 & m0>=5M ex-robinhood",[c for c in V if F4(c) and c["m0"]>=5e6 and c.get("chain")!="robinhood"])
r("F4 & m0<5M",[c for c in V if F4(c) and c["m0"]<5e6])
r("momentum_7d & m0>=5M",[c for c in V if "momentum_7d" in c["surf"] and c["m0"]>=5e6])
r("n_surfaces>=3",[c for c in V if len(c["surf"])>=3])
r("n_surfaces==1",[c for c in V if len(c["surf"])==1])
r("persist>=7d (any mcap)",[c for c in V if c["persist"]])
