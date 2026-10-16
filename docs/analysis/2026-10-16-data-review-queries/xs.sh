set -e
mkdir -p /tmp/data-review/xs; cd /tmp/data-review/xs
cp /root/xs_study/*.py .
sed -i 's#file:/root/gecko-alpha/scout.db#file:/root/xs_study/extract/history_prices.db#' audit.py
sed -i 's#open("/root/xs_study/\([a-z0-9]*\)\.py")#open("/tmp/data-review/xs/\1.py")#g' *.py
grep -n "open(" rerun.py analyze2.py robust.py audit.py | head
echo "=== rerun(extract)"; python3 rerun.py 2>&1 | tail -20
echo "=== analyze2(extract)"; python3 analyze2.py 2>&1 | sed -n '1,40p'
cat > sweep.py <<'P'
src=open("/tmp/data-review/xs/analyze2.py").read().split('print("rule | n | mean')[0]
exec(src)
import random
random.seed(7)
def r(lab,v):
    if len(v)<5: print(lab,"n",len(v)); return
    xs=sorted(c["x"] for c in v); b=m(v,"bx")
    boots=sorted(st.mean(random.choices(xs,k=len(xs))) for _ in range(4000))
    print(f"{lab:38} n={len(v):4} mean={st.mean(xs):.2f} med={st.median(xs):.2f} btc={b:.2f} ex3={st.mean(xs[:-3]):.2f} >=2x={100*sum(a>=2 for a in xs)/len(xs):.0f}% <=0.5x={100*sum(a<=.5 for a in xs)/len(xs):.0f}% 7x+={sum(a>=7 for a in xs)} CI90={boots[200]:.2f}-{boots[3800]:.2f}")
print("== F4 sensitivity sweep (descriptive, NOT prereg)")
for A in (30,60,90,120,180):
  for V in (1.5,2.0,3.0,5.0):
    r(f"age<={A} vr>={V}",[c for c in live if c.get("age") is not None and c["age"]<=A and c.get("vr") is not None and c["vr"]>=V])
print("== vr buckets (all ages)")
for lo,hi in ((0,0.5),(0.5,1),(1,2),(2,5),(5,1e9)):
    r(f"vr {lo}-{hi}",[c for c in live if c.get("vr") is not None and lo<=c["vr"]<hi])
print("== entry mcap buckets")
for lo,hi in ((0,1e6),(1e6,5e6),(5e6,25e6),(25e6,100e6),(100e6,1e12)):
    r(f"m0 {lo/1e6:.0f}-{hi/1e6:.0f}M",[c for c in live if lo<=c["m0"]<hi])
print("== F4 x mcap")
F4=lambda c:F1(c) and F3(c)
for lo,hi in ((0,1e6),(1e6,5e6),(5e6,25e6),(25e6,1e12)):
    r(f"F4 m0 {lo/1e6:.0f}-{hi/1e6:.0f}M",[c for c in live if F4(c) and lo<=c["m0"]<hi])
print("== F4 x chain")
for ch in ("solana","robinhood","binance-smart-chain","multi","ethereum","base"):
    r(f"F4 chain {ch}",[c for c in live if F4(c) and c.get("chain")==ch])
print("== R0 by month")
for mo in sorted({c["t0"][:7] for c in live}):
    r(f"R0 {mo}",[c for c in live if c["t0"][:7]==mo])
print("== R0 by chain")
for ch in ("solana","robinhood","binance-smart-chain","multi","ethereum","base","none"):
    r(f"R0 chain {ch}",[c for c in live if c.get("chain")==ch])
print("== surfaces")
for s in ["prediction","acceleration","slow_burn","paper_trade","volume_spike","momentum_7d","velocity"]:
    r(f"surface {s}",[c for c in live if s in c["surf"]])
print("== winners >=7x list")
for c in sorted(live,key=lambda c:-c["x"])[:20]: print(c["cid"],round(c["x"],1),c["t0"][:10],c.get("chain"),round(c.get("age") or -1),round(c.get("vr") or -1,1),round(c["m0"]/1e6,2),sorted(c["surf"]))
P
echo "=== sweep"; python3 sweep.py 2>&1 | grep -v "^rule\|^R[0-5] \|^universe\|^7x+\|^sensitivity" 
