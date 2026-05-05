# DS5220: DP3 — FDA Food Recall Tracker

## Data Source

**FDA Food Enforcement API** — `https://api.fda.gov/food/enforcement.json`

Tracks food recall events reported to the FDA, no API key required. The FDA publishes new enforcement actions on a rolling basis, making this a meaningful time series of real-world food safety events.

**Cadence:** Every 1 hour (`rate(1 hour)` EventBridge rule)

**Seeding:** Since it would take more time to wait for the USDA to find recalls, performed a one time backfill to load ~1000 historical records spanning several months. The hourly ingest job fetches the 100 most recent recalls and skips duplicates, so the table grows as new recalls are published.

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

Base URL: `https://7qvma9ivok.execute-api.us-east-1.amazonaws.com/api`

### `GET /`
Returns project description and list of available resources.

**Example response:**
```json
{
  "about": "Tracks FDA food enforcement recall events over time. Data is sourced from the FDA's open API and stored in DynamoDB.",
  "resources": ["current", "trend", "plot"]
}
```

### `GET /current`
Returns a readable summary of the most recent food recall including firm name, product, reason, and date.

**Example response:**
```json
{"response": "Most recent recall (April 29, 2026): Marquez Brothers International, Inc. recalled 'EL MEXICANO agua fresca de horchata...' due to: Undeclared milk."}
```

### `GET /trend`
Returns a breakdown of recalls by FDA classification over the last 90 days. Class I = most serious (health hazard), Class III = least serious.

**Example response:**
```json
{"response": "In the last 90 days, 259 food recalls were recorded. Breakdown by FDA class — Class I: 67, Class II: 166, Class III: 26. (Class I = most serious, Class III = least serious)"}
```

### `GET /plot`
Generates a weekly bar chart of recall counts for the last 12 weeks, uploads it to S3, and returns the public URL.

**Example response:**
```json
{"response": "https://dp3-usda-ds5220.s3.amazonaws.com/dp3/fda-food-recalls/latest.png"}
```


---

## Deployment Notes

### IAM
- The Chalice Lambda role (`fda-food-recalls-dev-api_handler`) has an inline policy granting `dynamodb:Scan`, `dynamodb:GetItem`, `dynamodb:Query` on the table and `s3:PutObject`, `s3:GetObject` on the S3 bucket.
- The ingest Lambda role has an inline policy granting `dynamodb:PutItem`, `dynamodb:GetItem`, `dynamodb:Scan`.
- `autogen_policy: false` is set in `.chalice/config.json` so Chalice does not overwrite the custom policy on deploy.

### S3
- Bucket `dp3-usda-ds5220` has public access enabled and a bucket policy allowing `s3:GetObject` on all objects so plot URLs are publicly readable.
- Per-object ACLs are disabled (AWS default) — public access is handled via bucket policy only.

### matplotlib
- Provided via a Klayers Lambda layer for Python 3.11 in `us-east-1`.
- `MPLCONFIGDIR=/tmp` is set as an environment variable to avoid read-only filesystem errors in the Lambda runtime.

### 1. DynamoDB Table
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
Create a bucket, disable ACLs, turn off block public access, and attach a public-read bucket policy.

### 3. Ingest Lambda
```bash
cd ingest
pip install requests -t package/
cp lambda.py package/
cd package && zip -r ../ingest.zip . && cd ..
aws lambda update-function-code \
  --function-name fda-food-recalls-ingest \
  --zip-file fileb://ingest.zip
aws lambda update-function-configuration \
  --function-name fda-food-recalls-ingest \
  --timeout 60 \
  --environment "Variables={DYNAMODB_TABLE=fda-food-recalls}"
```

Add an EventBridge trigger:
```bash
aws events put-rule \
  --name fda-food-recalls-ingest \
  --schedule-expression "rate(1 hour)" \
  --state ENABLED

aws events put-targets \
  --rule fda-food-recalls-ingest \
  --targets "Id=1,Arn=arn:aws:lambda:us-east-1:472821068498:function:fda-food-recalls-ingest"

aws lambda add-permission \
  --function-name fda-food-recalls-ingest \
  --statement-id EventBridgeInvoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:472821068498:rule/fda-food-recalls-ingest
```

### 4. Chalice API
```bash
cd chalice-api
pip install chalice
chalice deploy
```

### 5. Register with Discord Bot
