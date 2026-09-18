"""
db.py — MySQL + SQL Server database helper
Provides execute_update() / execute_query() for MySQL (primary)
and mssql_execute_update() / mssql_execute_query() for SQL Server (secondary).
Token operations use both databases for redundancy.
Connection parameters are read from environment variables (see .env).
"""
 
import os
import logging
import mysql.connector
from mysql.connector import Error
import pyodbc
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
 
# Always load .env relative to this file's location, regardless of cwd
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)
 
logger = logging.getLogger("db")


def _int_env(name: str, default: int) -> int:
    """Read an int env var without raising when it is unset or blank."""
    val = os.getenv(name)
    try:
        return int(val) if val else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# MySQL connection config (from .env)
# ---------------------------------------------------------------------------
_DB_CONFIG = {
    "host":     os.getenv("MYSQL_HOST",     ""),
    "port":     _int_env("MYSQL_PORT", 3306),
    "database": os.getenv("MYSQL_DATABASE", ""),
    "user":     os.getenv("MYSQL_USER",     ""),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "connect_timeout": 3,
    "autocommit": False,
}

# ---------------------------------------------------------------------------
# SQL Server connection config (from .env)
# ---------------------------------------------------------------------------
_MSSQL_CONFIG = {
    "server":   os.getenv("DB_SERVER",   ""),
    "port":     _int_env("DB_PORT", 1433),
    "database": os.getenv("DB_DATABASE", ""),
    "user":     os.getenv("DB_USER",     ""),
    "password": os.getenv("DB_PASSWORD", ""),
}
 
 
def _get_connection() -> mysql.connector.MySQLConnection:
    """Open and return a new MySQL connection."""
    return mysql.connector.connect(**_DB_CONFIG)


def _get_mssql_connection() -> pyodbc.Connection:
    """Open and return a new SQL Server connection via pyodbc."""
    # Use Driver 18 in Docker (msodbcsql18), Driver 17 on Windows
    driver = "ODBC Driver 18 for SQL Server" if not os.path.exists("C:\\") else "ODBC Driver 17 for SQL Server"
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={_MSSQL_CONFIG['server']},{_MSSQL_CONFIG['port']};"
        f"DATABASE={_MSSQL_CONFIG['database']};"
        f"UID={_MSSQL_CONFIG['user']};"
        f"PWD={_MSSQL_CONFIG['password']};"
        f"Connection Timeout=3;"
        f"TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str)
 
 
def execute_update(query: str, params: tuple = ()) -> bool:
    """
    Execute a single DML statement (UPDATE / INSERT / DELETE).
 
    Parameters
    ----------
    query : str
        The SQL statement with optional %s placeholders.
    params : tuple
        Values to bind to the placeholders (prevents SQL injection).
 
    Returns
    -------
    bool
        True on success, False on any database error.
    """
    conn = None
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        logger.debug("execute_update succeeded: rows_affected=%d", cursor.rowcount)
        return True
    except Error as exc:
        logger.error("DB execute_update failed: %s | query=%s", exc, query[:200])
        if conn:
            try:
                conn.rollback()
            except Error:
                pass
        return False
    finally:
        if conn and conn.is_connected():
            conn.close()
 
 
def execute_query(query: str, params: tuple = ()) -> list[dict] | None:
    """
    Execute a SELECT statement and return rows as a list of dicts.
 
    Parameters
    ----------
    query : str
        The SQL statement with optional %s placeholders.
    params : tuple
        Values to bind to the placeholders (prevents SQL injection).
 
    Returns None on error.
    """
    conn = None
    try:
        conn = _get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return rows
    except Error as exc:
        logger.error("DB execute_query failed: %s | query=%s", exc, query[:200])
        return None
    finally:
        if conn and conn.is_connected():
            conn.close()


# ---------------------------------------------------------------------------
# SQL Server helpers
# ---------------------------------------------------------------------------

