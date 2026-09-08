from __future__ import annotations


def mask_token(token: str, prefix_len: int = 6, suffix_len: int = 4) -> str:
    """Return a non-reversible masked form safe for logs (never log full tokens)."""
    t = str(token or "").strip()
    if not t:
        return ""
    if len(t) <= prefix_len + suffix_len:
        return "*" * min(len(t), 8)
    return f"{t[:prefix_len]}...{t[-suffix_len:]}"


def redact_query_params(params: dict) -> dict:
    """Copy of params with access_token masked for debug logging."""
    out = dict(params)
    if "access_token" in out and out["access_token"]:
        out["access_token"] = mask_token(str(out["access_token"]))
    return out
