import json, os, glob, collections, datetime as dt
D = "/root/xs_study"
def d(ms): return dt.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d")
keys = collections.Counter(); first = []; last = []; npts = []; gaps = 0
for f in glob.glob(D + "/charts/*.json"):
    j = json.load(open(f)); keys[tuple(sorted(j))] += 1
    p = j.get("prices") or []
    if len(p) >= 2:
        first.append(p[0][0]); last.append(p[-1][0]); npts.append(len(p))
        if any(p[i + 1][0] - p[i][0] > 3 * 86400e3 for i in range(len(p) - 1)): gaps += 1
npts.sort()
print("charts:", sum(keys.values()), "keysets:", dict(keys))
print("first point range", d(min(first)), d(max(first)), "| last point range", d(min(last)), d(max(last)))
print("points per chart min/median/max", npts[0], npts[len(npts) // 2], npts[-1], "| charts with >3d gap", gaps)
st = collections.Counter(); fields = collections.Counter(); oh = []
for f in glob.glob(D + "/v2/coins/*.json"):
    j = json.load(open(f)); st[j.get("ohlcv_status")] += 1
    for k in j: fields[k] += 1
    if j.get("ohlcv"): oh.append(len(j["ohlcv"]))
print("v2 files", sum(st.values()), "ohlcv_status", dict(st))
print("v2 fields", dict(fields))
oh.sort(); print("ohlcv candles per coin min/median/max", oh[0], oh[len(oh) // 2], oh[-1], "n", len(oh))
b = json.load(open(D + "/btc.json"))["prices"]; print("btc", d(b[0][0]), d(b[-1][0]), len(b))