def mssql_execute_update(query: str, params: tuple = ()) -> bool:
    """Execute a DML statement on SQL Server. Uses ? placeholders."""
    conn = None
    try:
        conn = _get_mssql_connection()
        cursor = conn.cursor()
        cursor.execute(query, params)
        conn.commit()
        logger.debug("mssql_execute_update succeeded: rows_affected=%d", cursor.rowcount)
        return True
    except Exception as exc:
        logger.error("[SQL_SERVER] execute_update failed: %s | query=%s", exc, query[:200])
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def mssql_execute_query(query: str, params: tuple = ()) -> list[dict] | None:
    """Execute a SELECT on SQL Server and return rows as a list of dicts."""
    conn = None
    try:
        conn = _get_mssql_connection()
        cursor = conn.cursor()
        cursor.execute(query, params)
        columns = [desc[0] for desc in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        return rows
    except Exception as exc:
        logger.error("[SQL_SERVER] execute_query failed: %s | query=%s", exc, query[:200])
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Dual-DB token helpers (MySQL primary, SQL Server secondary)
# ---------------------------------------------------------------------------

def save_token_to_both_dbs(token_column: str, token_value: str, business_entity: str = "1000") -> bool:
    """
    Save a token to BOTH MySQL and SQL Server.
    If one DB fails, log the error and still try the other.
    Returns True if at least one DB succeeded.
    """
    mysql_ok = False
    mssql_ok = False

    # ---- MySQL ----
    try:
        rows = execute_query(
            "SELECT id FROM TokenTable WHERE business_entity = %s LIMIT 1",
            (business_entity,),
        )
        if rows is None:
            # Connection failed — skip INSERT attempt to avoid a second timeout
            print(f"[MYSQL] ERROR: Failed to save {token_column} for business_entity={business_entity}")
        elif rows:
            q = f"UPDATE TokenTable SET {token_column} = %s WHERE business_entity = %s"
            mysql_ok = execute_update(q, (token_value, business_entity))
            if mysql_ok:
                print(f"[MYSQL] {token_column} saved for business_entity={business_entity}")
            else:
                print(f"[MYSQL] ERROR: Failed to save {token_column} for business_entity={business_entity}")
        else:
            q = f"INSERT INTO TokenTable (business_entity, {token_column}) VALUES (%s, %s)"
            mysql_ok = execute_update(q, (business_entity, token_value))
            if mysql_ok:
                print(f"[MYSQL] {token_column} saved for business_entity={business_entity}")
            else:
                print(f"[MYSQL] ERROR: Failed to save {token_column} for business_entity={business_entity}")
    except Exception as exc:
        print(f"[MYSQL] ERROR saving {token_column}: {exc}")
        logger.error("[MYSQL] Token save failed: %s", exc)

    # ---- SQL Server ----
    try:
        rows = mssql_execute_query(
            "SELECT TOP 1 id FROM TokenTable WHERE business_entity = ?",
            (business_entity,),
        )
        if rows:
            q = f"UPDATE TokenTable SET {token_column} = ? WHERE business_entity = ?"
            mssql_ok = mssql_execute_update(q, (token_value, business_entity))
        else:
            q = f"INSERT INTO TokenTable (business_entity, {token_column}) VALUES (?, ?)"
            mssql_ok = mssql_execute_update(q, (business_entity, token_value))

        if mssql_ok:
            print(f"[SQL_SERVER] {token_column} saved for business_entity={business_entity}")
        else:
            print(f"[SQL_SERVER] ERROR: Failed to save {token_column} for business_entity={business_entity}")
    except Exception as exc:
        print(f"[SQL_SERVER] ERROR saving {token_column}: {exc}")
        logger.error("[SQL_SERVER] Token save failed: %s", exc)

    return mysql_ok or mssql_ok


def fetch_tokens_from_both_dbs(business_entity: str = "1000") -> dict:
    """
    Fetch ex_auth_token and ws_auth_token.
    Priority: MySQL first. If MySQL fails or returns empty, fallback to SQL Server.
    Returns dict with keys 'ex_auth_token' and 'ws_auth_token' (may be empty strings).
    """
    result = {"ex_auth_token": "", "ws_auth_token": ""}

    # ---- Try MySQL first (priority) ----
    mysql_ok = False
    try:
        rows = execute_query(
            "SELECT ex_auth_token, ws_auth_token FROM TokenTable WHERE business_entity = %s LIMIT 1",
            (business_entity,),
        )
        if rows and rows[0]:
            ex = rows[0].get("ex_auth_token") or ""
            ws = rows[0].get("ws_auth_token") or ""
            if ex or ws:
                result["ex_auth_token"] = ex
                result["ws_auth_token"] = ws
                mysql_ok = True
                print(f"[MYSQL] Tokens fetched (ex_len={len(ex)}, ws_len={len(ws)})")
            else:
                print("[MYSQL] Tokens present but empty")
        else:
            print("[MYSQL] No token rows found")
    except Exception as exc:
        print(f"[MYSQL] ERROR fetching tokens: {exc}")
        logger.error("[MYSQL] Token fetch failed: %s", exc)

    if mysql_ok:
        return result

    # ---- Fallback to SQL Server ----
    print("[FALLBACK] MySQL tokens unavailable, trying SQL Server...")
    try:
        rows = mssql_execute_query(
            "SELECT TOP 1 ex_auth_token, ws_auth_token FROM TokenTable WHERE business_entity = ?",
            (business_entity,),
        )
        if rows and rows[0]:
            ex = rows[0].get("ex_auth_token") or ""
            ws = rows[0].get("ws_auth_token") or ""
            result["ex_auth_token"] = ex
            result["ws_auth_token"] = ws
            print(f"[SQL_SERVER] Tokens fetched (ex_len={len(ex)}, ws_len={len(ws)})")
        else:
            print("[SQL_SERVER] No token rows found")
    except Exception as exc:
        print(f"[SQL_SERVER] ERROR fetching tokens: {exc}")
        logger.error("[SQL_SERVER] Token fetch failed: %s", exc)

    return result
# ---------------------------------------------------------------------------
# The practice management system's GET /v2/appointments endpoint returns "ActiveRecord::RecordNotFound"
# for far-future dates even though POST /v2/appointments (create) succeeds.
# We persist booked appointments locally so rescheduling / cancellation flows
# can still find them when the practice management system search endpoint fails.
# ---------------------------------------------------------------------------

def ensure_booked_appointments_table() -> bool:
    """Create the BookedAppointments table if it does not already exist."""
    ddl = """
    CREATE TABLE IF NOT EXISTS BookedAppointments (
        appointment_id   BIGINT       PRIMARY KEY,
        patient_id       BIGINT       NOT NULL,
        business_entity_id INT        NOT NULL DEFAULT 1000,
        provider_id      INT          NULL,
        resource_id      INT          NULL,
        location_id      INT          NULL,
        nature_of_visit_id INT        NULL,
        appointment_date DATE         NOT NULL,
        start_time_iso   VARCHAR(60)  NULL,
        end_time_iso     VARCHAR(60)  NULL,
        reason_for_visit VARCHAR(500) NULL,
        status           VARCHAR(20)  NOT NULL DEFAULT 'booked',
        created_at       DATETIME     DEFAULT CURRENT_TIMESTAMP
    )
    """
    return execute_update(ddl)


def save_booked_appointment(
    appointment_id: int,
    patient_id: int,
    appointment_date: str,
    start_time_iso: str = None,
    end_time_iso: str = None,
    provider_id: int = None,
    resource_id: int = None,
    location_id: int = None,
    nature_of_visit_id: int = None,
    reason_for_visit: str = None,
    business_entity_id: int = 1000,
) -> bool:
    """Insert (or update) a booked appointment in the local store."""
    ensure_booked_appointments_table()
    sql = """
    INSERT INTO BookedAppointments
        (appointment_id, patient_id, business_entity_id, provider_id,
         resource_id, location_id, nature_of_visit_id, appointment_date,
         start_time_iso, end_time_iso, reason_for_visit, status)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'booked')
    ON DUPLICATE KEY UPDATE
        patient_id         = VALUES(patient_id),
        business_entity_id = VALUES(business_entity_id),
        provider_id        = VALUES(provider_id),
        resource_id        = VALUES(resource_id),
        location_id        = VALUES(location_id),
        nature_of_visit_id = VALUES(nature_of_visit_id),
        appointment_date   = VALUES(appointment_date),
        start_time_iso     = VALUES(start_time_iso),
        end_time_iso       = VALUES(end_time_iso),
        reason_for_visit   = VALUES(reason_for_visit),
        status             = 'booked'
    """
    params = (
        appointment_id, patient_id, business_entity_id, provider_id,
        resource_id, location_id, nature_of_visit_id, appointment_date,
        start_time_iso, end_time_iso, reason_for_visit,
    )
    ok = execute_update(sql, params)
    if ok:
        print(f"[LOCAL_STORE] Saved appointment {appointment_id} for patient {patient_id} on {appointment_date}")
    return ok


def get_local_appointments(patient_id: int, start_date: str, end_date: str) -> Optional[List[Dict[str, Any]]]:
    """Retrieve locally stored appointments for a patient within a date range."""
    ensure_booked_appointments_table()
    sql = """
    SELECT appointment_id, patient_id, business_entity_id, provider_id,
           resource_id, location_id, nature_of_visit_id, appointment_date,
           start_time_iso, end_time_iso, reason_for_visit, status
    FROM BookedAppointments
    WHERE patient_id = %s
      AND appointment_date >= %s
      AND appointment_date <= %s
      AND status = 'booked'
    """
    return execute_query(sql, (patient_id, start_date, end_date))


def mark_appointment_cancelled(appointment_id: int) -> bool:
    """Mark a locally stored appointment as cancelled."""
    ensure_booked_appointments_table()
    sql = "UPDATE BookedAppointments SET status = 'cancelled' WHERE appointment_id = %s"
    return execute_update(sql, (appointment_id,))
