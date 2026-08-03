from google.cloud import bigquery

PROJECT_ID = "github-fake-star"
BUCKET_NAME = "fake_star"

client = bigquery.Client(project=PROJECT_ID)

# Latest partition ID
partition_id = "20260316"

table_ref = f"openssf.scorecardcron.scorecard-v2${partition_id}"
destination_uri = f"gs://{BUCKET_NAME}/scorecard/export-{partition_id}-*.json"

print(f"Exporting partition {partition_id} to JSON format...")

extract_job = client.extract_table(
    table_ref,
    destination_uri,
    job_config=bigquery.ExtractJobConfig(
        destination_format=bigquery.DestinationFormat.NEWLINE_DELIMITED_JSON  # Switched to JSON
        # The compression parameter is not required; JSON is uncompressed by default, but it may be added
    )
)

extract_job.result()
print(f"✅ Export complete: {destination_uri}")