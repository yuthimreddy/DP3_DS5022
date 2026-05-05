# DS5220: DP3 — FDA Food Recall Tracker

## Data Source

**FDA Food Enforcement API** — `https://api.fda.gov/food/enforcement.json`

Tracks food recall events reported to the FDA, no API key required to access. The FDA publishes new enforcement actions on a rolling basis, making this a meaningful time series of real world food safety events.

**Cadence:** Every 1 hour (`rate(1 hour)` EventBridge rule)

---

## Storage Schema (DynamoDB)

Table name: `fda-food-recalls`

| Attribute | Type | Notes |
|---|---|---|
| `recall_number` | String (PK) | Unique recall identifier |
| `report_date` | String (SK) | Format: `YYYYMMDD` — used for range queries |
| `product_description` | String | What was recalled (truncated to 1000 chars) |
| `reason_for_recall` | String | Why it was recalled (truncated to 500 chars) |
| `classification` | String | Class I / II / III (severity) |
| `status` | String | Ongoing, Terminated, etc. |
| `state` | String | US state of the recalling firm |
| `recalling_firm` | String | Company name |
| `voluntary_mandated` | String | Voluntary or FDA-mandated |
| `ingested_at` | Number | Unix timestamp of when we stored it |

---

## API Resources

### `GET /`
Returns project description and list of available resources.

### `GET /current`
Returns a readable summary of the most recent food recall with the following attributes: firm name, product, reason, and date.

**Example:**
> `Most recent recall (April 28, 2025): Acme Foods recalled 'Organic peanut butter 16oz' due to: Potential Salmonella contamination.`

### `GET /trend`
Returns a breakdown of recalls by FDA classification over the last 90 days. Class I = most serious (health hazard), Class III = least serious.

**Example:**
> `In the last 90 days, 143 food recalls were recorded. Breakdown by FDA class — Class I: 61, Class II: 74, Class III: 8.`

### `GET /plot`
Generates a bar chart of weekly recall counts for the last 12 weeks, uploads it to S3, and returns the public URL.

---

## Architecture

```
EventBridge (rate 1 hour)
    └─> Ingest Lambda (ingest/lambda_function.py)
            └─> FDA API (public, no auth)
            └─> DynamoDB (fda-food-recalls)

API Gateway
    └─> Chalice Lambda (chalice-api/app.py)
            ├─> GET /          → project info
            ├─> GET /current   → latest recall
            ├─> GET /trend     → 90-day class breakdown
            └─> GET /plot      → weekly chart → S3 → public URL
```

---

## Setup

### 1. DynamoDB Table
Create via AWS Console or CLI:
```bash
aws dynamodb create-table \
  --table-name fda-food-recalls \
  --attribute-definitions \
    AttributeName=recall_number,AttributeType=S \
    AttributeName=report_date,AttributeType=S \
  --key-schema \
    AttributeName=recall_number,KeyType=HASH \
    AttributeName=report_date,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST
```

### 2. S3 Bucket
Create a bucket and note the name. Update `S3_BUCKET` env var in both Lambdas.

### 3. Ingest Lambda
- Zip `ingest/lambda_function.py` + `requests` library
- Set env var `DYNAMODB_TABLE=fda-food-recalls`
- Add an EventBridge trigger: `rate(1 hour)`

### 4. Chalice API
```bash
cd chalice-api
pip install chalice
chalice deploy
```
Update `.chalice/policy-dev.json` with your actual S3 bucket ARN before deploying.

Set env vars on the deployed Lambda:
- `DYNAMODB_TABLE=fda-food-recalls`
- `S3_BUCKET=your-actual-bucket-name`

### 5. Register with Discord Bot
```
/register <your-project-id> <your-username> <your-api-gateway-url>
```