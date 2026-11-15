import contextvars, hashlib, hmac, secrets, sqlite3
from datetime import datetime, timedelta, timezone

current_user=contextvars.ContextVar("current_user",default=None)
COOKIE="gf_session"
def hash_password(password: str) -> str:
    salt=secrets.token_bytes(16); value=hashlib.scrypt(password.encode(),salt=salt,n=2**14,r=8,p=1,dklen=32)
    return f"scrypt${salt.hex()}${value.hex()}"
def verify_password(password: str, stored: str) -> bool:
    try:
        alg,salt,digest=stored.split("$"); got=hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=2**14,r=8,p=1,dklen=32)
        return alg=="scrypt" and hmac.compare_digest(got.hex(),digest)
    except Exception: return False
def session(c: sqlite3.Connection,user_id:int):
    raw=secrets.token_urlsafe(32); c.execute("INSERT INTO sessions(token_hash,user_id,expires) VALUES(?,?,?)",(hashlib.sha256(raw.encode()).hexdigest(),user_id,(datetime.now(timezone.utc)+timedelta(days=7)).isoformat())); c.commit(); return raw
def lookup(c,raw):
    if not raw:return None
    h=hashlib.sha256(raw.encode()).hexdigest()
    row=c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?",(h,datetime.now(timezone.utc).isoformat())).fetchone()
    return dict(row) if row and row["status"]=="active" else None
