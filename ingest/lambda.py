import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
import requests
from botocore.exceptions import ClientError

# Logging setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuring DynamoDB table name from environment variable
FDA_URL = "https://api.fda.gov/food/enforcement.json"
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "fda-food-recalls")
FETCH_LIMIT = 100  # records per ingest run

# init boto3 DynamoDB resource and table reference
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)


# Lambda handler function:
def fetch_recalls(limit: int = FETCH_LIMIT) -> list[dict]:
    """Fetch the most recent food enforcement records from the FDA API."""
    params = {
        "sort": "report_date:desc",
        "limit": limit,
    }
    logger.info("Fetching %d records from FDA enforcement API", limit)
    try: # fetching data from FDA API, if any error occurs, it will be logged and raised to be handled by the caller
        response = requests.get(FDA_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        logger.info("Received %d records from FDA API", len(results))
        return results
    except requests.exceptions.Timeout:
        logger.error("Request to FDA API timed out")
        raise
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error from FDA API: %s", e)
        raise
    except requests.exceptions.RequestException as e:
        logger.error("Request error fetching FDA data: %s", e)
        raise
    except (KeyError, ValueError) as e:
        logger.error("Unexpected response shape from FDA API: %s", e)
        raise

# Function to write a recall record to DynamoDB with idempotency check:
def write_recall(record: dict) -> bool: # returns True if a new record was written, False if it was a duplicate and skipped
    """
    Write a single recall record to DynamoDB.
    Uses condition_expression to skip duplicates (idempotent).
    Returns True if written, False if already existed.
    """
    recall_number = record.get("recall_number", "").strip()
    report_date = record.get("report_date", "").strip()
# validation to ensure recall_number and report_date are present, if not, log a warning and skip the record
    if not recall_number or not report_date:
        logger.warning("Skipping record missing recall_number or report_date: %s", record)
        return False
# writing the recall record to DynamoDB, with a condition to ensure we don't overwrite existing records with the same recall_number
    item = {
        "recall_number": recall_number,
        "report_date": report_date,
        "product_description": record.get("product_description", "")[:1000],
        "reason_for_recall": record.get("reason_for_recall", "")[:500],
        "classification": record.get("classification", ""),
        "status": record.get("status", ""),
        "state": record.get("state", ""),
        "recalling_firm": record.get("recalling_firm", ""),
        "voluntary_mandated": record.get("voluntary_mandated", ""),
        "ingested_at": int(time.time()),
    }
# attempting to write the item to DynamoDB, if a record with the same recall_number already exists, it will raise a ConditionalCheckFailedException which we catch to identify duplicates
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(recall_number)",
        )
        logger.info("Wrote new recall: %s (%s)", recall_number, report_date)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.debug("Recall already exists, skipping: %s", recall_number)
            return False
        logger.error("DynamoDB error writing recall %s: %s", recall_number, e)
        raise

# Main Lambda handler function:
def lambda_handler(event, context):
    """Entry point — fetch recent recalls and write new ones to DynamoDB."""
    logger.info(
        "Ingest Lambda started at %s",
        datetime.now(timezone.utc).isoformat(),
    )
# Fetching recalls from FDA API, if any error occurs during fetching, 
# it will be logged and the function will return a 500 status code with 
# the error message
    try:
        records = fetch_recalls()
    except Exception as e:
        logger.error("Failed to fetch recalls, aborting run: %s", e)
        return {"statusCode": 500, "body": f"Fetch failed: {e}"}

    written = 0
    skipped = 0
    errors = 0
# Iterating through the fetched records and attempting to write
# each one to DynamoDB, keeping track of how many were written, skipped 
# as duplicates, or had errors during writing
    for record in records:
        try:
            if write_recall(record):
                written += 1
            else:
                skipped += 1
        except Exception as e:
            logger.error("Failed to write record: %s | error: %s", record.get("recall_number"), e)
            errors += 1
# return back a summary of the ingest process, 
# including how many records were fetched, written,
# skipped as duplicates, and had errors. This will 
# be useful for monitoring and debugging the Lambda 
# function's performance over time.
    summary = {
        "total_fetched": len(records),
        "written": written,
        "skipped_duplicates": skipped,
        "errors": errors,
    }
    logger.info("Ingest complete: %s", summary)
    return {"statusCode": 200, "body": json.dumps(summary)}
