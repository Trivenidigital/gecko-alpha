import sqlite3, re, time
con = sqlite3.connect("file:/root/gecko-alpha/scout.db?mode=ro", uri=True, timeout=30)
cur = con.cursor()
tabs = [r[0] for r in cur.execute("select name from sqlite_master where type='table' order by name")]
pat = re.compile(r"(_at$|_ts$|^ts$|time|date|observed|created|posted|detected|snapshot)", re.I)
for t in tabs:
    cols = [(c[1], c[2]) for c in cur.execute(f"pragma table_info('{t}')")]
    tcols = [c for c, ty in cols if pat.search(c)]
    n = cur.execute(f"select count(*) from '{t}'").fetchone()[0]
    out = [t, str(n)]
    if n and tcols:
        # prefer created/detected/observed-like columns
        pref = sorted(tcols, key=lambda c: (0 if re.search("created|detected|observed|first_seen|posted|opened|predicted|snapshot_at|discovered|recorded", c, re.I) else 1))
        for c in pref[:2]:
            try:
                mn, mx = cur.execute(f"select min(\"{c}\"), max(\"{c}\") from '{t}' where \"{c}\" is not null and \"{c}\"!=''").fetchone()
                out.append(f"{c}:{mn}..{mx}")
            except Exception as e:
                out.append(f"{c}:ERR{e}")
    out.append("cols=" + ",".join(c for c, _ in cols))
    print(" | ".join(out), flush=True)
