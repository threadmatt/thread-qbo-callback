import base64
import csv
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .models import Metric, Table
from .utils import month_bounds, safe_text


QBO_SCOPE = "com.intuit.quickbooks.accounting"
AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
PRODUCTION_API_BASE = "https://quickbooks.api.intuit.com"
SANDBOX_API_BASE = "https://sandbox-quickbooks.api.intuit.com"

REPORT_NAMES = {
    "profit_and_loss": "ProfitAndLoss",
    "balance_sheet": "BalanceSheet",
    "cash_flow": "CashFlow",
}


class QuickBooksError(RuntimeError):
    pass


class QuickBooksAuthError(QuickBooksError):
    pass


HttpRequest = Callable[[str, str, Dict[str, str], Optional[bytes]], Dict[str, Any]]


@dataclass
class NormalizedReport:
    report_name: str
    columns: List[str]
    rows: List[List[str]]


@dataclass
class QuickBooksCacheResult:
    reports: Dict[str, Dict[str, Any]]
    raw_paths: Dict[str, Path]
    normalized_paths: Dict[str, Path]


def quickbooks_online_enabled(config: Dict[str, Any]) -> bool:
    return bool(config.get("quickbooks", {}).get("enabled", False))


def generate_state() -> str:
    return secrets.token_urlsafe(24)


