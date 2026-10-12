"""Insert an already validated interaction into the caller's transaction.

Readiness/send admission remains the owning service's responsibility. This
helper allows the new supplied-branch context to commit with the CRM event.
"""


def insert_interaction_record(connection, record):
    columns = ("id", "account_id", "person_id", "contact_point_id", "message_draft_id", "export_id", "sequence_step", "interaction_type", "direction", "channel", "occurred_at", "outcome", "notes", "dedupe_key", "payload_json", "created_at", "outreach_purpose", "branch_id")
    connection.execute(
        "INSERT INTO interactions (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
        tuple(record.get(column) for column in columns),
    )
