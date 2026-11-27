#!/usr/bin/env python3
"""
YouTube Data API v3 — Video Statistics & Channel Details Fetcher + Google Sheets Sync

Setup:
    export YOUTUBE_API_KEY="your_youtube_api_key"

    # Optional (only needed when pushing to Google Sheets via --sheet-id):
    pip install gspread
    export GOOGLE_SERVICE_ACCOUNT_FILE="service_account.json"

Examples:
    # Fetch video stats and print JSON (no external packages needed)
    python3 youtube_stats.py video "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    # Fetch a channel's latest 10 uploads and push directly to a Google Sheet
    python3 youtube_stats.py channel "@GoogleDevelopers" --recent 10 \
        --sheet-id "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit" \
        --worksheet "Video Stats" \
        --service-account "service_account.json"
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_BASE = "https://www.googleapis.com/youtube/v3"


def api_get(endpoint: str, params: dict[str, Any], api_key: str) -> dict[str, Any]:
    """Perform a GET request against the YouTube Data API v3."""
    query = {**params, "key": api_key}
    url = f"{API_BASE}/{endpoint}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"YouTube API HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error reaching YouTube API: {exc.reason}") from exc


def extract_video_id(value: str) -> str:
    """Extract a YouTube video ID from a URL or return the ID as-is."""
    value = value.strip()
    patterns = [
        r"(?:v=|/v/|/embed/|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return value


def extract_sheet_id(value: str) -> str:
    """Extract a Google Spreadsheet ID from a URL or return the ID as-is."""
    value = value.strip()
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9-_]+)", value)
    return match.group(1) if match else value


def parse_channel_identifier(value: str) -> tuple[str, str]:
    """Return ('id', channel_id) or ('forHandle', handle) from a URL, handle, or ID."""
    value = value.strip().rstrip("/")
    if "youtube.com/" in value:
        channel_match = re.search(r"youtube\.com/channel/(UC[A-Za-z0-9_-]+)", value)
        if channel_match:
            return "id", channel_match.group(1)
        handle_match = re.search(r"youtube\.com/(@[^/?#]+)", value)
        if handle_match:
            return "forHandle", handle_match.group(1)
    if value.startswith("@"):
        return "forHandle", value
    if value.startswith("UC") and len(value) >= 20:
        return "id", value
    return "forHandle", f"@{value}"


def format_duration(iso_duration: str) -> str:
    """Convert ISO-8601 duration (e.g. PT12M34S) into HH:MM:SS or MM:SS."""
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso_duration or "")
    if not match:
        return iso_duration or ""
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def fetch_videos(video_inputs: list[str], api_key: str) -> list[dict[str, Any]]:
    """Fetch snippet, contentDetails, and statistics for up to 50 videos per call."""
    video_ids = [extract_video_id(v) for v in video_inputs]
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    results: list[dict[str, Any]] = []

    for i in range(0, len(video_ids), 50):
        batch = video_ids[i : i + 50]
        data = api_get(
            "videos",
            {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(batch),
            },
            api_key,
        )
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            details = item.get("contentDetails", {})
            results.append(
                {
                    "fetched_at": fetched_at,
                    "video_id": item.get("id", ""),
                    "url": f"https://www.youtube.com/watch?v={item.get('id', '')}",
                    "title": snippet.get("title", ""),
                    "channel_title": snippet.get("channelTitle", ""),
                    "channel_id": snippet.get("channelId", ""),
                    "published_at": snippet.get("publishedAt", ""),
                    "duration": format_duration(details.get("duration", "")),
                    "views": int(stats.get("viewCount", 0)),
                    "likes": int(stats.get("likeCount", 0)),
                    "comments": int(stats.get("commentCount", 0)),
                    "tags": ", ".join(snippet.get("tags", [])),
                }
            )
    return results


def fetch_channel(identifier: str, api_key: str, recent_count: int = 0) -> dict[str, Any]:
    """Fetch channel details and optionally its most recent uploads."""
    param_name, param_value = parse_channel_identifier(identifier)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    data = api_get(
        "channels",
        {
            "part": "snippet,contentDetails,statistics",
            param_name: param_value,
        },
        api_key,
    )
    items = data.get("items", [])
    if not items:
        raise RuntimeError(f"No channel found for {identifier!r}")

    ch = items[0]
    snippet = ch.get("snippet", {})
    stats = ch.get("statistics", {})
    uploads_playlist = (
        ch.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )

    channel_info: dict[str, Any] = {
        "fetched_at": fetched_at,
        "channel_id": ch.get("id", ""),
        "title": snippet.get("title", ""),
        "custom_url": snippet.get("customUrl", ""),
        "country": snippet.get("country", ""),
        "published_at": snippet.get("publishedAt", ""),
        "subscribers": int(stats.get("subscriberCount", 0)),
        "hidden_subscribers": bool(stats.get("hiddenSubscriberCount", False)),
        "total_views": int(stats.get("viewCount", 0)),
        "video_count": int(stats.get("videoCount", 0)),
        "uploads_playlist_id": uploads_playlist,
        "description": snippet.get("description", ""),
    }

    if recent_count > 0 and uploads_playlist:
        pl_data = api_get(
            "playlistItems",
            {
                "part": "contentDetails",
                "playlistId": uploads_playlist,
                "maxResults": min(recent_count, 50),
            },
            api_key,
        )
        recent_ids = [
            it["contentDetails"]["videoId"]
            for it in pl_data.get("items", [])
            if it.get("contentDetails", {}).get("videoId")
        ]
        channel_info["recent_videos"] = (
            fetch_videos(recent_ids, api_key) if recent_ids else []
        )
    else:
        channel_info["recent_videos"] = []

    return channel_info


def write_csv(rows: list[dict[str, Any]], path: str) -> None:
    """Write a list of flat dictionaries to a CSV file."""
    if not rows:
        print("No rows to write.", file=sys.stderr)
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {len(rows)} row(s) to {path}")


def push_to_google_sheet(
    rows: list[dict[str, Any]],
    sheet_id_or_url: str,
    worksheet_name: str,
    service_account_path: str | None = None,
) -> None:
    """Append rows to a Google Sheet using gspread and a Service Account JSON."""
    if not rows:
        print("No rows to push to Google Sheets.", file=sys.stderr)
        return

    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError(
            "Missing 'gspread' package. Install it with: pip install gspread"
        ) from exc

    spreadsheet_id = extract_sheet_id(sheet_id_or_url)
    client = (
        gspread.service_account(filename=service_account_path)
        if service_account_path
        else gspread.service_account()
    )

    spreadsheet = client.open_by_key(spreadsheet_id)
    try:
        ws = spreadsheet.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=worksheet_name,
            rows=max(100, len(rows) + 10),
            cols=len(rows[0]),
        )

    headers = list(rows[0].keys())
    existing_headers = ws.row_values(1)
    values_to_append: list[list[Any]] = []
    if not existing_headers:
        values_to_append.append(headers)

    for row in rows:
        values_to_append.append([row.get(col, "") for col in headers])

    ws.append_rows(values_to_append, value_input_option="USER_ENTERED")
    print(
        f"Pushed {len(rows)} row(s) to '{spreadsheet.title}' -> '{worksheet_name}'."
    )


def add_output_flags(cmd_parser: argparse.ArgumentParser) -> None:
    """Attach shared --csv and Google Sheets flags to a subcommand."""
    cmd_parser.add_argument("--csv", help="Optional output CSV file path.")
    cmd_parser.add_argument(
        "--sheet-id",
        help="Google Sheet ID or full URL to append rows into.",
    )
    cmd_parser.add_argument(
        "--worksheet",
        default="YouTube Stats",
        help="Target tab/worksheet name (default: 'YouTube Stats').",
    )
    cmd_parser.add_argument(
        "--service-account",
        default=os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
        help=(
            "Path to Google Service Account JSON key "
            "(defaults to GOOGLE_SERVICE_ACCOUNT_FILE or ~/.config/gspread/service_account.json)."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch YouTube stats via Data API v3 and optionally push to Google Sheets."
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("YOUTUBE_API_KEY", ""),
        help="YouTube Data API v3 key (defaults to YOUTUBE_API_KEY env var).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    vid_cmd = sub.add_parser("video", help="Fetch statistics for one or more videos.")
    vid_cmd.add_argument("videos", nargs="+", help="Video IDs or YouTube URLs.")
    add_output_flags(vid_cmd)

    ch_cmd = sub.add_parser("channel", help="Fetch details for a YouTube channel.")
    ch_cmd.add_argument("channel", help="Channel @handle, UC... ID, or URL.")
    ch_cmd.add_argument(
        "--recent",
        type=int,
        default=0,
        help="Also fetch stats for the N most recent uploads (1-50).",
    )
    add_output_flags(ch_cmd)

    args = parser.parse_args()
    if not args.api_key:
        print(
            "Error: Provide --api-key or set the YOUTUBE_API_KEY environment variable.",
            file=sys.stderr,
        )
        return 1

    try:
        if args.command == "video":
            rows = fetch_videos(args.videos, args.api_key)
            print(json.dumps(rows, indent=2, ensure_ascii=False))
        else:
            channel = fetch_channel(args.channel, args.api_key, recent_count=args.recent)
            print(json.dumps(channel, indent=2, ensure_ascii=False))
            rows = (
                channel["recent_videos"]
                if channel["recent_videos"]
                else [{k: v for k, v in channel.items() if k != "recent_videos"}]
            )

        if args.csv:
            write_csv(rows, args.csv)
        if args.sheet_id:
            push_to_google_sheet(
                rows,
                sheet_id_or_url=args.sheet_id,
                worksheet_name=args.worksheet,
                service_account_path=args.service_account,
            )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())