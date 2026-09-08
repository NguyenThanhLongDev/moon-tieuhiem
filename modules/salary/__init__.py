from __future__ import annotations


def register_salary_module(app, login_required, page_template: str) -> None:
    from .api import register_salary_routes

    register_salary_routes(app, login_required=login_required, page_template=page_template)
