#!/usr/bin/env python3
"""In ra URL map nếu có route đồng bộ nhanh — chạy trong thư mục posbottieuhiem, đã cài deps."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

def main() -> None:
    from web_app import app  # noqa: WPS433
    want = ("sync-quick", "sync_quick", "outbound")
    for r in sorted(app.url_map.iter_rules(), key=lambda x: str(x.rule)):
        if "kho-vat-ly" in str(r.rule) and "outbound" in str(r.rule) and "sync" in str(r.rule):
            print(f"{r.rule!s:50}  {list(r.methods - {'HEAD', 'OPTIONS'})!s}  {r.endpoint}")
    # Tóm tắt
    all_rules = [str(x.rule) for x in app.url_map.iter_rules()]
    ok = any("sync-quick" in x or "sync_quick" in x for x in all_rules)
    print("---")
    print("outbound_sync_quick OK:" if ok else "THIẾU route sync-quick — pull code + restart process", ok)

if __name__ == "__main__":
    main()
