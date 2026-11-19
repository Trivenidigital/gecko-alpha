import json, sqlite3, collections
ev = json.load(open("/root/xs_study/events.json"))
con = sqlite3.connect("file:/root/gecko-alpha/scout.db?mode=ro", uri=True)
c = collections.Counter(); mn = {}; mx = {}
for cid, e in ev.items():
    for t, s in e:
        c[s] += 1; mn[s] = min(mn.get(s, t), t); mx[s] = max(mx.get(s, t), t)
print("events.json events per surface (n, first, last):")
for s in c: print(" ", s, c[s], mn[s][:10], mx[s][:10])
q = {"acceleration": "select count(*), min(detected_at) from gainer_acceleration where detected_at<'2026-09-19'",
     "momentum_7d": "select count(*), min(detected_at) from momentum_7d where detected_at<'2026-09-19'",
     "volume_spike": "select count(*), min(detected_at) from volume_spikes where detected_at<'2026-09-19'",
     "velocity": "select count(*), min(detected_at) from velocity_alerts",
     "slow_burn": "select count(*), min(detected_at) from slow_burn_candidates where detected_at<'2026-09-19'",
     "prediction": "select count(*), min(predicted_at) from predictions where predicted_at<'2026-09-19'",
     "paper_trade": "select count(*), min(opened_at) from paper_trades where opened_at>'2020'"}
print("live DB rows today (<09-19):")
for s, sql in q.items(): print(" ", s, con.execute(sql).fetchone())
ctrl = collections.defaultdict(set)
for cid, isc in con.execute("select coin_id, is_control from predictions"): ctrl[cid].add(isc)
only_pred = [cid for cid, e in ev.items() if {s for _, s in e} == {"prediction"}]
only_ctrl = [cid for cid in only_pred if ctrl.get(cid) == {1}]
print("coins flagged ONLY by predictions:", len(only_pred), "| of which ONLY control-arm predictions:", len(only_ctrl))
