locations = {
    "MAIN CLINIC LOCATION": 50001,
}


resources_provider = {
    "JANE SMITH C. N. P.": {
        "resource_id": 30001,
        "provider_ids": [{"JANE SMITH": 20001}],
        "default_provider": 20001
    },
    "M. A.": {
        "resource_id": 30002,
        "provider_ids": [
            {"JANE SMITH": 20001},
            {"JOHN DOE": 20002}
        ],
        "default_provider": 20001
    },
    "JOHN DOE CNP": {
        "resource_id": 30003,
        "provider_ids": [{"JOHN DOE": 20002}],
        "default_provider": 20002
    }
}

providers = {
    "JANE SMITH": 20001,
    "JOHN DOE": 20002,
}



OFFICE_ADDRESS = {
    "street": "XXXX",
    "city": "XXXX",
    "state": "XXXX",
    "zip": "XXXXX",
    "full_address": "XXXX"
}

OFFICE_CONTACT = {
    "phone": "XXX-XXX-XXXX",
    "fax": "XXX-XXX-XXXX"
}

# Default location ID (first/only location)
DEFAULT_LOCATION_ID = next(iter(locations.values()))

# Default provider ID (fallback when prescriber not matched)
DEFAULT_PROVIDER_ID = next(iter(providers.values()))


def resolve_provider_id(prescriber_name: str) -> int:
    """Resolve a prescriber name to a provider_id from the knowledge base.

    Performs case-insensitive partial matching against the providers dict.
    Falls back to the default provider if no match is found.
    """
    if not prescriber_name:
        return DEFAULT_PROVIDER_ID

    name = prescriber_name.strip().upper()

    # Exact match
    if name in providers:
        return providers[name]

    # Partial match: check if the input contains or is contained in a provider name
    for kb_name, pid in providers.items():
        if name in kb_name or kb_name in name:
            return pid

    # Try matching just the last name
    parts = name.split()
    for kb_name, pid in providers.items():
        kb_parts = kb_name.split()
        if parts[-1] in kb_parts or kb_parts[-1] in parts:
            return pid

    return DEFAULT_PROVIDER_ID


def resolve_location_id() -> int:
    """Return the default location ID."""
    return DEFAULT_LOCATION_ID


def get_provider_list() -> list[str]:
    """Return the list of provider names for prompting."""
    return list(providers.keys())




# ---------------------------------------------------------------------------
# Task queue IDs — keyed by provider name (matches `providers` dict keys)
# ---------------------------------------------------------------------------
TASK_QUEUES = {
    "JANE SMITH":   40001,   # Dr. Jane Smith provider queue
    "JOHN DOE": 40002,  # Dr. John Doe provider queue
}

# Default task queue (falls back to Jane Smith when prescriber not matched)
DEFAULT_TASK_QUEUE_ID = TASK_QUEUES["JANE SMITH"]

# ---------------------------------------------------------------------------
# Fixed task defaults — injected automatically for every Create_Task call
# ---------------------------------------------------------------------------
TASK_DEFAULTS = {
    "task_type_id":         "10",         # Patient task type
    "task_request_type_id": "298",        # Telephone Message
    "task_priority_id":     "2",          # Normal priority
    "task_set_id":          "100000001",
    "task_status_id":       "1",          # Open
}


def resolve_task_queue_id(prescriber_name: str) -> int:
    """Resolve a prescriber name to the correct task_queue_id.

    Performs case-insensitive partial matching against TASK_QUEUES.
    Falls back to DEFAULT_TASK_QUEUE_ID if no match is found.
    """
    if not prescriber_name:
        return DEFAULT_TASK_QUEUE_ID

    name = prescriber_name.strip().upper()

    # Exact match
    if name in TASK_QUEUES:
        return TASK_QUEUES[name]

    # Partial match
    for kb_name, qid in TASK_QUEUES.items():
        if name in kb_name or kb_name in name:
            return qid

    # Last-name match
    parts = name.split()
    for kb_name, qid in TASK_QUEUES.items():
        kb_parts = kb_name.split()
        if parts[-1] in kb_parts or kb_parts[-1] in parts:
            return qid

    return DEFAULT_TASK_QUEUE_ID


