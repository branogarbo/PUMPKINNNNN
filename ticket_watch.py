#!/usr/bin/env python3
"""Watch ticket inventory using Python 3.9+; no third-party packages required.

Usage:
    python ticket_watch.py --url "https://example.com/api/inventory"
    python ticket_watch.py --url "https://example.com/api/inventory" --once
    python ticket_watch.py --url "https://example.com/api/inventory" --min-quantity 2

Optional: set TICKET_API_HEADERS to a JSON object of HTTP headers if the
endpoint requires authentication, e.g. {"Authorization": "Bearer ..."}.
Alternatively, use --headers-file headers.json (plain JSON, no cURL escaping).
Keep credentials out of the script. Requests are GET requests only.
"""

import argparse
import gzip
import json
import logging
import math
import os
import time
import webbrowser
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def load_headers(filename=None):
    """File headers override environment headers; accept Windows UTF-8 BOMs."""
    sources = [json.loads(os.environ.get("TICKET_API_HEADERS", "{}"))]
    if filename:
        with open(filename, encoding="utf-8-sig") as stream:
            sources.append(json.load(stream))
    headers = {}
    for source in sources:
        if not isinstance(source, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in source.items()
        ):
            raise ValueError("Headers must be a JSON object of string values")
        if any(not k or any(c in k + v for c in "\r\n") for k, v in source.items()):
            raise ValueError("Header names must be nonempty and headers cannot contain newlines")
        headers.update({k.lower(): v for k, v in source.items()})
    return headers


def read_payload(response):
    """Report non-JSON responses without logging their body or credentials."""
    body = response.read()
    status = response.status
    content_type = response.headers.get("Content-Type", "unspecified")
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding == "gzip":
        body = gzip.decompress(body)
    elif encoding == "deflate":
        body = zlib.decompress(body)
    elif encoding not in ("", "identity"):
        raise ValueError("Unsupported response encoding %r; remove Accept-Encoding "
                         "from your headers file" % encoding)
    description = "HTTP %s; Content-Type=%s" % (status, content_type)
    if not body.strip():
        raise ValueError("Empty API response (%s). Check authorization and event headers."
                         % description)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        cloudflare = (response.headers.get("cf-mitigated", "").lower() == "challenge"
                      or b"/cdn-cgi/challenge-platform/" in body.lower())
        html = body.lstrip().lower().startswith((b"<!doctype html", b"<html"))
        kind = "Cloudflare challenge" if cloudflare else "HTML page" if html else "non-JSON body"
        raise ValueError("API returned %s (%s). Check authorization, event headers, "
                         "and session cookies. A browser challenge cannot be completed "
                         "by this HTTP polling script." % (kind, description)) from exc


def get_quantity(payload, path, date):
    """Require exactly one daily inventory row; never match time-slot rows."""
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise ValueError("API response is not an object with code=200")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise ValueError("API response 'data' is not an array")
    matches = [row for row in rows if isinstance(row, dict)
               and row.get("path") == path and row.get("itemKey") == date]
    if len(matches) != 1:
        raise ValueError("Expected one matching daily inventory row; found %d"
                         % len(matches))
    quantity = matches[0].get("quantity")
    if type(quantity) is not int or quantity < 0:
        raise ValueError("Matching row has a missing or invalid quantity")
    return quantity


