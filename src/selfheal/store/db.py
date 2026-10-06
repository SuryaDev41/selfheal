"""
Database operations for caching and telemetry
Creates data/store/ directory on first run
"""

import sqlite3
from pathlib import Path
from typing import Dict, Optional

# Database path
DB_PATH = "data/store/telemetry.db"

def ensure_db_dir():
    """Ensure data/store/ directory exists"""
    db_dir = Path("data/store")
    db_dir.mkdir(parents=True, exist_ok=True)  # ← Creates folder!

def get_connection(db_path: str = DB_PATH):
    """Get database connection"""
    ensure_db_dir()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(db_path: str = DB_PATH):
    """Initialize database schema"""
    ensure_db_dir()
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    # Create LLM calls table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            test_case_id TEXT,
            step_no INTEGER,
            reason TEXT,  -- 'identify' or 'heal'
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            cost_usd REAL,
            latency_ms INTEGER,
            status TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create elements cache table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS elements (
            app TEXT,
            env TEXT,
            page TEXT,
            target TEXT,
            primary_locator TEXT,
            fallback_locators TEXT,
            fingerprint TEXT,
            version INTEGER DEFAULT 1,
            status TEXT DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            healed_at DATETIME,
            created_by TEXT,
            PRIMARY KEY (app, env, page, target)
        )
    """)
    
    conn.commit()
    conn.close()

def log_llm_call(run_id: str, test_case_id: str, step_no: int, reason: str,
                 model: str, input_tokens: int, output_tokens: int,
                 cost: float, latency_ms: int, status: str = 'success',
                 db_path: str = DB_PATH):
    """Log LLM call to database"""
    
    ensure_db_dir()
    conn = get_connection(db_path)
    cursor = conn.cursor()
    total_tokens = input_tokens + output_tokens
    
    cursor.execute(
        """INSERT INTO llm_calls 
           (run_id, test_case_id, step_no, reason, model,
            input_tokens, output_tokens, total_tokens, cost_usd,
            latency_ms, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, test_case_id, step_no, reason, model,
         input_tokens, output_tokens, total_tokens, cost,
         latency_ms, status)
    )
    
    conn.commit()
    conn.close()

def get_run_llm_stats(run_id: str, db_path: str = DB_PATH) -> Dict:
    """Get LLM stats for a specific run"""
    
    ensure_db_dir()
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute(
        """SELECT 
           SUM(total_tokens) as total_tokens,
           SUM(cost_usd) as total_cost,
           COUNT(*) as call_count,
           AVG(latency_ms) as avg_latency
           FROM llm_calls
           WHERE run_id = ?""",
        (run_id,)
    )
    
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            'total_tokens': row['total_tokens'] or 0,
            'total_cost': row['total_cost'] or 0.0,
            'call_count': row['call_count'] or 0,
            'avg_latency': row['avg_latency'] or 0
        }
    
    return {
        'total_tokens': 0,
        'total_cost': 0.0,
        'call_count': 0,
        'avg_latency': 0
    }

def get_total_usage(db_path: str = DB_PATH) -> Dict:
    """Get total usage across all runs"""
    
    ensure_db_dir()
    conn = get_connection(db_path)
    cursor = conn.cursor()
    
    cursor.execute(
        """SELECT 
           SUM(total_tokens) as total_tokens,
           SUM(cost_usd) as total_cost,
           COUNT(*) as call_count
           FROM llm_calls"""
    )
    
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return {
            'total_tokens': row['total_tokens'] or 0,
            'total_cost': row['total_cost'] or 0.0,
            'call_count': row['call_count'] or 0
        }
    
    return {
        'total_tokens': 0,
        'total_cost': 0.0,
        'call_count': 0
    }

# Initialize database on import
ensure_db_dir()
