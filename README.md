# Thread Monthly Investor Packet Automation

This repository generates a monthly investor update packet after finance close.
It now uses the February 2026 Thread investor packet as the calibrated template
spine: the generated draft follows the recurring 23-slide structure and maps
each slide to workbook ranges, HubSpot aggregates, Snowflake extracts, or
Omni extracts, or reviewed narrative fields.

## What It Produces

For a reporting month such as `2026-04`, the CLI writes:

- `Thread Investor Update - 2026-04.pptx`
- `Thread Investor Update - 2026-04.pdf`
- `source-ledger-2026-04.csv`
- `qa-summary-2026-04.txt`

The generated deck and PDF are always treated as drafts until a human approves
the source ledger and narrative. Missing required calibrated workbook ranges
will fail the approval gate.

## Quick Start

Run the sample packet:

```bash
python3 -m investor_packet --month 2026-04 --input-dir examples/input --output-dir outputs/2026-04/drafts --allow-qa-errors
```

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

For the monthly operating workflow, use `monthly-close-runbook.md`. It defines
the month folder, required finance/HubSpot/Snowflake/Omni inputs, dry-run
command, approval gate review, and April source-completion checklist.

## Monthly Input Folder

The expected v1 input folder is:

```text
monthly-input/
  Thread Monthly Reporting Template - Feb-26 v 1.1.xlsx
  Thread+Magic,+Inc_Balance+Sheet-TEMPLATE-Feb 26.xlsx
  Thread+Magic,+Inc_Statement+of+Cash+Flows-TEMPLATE-Feb 26.xlsx
  quickbooks_pnl.csv
  quickbooks_balance_sheet.csv
  hubspot_metrics.csv
  hubspot_deals.csv
  hubspot_goals.csv
  hubspot_owner_quotas.csv
  hubspot_t12m_fta_source_attribution.csv
  hubspot_t12m_sdr_generation.csv
  hubspot_t12m_ae_performance.csv
  hubspot_t12m_ge_performance.csv
  snowflake/
    product_metrics.csv
  omni/
    product_metrics.csv
```

Month-specific `inputs/YYYY-MM/` folders are local/private working folders and
are ignored by git by default. Keep reusable sample data under `examples/`.

CSV and XLSX are supported for QuickBooks reports. The Snowflake folder is the
default bridge for v1 when the reporting marts already exist but direct
credentials are not configured in the packet runner.

QuickBooks Online can also be used as a direct finance source. Set
`quickbooks.enabled` to `true` in a config override such as
`configs/qbo.example.json`, then provide
`QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, and `QBO_REDIRECT_URI`. Use an HTTPS
redirect URI registered in Intuit Developer for live production data. Tokens are
stored outside the repo by default at
`~/.config/thread-investor-packet/quickbooks_tokens.json`; override with
`QBO_TOKEN_FILE` if needed.

For live production OAuth, deploy the copy-back callback receiver behind HTTPS
and register its callback URL in Intuit Developer:

```bash
PYTHONPATH=src python3 -m investor_packet.qbo_callback --host 0.0.0.0 --port 8000
```

The repo includes a `Dockerfile` and `Procfile` for simple HTTPS hosts. Container
start command:

```bash
docker build -t thread-qbo-callback .
docker run -p 8000:8000 thread-qbo-callback
```

Procfile hosts should use the included `web` process; set the platform `PORT`
as usual.

Set the Intuit redirect URI and `QBO_REDIRECT_URI` to:

```text
https://<your-domain>/qbo/callback
```

The callback receiver only displays Intuit's returned `code`, `realmId`, and
`state` as a copyable URL. It does not store tokens, exchange the code, or need
QuickBooks client secrets.

Authorize once:

```bash
PYTHONPATH=src python3 -m investor_packet quickbooks --config configs/qbo.json connect
```

After Intuit redirects to the callback page, copy the full callback URL shown on
that page and paste it into the waiting local CLI prompt.

Fetch and cache QBO finance reports for a month:

```bash
PYTHONPATH=src python3 -m investor_packet quickbooks --config configs/qbo.json test \
  --month 2026-04 \
  --input-dir inputs/2026-04 \
  --refresh
