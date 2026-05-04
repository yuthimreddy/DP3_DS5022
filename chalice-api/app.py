# Imports:
import io 
import logging
import os 
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone


# Application imports:
import boto3
import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests
from boto3.dynamodb.conidtions import Key
from botocore.exceptions import ClientError
from chalice import Chalice
from decimal import Decimal

# setting up logging:
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# setting up chalice config:
app = Chalice(app_name="fda-food-recalls")
logger.setLevel(logging.INFO)

# Defining env vars:
TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "fda-food-recalls")
S3_BUCKET = os.environ.get("S3_BUCKET", "your-dp3-bucket")
S3_PLOT_KEY = "dp3/fda-food-recalls/latest.png"

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3") # importing boto3 client

# Function to fetch recall data from the FDA API:
def scan_recent_recalls(days: int = 90) -> list[dict]:
    """Scanning DynamoDB for recalls with report_date in the last `days` days.
    Returns a list of item dicts. """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%d")
    logger.info("Scanning recalls with report_date >= %s", cutoff)

    items = []
    try:
        response = table.scan(
            FilterExpression="report_date >= :cutoff"
            ExpressionAttributeValues={":cutoff":cutoff},
        )
        items.extend(response("Items", []))

        while "LastEvaluatedKey" in response: # if we have more results to paginate through, keep scanning
            response = table.scan(
                FilterExpression="report_date >= :cutoff",
                ExpressionAttributeValues={":cutoff": cutoff},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            items.extend(response.get("Items", []))

        logger.info("Scan returned %d items", len(items))
    except ClientError as e:
        logger.error("DynamoDB scan failed: %s", e)
        raise
# does a full scan and sort to find the most recent recall. This is not the most efficient way to do this, but it avoids the need for a secondary index on report_date.
def get_latest_recall() -> dict | None:
    """Return the single most recent recall from DynamoDB through a full scan and sort"""
    try:
        items = scan_recent_recalls(days=365)  # Scan for recalls in the last yea
        if not items:
            logger.warning("No recalls found in DynamoDB")
            return None
        latest = max(items, key=lambda x: (x.get("report_date", ""), x.get("ingested_at", 0)))
        logger.info("Latest recall found: %s on %s", latest.get("recall_number"), latest.get("report_date"))
        return latest
    except Exception as e:
        logger.error("Error fetching latest recall: %s", e)
        raise
# Function to generate and upload plot to S3:
def generate_and_upload_plot(items: list[dict]) -> str:
    """Build a weekly bar char of recall counts from `items` and upload to S3. Returns the S3 URL of the plot."""
    logger.info("generating plot from %ed items", len(items))
   
   # aggregating into week buckets:
    week_counts: Counter = Counter()
    for item in items:
        date_str = item.get("report_date", "")
        try:
            dt = datetime.striptime(date_str, "%Y%m%d")
            week_start = dt - timedelta(days = dt.weekday())
            week_counts[week_start] += 1
        except (ValueError, TypeError):
            logger.debug("Could not parse report_date: %s", date_str)
# if there is no plottable data, raise an error to avoid uploading 
# an empty plot
    if not week_counts:
        logger.warning("No plottable data found")
        raise ValueError("No data to plot")
    
    weeks = sorted(week_counts.keys())[-12:]
    counts = [week_counts[w] for w in weeks]

    # Making Plot:
    fig, ax = plt.subplots(figsize=(10,6))
    ax.bar(weeks, counts, width = 5, color = '#EA1744')
    # FORMATTING:
    ax.set_title("FDA Food Recalls (Last 12 Weeks)", fontsize = 16, weight = 'bold')
    ax.set_xlabel("Week Starting", fontsize = 14, weight = 'bold')
    ax.set_ylabel("Number of Recalls", fontsize = 14, weight = 'bold')
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    fig.autofmt_xdate(rotation=30)
    plt.tight_layout()

    # Writing buffer and uploading to s3:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    buf.seek(0)
    plt.close(fig)

    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=S3_PLOT_KEY,
            Body=buf.read(),
            ContentType="image/png",
            ACL="public-read",
        )
        url = f"https://{S3_BUCKET}.s3.amazonaws.com/{S3_PLOT_KEY}"
        logger.info("Plot uploaded to S3: %s", url)
        return url 
    except ClientError as e:
        logger.error("S3 upload failed: %s", e)
        raise

# Routes:

# Provides a simple overview of the API and its resources.
@app.route("/")
def index():
    return{
       "about": (
            "Tracks FDA food enforcement (recall events over time."
            "Data is sourced from the FDA's open API and stored in DynamoDB."   

        ),
        "resources": ["current", "trend", "plot"],
        }
# Returns a text summary of the most recent recall, including the recalling 
# firm, product description, reason for recall, and report date. This gives
# users a quick snapshot of the latest recall activity.
@app.route("/current")
def current():
    """Return the most recent food recall."""
    logger.info("GET /current called")
    try:
        recall = get_latest_recall()
        if not recall:
            return {"response": "No data available yet"}
        
        firm = recall.get("recalling_firm", "Unknown firm")
        product = recall.get("product_description", "Unknown product")[:120]
        date = recall.get("report_date", "")
        # Format date from YYYYMMDD → Month DD, YYYY
        try:
            date = datetime.strptime(date, "%Y%m%d").strftime("%B %d, %Y")
        except (ValueError, TypeError):
            pass

        msg = f"Most recent recall ({date}): {firm} recalled '{product}' due to: {reason}."
        return {"response": msg}

    except Exception as e:
        logger.error("/current error: %s", e)
        return {"response": f"Error retrieving current recall: {e}"}

# returns a text summary of recall trends over the last 90 days, broken down by FDA classification (I, II, III) and total count. 
# This is a simple way to track whether recall activity is increasing or decreasing over time, and whether the severity of recalls is changing.
@app.route("/trend")
def trend():
    """Return recall counts broken down by classification over the last 90 days."""
    logger.info("GET /trend called")
    try:
        items = scan_recent_recalls(days=90)
        if not items:
            return {"response": "No recall data available yet."}

        class_counts: Counter = Counter()
        for item in items:
            cls = item.get("classification", "Unknown").strip()
            class_counts[cls] += 1

        total = sum(class_counts.values())
        parts = ", ".join(
            f"{cls}: {cnt}" for cls, cnt in sorted(class_counts.items())
        )
        msg = (
            f"In the last 90 days, {total} food recalls were recorded. "
            f"Breakdown by FDA class — {parts}. "
            f"(Class I = most serious, Class III = least serious)"
        )
        return {"response": msg}

    except Exception as e:
        logger.error("/trend error: %s", e)
        return {"response": f"Error computing trend: {e}"}
# generates a weekly bar chart of recall counts for the last 90 days, and
# returns the S3 URL of the plot. This provides users with a visual 
# representation of recall activity over time, making it easier to spot 
# trends and patterns.

# The plot is cached in S3 and only updated when this endpoint is called
# so the first call may take a few seconds to generate and upload the plot
# but subsequent calls will be fast until new data comes in. This is a
# simple way to balance performance with up-to-date visuals without needing a
# separate caching layer or scheduled job.
@app.route("/plot")
def plot():
    """Generate (or return cached) a weekly recall bar chart from S3."""
    logger.info("GET /plot called")
    try:
        items = scan_recent_recalls(days=90)
        if not items:
            return {"response": "No data collected yet — check back after the ingestion pipeline has run."}

        url = generate_and_upload_plot(items)
        return {"response": url}

    except Exception as e:
        logger.error("/plot error: %s", e)
        return {"response": f"Error generating plot: {e}"}




        
        


    #




                  

