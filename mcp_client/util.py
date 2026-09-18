import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

# Ensure project root is on sys.path so top-level modules are importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Import from mcp libraries
from mcp.types import Tool as MCPTool, CallToolResult
from .server import MCPServer

# Knowledge base for provider/location lookups
from knowledge_base import (
    providers as KB_PROVIDERS,
    locations as KB_LOCATIONS,
    resolve_provider_id,
    resolve_location_id,
    resolve_task_queue_id,
    TASK_DEFAULTS,
)


# A minimal FunctionTool class used by the agent.
class FunctionTool:
    def __init__(self, name: str, description: str, params_json_schema: Dict[str, Any], on_invoke_tool, strict_json_schema: bool = False):
        self.name = name
        self.description = description
        self.params_json_schema = params_json_schema
        self.on_invoke_tool = on_invoke_tool  # This should be an async function.
        self.strict_json_schema = strict_json_schema

    def __repr__(self):
        return f"FunctionTool(name={self.name})"


# =========================================================================
# Patient Verification – deterministic filter functions
# =========================================================================

def _is_auth_error(raw_text: str) -> bool:
    """Check if the API response indicates an authorization failure."""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return False
    # Response is typically [{"error":{"error_code":"0037","message":"Authorization Failed"}}]
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict):
            err = item.get("error", {})
            if isinstance(err, dict):
                msg = (err.get("message") or "").lower()
                code = err.get("error_code") or ""
                if "authorization failed" in msg or code == "0037":
                    return True

    # Keyword phrases that clearly indicate auth/token failures
    lower = raw_text.lower()
    auth_phrases = [
        "unauthorized", "forbidden",
        "token expired", "token invalid", "invalid token",
        "authentication failed", "auth failed", "not authenticated",
        "access denied", "invalid credentials", "session expired",
        "invalid_grant", "token_expired", "authorization failed",
    ]
    if any(phrase in lower for phrase in auth_phrases):
        return True

    return False


def _parse_patients_from_response(raw_text: str) -> List[Dict[str, Any]]:
    """Parse the MCP API response into a flat list of patient dicts."""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError):
        return []

    # Response may be: [{patients:[...]}, ...], [patient, ...], or {patients:[...]}
    if isinstance(data, list):
        if data and isinstance(data[0], dict) and "patients" in data[0]:
            return data[0].get("patients", [])
        # Could also be a flat list of patient dicts
        if data and isinstance(data[0], dict) and ("id" in data[0] or "first_name" in data[0]):
            return data
        return []
    if isinstance(data, dict):
        if "patients" in data:
            return data["patients"]
        if "id" in data or "first_name" in data:
            return [data]
    return []


def _normalize_name(name: str) -> str:
    """Lowercase, strip whitespace for comparison."""
    return (name or "").strip().lower()


def _filter_by_name(patients: List[Dict], first_name: str, last_name: str) -> List[Dict]:
    """Return patients whose first AND last name match (case-insensitive)."""
    fn = _normalize_name(first_name)
    ln = _normalize_name(last_name)
    matched = []
    for p in patients:
        p_fn = _normalize_name(p.get("first_name") or p.get("firstName") or "")
        p_ln = _normalize_name(p.get("last_name") or p.get("lastName") or "")
        if p_fn == fn and p_ln == ln:
            matched.append(p)
    return matched


