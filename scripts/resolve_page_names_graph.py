#!/usr/bin/env python3
"""Lấy TÊN + ẢNH THẬT của page qua Graph API (dùng mọi token admin) → fb_page_names.

Cho page chạy ads mà chưa có tên (hoặc tên campaign rác). Với page mà 1 token
nào đó là admin → /{page_id}?fields=name,picture trả tên+ảnh thật. Ảnh
graph.facebook.com/{id}/picture là công khai (không cần token) — luôn có.

Chạy: export $(grep -v '^#' deploy/cpqc.env | xargs) && .venv/bin/python scripts/resolve_page_names_graph.py
"""
import json, logging, os, sys, urllib.request, urllib.error
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
if str(BASE) not in sys.path: sys.path.insert(0,str(BASE))
logging.basicConfig(level=logging.INFO, format="%(message)s")
log=logging.getLogger("resolve_names")
G="https://graph.facebook.com/v20.0"

def toks():
    out=[]
    for v in (os.environ.get("FACEBOOK_ACCESS_TOKENS",""),os.environ.get("FACEBOOK_ACCESS_TOKEN","")):
        for t in v.replace("\n",",").split(","):
            t=t.strip()
            if t and t not in out: out.append(t)
    return out

def g(u):
    try:
        with urllib.request.urlopen(u,timeout=20) as r: return json.load(r), None
    except urllib.error.HTTPError as e:
        try: return None, json.load(e).get("error",{})
        except Exception: return None, {"message":str(e)}
    except Exception as e: return None, {"message":str(e)}

def main():
    from db import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""SELECT DISTINCT s.page_id FROM fb_ads_page_daily_spend s
                        LEFT JOIN fb_page_names n ON n.page_id=s.page_id AND COALESCE(n.name,'')<>''
                        WHERE n.page_id IS NULL AND COALESCE(s.page_id,'')<>''""")
        missing=[r[0] for r in cur.fetchall()]
    log.info("=== %d page cần lấy tên thật ===", len(missing))
    TS=toks()
    ok=0
    for pid in missing:
        name=pic=None
        for t in TS:
            d,err=g(f"{G}/{pid}?fields=name,picture.type(large){{url}}&access_token={t}")
            if d and d.get("name"):
                name=d["name"]; pic=((d.get("picture") or {}).get("data") or {}).get("url"); break
            if err and str(err.get("code"))=="4":  # rate limit → dừng sớm
                log.info("rate-limit, dừng ở %d/%d", ok, len(missing)); 
                _flush(); return
        if not pic:  # ảnh công khai kể cả không token
            pic=f"https://graph.facebook.com/{pid}/picture?type=large"
        if name:
            from db import get_conn
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO fb_page_names (page_id,name,picture_url,scraped_at)
                               VALUES (%s,%s,%s,now())
                               ON CONFLICT (page_id) DO UPDATE SET name=EXCLUDED.name,
                                 picture_url=EXCLUDED.picture_url, scraped_at=now()""",(pid,name,pic))
                conn.commit()
            ok+=1
            if ok%10==0: log.info("  ...%d page có tên", ok)
    log.info("=== DONE: lấy tên thật %d/%d page ===", ok, len(missing))

def _flush(): pass

if __name__=="__main__": main()
