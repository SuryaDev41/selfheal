"""Cache resolver for element locators"""

import sqlite3
from pathlib import Path

class CacheResolver:
    """SQLite-based cache for element locators"""
    
    def __init__(self, db_path: str = "data/store/elements.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Initialize database if needed"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Create table if it doesn't exist
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS elements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app TEXT NOT NULL,
                    env TEXT NOT NULL,
                    page TEXT NOT NULL,
                    target TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(app, env, page, target)
                )
            """)
            
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ Database init error: {e}")
    
    def get_cached_element(self, app: str, env: str, page: str, target: str) -> str:
        """
        Get cached element locator
        
        Args:
            app: Application name
            env: Environment
            page: Page name
            target: Element target
            
        Returns:
            Locator string if found, None otherwise
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Query with correct column name
            cursor.execute("""
                SELECT locator FROM elements 
                WHERE app=? AND env=? AND page=? AND target=?
            """, (app, env, page, target))
            
            result = cursor.fetchone()
            conn.close()
            
            if result:
                return result[0]
            return None
            
        except Exception as e:
            print(f"⚠️ Cache lookup error: {e}")
            return None
    
    def cache_element(self, app: str, env: str, page: str, target: str, locator: str):
        """
        Cache an element locator
        
        Args:
            app: Application name
            env: Environment
            page: Page name
            target: Element target
            locator: XPath/CSS locator
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # Insert or replace
            cursor.execute("""
                INSERT OR REPLACE INTO elements (app, env, page, target, locator)
                VALUES (?, ?, ?, ?, ?)
            """, (app, env, page, target, locator))
            
            conn.commit()
            conn.close()
            
            print(f"    💾 Cached: {app} / {env} / {page} / {target}")
            
        except Exception as e:
            print(f"⚠️ Cache save error: {e}")
    
    def clear_cache(self, app: str = None):
        """Clear cache for an app"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            if app:
                cursor.execute("DELETE FROM elements WHERE app=?", (app,))
            else:
                cursor.execute("DELETE FROM elements")
            
            conn.commit()
            conn.close()
            
            print(f"✅ Cache cleared for {app or 'all apps'}")
            
        except Exception as e:
            print(f"⚠️ Cache clear error: {e}")
    
    def get_stats(self) -> dict:
        """Get cache statistics"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            cursor.execute("SELECT COUNT(*) FROM elements")
            total = cursor.fetchone()[0]
            
            conn.close()
            
            return {
                'total_cached_elements': total
            }
        except Exception as e:
            print(f"⚠️ Stats error: {e}")
            return {'total_cached_elements': 0}