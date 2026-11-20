# Payment Reconciliation System

Full-stack FastAPI + React/Vite application for generating synthetic payment data and reconciling platform transactions against bank settlements.

The project currently contains two connected workflows:

1. **Dataset Generator** — creates reproducible platform, settlement, and ground-truth files.
2. **Reconciliation Engine** — accepts the two CSV files, validates and matches them, classifies findings, and calculates financial impact.

There is no database or authentication layer. Reconciliation results are calculated in memory for each request and can be exported from the UI.

## Features

### Dataset Generator

- Configurable payment count, date range, amount range, failure/refund percentages, settlement delay, and random seed.
- Generates `platform_transactions.csv` as the parent dataset.
- Derives `bank_settlements.csv` only from eligible successful platform events.
- Uses UTC timestamps with explicit timezone information.
- Uses `Decimal` and `ROUND_HALF_UP` for two-decimal INR amounts.
- Supports controlled test discrepancies:
  - Cross-month settlement
  - Aggregate rounding differences
  - Duplicate settlement entries
  - Orphan refunds
- Writes discrepancy metadata to `ground_truth.json`.

### Reconciliation Engine

- Uploads platform and bank CSV files through the **Reconciliation engine** tab.
- Validates headers, row values, dates, amounts, currencies, statuses, and refund references.
- Preserves row-specific validation errors instead of silently discarding invalid rows.
- Detects duplicate platform IDs, settlement IDs, and multiple bank records for one transaction.
- Performs a transaction-ID-based full outer match without amount-only matching or Cartesian joins.
- Classifies records as `MATCHED`, `PENDING`, `OVERDUE`, `MISMATCH`, `UNMATCHED_BANK`, `REVIEW_REQUIRED`, or `INVALID`.
- Checks settlement timing, late settlement, cross-month timing, refund relationships, failed-payment exclusions, and amount mismatches.
- Calculates signed payment/refund totals and a financial reconciliation bridge.
- Provides searchable and filterable transaction details in the UI.

## Project structure

```text
.
├── backend/
│   ├── app/
│   │   ├── config.py              # Generator configuration and validation
│   │   ├── generator.py           # Platform generation and settlement derivation
│   │   ├── main.py                # FastAPI application and routes
│   │   ├── models.py              # Platform and settlement dataclasses
│   │   ├── reconciliation.py      # CSV validation and reconciliation engine
│   │   └── serialization.py       # CSV, JSON, and ZIP serialization
│   ├── tests/
│   │   ├── test_generator.py
│   │   └── test_reconciliation.py
│   ├── .gitignore
│   ├── requirements.txt
│   └── pytest.ini
├── frontend/
│   ├── src/
│   │   ├── main.jsx               # Generator and reconciliation UI
│   │   └── styles.css             # Shared responsive visual system
│   ├── index.html
│   ├── package.json
│   └── package-lock.json
└── README.md
```

## Requirements

- Python 3.11+ recommended
- Node.js 18+ and npm

## Setup

### 1. Backend

From the project root:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate       # macOS/Linux
```

On Windows PowerShell, activate with:

```powershell
.venv\Scripts\Activate.ps1
```

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Start FastAPI:

```bash
uvicorn app.main:app --reload
```

The backend is available at `http://localhost:8000`. Interactive API documentation is at `http://localhost:8000/docs`.

### 2. Frontend

Open a second terminal from the project root:

```bash
cd frontend
npm install
npm run dev
```

Open the Vite URL shown in the terminal, normally `http://localhost:5173`.

The frontend calls `http://localhost:8000/api` and the backend includes CORS support for the Vite development ports.

## Running the application

### Generate a dataset

1. Open the **Dataset generator** tab.
2. Set the payment count and other configuration values.
3. Optionally enable discrepancy injection and choose counts for each anomaly type.
4. Click **Generate preview**.
5. Download individual CSV/JSON files or the ZIP bundle.

The random seed makes generation repeatable. Identical configuration plus identical seed produces the same records and discrepancy choices.

### Run reconciliation

