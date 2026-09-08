#!/usr/bin/env python3
"""Tính lại fb_ads_page_daily_spend từ ad-level mb_fb_entity_daily — ĐÚNG & KHÔNG LỆCH.

Quy tắc:
- Mỗi ad gán về page của nó (page_id đã resolve).
- Ad mồ côi (page_id rỗng): suy page theo (1) campaign_id đã từng resolve trong cùng TK,
  rồi (2) khớp tên page với tên campaign trong cùng TK.
- Bất biến: Σ(page) == tổng ad-level của TK/ngày (== Facebook account-level).
  Nếu còn ad mồ côi KHÔNG suy được page → BỎ QUA ngày đó (không ghi), liệt kê để xử tay.

Mặc định DRY-RUN. Thêm --apply để ghi thật (có backup CSV mỗi TK/ngày).
"""
import argparse, csv, os, sys, datetime as dt
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_conn

BK = "/home/admin1/tieuhiemsoft/posbottieuhiem/backup/rebuild"

def drange(a, b):
    a = dt.date.fromisoformat(a); b = dt.date.fromisoformat(b)
    while a <= b:
        yield a.isoformat(); a += dt.timedelta(days=1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", required=True)
    ap.add_argument("--to", dest="dto", required=True)
    ap.add_argument("--account", default=None)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.apply:
        os.makedirs(BK, exist_ok=True)

    with get_conn() as conn, conn.cursor() as cur:
        if a.account:
            accounts = [a.account]
        else:
            cur.execute("""SELECT DISTINCT account_id FROM mb_fb_entity_daily
                            WHERE metric_date BETWEEN %s AND %s AND account_id<>''
                            ORDER BY 1""", (a.dfrom, a.dto))
            accounts = [r[0] for r in cur.fetchall()]

        n_fixed = n_skip = n_nochange = 0
        sum_before = sum_after = 0.0
        skips = []
        changed_samples = []

        for ACC in accounts:
            cur.execute("""SELECT DISTINCT page_id, page_name FROM fb_ads_page_daily_spend
                            WHERE fb_ad_account_id=%s AND page_name<>''""", (ACC,))
            pages = {str(p): n for p, n in cur.fetchall()}
            # campaign_id -> page (đã resolve trong TK, mọi ngày)
            cur.execute("""SELECT campaign_id, page_id, COUNT(*) FROM mb_fb_entity_daily
                            WHERE account_id=%s AND page_id IS NOT NULL AND page_id<>'' AND campaign_id<>''
                            GROUP BY campaign_id, page_id""", (ACC,))
            cp_tmp = defaultdict(dict)
            for cid, pid, c in cur.fetchall():
                cp_tmp[str(cid)][str(pid)] = c
            camp_page = {cid: max(d, key=d.get) for cid, d in cp_tmp.items()}

            def match_name(campaign):
                camp = (campaign or "").lower().strip()
                best, bl = None, -1
                for pid, nm in pages.items():
                    nml = (nm or "").lower().strip()
                    if nml and (camp.startswith(nml) or nml in camp) and len(nml) > bl:
                        best, bl = pid, len(nml)
                return best

            for DATE in drange(a.dfrom, a.dto):
                cur.execute("""SELECT page_id, campaign_id, campaign_name, spend, impressions, clicks
                                 FROM mb_fb_entity_daily WHERE account_id=%s AND metric_date=%s""", (ACC, DATE))
                rows = cur.fetchall()
                if not rows:
                    continue
                agg = defaultdict(lambda: [0.0, 0, 0]); unmatched = 0.0; unc = set(); ent_total = 0.0
                for pid, cid, camp, sp, imp, cl in rows:
                    sp = float(sp or 0); ent_total += sp
                    tp = str(pid) if pid else (camp_page.get(str(cid or "")) or match_name(camp))
                    if not tp:
                        unmatched += sp; unc.add(camp); continue
                    agg[tp][0] += sp; agg[tp][1] += int(imp or 0); agg[tp][2] += int(cl or 0)

                cur.execute("""SELECT page_id, spend FROM fb_ads_page_daily_spend
                                WHERE fb_ad_account_id=%s AND metric_date=%s""", (ACC, DATE))
                cur_rows = cur.fetchall()
                cur_total = sum(float(r[1] or 0) for r in cur_rows)
                new_total = sum(v[0] for v in agg.values())

                if unmatched > 0.5:
                    n_skip += 1
                    skips.append((ACC, DATE, "mồ_côi", round(unmatched), list(unc)[:2]))
                    continue

                # Anchor Facebook = MAX/(account,date) trong fb_ads_daily_metrics
                # (bảng này lặp số TK theo từng shop → phải MAX, không SUM).
                cur.execute("""SELECT COALESCE(MAX(spend),0) FROM fb_ads_daily_metrics
                                WHERE fb_ad_account_id=%s AND metric_date=%s""", (ACC, DATE))
                fb_max = float(cur.fetchone()[0] or 0)
                # entity phải == Facebook account-level; bỏ qua chênh do làm tròn (<=0.3% hoặc 10đ).
                # Lệch lớn = ad-level thiếu / ngày còn đang chạy → KHÔNG rebuild.
                tol = max(10.0, fb_max * 0.003)
                if abs(ent_total - fb_max) > tol:
                    n_skip += 1
                    skips.append((ACC, DATE, "entity≠FB", round(ent_total), round(fb_max)))
                    continue
                # đã khớp hết → Σ == ent_total (== Facebook). So sánh với hiện tại.
                cur_split = {str(p): float(s or 0) for p, s in cur_rows}
                new_split = {p: round(v[0]) for p, v in agg.items()}
                if {p: round(s) for p, s in cur_split.items()} == new_split:
                    n_nochange += 1
                    continue
                n_fixed += 1; sum_before += cur_total; sum_after += new_total
                if len(changed_samples) < 12:
                    changed_samples.append((ACC, DATE, round(cur_total), round(new_total), len(cur_rows), len(agg)))

                if a.apply:
                    with open(f"{BK}/{ACC}_{DATE}.csv", "w", newline="") as f:
                        w = csv.writer(f); w.writerow(["account","page_id","spend","date"])
                        for p, s in cur_rows: w.writerow([ACC, p, s, DATE])
                    cur.execute("DELETE FROM fb_ads_page_daily_spend WHERE fb_ad_account_id=%s AND metric_date=%s", (ACC, DATE))
                    for pid, (sp, imp, cl) in agg.items():
                        cur.execute("""INSERT INTO fb_ads_page_daily_spend
                                         (fb_ad_account_id,page_id,page_name,metric_date,spend,impressions,clicks,synced_at)
                                       VALUES (%s,%s,%s,%s::date,%s,%s,%s,NOW())""",
                                    (ACC, pid, pages.get(pid, ""), DATE, sp, imp, cl))
                    conn.commit()

        mode = "APPLY" if a.apply else "DRY-RUN"
        print(f"=== {mode}  {a.dfrom}..{a.dto}  ({len(accounts)} TK) ===")
        print(f"  sửa (đổi số) : {n_fixed} TK-ngày   tổng {sum_before:,.0f} -> {sum_after:,.0f}")
        print(f"  đã đúng sẵn  : {n_nochange} TK-ngày (không đổi)")
        print(f"  BỎ QUA (mồ côi chưa map): {n_skip} TK-ngày")
        if changed_samples:
            print("  --- mẫu thay đổi ---")
            for acc, d, b, n, nb, na in changed_samples:
                print(f"    TK={acc} {d}  {b:,.0f} -> {n:,.0f}  ({nb}→{na} page)")
        if skips:
            print(f"  --- {min(len(skips),20)}/{len(skips)} ngày bỏ qua ---")
            for s in skips[:20]:
                acc, d, reason, val, extra = s
                print(f"    TK={acc} {d}  [{reason}] {val:,.0f}  {extra}")

if __name__ == "__main__":
    main()
