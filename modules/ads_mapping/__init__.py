from __future__ import annotations

from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask

# Luồng chính: modules/ads_allocation. Routes dưới /ads-mapping/* giữ tương thích (deprecated).


def register_ads_mapping_module(
    app: "Flask",
    *,
    login_required: Callable,
    page_template: str,
    get_allowed_shop_keys: Callable[[], Optional[set]],
    can_view_ads_global: Callable[[], bool],
) -> None:
    from .api import register_ads_mapping_routes

    register_ads_mapping_routes(
        app,
        login_required=login_required,
        page_template=page_template,
        get_allowed_shop_keys=get_allowed_shop_keys,
        can_view_ads_global=can_view_ads_global,
    )