1. Open the **Reconciliation engine** tab.
2. Upload `platform_transactions.csv` and `bank_settlements.csv`.
3. Optionally set an historical as-of date.
4. Set the expected calendar-day settlement delay and rounding threshold.
5. Click **Run reconciliation**.
6. Use the filters, search box, and row selection to inspect findings.
7. Download the JSON reconciliation report.

The as-of date is not implicitly taken from the current server clock when supplied. Settlement windows use calendar days, not banking business days.

## Data flow and logic

```text
Generator configuration
        ↓
Seeded platform transactions
        ↓
Eligible successful events
        ↓
Derived bank settlements
        ↓
Optional controlled discrepancies
        ↓
CSV upload
        ↓
Schema/data validation
        ↓
Duplicate detection
        ↓
Transaction-ID matching
        ↓
Date, amount, refund, and status checks
        ↓
Financial totals and reconciliation bridge
        ↓
Reconciliation dashboard
```

### Matching behavior

Matching is performed by `transaction_id`. A platform row can have zero, one, or multiple bank rows:

- Zero bank rows: `PENDING` or `OVERDUE` for successful events, depending on the settlement deadline.
- One bank row: amount, type, currency, dates, and timing are compared.
- Multiple bank rows: matching is ambiguous and is reported as `DUPLICATE_BANK_TRANSACTION` / `REVIEW_REQUIRED`.
- Bank-only rows: reported as `UNMATCHED_BANK`.

Failed platform payments without bank rows are intentionally not reported as missing settlements. A bank row for a failed payment produces `UNEXPECTED_SETTLEMENT_FOR_FAILED_PAYMENT`.

### Status precedence

When several conditions apply, the engine uses deterministic precedence:

```text
INVALID
UNMATCHED_BANK
REVIEW_REQUIRED
MISMATCH
OVERDUE
PENDING
MATCHED
```

Detailed finding codes are retained even when the primary status is overridden by a higher-priority condition.

### Financial calculations

Payments are positive and refunds are negative when calculating net totals. The report includes:

- Net expected platform amount
- Net actual bank amount
- Raw net difference
- Matched-record difference
- Pending and overdue exposure
- Duplicate-related amount
- Unmatched bank amount
- Aggregate rounding candidate
- Unresolved financial difference

The engine only treats small individual mismatches as a rounding candidate when their absolute value is within the configured threshold. It does not claim that rounding is the proven cause.

## API reference

| Endpoint | Method | Purpose |
|---|---:|---|
| `/api/health` | GET | Health check |
| `/api/generator/defaults` | GET | Generator defaults |
| `/api/generator/preview` | POST | Generate preview and summary |
| `/api/generator/download` | POST | Download all three generator files as ZIP |
| `/api/generator/download/{filename}` | POST | Download one generated CSV/JSON file |
| `/api/reconcile` | POST | Multipart CSV reconciliation |

### Reconciliation request

`POST /api/reconcile` uses `multipart/form-data`:

- `platform_file`: platform CSV
- `bank_file`: bank CSV
- `as_of_date`: optional `YYYY-MM-DD`
- `settlement_delay_days`: integer, default `2`
- `rounding_threshold`: decimal string, default `0.01`

Example:

```bash
curl -X POST http://localhost:8000/api/reconcile \
  -F "platform_file=@platform_transactions.csv" \
  -F "bank_file=@bank_settlements.csv" \
  -F "as_of_date=2024-04-01" \
  -F "settlement_delay_days=2" \
  -F "rounding_threshold=0.01"
```

The response contains `summary`, `financial_summary`, `discrepancies`, `matched_transactions`, `validation_errors`, and `reconciliation_metadata`.

## Testing and production build

Run backend tests:

```bash
cd backend
pytest -q
```

Build the frontend:

```bash
cd frontend
npm run build
```

The tests cover clean generation, deterministic seeds, parent-child integrity, refunds, failed-payment exclusion, discrepancy injection, ZIP contents, matching, pending/overdue states, cross-month settlements, amount mismatches, unmatched bank rows, signed refunds, and validation behavior.

## Security and operational notes

- The upload endpoint limits each CSV to 10 MB and accepts CSV filenames only.
- Uploaded files and reconciliation results are processed in memory; no files are persisted by the backend.
