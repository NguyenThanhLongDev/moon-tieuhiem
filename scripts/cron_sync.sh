#!/usr/bin/env bash
# Cron moon (cpqc): đồng bộ chi phí QC + POS (dashboard + chi phí quảng cáo).
# 2 chế độ:
#   (mặc định) NHẸ : chỉ HÔM NAY, SYNC_DAYS=2  → chạy mỗi 30 phút giờ làm việc
#   "full"     ĐẦY : HÔM NAY + HÔM QUA, SYNC_DAYS=150 → chạy 1 lần/ngày (1h sáng) để refresh history
# KHÔNG kéo sản phẩm/tồn kho.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

set -a
# shellcheck disable=SC1091
source deploy/cpqc.env
set +a
export SKIP_SYNC_PRODUCTS=1

mkdir -p logs
LOG="logs/cron_sync.log"
PY=".venv/bin/python"
TODAY="$(date +%F)"
YDAY="$(date -d 'yesterday' +%F 2>/dev/null || date +%F)"

MODE="${1:-light}"
if [[ "$MODE" == "full" ]]; then
  export SYNC_DAYS=150
  DAYS=("$YDAY" "$TODAY")
  FB_FROM="$YDAY"
else
  export SYNC_DAYS=2
  DAYS=("$TODAY")
  FB_FROM="$TODAY"
fi

echo "===== $(date '+%F %T') CRON SYNC [$MODE] =====" >> "$LOG"

# 1) Chi phí FB theo page
"$PY" scripts/sync_fb_ads_by_page.py --date-from "$FB_FROM" --date-to "$TODAY" >> "$LOG" 2>&1 \
  || echo "WARN: sync_fb_ads_by_page lỗi" >> "$LOG"

# 2) POS + chi phí FB theo account (run_auto_refresh step 7)
for D in "${DAYS[@]}"; do
  bash run_auto_refresh_all_data.sh "$D" >> "$LOG" 2>&1 \
    || echo "WARN: refresh $D lỗi" >> "$LOG"
done

# 3) Tên page THẬT (scrape m.facebook.com — không cần token; tự bỏ page đã scrape <7 ngày)
"$PY" scripts/scrape_page_names_cache.py >> "$LOG" 2>&1 \
  || echo "WARN: scrape page names lỗi" >> "$LOG"

# 3b) Tên+ảnh THẬT qua Graph (page admin) — cho page scrape công khai không ra
"$PY" scripts/resolve_page_names_graph.py >> "$LOG" 2>&1 \
  || echo "WARN: resolve page names graph lỗi" >> "$LOG"

# 4) Trạng thái + kết quả campaign (trang Page → TK & Camp)
"$PY" scripts/sync_fb_campaign_status.py >> "$LOG" 2>&1 \
  || echo "WARN: campaign status lỗi" >> "$LOG"
"$PY" scripts/sync_fb_campaign_results.py --date-from "$FB_FROM" --date-to "$TODAY" >> "$LOG" 2>&1 \
  || echo "WARN: campaign results lỗi" >> "$LOG"

# 5) Link landing page (ladipage) per ad (trang Landing Pages)
"$PY" scripts/sync_fb_ad_landing_links.py >> "$LOG" 2>&1 \
  || echo "WARN: landing links lỗi" >> "$LOG"

# 6) Bắt sale gian lận — chụp hội thoại FB (page nào có quyền pages_messaging)
#    Bản NHANH (mặc định, chạy 4h/lần): chỉ hội thoại 26h gần nhất, bỏ /blocked
#    → giảm ~85% lượt gọi Graph API. Bản FULL chỉ chạy 1h sáng (MODE=full)
#    để không đốt hạn mức app FB gây lỗi "(#4) request limit" (sếp 12/08).
if [ "$MODE" = "full" ]; then
  "$PY" -m modules.fraud_detect.adapter_facebook >> "$LOG" 2>&1 \
    || echo "WARN: fraud FB sync lỗi" >> "$LOG"
else
  "$PY" -m modules.fraud_detect.adapter_facebook --quick >> "$LOG" 2>&1 \
    || echo "WARN: fraud FB sync (quick) lỗi" >> "$LOG"
fi
"$PY" scripts/check_landing_links.py >> "$LOG" 2>&1 \
  || echo "WARN: check landing links lỗi" >> "$LOG"

echo "===== DONE [$MODE] $(date '+%F %T') =====" >> "$LOG"
