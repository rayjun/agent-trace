#!/usr/bin/env python3
"""Export one full span from the authenticated loopback AgentTrace API.

The output contains unredacted span data. Store and share it carefully.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_span(trace: str, span: str, port: int, timeout: float = 10, *,
               agent: str | None = None, session: str | None = None) -> dict:
    directory = Path(os.environ.get("AGENTTRACE_PLATFORM_DIR") or Path.home() / ".agenttrace").expanduser()
    token = (directory / "server.token").read_text(encoding="utf-8").strip()
    if not token or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("Invalid server token file")
    url = (f"http://127.0.0.1:{port}/api/traces/" + urllib.parse.quote(trace, safe="")
           + "/spans/" + urllib.parse.quote(span, safe=""))
    selectors = {key: value for key, value in (("agent", agent), ("session", session))
                 if value is not None}
    if selectors:
        url += "?" + urllib.parse.urlencode(selectors)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, method="GET")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError("Span request did not succeed")
        data = json.load(response)
    if not isinstance(data, dict) or data.get("trace_id") != trace or data.get("span_id") != span:
        raise ValueError("API response trace/span identity mismatch; no output written")
    if ((agent is not None and data.get("adapter_id") != agent)
            or (session is not None and data.get("session_id") != session)):
        raise ValueError("API response namespace mismatch; no output written")
    return data


def write_export(output: Path, data: dict) -> None:
    serialized = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(serialized)
    except BaseException:
        output.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", required=True)
    parser.add_argument("--span", required=True)
    parser.add_argument("--agent")
    parser.add_argument("--session")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--port", default=8741, type=int)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.trace or not args.span or args.trace in {".", ".."} or args.span in {".", ".."}:
        parser.error("--trace and --span must be nonempty identifiers, not dot segments")
    try:
        if args.output.exists() or args.output.is_symlink():
            raise FileExistsError
        data = fetch_span(args.trace, args.span, args.port, agent=args.agent, session=args.session)
        write_export(args.output, data)
    except FileExistsError:
        print("Error: output already exists; refusing to overwrite", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as exc:
        print(f"Error: span request failed (HTTP {exc.code}); no export written", file=sys.stderr)
        return 1
    except ValueError:
        print("Error: invalid token, JSON, or trace/span identity; no export written", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, urllib.error.URLError):
        print("Error: export failed; check the local server, token file and output permissions", file=sys.stderr)
        return 1
    print("Exported full span to the requested output (mode 0600); contains sensitive data")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