def _normalize_dob(dob_string: str) -> Optional[str]:
    """Normalize a date-of-birth string into YYYY-MM-DD for comparison.

    Handles formats like:
      - 1975-06-05, 06/05/1975, 06-05-1975
      - June 5, 1975  /  June 5th, 1975
    Returns None if parsing fails.
    """
    if not dob_string:
        return None
    s = dob_string.strip()

    # Strip ISO-8601 time + timezone suffix (e.g. "2002-07-01T12:00:00-04:00" → "2002-07-01")
    iso_match = re.match(r'^(\d{4}-\d{2}-\d{2})[T ]', s)
    if iso_match:
        s = iso_match.group(1)

    # Remove ordinal suffixes (1st, 2nd, 3rd, 4th, …)
    s = re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', s, flags=re.IGNORECASE)

    # Try common formats
    for fmt in (
        "%Y-%m-%d",   # 1975-06-05
        "%m/%d/%Y",   # 06/05/1975
        "%m-%d-%Y",   # 06-05-1975
        "%B %d, %Y",  # June 5, 1975
        "%b %d, %Y",  # Jun 5, 1975
        "%B %d %Y",   # June 5 1975
        "%b %d %Y",   # Jun 5 1975
        "%d %B %Y",   # 5 June 1975
        "%d %b %Y",   # 5 Jun 1975
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _filter_by_dob(patients: List[Dict], date_of_birth: str) -> List[Dict]:
    """Return patients whose DOB matches the given date_of_birth."""
    target = _normalize_dob(date_of_birth)
    if target is None:
        # Can't normalize caller's DOB → skip DOB filter, return all
        return patients

    matched = []
    for p in patients:
        p_dob_raw = p.get("date_of_birth") or p.get("dob") or p.get("dateOfBirth") or ""
        p_dob = _normalize_dob(p_dob_raw)
        if p_dob == target:
            matched.append(p)
    return matched


def _get_patient_status(patient: Dict) -> Optional[str]:
    """Extract patient status from top-level or provider_assignment_indicator."""
    status = patient.get("status")
    if status:
        return status.strip().upper()
    pai = patient.get("provider_assignment_indicator")
    if isinstance(pai, dict):
        s = pai.get("status")
        if s:
            return s.strip().upper()
    return None


def _filter_by_active_status(patients: List[Dict]) -> List[Dict]:
    """Return only patients whose status is 'A' (active).

    Patients without a status field are kept (assumed active).
    Only patients with an explicit non-'A' status are filtered out.
    """
    result = []
    for p in patients:
        status = _get_patient_status(p)
        if status is None or status == "A":
            result.append(p)
    return result


def verify_patient(raw_text: str, first_name: str, last_name: str, date_of_birth: str) -> Dict[str, Any]:
    """Deterministic patient verification pipeline.

    1. Parse API response → list of patients
    2. Filter out inactive patients (status != 'A')
    3. Filter by name (exact, case-insensitive)
    4. Filter by DOB (normalized date comparison)
    5. Return structured result dict.
    """
    all_patients = _parse_patients_from_response(raw_text)
    print(f"[VERIFY] Total patients returned by API: {len(all_patients)}")

    for p in all_patients:
        p_name = f"{p.get('first_name', '?')} {p.get('last_name', '?')}"
        p_status = _get_patient_status(p)
        print(f"[VERIFY]   Patient: {p_name} | id={p.get('id')} | status={p_status or 'None (assumed active)'}")

    all_patients = _filter_by_active_status(all_patients)
    print(f"[VERIFY] After active-status filter: {len(all_patients)}")

    if not all_patients:
        return {
            "verified": False,
            "error_type": "name_not_found",
            "message": f"No patients found matching '{first_name} {last_name}'.",
            "patient_data": [],
        }

    name_matches = _filter_by_name(all_patients, first_name, last_name)
    print(f"[VERIFY] After name filter ('{first_name} {last_name}'): {len(name_matches)} match(es)")

    if not name_matches:
        return {
            "verified": False,
            "error_type": "name_not_found",
            "message": f"No patients found with name '{first_name} {last_name}'.",
            "patient_data": all_patients,
        }

    dob_matches = _filter_by_dob(name_matches, date_of_birth)
    print(f"[VERIFY] After DOB filter ('{date_of_birth}'): {len(dob_matches)} match(es)")

    if not dob_matches:
        return {
            "verified": False,
            "error_type": "dob_mismatch",
            "message": f"Found patient(s) named '{first_name} {last_name}' but date of birth does not match.",
            "patient_stated_dob": date_of_birth,
            "patient_data": name_matches,
        }

    if len(dob_matches) == 1:
        patient = dob_matches[0]
        return {
            "verified": True,
            "matched_patient": patient,
            "last_seen_date": patient.get("last_seen_date") or patient.get("lastSeenDate"),
            "patient_data": dob_matches,
        }

    # Multiple DOB matches (rare but possible with duplicates)
    return {
        "verified": True,
        "matched_patient": dob_matches[0],
        "last_seen_date": dob_matches[0].get("last_seen_date") or dob_matches[0].get("lastSeenDate"),
        "message": f"Multiple records matched ({len(dob_matches)}). Using first match.",
        "patient_data": dob_matches,
    }


class MCPUtil:
    # Track the last verified patient_id for encounter creation.
    _last_verified_patient_id: str | None = None

    # Auth-failure message returned to LLM so it triggers transfer_to_human
    _AUTH_FAILED_TRANSFER_MSG = (
        "AUTHORIZATION_FAILED: The system could not authenticate after multiple attempts. "
        "Please transfer the caller to a human agent immediately using transfer_to_human."
    )

    @staticmethod
    async def _call_with_auth_retry(server, tool_name: str, arguments: dict, extract_fn, max_retries: int = 2):
        """Call an MCP tool; on auth failure, refresh DB tokens and retry up to max_retries times.

        Returns (raw_text, auth_ok).  If auth_ok is False after all retries,
        the caller should return _AUTH_FAILED_TRANSFER_MSG.
        """
        from agent import load_tokens_from_db

        for attempt in range(1, max_retries + 1):
            result = await server.call_tool(tool_name, arguments)
            raw_text = extract_fn(result)

            if not _is_auth_error(raw_text):
                return raw_text, True  # success

            print(f"[AUTH_RETRY] Attempt {attempt}/{max_retries} — authorization failed for {tool_name}, refreshing tokens...")
            load_tokens_from_db()
            fresh_token = os.environ.get("COMPANION_TOKEN")
            if fresh_token:
                arguments["web_token"] = fresh_token
                print(f"[AUTH_RETRY] Refreshed COMPANION_TOKEN (len={len(fresh_token)})")

        # All retries exhausted
        print(f"[AUTH_RETRY] All {max_retries} attempts failed for {tool_name} — requesting transfer to human")
        return raw_text, False

    @staticmethod
    def _extract_raw_text(result) -> str:
        """Extract raw text string from an MCP tool call result."""
        if isinstance(result, dict) and "content" in result:
            content = result["content"]
            if isinstance(content, list) and content:
                first = content[0]
                return first if isinstance(first, str) else (
                    first.text if hasattr(first, "text") else str(first)
                )
        elif hasattr(result, "content") and isinstance(result.content, list):
            first = result.content[0]
            return first if isinstance(first, str) else (
                first.text if hasattr(first, "text") else str(first)
            )
        return str(result)

    @classmethod
    async def get_function_tools(cls, server, convert_schemas_to_strict: bool) -> List[FunctionTool]:
        tools = await server.list_tools()
        function_tools = []
        for tool in tools:
            ft = cls.to_function_tool(tool, server, convert_schemas_to_strict)
            function_tools.append(ft)
        return function_tools

    # --- Patient_Verification: custom schema exposed to the LLM ---
    _PATIENT_VERIFICATION_SCHEMA = {
        "type": "object",
        "properties": {
            "first_name": {
                "type": "string",
                "description": "Patient's first name.",
            },
            "last_name": {
                "type": "string",
                "description": "Patient's last name.",
            },
            "date_of_birth": {
                "type": "string",
                "description": "Patient's date of birth as stated by the caller (e.g. 'June 5, 1975').",
            },
        },
        "required": ["first_name", "last_name", "date_of_birth"],
    }

    # --- Create_Encounter: custom schema exposed to the LLM ---
    _CREATE_ENCOUNTER_SCHEMA = {
        "type": "object",
        "properties": {
            "prescriber_name": {
                "type": "string",
                "description": "Name of the prescribing provider (e.g. 'Jane Smith'). System will resolve to provider_id and location_id.",
            },
            "date_of_service": {
                "type": "string",
                "description": "Date of service in YYYY-MM-DD format. Defaults to today.",
            },
        },
        "required": ["prescriber_name"],
    }

    # --- Create_Task: custom schema exposed to the LLM ---
    _CREATE_TASK_SCHEMA = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Medication line formatted as 'RX \u2013 {drug_name} \u2013 {dosage} \u2013 {pharmacy_name} near {pharmacy_location}'. Omit dosage if unknown. Omit location if not provided. Do NOT use parentheses around the location.",
            },
            "prescriber_name": {
                "type": "string",
                "description": "Name of the prescribing provider (e.g. 'Jane Smith'). Used to route the task to the correct queue.",
            },
            "is_urgent": {
                "type": "boolean",
                "description": "REQUIRED. Set to true if the patient marked this refill as urgent in Step 3d, otherwise set to false.",
            },
        },
        "required": ["description", "prescriber_name", "is_urgent"],
    }

    @classmethod
    def to_function_tool(cls, tool, server, convert_schemas_to_strict: bool) -> FunctionTool:
        original_schema = tool.inputSchema or {}
        schema = dict(original_schema)
        props = dict(schema.get("properties", {}) or {})

        # Ensure ID-like fields are typed as string (LiveKit pydantic rejects anyOf)
        def _ensure_string_type(prop: Dict[str, Any]) -> Dict[str, Any]:
            if not isinstance(prop, dict):
                return prop
            prop = dict(prop)
            # Remove anyOf if present, force to string
            prop.pop("anyOf", None)
            t = prop.get("type")
            if t in ("number", "integer") or t is None:
                prop["type"] = "string"
            return prop

        id_like_fields = [
            "business_entity_id", "provider_id", "location_id",
            "patient_id", "nature_of_visit_id", "encounter_id",
            "task_queue_id", "task_status_id", "task_priority_id",
            "task_request_type_id", "task_type_id",
        ]
        for f in id_like_fields:
            if f in props:
                props[f] = _ensure_string_type(props[f])

        if props:
            schema["properties"] = props

        # --- Companion tools: the only 4 tools that get token injection ---
        COMPANION_TOOLS = {
            "Patient_Verification",
            "Create_Encounter",
            "Open_Encounter_tab",
            "Create_Note",
            "Create_Task",
        }

        # Override Patient_Verification schema to expose first_name/last_name/dob
        is_patient_verification = (getattr(tool, "name", "") == "Patient_Verification")
        if is_patient_verification:
            schema = dict(cls._PATIENT_VERIFICATION_SCHEMA)

        # Override Create_Encounter schema to accept prescriber_name
        is_create_encounter = (getattr(tool, "name", "") == "Create_Encounter")
        if is_create_encounter:
            schema = dict(cls._CREATE_ENCOUNTER_SCHEMA)

        # Override Create_Task schema to only expose name/description/prescriber_name
        is_create_task = (getattr(tool, "name", "") == "Create_Task")
        if is_create_task:
            schema = dict(cls._CREATE_TASK_SCHEMA)

        # Make web_token optional in the exposed schema since we inject it at call-time
        try:
            req = list(schema.get("required", []) or [])
            if getattr(tool, "name", "") in COMPANION_TOOLS:
                if "web_token" in req:
                    req.remove("web_token")
                if "token" in req:
                    req.remove("token")
                schema["required"] = req
        except Exception:
            pass

        async def invoke_tool(context: Any, input_json: str, current_tool_name=tool.name) -> str:
            try:
                arguments = json.loads(input_json) if input_json else {}
            except Exception as e:
                return f"Error parsing input JSON for tool '{current_tool_name}': {e}"

            # Use the original server-provided schema to determine expected types
            original_props = (original_schema.get("properties") or {}) if isinstance(original_schema, dict) else {}

            # Type coercion based on server schema
            for key, val in list(arguments.items()):
                prop = original_props.get(key)
                if not isinstance(prop, dict):
                    continue
                t = prop.get("type")
                if t == "string" and val is not None and not isinstance(val, str):
                    arguments[key] = str(val)

            # Ensure all ID-like fields are strings (LLM may send as int)
            for key, val in list(arguments.items()):
                if key.endswith("_id") and isinstance(val, (int, float)):
                    arguments[key] = str(int(val))

            # --- Inject companion token for the 4 registered tools ---
            if current_tool_name in COMPANION_TOOLS:
                token = os.environ.get("COMPANION_TOKEN")
                if token is not None:
                    arguments["web_token"] = token
                    print(f"[TOKEN_INJECT] Set web_token from COMPANION_TOKEN (len={len(token)}) for {current_tool_name}")
                else:
                    print(f"[TOKEN_INJECT] WARNING: COMPANION_TOKEN not found in environment for {current_tool_name}")

                # Inject business_entity_id for tools that need it
                if "business_entity_id" in original_props:
                    biz_id = os.environ.get("BUSINESS_ID", "1000")
                    arguments["business_entity_id"] = biz_id
                    print(f"[BUSINESS_ENTITY_INJECT] Set business_entity_id={biz_id} for {current_tool_name}")

            # =============================================================
            # Patient_Verification: function-based pipeline
            # =============================================================
            if current_tool_name == "Patient_Verification":
                first_name = arguments.pop("first_name", "")
                last_name = arguments.pop("last_name", "")
                date_of_birth = arguments.pop("date_of_birth", "")

                # Build search_term for the MCP API (server expects this field)
                arguments["search_term"] = f"{first_name} {last_name}".strip()

                try:
                    raw_text, auth_ok = await cls._call_with_auth_retry(
                        server, current_tool_name, arguments, cls._extract_raw_text, max_retries=2
                    )
                    print(f"[PATIENT_VERIFICATION_RAW] {raw_text[:500]}")

                    if not auth_ok:
                        return json.dumps({
                            "verified": False,
                            "error_type": "authorization_failed",
                            "message": cls._AUTH_FAILED_TRANSFER_MSG,
                        })

                    # Run deterministic verification pipeline
                    verification = verify_patient(raw_text, first_name, last_name, date_of_birth)

                    # Store patient_id in class state if verified
                    if verification.get("verified"):
                        matched = verification.get("matched_patient", {})
                        pid = matched.get("id")
                        if pid:
                            cls._last_verified_patient_id = str(pid)
                            print(f"[PATIENT_ID_STORED] Stored verified patient_id={cls._last_verified_patient_id}")

                    return json.dumps(verification)
                except Exception as e:
                    return json.dumps({
                        "verified": False,
                        "error_type": "api_error",
                        "message": f"Error calling Patient_Verification: {type(e).__name__}: {e}",
                    })

            # =============================================================
            # Create_Encounter: resolve prescriber_name → provider_id + location_id
            # =============================================================
            if current_tool_name == "Create_Encounter":
                # Auto-inject patient_id from the last verified patient
                if cls._last_verified_patient_id:
                    arguments["patient_id"] = cls._last_verified_patient_id
                    print(f"[ENCOUNTER_INJECT] patient_id={cls._last_verified_patient_id}")
                else:
                    # If LLM passed it (shouldn't happen), coerce to string
                    pid = arguments.get("patient_id")
                    if pid is not None:
                        arguments["patient_id"] = str(int(pid)) if isinstance(pid, (int, float)) else str(pid)

                prescriber_name = arguments.pop("prescriber_name", "")
                provider_id = str(resolve_provider_id(prescriber_name))
                location_id = str(resolve_location_id())

                arguments["provider_id"] = provider_id
                arguments["location_id"] = location_id
                print(f"[ENCOUNTER_RESOLVE] prescriber_name='{prescriber_name}' → provider_id={provider_id}, location_id={location_id}")

                # Default date_of_service to today if not provided
                if not arguments.get("date_of_service"):
                    arguments["date_of_service"] = datetime.now().strftime("%Y-%m-%d")

                # Default nature_of_visit_id if not provided
                if not arguments.get("nature_of_visit_id"):
                    arguments["nature_of_visit_id"] = ""

            # =============================================================
            # Create_Task: inject patient_id, task_queue_id, and fixed defaults
            # =============================================================
            if current_tool_name == "Create_Task":
                # Hardcode the fixed task name
                arguments["name"] = "Prescription Refill"
                print("[TASK_INJECT] name='Prescription Refill'")
                # Auto-inject patient_id from the last verified patient
                if cls._last_verified_patient_id:
                    arguments["patient_id"] = cls._last_verified_patient_id
                    print(f"[TASK_INJECT] patient_id={cls._last_verified_patient_id}")
                else:
                    print("[TASK_INJECT] ERROR: No verified patient_id available — cannot create task")
                    return json.dumps({
                        "success": False,
                        "error": "Patient not verified. Cannot create task without a verified patient. Please verify the patient first using Patient_Verification.",
                    })

                # Resolve task_queue_id from prescriber_name then remove it
                prescriber_name = arguments.pop("prescriber_name", "")
                task_queue_id = str(resolve_task_queue_id(prescriber_name))
                arguments["task_queue_id"] = task_queue_id
                print(f"[TASK_INJECT] prescriber_name='{prescriber_name}' → task_queue_id={task_queue_id}")

                # Inject all fixed task defaults from knowledge base
                for key, val in TASK_DEFAULTS.items():
                    arguments[key] = val
                    print(f"[TASK_INJECT] {key}={val}")

                # Override task_priority_id if marked urgent
                is_urgent = arguments.pop("is_urgent", False)
                if is_urgent:
                    arguments["task_priority_id"] = "3"  # Urgent priority
                    print("[TASK_INJECT] is_urgent=True → task_priority_id=3 (Urgent)")
                else:
                    print("[TASK_INJECT] is_urgent=False → task_priority_id=2 (Normal)")

                # Inject user_profile_id from env if available
                user_profile_id = os.environ.get("USER_PROFILE_ID")
                if user_profile_id:
                    arguments["user_profile_id"] = user_profile_id
                    print(f"[TASK_INJECT] user_profile_id={user_profile_id}")

            # =============================================================
            # All tools – call with auth retry for companion tools
            # =============================================================
            try:
                # For companion tools, use auth-retry; for others, call directly
                if current_tool_name in COMPANION_TOOLS:
                    raw_text, auth_ok = await cls._call_with_auth_retry(
                        server, current_tool_name, arguments, cls._extract_raw_text, max_retries=2
                    )
                    if not auth_ok:
                        return cls._AUTH_FAILED_TRANSFER_MSG

                    # Open_Encounter_tab returns massive XML — return only key fields
                    if current_tool_name == "Open_Encounter_tab":
                        try:
                            data = json.loads(raw_text)
                            summary = {
                                "encounter_note_id": data.get("id") or data.get("encounter_note_id"),
                                "encounter_id": data.get("encounter_id") or (data.get("plan_section", {}) or {}).get("encounter", {}).get("id"),
                                "patient_name": data.get("patient_name"),
                                "date_of_service": data.get("date_of_service"),
                                "location_id": data.get("location_id"),
                                "success": True,
                            }
                            return json.dumps(summary)
                        except (json.JSONDecodeError, TypeError):
                            return '{"success": true, "message": "Encounter tab opened"}'

                    # Companion tool succeeded — return raw text
                    return raw_text

                # Non-companion tools — call directly (no auth retry)
                result = await server.call_tool(current_tool_name, arguments)

                # Convert result to dict for post-processing
                if isinstance(result, dict):
                    result_dict = result
                elif hasattr(result, 'content'):
                    result_dict = {"content": result.content}
                elif hasattr(result, '__dict__'):
                    result_dict = result.__dict__
                else:
                    result_dict = {"content": [str(result)]}

                # --- Return result as string ---
                if "content" in result_dict and isinstance(result_dict["content"], list) and len(result_dict["content"]) >= 1:
                    if len(result_dict["content"]) == 1:
                        content_item = result_dict["content"][0]
                        if hasattr(content_item, 'text'):
                            return str(content_item.text)
                        elif isinstance(content_item, (str, int, float, bool)):
                            return str(content_item)
                        else:
                            try:
                                return json.dumps(content_item)
                            except TypeError:
                                return str(content_item)
                    else:
                        items = []
                        for ci in result_dict["content"]:
                            if hasattr(ci, 'text'):
                                items.append(ci.text)
                            elif isinstance(ci, (str, int, float, bool)):
                                items.append(str(ci))
                            else:
                                items.append(ci)
                        try:
                            return json.dumps(items)
                        except TypeError:
                            return str(items)
                else:
                    try:
                        return json.dumps(result_dict)
                    except TypeError:
                        return str(result_dict)
            except Exception as e:
                error_msg = str(e) if str(e) else repr(e)
                error_type = type(e).__name__
                if not error_msg:
                    error_msg = f"{error_type} (no error message provided)"
                return f"Error calling tool '{current_tool_name}': {error_type}: {error_msg}"

        return FunctionTool(
            name=tool.name,
            description=tool.description,
            params_json_schema=schema,
            on_invoke_tool=invoke_tool,
            strict_json_schema=convert_schemas_to_strict,
        )
