import argparse
import html
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping, Optional, Tuple


CALLBACK_PATH = "/qbo/callback"
HEALTH_PATH = "/healthz"


def callback_response(path: str, headers: Optional[Mapping[str, Any]] = None) -> Tuple[int, str, bytes]:
    parsed = urllib.parse.urlparse(path)
    if parsed.path == HEALTH_PATH:
        return HTTPStatus.OK, "text/plain; charset=utf-8", b"ok\n"
    if parsed.path != CALLBACK_PATH:
        return HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found\n"

    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    full_url = _public_url(path, headers or {})
    error = _first_param(params, "error")
    if error:
        description = _first_param(params, "error_description") or error
        return (
            HTTPStatus.BAD_REQUEST,
            "text/html; charset=utf-8",
            _page(
                "QuickBooks Authorization Error",
                [
                    ("Error", error),
                    ("Description", description),
                    ("Callback URL", full_url),
                ],
                full_url,
                "QuickBooks returned an authorization error. Review the message below, then restart the local connect command if needed.",
            ),
        )

    missing = [name for name in ("code", "realmId", "state") if not _first_param(params, name)]
    if missing:
        return (
            HTTPStatus.BAD_REQUEST,
            "text/html; charset=utf-8",
            _page(
                "QuickBooks Callback Missing Fields",
                [
                    ("Missing", ", ".join(missing)),
                    ("Callback URL", full_url),
                ],
                full_url,
                "The callback did not include all fields required by the local CLI.",
            ),
        )

    return (
        HTTPStatus.OK,
        "text/html; charset=utf-8",
        _page(
            "QuickBooks Authorization Ready",
            [
                ("Company realmId", _first_param(params, "realmId")),
                ("OAuth state", _first_param(params, "state")),
                ("Authorization code", _first_param(params, "code")),
            ],
            full_url,
            "Copy the full callback URL below and paste it into the waiting local CLI prompt.",
        ),
    )


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    server = ThreadingHTTPServer((host, port), QuickBooksCallbackHandler)
    print(f"QuickBooks callback receiver listening on http://{host}:{port}{CALLBACK_PATH}")
    print("Use an HTTPS reverse proxy or deployment host for production Intuit redirects.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping QuickBooks callback receiver.")
    finally:
        server.server_close()


class QuickBooksCallbackHandler(BaseHTTPRequestHandler):
    server_version = "ThreadQBOCallback/1.0"

    def do_GET(self) -> None:
        status, content_type, body = callback_response(self.path, self.headers)
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        parsed = urllib.parse.urlparse(self.path)
        self.log_message('"%s %s %s" %s %s', self.command, parsed.path, self.request_version, code, size)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve a copy-back QuickBooks OAuth callback page")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host, e.g. 127.0.0.1 for local dev or 0.0.0.0 behind a proxy")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


def _page(title: str, rows: list, callback_url: str, lead: str) -> bytes:
    escaped_title = html.escape(title)
    escaped_lead = html.escape(lead)
    escaped_url = html.escape(callback_url, quote=True)
    detail_rows = "\n".join(
        f"<tr><th>{html.escape(label)}</th><td><code>{html.escape(value)}</code></td></tr>"
        for label, value in rows
    )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escaped_title}</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f7f8f5; color: #10211e; }}
    main {{ max-width: 760px; margin: 48px auto; padding: 0 24px; }}
    h1 {{ font-size: 28px; line-height: 1.2; margin: 0 0 12px; }}
    p {{ font-size: 16px; line-height: 1.5; }}
    table {{ border-collapse: collapse; width: 100%; margin: 24px 0; background: #fff; border: 1px solid #d8ddd8; }}
    th, td {{ text-align: left; border-bottom: 1px solid #e6e9e6; padding: 12px; vertical-align: top; }}
    th {{ width: 180px; color: #39524d; }}
    code, textarea {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; }}
    textarea {{ box-sizing: border-box; width: 100%; min-height: 120px; padding: 12px; border: 1px solid #b9c4bd; border-radius: 6px; }}
    button {{ margin-top: 12px; padding: 10px 14px; border: 0; border-radius: 6px; background: #005c55; color: #fff; cursor: pointer; }}
    .hint {{ color: #49635d; }}
  </style>
</head>
<body>
  <main>
    <h1>{escaped_title}</h1>
    <p>{escaped_lead}</p>
    <table>{detail_rows}</table>
    <label for="callback-url"><strong>Full callback URL</strong></label>
    <textarea id="callback-url" readonly>{escaped_url}</textarea>
    <button type="button" onclick="navigator.clipboard && navigator.clipboard.writeText(document.getElementById('callback-url').value)">Copy callback URL</button>
    <p class="hint">The local CLI validates the OAuth state and exchanges the authorization code for tokens. This page does not store anything.</p>
  </main>
</body>
</html>
"""
    return document.encode("utf-8")


def _first_param(params: Mapping[str, list], key: str) -> str:
    values = params.get(key, [])
    return str(values[0]) if values else ""


def _public_url(path: str, headers: Mapping[str, Any]) -> str:
    parsed = urllib.parse.urlparse(path)
    if parsed.scheme and parsed.netloc:
        return path
    proto = _header(headers, "X-Forwarded-Proto") or "http"
    host = _header(headers, "X-Forwarded-Host") or _header(headers, "Host") or "localhost"
    return f"{proto}://{host}{path}"


def _header(headers: Mapping[str, Any], name: str) -> str:
    value = headers.get(name) if hasattr(headers, "get") else ""
    if not value:
        value = headers.get(name.lower()) if hasattr(headers, "get") else ""
    return str(value).split(",", 1)[0].strip() if value else ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
