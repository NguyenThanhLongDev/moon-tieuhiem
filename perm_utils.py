"""
perm_utils.py – Dynamic permission system
Loads/saves permissions.json and provides helpers for checking access.
"""
import json
import os
import threading

_PERMS_PATH = os.path.join(os.path.dirname(__file__), "permissions.json")
_lock = threading.Lock()
_cache: dict | None = None
_cache_mtime: float | None = None

# Roles that always bypass all permission checks (full access)
SUPER_ROLES = {"admin", "superadmin", "manager"}


def load_perms() -> dict:
    global _cache, _cache_mtime
    try:
        mtime = os.path.getmtime(_PERMS_PATH)
    except FileNotFoundError:
        return {"modules": [], "role_permissions": {}, "path_map": {}}
    with _lock:
        if _cache is None or _cache_mtime != mtime:
            try:
                with open(_PERMS_PATH, encoding="utf-8") as f:
                    _cache = json.load(f)
                _cache_mtime = mtime
            except Exception:
                pass
        return dict(_cache) if _cache else {}


def save_perms(perms: dict) -> None:
    global _cache, _cache_mtime
    with _lock:
        with open(_PERMS_PATH, "w", encoding="utf-8") as f:
            json.dump(perms, f, ensure_ascii=False, indent=2)
        _cache = None
        _cache_mtime = None


def path_to_module(path: str, path_map: dict) -> str | None:
    """Map a request path to a module key. Returns None = no permission needed."""
    skip_prefixes = ("/static", "/favicon")
    skip_exact = {"/login", "/logout", "/sync-pos-now"}
    if path in skip_exact or any(path.startswith(p) for p in skip_prefixes):
        return None
    # Match longest prefix first (more specific wins)
    best_key: str | None = None
    best_len = 0
    for prefix, mod_key in path_map.items():
        if prefix == "/":
            continue  # Handle last
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            if len(prefix) > best_len:
                best_key = mod_key
                best_len = len(prefix)
    if best_key:
        return best_key
    # Fall back to "/"
    if "/" in path_map and path == "/":
        return path_map["/"]
    return None


def get_allowed_modules(role: str, perms: dict) -> set | None:
    """
    Returns a set of module keys the role can access.
    Returns None = unrestricted (all modules allowed).
    """
    if role in SUPER_ROLES:
        return None
    role_perms = perms.get("role_permissions", {}).get(role, [])
    if "*" in role_perms:
        return None
    return set(role_perms)


def can_access_module(role: str, module_key: str | None, perms: dict) -> bool:
    """Check if a role can access a given module key."""
    if module_key is None:
        return True
    allowed = get_allowed_modules(role, perms)
    if allowed is None:
        return True
    return module_key in allowed


def has_permission(role: str, module_key: str) -> bool:
    """Check if a role has a specific permission — auto-loads permissions.json.
    Use this for fine-grained sub-feature checks (vd: kvl_xk_noi_bo).
    """
    if role in SUPER_ROLES:
        return True
    perms = load_perms()
    allowed = get_allowed_modules(role, perms)
    if allowed is None:
        return True
    return module_key in allowed
