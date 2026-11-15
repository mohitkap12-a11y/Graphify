import sqlite3
from .config import db_path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE COLLATE NOCASE,password_hash TEXT NOT NULL,display_name TEXT,role TEXT NOT NULL DEFAULT 'member',status TEXT NOT NULL DEFAULT 'pending',note TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP,decided_by TEXT,decided_at TEXT,last_login TEXT);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS repositories(id TEXT PRIMARY KEY,name TEXT NOT NULL,root TEXT NOT NULL,remote TEXT,default_branch TEXT NOT NULL,allowed_branches TEXT NOT NULL DEFAULT '[]',options TEXT NOT NULL DEFAULT '{}',created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS repo_access(user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,repo_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,PRIMARY KEY(user_id,repo_id));
CREATE TABLE IF NOT EXISTS graphs(id INTEGER PRIMARY KEY,repo_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,branch TEXT NOT NULL,version TEXT NOT NULL,commit_sha TEXT,path TEXT NOT NULL,node_count INTEGER DEFAULT 0,edge_count INTEGER DEFAULT 0,created TEXT DEFAULT CURRENT_TIMESTAMP,active INTEGER DEFAULT 1);
CREATE INDEX IF NOT EXISTS ix_graph_current ON graphs(repo_id,branch,active);
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,repo_id TEXT NOT NULL,branch TEXT NOT NULL,mode TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',requested_sha TEXT,started TEXT,finished TEXT,log TEXT DEFAULT '',error TEXT,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS api_tokens(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,prefix TEXT NOT NULL,scopes TEXT NOT NULL,repo_ids TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,last_used TEXT,revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS mcp_clients(token_id INTEGER PRIMARY KEY REFERENCES api_tokens(id) ON DELETE CASCADE,name TEXT,version TEXT,seen TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS mcp_calls(id INTEGER PRIMARY KEY,request_id TEXT NOT NULL,ts TEXT DEFAULT CURRENT_TIMESTAMP,actor TEXT,token_id INTEGER,client_name TEXT,client_version TEXT,tool TEXT NOT NULL,repo_id TEXT,branch TEXT,outcome TEXT NOT NULL,duration_ms REAL DEFAULT 0,result_bytes INTEGER DEFAULT 0,matched_nodes INTEGER DEFAULT 0,matched_edges INTEGER DEFAULT 0,query_hash TEXT,query_preview TEXT,followup_of INTEGER,error_class TEXT,graph_version TEXT,commit_sha TEXT);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,ts TEXT DEFAULT CURRENT_TIMESTAMP,actor TEXT,action TEXT NOT NULL,target TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""

def connect():
    c=sqlite3.connect(db_path(), timeout=30, check_same_thread=False)
    c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init():
    c=connect(); c.executescript(SCHEMA)
    columns={r[1] for r in c.execute("PRAGMA table_info(mcp_calls)")}
    for name,kind in (("client_name","TEXT"),("client_version","TEXT"),("matched_nodes","INTEGER DEFAULT 0"),("matched_edges","INTEGER DEFAULT 0"),("followup_of","INTEGER")):
        if name not in columns:c.execute(f"ALTER TABLE mcp_calls ADD COLUMN {name} {kind}")
    c.commit(); c.close(); db_path().chmod(0o600)

def as_dict(row): return dict(row) if row else None