def retry_after_seconds(value):
    """Accept Retry-After as seconds or an HTTP date."""
    if not value:
        return 0.0
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def alert(quantity, args):
    logging.warning("TICKETS AVAILABLE: %d for %s (%s). Check the booking page!",
                    quantity, args.date, args.path)
    # Windows gets an audible tone; terminal bell support varies on other OSes.
    if os.name == "nt":
        try:
            import winsound
            winsound.Beep(1000, 700)
        except (ImportError, RuntimeError):
            print("\a", end="", flush=True)
    else:
        print("\a", end="", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="Inventory API endpoint URL")
    parser.add_argument("--date", default="2026-10-30", help="Exact daily itemKey")
    parser.add_argument("--path", default="tickets.adult", help="Exact inventory path")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between polls (default: 60)")
    parser.add_argument("--min-quantity", type=int, default=1, help="Tickets needed (default: 1)")
    parser.add_argument("--once", action="store_true", help="Check once and exit")
    parser.add_argument("--open-url", help="Booking page to open on each unavailable-to-available transition")
    parser.add_argument("--headers-file", help="JSON file containing request headers, including authorization")
    args = parser.parse_args()
    if args.interval < 1 or args.min_quantity < 1:
        parser.error("--interval and --min-quantity must be positive integers")
    for value in (args.url, args.open_url):
        if value and (urlsplit(value).scheme not in ("http", "https")
                      or not urlsplit(value).netloc):
            parser.error("URLs must be absolute HTTP or HTTPS URLs")
    try:
        extra_headers = load_headers(args.headers_file)
    except (ValueError, OSError):
        parser.error("Cannot load headers: check --headers-file and TICKET_API_HEADERS; "
                     "use a JSON object of string headers without cURL escaping")
    headers = {"accept": "application/json", "cache-control": "no-cache",
               "accept-encoding": "identity", "user-agent": "TicketInventoryWatcher/1.0"}
    headers.update(extra_headers)
    request = Request(args.url, headers=headers, method="GET")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    logging.info("Watching %s / %s; need %d ticket(s); interval=%ds. Ctrl+C stops.",
                 args.path, args.date, args.min_quantity, args.interval)
    was_available = False
    last_alert = None
    failures = 0
    try:
        while True:
            delay = args.interval
            try:
                with urlopen(request, timeout=20) as response:
                    payload = read_payload(response)
                quantity = get_quantity(payload, args.path, args.date)
                failures = 0
                available = quantity >= args.min_quantity
                logging.info("%s: quantity=%d (%s)", args.date, quantity,
                             "AVAILABLE" if available else "below requested quantity")
                now = time.monotonic()
                # Alert immediately, then every five minutes while still available.
                if available and (not was_available or last_alert is None
                                  or now - last_alert >= 300):
                    alert(quantity, args)
                    if args.open_url and not was_available:
                        try:
                            if not webbrowser.open(args.open_url):
                                logging.warning("Could not open booking page; open it manually.")
                        except webbrowser.Error:
                            logging.warning("Could not open booking page; open it manually.")
                    last_alert = now
                was_available = available
                if args.once:
                    return 0 if available else 1
            except HTTPError as exc:
                retry = retry_after_seconds(exc.headers.get("Retry-After"))
                status = exc.code
                exc.close()
                # Authentication/request errors need attention, not repeated requests.
                if 400 <= status < 500 and status not in (408, 429):
                    logging.error("HTTP %d: check endpoint, access, and required headers.", status)
                    if status in (401, 403):
                        logging.error("Check Authorization and event headers in --headers-file. "
                                      "Session cookies may have expired; a browser challenge "
                                      "may also prevent direct HTTP polling.")
                    return 2
                failures += 1
                delay = max(args.interval, min(900, 60 * 2 ** min(failures - 1, 4)), retry)
                logging.warning("HTTP %d; retrying in %.0fs.", status, delay)
                if args.once:
                    return 2
            except (URLError, OSError, ValueError, HTTPException, zlib.error) as exc:
                failures += 1
                delay = max(args.interval, min(900, 60 * 2 ** min(failures - 1, 4)))
                # Avoid logging request URLs/headers, which may contain credentials.
                logging.warning("Check failed (%s); retrying in %.0fs.", type(exc).__name__, delay)
                if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError):
                    logging.warning("%s", exc)
                if args.once:
                    return 2
            time.sleep(delay)
    except KeyboardInterrupt:
        logging.info("Stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