def default_token_path() -> Path:
    configured = os.environ.get("QBO_TOKEN_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "thread-investor-packet" / "quickbooks_tokens.json"


def parse_oauth_callback(callback_url: str, expected_state: str) -> Tuple[str, str]:
    parsed = urllib.parse.urlparse(callback_url)
    query = parsed.query or callback_url
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    error = _first_param(params, "error")
    if error:
        description = _first_param(params, "error_description") or error
        raise QuickBooksAuthError(f"QuickBooks authorization failed: {description}")
    state = _first_param(params, "state")
    if not state or state != expected_state:
        raise QuickBooksAuthError("QuickBooks OAuth state did not match; refusing callback")
    code = _first_param(params, "code")
    realm_id = _first_param(params, "realmId")
    if not code:
        raise QuickBooksAuthError("QuickBooks callback did not include an authorization code")
    if not realm_id:
        raise QuickBooksAuthError("QuickBooks callback did not include a realmId/company id")
    return code, realm_id


def client_from_environment(config: Dict[str, Any], http_request: Optional[HttpRequest] = None) -> "QuickBooksOnlineClient":
    qbo_config = config.get("quickbooks", {})
    client_id = os.environ.get("QBO_CLIENT_ID")
    client_secret = os.environ.get("QBO_CLIENT_SECRET")
    redirect_uri = os.environ.get("QBO_REDIRECT_URI") or qbo_config.get("redirect_uri", "")
    environment = os.environ.get("QBO_ENV") or qbo_config.get("environment", "production")
    if not client_id:
        raise QuickBooksAuthError("QBO_CLIENT_ID is required for QuickBooks Online")
    if not client_secret:
        raise QuickBooksAuthError("QBO_CLIENT_SECRET is required for QuickBooks Online")
    if not redirect_uri:
        raise QuickBooksAuthError("QBO_REDIRECT_URI or quickbooks.redirect_uri is required for QuickBooks Online")
    return QuickBooksOnlineClient(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        environment=environment,
        token_path=default_token_path(),
        http_request=http_request,
    )


class QuickBooksOnlineClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        environment: str = "production",
        token_path: Optional[Path] = None,
        http_request: Optional[HttpRequest] = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.environment = environment
        self.token_path = token_path or default_token_path()
        self.http_request = http_request or _urllib_json_request

    @property
    def api_base_url(self) -> str:
        return SANDBOX_API_BASE if self.environment.lower() == "sandbox" else PRODUCTION_API_BASE

    def authorization_url(self, state: Optional[str] = None) -> Tuple[str, str]:
        state = state or generate_state()
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "scope": QBO_SCOPE,
                "redirect_uri": self.redirect_uri,
                "state": state,
            }
        )
        return f"{AUTH_URL}?{query}", state

    def exchange_code(self, code: str, realm_id: str) -> Dict[str, Any]:
        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        )
        token = self._token_payload(payload, realm_id)
        self.save_token(token)
        return token

    def refresh_access_token(self) -> Dict[str, Any]:
        token = self.load_token()
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise QuickBooksAuthError("QuickBooks token file does not contain a refresh token")
        payload = self._token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
        )
        refreshed = self._token_payload(payload, token.get("realm_id", ""))
        self.save_token(refreshed)
        return refreshed

    def ensure_access_token(self) -> Dict[str, Any]:
        token = self.load_token()
        if token.get("access_token") and float(token.get("expires_at", 0)) > time.time() + 120:
            return token
        return self.refresh_access_token()

    def load_token(self) -> Dict[str, Any]:
        if not self.token_path.exists():
            raise QuickBooksAuthError(f"QuickBooks token file not found: {self.token_path}")
        with self.token_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def save_token(self, token: Dict[str, Any]) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        with self.token_path.open("w", encoding="utf-8") as handle:
            json.dump(token, handle, indent=2, sort_keys=True)
            handle.write("\n")
        try:
            os.chmod(self.token_path, 0o600)
        except OSError:
            pass

    def get_report(self, report_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        token = self.ensure_access_token()
        realm_id = token.get("realm_id")
        if not realm_id:
            raise QuickBooksAuthError("QuickBooks token file does not contain a realm_id")
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value not in {"", None}})
        url = f"{self.api_base_url}/v3/company/{realm_id}/reports/{report_name}"
        if query:
            url = f"{url}?{query}"
        return self.http_request(
            "GET",
            url,
            {"Authorization": f"Bearer {token['access_token']}", "Accept": "application/json"},
            None,
        )

    def _token_request(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        credentials = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        auth = base64.b64encode(credentials).decode("ascii")
        data = urllib.parse.urlencode(fields).encode("utf-8")
        return self.http_request(
            "POST",
            TOKEN_URL,
            {
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data,
        )

    def _token_payload(self, payload: Dict[str, Any], realm_id: str) -> Dict[str, Any]:
        now = int(time.time())
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not access_token or not refresh_token:
            raise QuickBooksAuthError("QuickBooks token response was missing access_token or refresh_token")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "realm_id": realm_id,
            "token_type": payload.get("token_type", "bearer"),
            "scope": payload.get("scope", QBO_SCOPE),
            "obtained_at": now,
            "expires_at": now + int(payload.get("expires_in", 3600)),
            "refresh_token_expires_at": now + int(payload.get("x_refresh_token_expires_in", 8726400)),
        }


def read_quickbooks_online(
    input_dir: Path,
    config: Dict[str, Any],
    month: str,
    force_refresh: bool = False,
    require_enabled: bool = True,
    client: Optional[QuickBooksOnlineClient] = None,
) -> Tuple[List[Metric], List[Table]]:
    if require_enabled and not quickbooks_online_enabled(config):
        return [], []
    cache = load_or_fetch_reports(input_dir, config, month, force_refresh=force_refresh, client=client)
    normalized = normalize_cached_reports(cache.reports)
    write_normalized_cache(normalized, cache.normalized_paths)
    return metrics_and_tables_from_reports(normalized, config, month, cache.normalized_paths)


def load_or_fetch_reports(
    input_dir: Path,
    config: Dict[str, Any],
    month: str,
    force_refresh: bool = False,
    client: Optional[QuickBooksOnlineClient] = None,
) -> QuickBooksCacheResult:
    raw_paths, normalized_paths = cache_paths(input_dir, config, month)
    if not force_refresh and all(path.exists() for path in raw_paths.values()):
        reports = {name: _read_json(path) for name, path in raw_paths.items()}
        return QuickBooksCacheResult(reports=reports, raw_paths=raw_paths, normalized_paths=normalized_paths)

    qbo_client = client or client_from_environment(config)
    reports: Dict[str, Dict[str, Any]] = {}
    for cache_key, report_name in REPORT_NAMES.items():
        reports[cache_key] = qbo_client.get_report(report_name, report_params(cache_key, config, month))
    if config.get("quickbooks", {}).get("cache", {}).get("enabled", True):
        for name, payload in reports.items():
            _write_json(raw_paths[name], payload)
    return QuickBooksCacheResult(reports=reports, raw_paths=raw_paths, normalized_paths=normalized_paths)


def report_params(report_key: str, config: Dict[str, Any], month: str) -> Dict[str, Any]:
    qbo_config = config.get("quickbooks", {})
    start, end = month_bounds(month)
    fiscal_start_month = int(qbo_config.get("fiscal_year_start_month", 1))
    fiscal_start_year = start.year if start.month >= fiscal_start_month else start.year - 1
    fiscal_start = start.replace(year=fiscal_start_year, month=fiscal_start_month, day=1)
    base = {
        "minorversion": qbo_config.get("minorversion", 75),
        "accounting_method": qbo_config.get("accounting_method", "Accrual"),
    }
    if report_key == "balance_sheet":
        return {**base, "end_date": end.isoformat()}
    if report_key == "profit_and_loss":
        return {**base, "start_date": fiscal_start.isoformat(), "end_date": end.isoformat(), "summarize_column_by": "Months"}
    return {**base, "start_date": fiscal_start.isoformat(), "end_date": end.isoformat()}


def cache_paths(input_dir: Path, config: Dict[str, Any], month: str) -> Tuple[Dict[str, Path], Dict[str, Path]]:
    cache_config = config.get("quickbooks", {}).get("cache", {})
    raw_dir = input_dir / cache_config.get("raw_dir", "quickbooks/qbo_raw")
    normalized_dir = input_dir / cache_config.get("normalized_dir", "quickbooks")
    raw_paths = {
        "profit_and_loss": raw_dir / f"profit_and_loss_{month}.json",
        "balance_sheet": raw_dir / f"balance_sheet_{month}.json",
        "cash_flow": raw_dir / f"cash_flow_{month}.json",
    }
    normalized_paths = {
        "profit_and_loss": normalized_dir / f"quickbooks_pnl_qbo_{month}.csv",
        "balance_sheet": normalized_dir / f"quickbooks_balance_sheet_qbo_{month}.csv",
        "cash_flow": normalized_dir / f"quickbooks_cash_flow_qbo_{month}.csv",
    }
    return raw_paths, normalized_paths


def normalize_cached_reports(reports: Dict[str, Dict[str, Any]]) -> Dict[str, NormalizedReport]:
    return {
        key: normalize_report(payload, REPORT_NAMES[key])
        for key, payload in reports.items()
    }


def normalize_report(payload: Dict[str, Any], report_name: str) -> NormalizedReport:
    columns = _report_columns(payload)
    rows: List[List[str]] = []
    for row in payload.get("Rows", {}).get("Row", []):
        rows.extend(_flatten_qbo_row(row, columns, 0))
    return NormalizedReport(report_name=report_name, columns=columns, rows=rows)


def write_normalized_cache(reports: Dict[str, NormalizedReport], paths: Dict[str, Path]) -> None:
    for key, report in reports.items():
        path = paths[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(report.columns)
            writer.writerows(report.rows)


def metrics_and_tables_from_reports(
    reports: Dict[str, NormalizedReport],
    config: Dict[str, Any],
    month: str,
    normalized_paths: Dict[str, Path],
) -> Tuple[List[Metric], List[Table]]:
    metrics: List[Metric] = []
    tables: List[Table] = []
    qbo_config = config.get("quickbooks", {})

    for report_config in qbo_config.get("reports", []):
        report_name = report_config.get("name", "")
        cache_key = "profit_and_loss" if report_name == "profit_and_loss" else "balance_sheet"
        if cache_key not in reports:
            continue
        report = reports[cache_key]
        rows = [report.columns] + report.rows
        source = "QuickBooks Online:ProfitAndLoss" if cache_key == "profit_and_loss" else "QuickBooks Online:BalanceSheet"
        for metric_config in report_config.get("metrics", []):
            aliases = _metric_aliases(metric_config, qbo_config)
            value, matched_label = _find_metric_value(rows, aliases, month)
            required = bool(metric_config.get("required", False))
            status = "OK"
            notes = f"Matched row: {matched_label}" if matched_label else ""
            if value is None:
                status = "MISSING" if required else "WARN"
                notes = "No matching row/value found in QuickBooks Online report"
            metrics.append(
                Metric(
                    name=metric_config["metric"],
                    value=value,
                    unit=metric_config.get("unit", ""),
                    period=month,
                    source=source,
                    category=metric_config.get("category", "Financials"),
                    status=status,
                    notes=notes,
                    required=required,
                    source_workbook=normalized_paths[cache_key].name,
                    range_or_metric=metric_config["metric"],
                )
            )

    if "cash_flow" in reports:
        cash_flow_report = reports["cash_flow"]
        rows = [cash_flow_report.columns] + cash_flow_report.rows
        for metric_name, aliases in cash_flow_metric_aliases().items():
            value, matched_label = _find_metric_value(rows, aliases, month)
            metrics.append(
                Metric(
                    name=metric_name,
                    value=value,
                    unit="USD",
                    period=month,
                    source="QuickBooks Online:CashFlow",
                    category="Financials",
                    status="OK" if value is not None else "WARN",
                    notes=f"Matched row: {matched_label}" if matched_label else "No matching row/value found in QuickBooks Online cash flow report",
                    required=False,
                    source_workbook=normalized_paths["cash_flow"].name,
                    range_or_metric=metric_name,
                )
            )

    if "profit_and_loss" in reports:
        tables.append(_table_from_report(reports["profit_and_loss"], "QuickBooks Online Profit and Loss", normalized_paths["profit_and_loss"].name, None, ""))
    if "balance_sheet" in reports:
        tables.append(_table_from_report(reports["balance_sheet"], "QuickBooks Online Balance Sheet", normalized_paths["balance_sheet"].name, 7, "Balance Sheet"))
    if "cash_flow" in reports:
        tables.append(_table_from_report(reports["cash_flow"], "QuickBooks Online Cash Flow", normalized_paths["cash_flow"].name, 8, "Statement of Cash Flows"))

    return metrics, tables


def cash_flow_metric_aliases() -> Dict[str, List[str]]:
    return {
        "Net Cash from Operating Activities": ["Net Cash from Operating Activities", "Net cash provided by operating activities"],
        "Net Cash from Investing Activities": ["Net Cash from Investing Activities", "Net cash provided by investing activities"],
        "Net Cash from Financing Activities": ["Net Cash from Financing Activities", "Net cash provided by financing activities"],
        "Net Cash Increase": ["NET CASH INCREASE (DECREASE)", "Net increase in cash"],
        "Cash at Beginning of Period": ["Cash at Beginning of Period", "Cash at beginning of period"],
        "Cash at End of Period": ["CASH AT END OF PERIOD", "Cash at end of period"],
    }


def quickbooks_online_satisfies_mapping(mapping: Dict[str, Any], source_metrics: Iterable[Metric]) -> Optional[str]:
    workbook = mapping.get("workbook")
    if workbook == "balance_sheet_workbook" and _has_ok_source(source_metrics, "QuickBooks Online:BalanceSheet"):
        return "QuickBooks Online BalanceSheet report cached; calibrated balance sheet workbook not required for this source"
    if workbook == "cash_flow_workbook" and _has_ok_source(source_metrics, "QuickBooks Online:CashFlow"):
        return "QuickBooks Online CashFlow report cached; calibrated cash flow workbook not required for this source"
    return None


def _has_ok_source(source_metrics: Iterable[Metric], source: str) -> bool:
    return any(metric.source == source and metric.status not in {"MISSING", "ERROR"} for metric in source_metrics)


def _table_from_report(report: NormalizedReport, title: str, source_name: str, slide_number: Optional[int], slide_title: str) -> Table:
    return Table(
        title=title,
        columns=report.columns,
        rows=[row for row in report.rows if any(cell for cell in row)][:12],
        source=f"QuickBooks Online:{source_name}",
        category="Financials",
        slide_number=slide_number,
        slide_title=slide_title,
    )


def _metric_aliases(metric_config: Dict[str, Any], qbo_config: Dict[str, Any]) -> List[str]:
    aliases = list(metric_config.get("aliases", []))
    if metric_config.get("metric") != "EBITDA":
        return aliases
    if qbo_config.get("synthesize_ebitda_from_operating_income", False):
        return aliases + ["Operating Income", "Operating Income (Loss)", "Net Operating Income"]
    return [
        alias
        for alias in aliases
        if "operating income" not in alias.lower() and "net operating income" not in alias.lower()
    ]


def _find_metric_value(rows: List[List[str]], aliases: Iterable[str], month: str) -> Tuple[Optional[float], str]:
    from .financials import find_metric_value

    return find_metric_value(rows, aliases, month=month)


def _report_columns(payload: Dict[str, Any]) -> List[str]:
    columns = []
    for index, column in enumerate(payload.get("Columns", {}).get("Column", [])):
        title = safe_text(column.get("ColTitle"))
        if not title:
            title = "Line Item" if index == 0 else safe_text(column.get("ColType")) or f"Column {index + 1}"
        columns.append(title)
    return columns or ["Line Item", "Amount"]


def _flatten_qbo_row(row: Dict[str, Any], columns: List[str], depth: int) -> List[List[str]]:
    flattened: List[List[str]] = []
    header = row.get("Header", {})
    if header:
        flattened.extend(_row_from_col_data(header.get("ColData", []), columns, depth))
    col_data = row.get("ColData", [])
    if col_data:
        flattened.extend(_row_from_col_data(col_data, columns, depth))
    for child in row.get("Rows", {}).get("Row", []):
        flattened.extend(_flatten_qbo_row(child, columns, depth + 1))
    summary = row.get("Summary", {})
    if summary:
        flattened.extend(_row_from_col_data(summary.get("ColData", []), columns, depth))
    return flattened


def _row_from_col_data(col_data: List[Dict[str, Any]], columns: List[str], depth: int) -> List[List[str]]:
    values = [safe_text(item.get("value")) for item in col_data]
    if not values or not any(values):
        return []
    values = _pad(values, len(columns))
    if values[0]:
        values[0] = f"{'  ' * depth}{values[0]}"
    return [values]


def _pad(values: List[str], size: int) -> List[str]:
    padded = list(values[:size])
    while len(padded) < size:
        padded.append("")
    return padded


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _first_param(params: Dict[str, List[str]], key: str) -> str:
    values = params.get(key, [])
    return values[0] if values else ""


def _urllib_json_request(method: str, url: str, headers: Dict[str, str], data: Optional[bytes]) -> Dict[str, Any]:
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise QuickBooksError(f"QuickBooks request failed with HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise QuickBooksError(f"QuickBooks request failed: {exc.reason}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise QuickBooksError("QuickBooks response was not valid JSON") from exc
