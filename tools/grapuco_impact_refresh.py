#!/usr/bin/env python3
import argparse
import json
import os
import urllib.request
from pathlib import Path


API_URL = os.getenv("GRAPUCO_MCP_URL", "https://api.grapuco.com/mcp")


def load_repo_id(project_root: Path) -> str:
    cfg_path = project_root / ".grapuco" / "config.json"
    if not cfg_path.exists():
        raise RuntimeError("Missing .grapuco/config.json. Run: grapuco init")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for key in ("repositoryId", "repoId", "id"):
        if cfg.get(key):
            return cfg[key]
    raise RuntimeError("Cannot find repository ID in .grapuco/config.json")


def mcp_post(payload: dict, api_key: str, session_id: str | None = None):
    req = urllib.request.Request(API_URL, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("X-Api-Key", api_key)
    if session_id:
        req.add_header("Mcp-Session-Id", session_id)
    with urllib.request.urlopen(req, timeout=90) as resp:
        session_id = resp.headers.get("Mcp-Session-Id") or session_id
        body = resp.read().decode("utf-8", errors="ignore")
    if "data:" in body:
        body = body.split("data:", 1)[1].strip()
    return session_id, (json.loads(body) if body.strip() else {})


def call_tool(name: str, arguments: dict, api_key: str, session_id: str):
    _, response = mcp_post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        api_key,
        session_id,
    )
    for item in response.get("result", {}).get("content", []):
        if item.get("type") == "text":
            text = item.get("text", "")
            try:
                return json.loads(text)
            except Exception:
                return text
    return {}


def main():
    parser = argparse.ArgumentParser(description="Refresh Grapuco impact report.")
    parser.add_argument("--project-root", default=".", help="Project root path")
    parser.add_argument(
        "--files",
        nargs="+",
        default=["web_app.py", "telegram_bot.py", "db.py"],
        help="Critical files to run get_impact_analysis",
    )
    parser.add_argument(
        "--output-json",
        default="GRAPUCO_IMPACT_REPORT.json",
        help="Output JSON file path",
    )
    parser.add_argument(
        "--output-md",
        default="GRAPUCO_IMPACT_SUMMARY.md",
        help="Output markdown summary path",
    )
    args = parser.parse_args()

    api_key = os.getenv("GRAPUCO_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GRAPUCO_API_KEY in environment")

    root = Path(args.project_root).resolve()
    repo_id = load_repo_id(root)

    session_id, _ = mcp_post(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "grapuco-impact-refresh", "version": "1.0.0"},
            },
        },
        api_key,
    )

    report = {}
    for file_path in args.files:
        report[file_path] = call_tool(
            "get_impact_analysis",
            {"repositoryId": repo_id, "filePath": file_path},
            api_key,
            session_id,
        )

    json_path = root / args.output_json
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# Grapuco Impact Summary", "", f"Repository ID: `{repo_id}`", ""]
    for file_path in args.files:
        item = report.get(file_path, {})
        total = item.get("totalFlows", 0)
        affected = item.get("allAffectedFiles", [])
        lines.append(f"## `{file_path}`")
        lines.append(f"- Total affected flows: **{total}**")
        lines.append(f"- Affected files count: **{len(affected)}**")
        if affected:
            lines.append("- Top affected files:")
            for name in affected[:8]:
                lines.append(f"  - `{name}`")
        lines.append("")

    md_path = root / args.output_md
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
