#!/usr/bin/env python3
"""Seed dữ liệu mẫu module lương phase 1 (idempotent). Cần DATABASE_URL."""

from __future__ import annotations

import os
import sys

# Repo root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.salary import repositories as salary_repo  # noqa: E402
from modules.salary import service  # noqa: E402


def main() -> None:
    actor = os.environ.get("SALARY_SEED_ACTOR", "manual_seed")
    salary_repo.ensure_salary_schema()
    out = service.run_phase1_seed(changed_by=actor)
    print(out)


if __name__ == "__main__":
    main()
