#!/usr/bin/env python3
"""READ-ONLY phân loại: với danh sách tracking_code, đối chiếu trạng thái thật trên POS.
In ra mỗi đơn đang ở status POS nào (6=huỷ, 5=hoàn, 9=chờ, 2=gửi, 3=nhận).
KHÔNG update gì — chỉ báo cáo. Update để bước sau quyết định.
"""
import os, sys
from collections import defaultdict
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from db import query_all
from modules.kho_vat_ly.wh_sync_orders import fetch_orders_for_shop

TRACKINGS = sys.argv[1:] or [
    'SPXVN067464040635','SPXVN062469248075','SPXVN062613878775','SPXVN069011587315',
    'SPXVN069351867425','SPXVN066689560405','SPXVN060670111985','SPXVN068612943055',
    'SPXVN060963462235','SPXVN069953995995',
]
DATE_FROM = (date.today() - timedelta(days=35)).strftime("%Y-%m-%d")

rows = query_all("""
  SELECT DISTINCT r.tracking_code, r.shop_id, r.order_code, r.order_id_external,
         r.pancake_status, r.status, r.shop_name, s.pos_shop_id, s.pos_api_key
  FROM wh_outbound_requests r JOIN wh_shops s ON s.id=r.shop_id
  WHERE r.tracking_code = ANY(%s)
""", (TRACKINGS,))

seen = {}
for r in rows:
    seen[r[0]] = {'shop_id':r[1],'order_code':r[2],'ext':str(r[3]),'ps':r[4],
                  'st':r[5],'shop':r[6],'pos_shop':str(r[7]),'key':r[8]}

byshop = defaultdict(set)
for tk, d in seen.items():
    byshop[(d['pos_shop'], d['key'])].add(d['ext'])

pos_status = {}  # (pos_shop, ext) -> status int
for (pos_shop, key), exts in byshop.items():
    for stt in (6, 5, 9, 2, 3):
        try:
            orders, err = fetch_orders_for_shop(
                str(pos_shop), api_key=key, pancake_status=stt,
                date_from=DATE_FROM, date_to="", cursor_inserted_at="")
            for o in orders or []:
                oid = str(o.get('id'))
                if oid in exts and (pos_shop, oid) not in pos_status:
                    pos_status[(pos_shop, oid)] = stt
        except Exception as e:
            print(f"  [warn] shop {pos_shop} status {stt}: {str(e)[:60]}")

NAME = {6:'HUỶ(6)', 5:'HOÀN(5)', 9:'chờ chuyển(9)', 2:'đã gửi(2)', 3:'đã nhận(3)'}
print("%-19s %-13s %-7s %-13s %-14s" % ('TRACKING','SHOP','ĐƠN','DB(ps/st)','POS THẬT'))
to_cancel = []
for tk, d in seen.items():
    ps = pos_status.get((d['pos_shop'], d['ext']))
    posname = NAME.get(ps, 'KHÔNG THẤY/khác')
    print("%-19s %-13s %-7s %-13s %-14s" % (tk, d['shop'][:12], d['order_code'], d['ps']+'/'+d['st'], posname))
    if ps == 6:
        to_cancel.append((d['shop_id'], d['order_code'], tk))

print("\n=> Đơn POS đã HUỶ (status 6) cần update cancelled:", len(to_cancel))
for sid, oc, tk in to_cancel:
    print(f"   shop_id={sid} order={oc} ({tk})")