```

The QBO reader uses ProfitAndLoss, BalanceSheet, and CashFlow reports. It caches
raw report JSON and normalized CSVs under `inputs/YYYY-MM/quickbooks/`, then
feeds finance metrics and the balance sheet / cash flow slides. The reporting
workbook, HubSpot, Snowflake/Omni, and human narrative approvals remain separate
packet inputs.

First live validation checklist:

- Confirm `inputs/YYYY-MM/quickbooks/` contains normalized QBO CSVs.
- Run the packet with the QBO config override.
- Review `source-ledger-YYYY-MM.csv` for QBO ProfitAndLoss, BalanceSheet, and
  CashFlow rows.

The Omni folder is the reviewed bridge for Product, Operations, and Customer
Success metrics that live in Omni/Snowflake-backed models. The generator reads
saved extracts only; it does not query Omni live during packet generation.
`omni/product_metrics.csv` should use the columns `metric`, `value`, `period`,
`unit`, `slide_number`, `source_topic`, `source_field`, `definition`,
`definition_status`, `review_status`, and `notes`. Only `metric`, `value`,
`period`, and `unit` are required. The file may include the current reporting
month plus the prior 11 months, using one row per metric per month; older or
future periods are flagged for review. Keep `definition_status` and
`review_status` as review-required until the model owner has approved the
definition and extract.
The Omni config also maintains the product metric catalog: owner, source field,
working definition, unit, desired direction, and investor-facing label. The
source ledger adds Omni QA rows for missing current-month values, incomplete
history, duplicate metric-period rows, and unapproved definitions or extracts.
When Omni rows are available for slides 21-23, the deck uses them as the primary
visual source: current-month callout cards plus 12-month line charts and compact
bar charts replace the legacy Engineering KPIs workbook preview. Derived product
analytics such as connected end-client rate and usage per adopted partner are
written back into the ledger as review-required Omni-derived rows.

For HubSpot, prefer `hubspot_metrics.csv` when using the HubSpot connector to
pull investor-packet aggregates without storing raw deal/account rows. If that
file is absent, the runner falls back to `hubspot_deals.csv`, `hubspot_deals.json`,
or `HUBSPOT_ACCESS_TOKEN` and aggregates the raw deal export locally. The default
HubSpot scope is Sales + Upsell; Renewal is intentionally excluded until it is
added back to the investor-packet pipeline view.

For trailing-12-month HubSpot history, the raw deal export/API properties should
include `lead_source` (FTA Source), `referral_source` (FTA Source Detail),
`hubspot_owner_id`, `sdr`, and `commission_owner`. The generator uses FTA Source
for marketing channel attribution and normalizes HubSpot values to the five
investor-facing channels: Outbound, Live Event, Inbound, Referral, and Paid Ads.
It uses SDR for generated-deal counts, deal owner for AE win/loss, and commission owner for GE quota attainment. Add
`hubspot_goals.csv` from HubSpot Goals when quota attainment should be
calculated. The generator maps 2026 New Sales Goals to AE quota attainment and
2026 Existing Revenue Goals to GE quota attainment, with both Rep and Team
scopes supported. The Goals export can use columns such as `goal_name`, `scope`,
`owner_id`, `team_name`, `period` or `year`, `target`, and optional `actual` or
`progress`. If no usable Goals export is present, the older
`hubspot_owner_quotas.csv` fallback with `role`, `owner_id`, `period`, and
`quota` columns remains supported.

For a real month, keep the same workbook roles but update filenames in
`configs/default.json` or an override config. Workbook paths resolve from the
monthly input folder first, then `templates/`; absolute paths are also accepted.
The default filenames point at the February 2026 canonical files because those
are the first calibrated template fixtures.

## CLI

```bash
python3 -m investor_packet \
  --month YYYY-MM \
  --input-dir /path/to/monthly-input \
  --output-dir /path/to/monthly-output/drafts \
  --config configs/default.json
```

Use `--allow-qa-errors` when you want draft files even if required inputs are
missing or source validation fails. Without that flag, the CLI still writes the
artifacts, but exits with a non-zero status if the approval gate fails.

## Configuration

The generator is config-driven. Update `configs/default.json` to map:

- The calibrated packet manifest and workbook input filenames.
- QuickBooks report filenames, sheets, and metric aliases.
- HubSpot input/API fields, closed stages, and stage probabilities.
- Snowflake and Omni extract names and expected metric columns.
- Thread deck titles, colors, footer text, and chart/table defaults.

SQL files live under `sql/`. V1 keeps query logic outside the slide code so the
deck generator does not become the source of metric definitions.

`templates/packet_manifest.json` defines the recurring slide spine, owners,
approval requirements, and source mappings. The source ledger includes
`slide_number`, `slide_title`, `source_workbook`, `sheet`, `range_or_metric`,
and `review_required` so reviewers can trace every automated exhibit.

## Approval Gate

The packet is considered ready for review only when:

- Required QuickBooks exports are present and parseable.
- Required metrics are not missing or null.
- Metric periods match the requested month or are explicitly marked as reviewed.
- HubSpot pipeline is aggregated without exposing raw deal names.
- Required manifest workbook ranges are present and populated.
- The source ledger has no `ERROR` or `MISSING` rows.

The automation does not distribute investor materials. A human reviewer must
approve the draft deck, PDF, source ledger, and generated commentary.

## Calibration Tests

The test suite includes optional historical checks against the local February
2026 workbooks and prior packet files when they are present on the machine. They
verify the 23-slide reference structure, required workbook sheets/ranges, source
ledger coverage, and a February dry run with no missing required metrics.
