"""Local-first REST and MCP service for the Graphify platform."""
import hashlib,hmac,json,os,re,secrets,sqlite3,subprocess,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI,Request,HTTPException,Query
from fastapi.responses import JSONResponse,Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from . import __version__,auth,mcp
from .config import data_dir
from .db import connect,init
from .worker import enqueue

init(); app=FastAPI(title="Graphify Local Platform",version=__version__)
_login_hits={};_login_lock=__import__("threading").Lock()
SAFE={"GET","HEAD","OPTIONS"}; PUBLIC={("GET","/api/auth/me"),("POST","/api/auth/register"),("POST","/api/auth/login"),("POST","/api/auth/logout"),("GET","/api/bootstrap/status"),("POST","/api/bootstrap/complete"),("GET","/health"),("GET","/ready")}
ADMIN_PREFIXES=("/api/users","/api/activity","/api/jobs","/api/settings","/api/audit")
class Gate(BaseHTTPMiddleware):
 async def dispatch(self,request,call_next):
  path=request.url.path;method=request.method
  if path.startswith("/api/"):
   if method not in SAFE and request.headers.get("origin"):
    origin=urlsplit(request.headers["origin"]).netloc
    if origin not in {request.headers.get("host"),request.headers.get("x-forwarded-host")}:return JSONResponse({"detail":"cross-origin write refused"},403)
   c=connect();user=auth.lookup(c,request.cookies.get(auth.COOKIE));c.close()
   if (method,path) not in PUBLIC and not user:return JSONResponse({"detail":"sign in required"},401)
   if user and user["role"]!="admin" and path.startswith(ADMIN_PREFIXES):return JSONResponse({"detail":"admin access required"},403)
   if user and user["role"]!="admin" and method not in SAFE and path.startswith("/api/repositories"):return JSONResponse({"detail":"admin access required"},403)
   tok=auth.current_user.set(user)
   try:return await call_next(request)
   finally:auth.current_user.reset(tok)
  return await call_next(request)
app.add_middleware(Gate)

def me():
 u=auth.current_user.get()
 if not u:raise HTTPException(401,"sign in required")
 return u
def audit(c,actor,action,target=None,detail=None):c.execute("INSERT INTO audit(actor,action,target,detail) VALUES(?,?,?,?)",(actor,action,target,detail));c.commit()
def _poll_remotes():
 interval=max(30,int(os.environ.get("GRAPHIFY_POLL_SECONDS","120")))
 while True:
  try:
   c=connect();repos=c.execute("SELECT * FROM repositories WHERE remote IS NOT NULL AND remote!=''").fetchall()
   for repo in repos:
    for branch in [repo["default_branch"],*json.loads(repo["allowed_branches"])]:
     try:
      p=subprocess.run(["git","ls-remote",repo["remote"],f"refs/heads/{branch}"],capture_output=True,text=True,timeout=12)
      remote_sha=p.stdout.split()[0] if p.returncode==0 and p.stdout.strip() else None
      graph=c.execute("SELECT commit_sha FROM graphs WHERE repo_id=? AND branch=? AND active=1",(repo["id"],branch)).fetchone()
      if remote_sha and (not graph or graph[0]!=remote_sha):
       remote_ref="refs/remotes/graphify/"+branch
       fetch=subprocess.run(["git","-C",repo["root"],"fetch","--no-tags",repo["remote"],f"+refs/heads/{branch}:{remote_ref}"],capture_output=True,text=True,timeout=60)
       verify=subprocess.run(["git","-C",repo["root"],"cat-file","-e",remote_sha+"^{commit}"],capture_output=True,timeout=10)
       if fetch.returncode==0 and verify.returncode==0:enqueue(repo["id"],branch,"incremental",remote_sha)
     except (OSError,subprocess.TimeoutExpired):pass
   c.close()
  except Exception:pass
  time.sleep(interval)
@app.on_event("startup")
def start_poller():
 import threading
 c=connect();c.execute("UPDATE jobs SET state='failed',finished=CURRENT_TIMESTAMP,error='Service restarted while this build was running; last good graph was preserved' WHERE state='running'");queued=[r[0] for r in c.execute("SELECT id FROM jobs WHERE state='queued'")];roots=[r[0] for r in c.execute("SELECT root FROM repositories")];c.commit();c.close()
 import shutil
 for old in (data_dir()/"staging").iterdir():
  if old.is_dir():shutil.rmtree(old,ignore_errors=True)
 for root in roots:subprocess.run(["git","-C",root,"worktree","prune"],capture_output=True,text=True,timeout=15)
 for job_id in queued:threading.Thread(target=__import__("graphify_platform.worker",fromlist=["run_job"]).run_job,args=(job_id,),daemon=True).start()
 if os.environ.get("GRAPHIFY_POLLING","1").lower() not in ("0","false","off"):threading.Thread(target=_poll_remotes,daemon=True,name="graphify-remote-poller").start()
def public_user(u):return {k:u.get(k) for k in ("id","username","display_name","role","status","note","created","last_login")}
def verify_repo(root,branch):
 p=Path(root).expanduser().resolve()
 if not p.is_dir():raise HTTPException(422,"repository directory does not exist")
 git=subprocess.run(["git","-C",str(p),"rev-parse","--show-toplevel"],capture_output=True,text=True,timeout=8)
 if git.returncode or Path(git.stdout.strip()).resolve()!=p:raise HTTPException(422,"path must be a Git checkout root")
 fmt=subprocess.run(["git","check-ref-format","--branch",branch],capture_output=True,text=True,timeout=8)
 br=subprocess.run(["git","-C",str(p),"show-ref","--verify","--quiet",f"refs/heads/{branch}"],capture_output=True,text=True,timeout=8) if fmt.returncode==0 else None
 if not br or br.returncode:raise HTTPException(422,"configured branch is not available in this checkout")
 return p

@app.get("/health")
def health():return {"ok":True,"version":__version__}
@app.get("/ready")
def ready():
 try:c=connect();c.execute("SELECT 1");c.close();return {"ready":True}
 except Exception:return JSONResponse({"ready":False},503)
@app.get("/api/bootstrap/status")
def bootstrap_status():
 c=connect();n=c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0];c.close();return {"needed":n==0}
@app.post("/api/bootstrap/complete")
async def bootstrap_complete(request:Request):
 peer=request.client.host if request.client else ""
 forwarded=request.headers.get("x-forwarded-for","")
 if peer not in ("127.0.0.1","::1","localhost") or any(x.strip() not in ("127.0.0.1","::1","localhost") for x in forwarded.split(",") if x.strip()):raise HTTPException(403,"bootstrap is only available from loopback")
 data=await request.json();secret=os.environ.get("GRAPHIFY_BOOTSTRAP_SECRET","")
 try:issued=int(secret.split(".",1)[0])
 except (ValueError,IndexError):raise HTTPException(403,"invalid or expired bootstrap secret")
 if time.time()-issued>3600 or issued>time.time()+60 or not hmac.compare_digest(str(data.get("secret","")),secret):raise HTTPException(403,"invalid or expired bootstrap secret")
 username=str(data.get("username",""));password=str(data.get("password",""))
 if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,31}",username) or not 12<=len(password)<=256:raise HTTPException(422,"username must be 3-32 valid characters and password at least 12 characters")
 c=connect()
 if c.execute("SELECT 1 FROM users WHERE role='admin' AND status='active'").fetchone():c.close();raise HTTPException(409,"admin bootstrap already completed")
 try:c.execute("INSERT INTO users(username,display_name,password_hash,role,status,decided_by,decided_at) VALUES(?,?,?,'admin','active','bootstrap',CURRENT_TIMESTAMP)",(username,data.get("display_name"),auth.hash_password(password)));c.commit()
 except sqlite3.IntegrityError:c.close();raise HTTPException(409,"username already exists")
 c.execute("DELETE FROM settings WHERE key='bootstrap_secret_hash'");audit(c,username,"bootstrap_admin");c.close();os.environ.pop("GRAPHIFY_BOOTSTRAP_SECRET",None);return {"ok":True}
@app.post("/api/auth/register")
async def register(request:Request):
 d=await request.json();u=str(d.get("username","")).strip();pw=str(d.get("password",""))
 if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,31}",u) or len(pw)<8 or len(pw)>256:raise HTTPException(422,"username must be 3-32 valid characters and password at least 8 characters")
 c=connect()
 try:c.execute("INSERT INTO users(username,display_name,password_hash,note) VALUES(?,?,?,?)",(u,str(d.get("display_name", ""))[:80],auth.hash_password(pw),str(d.get("note", ""))[:500]));c.commit()
 except sqlite3.IntegrityError:c.close();raise HTTPException(409,"username is already taken")
 audit(c,u,"access_requested");c.close();return {"ok":True,"status":"pending"}
@app.post("/api/auth/login")
async def login(request:Request):
 ip=request.client.host if request.client else "unknown";now=time.monotonic()
 with _login_lock:
  attempts=[x for x in _login_hits.get(ip,[]) if now-x<60]
  if len(attempts)>=10:raise HTTPException(429,"too many sign-in attempts; wait a minute")
  attempts.append(now);_login_hits[ip]=attempts
 d=await request.json();u=str(d.get("username",""));pw=str(d.get("password",""));c=connect();row=c.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",(u,)).fetchone()
 if not row or not auth.verify_password(pw,row["password_hash"]):c.close();raise HTTPException(401,"invalid username or password")
 if row["status"]!="active":c.close();raise HTTPException(403,"account is pending approval or disabled")
 raw=auth.session(c,row["id"]);c.execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=?",(row["id"],));c.commit();c.close()
 res=JSONResponse({"ok":True});res.set_cookie(auth.COOKIE,raw,httponly=True,samesite="strict",secure=request.url.scheme=="https",max_age=604800,path="/");return res
@app.post("/api/auth/logout")
def logout(request:Request):
 raw=request.cookies.get(auth.COOKIE)
 if raw:
  c=connect();c.execute("DELETE FROM sessions WHERE token_hash=?",(hashlib.sha256(raw.encode()).hexdigest(),));c.commit();c.close()
 r=JSONResponse({"ok":True});r.delete_cookie(auth.COOKIE,path="/");return r
@app.get("/api/auth/me")
def whoami():
 u=auth.current_user.get()
 if not u:return {"authenticated":False}
 c=connect();pending=c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0] if u["role"]=="admin" else 0;c.close()
 return {"authenticated":True,"user":public_user(u),"pending_users":pending}
@app.get("/api/overview")
def overview():
 u=me();c=connect()
 if u["role"]=="admin":
  r=c.execute("SELECT COUNT(*) FROM repositories").fetchone()[0];g=c.execute("SELECT COUNT(*) FROM graphs WHERE active=1").fetchone()[0];j=c.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0];calls=c.execute("SELECT COUNT(*) FROM mcp_calls WHERE ts>=datetime('now','-7 days')").fetchone()[0];matches=c.execute("SELECT COUNT(*) FROM mcp_calls WHERE ts>=datetime('now','-7 days') AND outcome IN ('success_with_matches','truncated')").fetchone()[0];fail=c.execute("SELECT COUNT(*) FROM jobs WHERE state='failed'").fetchone()[0];recent=[dict(x) for x in c.execute("SELECT j.*,r.name repo_name FROM jobs j LEFT JOIN repositories r ON r.id=j.repo_id ORDER BY j.id DESC LIMIT 8")]
 else:
  ids=[x[0] for x in c.execute("SELECT repo_id FROM repo_access WHERE user_id=?",(u["id"],))];r=len(ids);marks=",".join("?" for _ in ids) or "NULL"
  g=c.execute(f"SELECT COUNT(*) FROM graphs WHERE active=1 AND repo_id IN ({marks})",ids).fetchone()[0];j=c.execute(f"SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running') AND repo_id IN ({marks})",ids).fetchone()[0];calls=c.execute("SELECT COUNT(*) FROM mcp_calls WHERE ts>=datetime('now','-7 days') AND actor=?",(u["username"],)).fetchone()[0];matches=c.execute("SELECT COUNT(*) FROM mcp_calls WHERE ts>=datetime('now','-7 days') AND actor=? AND outcome IN ('success_with_matches','truncated')",(u["username"],)).fetchone()[0];fail=c.execute(f"SELECT COUNT(*) FROM jobs WHERE state='failed' AND repo_id IN ({marks})",ids).fetchone()[0];recent=[dict(x) for x in c.execute(f"SELECT j.id,j.repo_id,j.branch,j.mode,j.state,j.requested_sha,j.started,j.finished,j.created,r.name repo_name FROM jobs j LEFT JOIN repositories r ON r.id=j.repo_id WHERE j.repo_id IN ({marks}) ORDER BY j.id DESC LIMIT 8",ids)]
 c.close();return {"repositories":r,"graphs":g,"active_jobs":j,"mcp_calls_7d":calls,"query_match_rate":round(matches/calls*100,1) if calls else 0,"failed_jobs":fail,"recent_jobs":recent}
@app.get("/api/repositories")
def repositories():
 u=me();c=connect();rows=c.execute("SELECT r.*,g.version,g.commit_sha,g.node_count,g.edge_count,g.created last_update,g.branch graph_branch FROM repositories r LEFT JOIN graphs g ON g.repo_id=r.id AND g.active=1 ORDER BY r.name").fetchall();out=[]
 for x in rows:
  d=dict(x)
  if u["role"]!="admin" and not c.execute("SELECT 1 FROM repo_access WHERE user_id=? AND repo_id=?",(u["id"],x["id"])).fetchone():continue
  if u["role"]!="admin":d.pop("root",None);d.pop("remote",None)
  out.append(d)
 c.close();return out
@app.post("/api/repositories")
async def add_repository(request:Request):
 d=await request.json();rid=str(d.get("id","")).strip();name=str(d.get("name","")).strip();branch=str(d.get("default_branch","main")).strip()
 if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,63}",rid) or not name or not re.fullmatch(r"[A-Za-z0-9_][\w./-]{0,119}",branch) or ".." in branch or "@{" in branch:raise HTTPException(422,"repository ID, name, or branch is invalid")
 root=verify_repo(d.get("root",""),branch);remote=str(d.get("remote", "")).strip() or None
 allowed_branches=d.get("allowed_branches",[])
 if not isinstance(allowed_branches,list) or any(not isinstance(b,str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,119}",b) or ".." in b or "@{" in b for b in allowed_branches):raise HTTPException(422,"one or more allowed branches are invalid")
 if remote and (remote.startswith("-") or "\n" in remote or "\x00" in remote or "@" in urlsplit(remote).netloc):raise HTTPException(422,"remote URL is invalid; do not embed credentials in the URL")
 c=connect()
 try:
  options=d.get("options",{})
  if not isinstance(options,dict) or len(json.dumps(options))>5000:raise HTTPException(422,"repository options are invalid")
  if set(options)-{"include_documents","local_backend","local_model","deep_mode"}:raise HTTPException(422,"unsupported repository option")
  if options.get("deep_mode") and not options.get("include_documents"):raise HTTPException(422,"deep mode requires document extraction and an explicitly configured local model")
  if options.get("include_documents") and (options.get("local_backend","ollama") not in ("ollama","openai") or not str(options.get("local_model","")).strip() or len(str(options.get("local_model","")))>160):raise HTTPException(422,"select a supported local model backend and model name")
  c.execute("INSERT INTO repositories(id,name,root,remote,default_branch,allowed_branches,options) VALUES(?,?,?,?,?,?,?)",(rid,name,str(root),remote,branch,json.dumps(allowed_branches),json.dumps(options)));c.commit()
 except sqlite3.IntegrityError:c.close();raise HTTPException(409,"repository ID already exists")
 audit(c,me()["username"],"repository_registered",rid);c.close();return {"ok":True,"id":rid}
@app.get("/api/repositories/{repo_id}")
def repo_detail(repo_id:str):
 c=connect();r=c.execute("SELECT * FROM repositories WHERE id=?",(repo_id,)).fetchone()
 if not r:c.close();raise HTTPException(404,"repository not found")
 u=me()
 if u["role"]!="admin" and not c.execute("SELECT 1 FROM repo_access WHERE user_id=? AND repo_id=?",(u["id"],repo_id)).fetchone():c.close();raise HTTPException(403,"repository access denied")
 d=dict(r)
 if u["role"]!="admin":d.pop("root",None);d.pop("remote",None)
 d["graphs"]=[dict(x) for x in c.execute("SELECT id,branch,version,commit_sha,node_count,edge_count,created,active FROM graphs WHERE repo_id=? ORDER BY id DESC",(repo_id,))]
 job_fields="id,branch,mode,state,requested_sha,started,finished,created" if u["role"]!="admin" else "id,branch,mode,state,requested_sha,started,finished,error,created"
 d["jobs"]=[dict(x) for x in c.execute(f"SELECT {job_fields} FROM jobs WHERE repo_id=? ORDER BY id DESC LIMIT 30",(repo_id,))];c.close();return d
@app.post("/api/repositories/{repo_id}/build")
async def build(repo_id:str,request:Request):
 d=await request.json();mode=d.get("mode","incremental");branch=str(d.get("branch", ""))
 if mode not in ("incremental","full"):raise HTTPException(422,"mode must be incremental or full")
 c=connect();r=c.execute("SELECT * FROM repositories WHERE id=?",(repo_id,)).fetchone();c.close()
 if not r:raise HTTPException(404,"repository not found")
 if me()["role"]!="admin":raise HTTPException(403,"admin access required")
 branch=branch or r["default_branch"]
 if branch not in [r["default_branch"],*json.loads(r["allowed_branches"])]:raise HTTPException(422,"branch is not allowed for this repository")
 track="refs/remotes/graphify/"+branch;target=subprocess.run(["git","-C",r["root"],"rev-parse",track],capture_output=True,text=True,timeout=10) if r["remote"] else None
 sha=target.stdout.strip() if target and target.returncode==0 else None
 jid=enqueue(repo_id,branch,mode,sha);c=connect();audit(c,me()["username"],"graph_build_requested",repo_id,json.dumps({"branch":branch,"mode":mode,"job_id":jid}));c.close();return {"job_id":jid,"state":"queued"}
@app.get("/api/jobs")
def jobs():
 c=connect();out=[dict(r) for r in c.execute("SELECT j.*,r.name repo_name FROM jobs j LEFT JOIN repositories r ON r.id=j.repo_id ORDER BY j.id DESC LIMIT 200")];c.close();return out
@app.get("/api/jobs/{job_id}")
def job(job_id:int):
 c=connect();r=c.execute("SELECT j.*,r.name repo_name FROM jobs j LEFT JOIN repositories r ON r.id=j.repo_id WHERE j.id=?",(job_id,)).fetchone();c.close()
 if not r:raise HTTPException(404,"job not found")
 return dict(r)
@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id:int):
 c=connect();r=c.execute("SELECT state,repo_id FROM jobs WHERE id=?",(job_id,)).fetchone()
 if not r:c.close();raise HTTPException(404,"job not found")
 if r["state"]!="queued":c.close();raise HTTPException(409,"only queued jobs can be cancelled")
 c.execute("UPDATE jobs SET state='cancelled',finished=CURRENT_TIMESTAMP WHERE id=? AND state='queued'",(job_id,));audit(c,me()["username"],"job_cancelled",str(job_id));c.commit();c.close();return {"ok":True}
@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id:int):
 c=connect();r=c.execute("SELECT repo_id,branch,mode,state,requested_sha FROM jobs WHERE id=?",(job_id,)).fetchone();c.close()
 if not r:raise HTTPException(404,"job not found")
 if r["state"] not in ("failed","cancelled"):raise HTTPException(409,"only failed or cancelled jobs can be retried")
 jid=enqueue(r["repo_id"],r["branch"],r["mode"],r["requested_sha"]);c=connect();audit(c,me()["username"],"job_retried",str(job_id),f"new job {jid}");c.close();return {"job_id":jid}
@app.get("/api/users")
def users():
 c=connect();out=[]
 for r in c.execute("SELECT * FROM users ORDER BY status='pending' DESC,username COLLATE NOCASE"):
  item={k:r[k] for k in ("id","username","display_name","role","status","note","created","decided_by","decided_at","last_login")};item["repositories"]=[x[0] for x in c.execute("SELECT repo_id FROM repo_access WHERE user_id=?",(r["id"],))];out.append(item)
 c.close();return out
@app.patch("/api/users/{user_id}")
async def user_update(user_id:int,request:Request):
 d=await request.json();role=d.get("role");status=d.get("status")
 if role not in (None,"admin","member","auditor") or status not in (None,"active","disabled","rejected"):raise HTTPException(422,"invalid role or status")
 c=connect();r=c.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
 if not r:c.close();raise HTTPException(404,"user not found")
 if r["id"]==me()["id"] and (role or r["role"]!="admin" or status and status!="active"):c.close();raise HTTPException(409,"cannot change your own access")
 if r["role"]=="admin" and r["status"]=="active" and ((role and role!="admin") or (status and status!="active")) and c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND status='active'").fetchone()[0]<=1:c.close();raise HTTPException(409,"cannot remove the last active admin")
 c.execute("UPDATE users SET role=COALESCE(?,role),status=COALESCE(?,status),decided_by=?,decided_at=CURRENT_TIMESTAMP WHERE id=?",(role,status,me()["username"],user_id));c.execute("DELETE FROM sessions WHERE user_id=?",(user_id,));audit(c,me()["username"],"user_access_changed",str(user_id),json.dumps({"role":role,"status":status}));c.commit();c.close();return {"ok":True}
@app.put("/api/users/{user_id}/repositories")
async def user_repositories(user_id:int,request:Request):
 d=await request.json();ids=d.get("repositories",[])
 if not isinstance(ids,list):raise HTTPException(422,"repositories must be an array")
 c=connect()
 if not c.execute("SELECT 1 FROM users WHERE id=?",(user_id,)).fetchone():c.close();raise HTTPException(404,"user not found")
 for rid in ids:
  if not c.execute("SELECT 1 FROM repositories WHERE id=?",(rid,)).fetchone():c.close();raise HTTPException(422,"unknown repository: "+str(rid))
 c.execute("DELETE FROM repo_access WHERE user_id=?",(user_id,));c.executemany("INSERT INTO repo_access(user_id,repo_id) VALUES(?,?)",[(user_id,r) for r in ids]);audit(c,me()["username"],"repository_access_changed",str(user_id),json.dumps(ids));c.commit();c.close();return {"ok":True}
@app.get("/api/tokens")
def tokens():
 u=me();c=connect();rows=c.execute("SELECT t.id,t.name,t.prefix,t.scopes,t.repo_ids,t.created,t.last_used,t.revoked,u.username FROM api_tokens t JOIN users u ON u.id=t.user_id ORDER BY t.id DESC");out=[dict(r) for r in rows if u["role"]=="admin" or r["username"]==u["username"]];c.close();return out
@app.post("/api/tokens")
async def create_token(request:Request):
 d=await request.json();name=str(d.get("name","Agent token")).strip()[:100];ids=d.get("repositories",[]);u=me();c=connect()
 if not isinstance(ids,list) or any(not isinstance(i,str) for i in ids):c.close();raise HTTPException(422,"repositories must be a list of repository IDs")
 if u["role"]!="admin":
  allowed={r[0] for r in c.execute("SELECT repo_id FROM repo_access WHERE user_id=?",(u["id"],))}
  if any(i not in allowed for i in ids):c.close();raise HTTPException(403,"token can only include repositories assigned to your account")
 for rid in ids:
  if not c.execute("SELECT 1 FROM repositories WHERE id=?",(rid,)).fetchone():c.close();raise HTTPException(422,"unknown repository")
 raw="gf_"+secrets.token_urlsafe(32);scopes=d.get("scopes",["graph:read"])
 cur=c.execute("INSERT INTO api_tokens(user_id,name,token_hash,prefix,scopes,repo_ids) VALUES(?,?,?,?,?,?)",(u["id"],name,hashlib.sha256(raw.encode()).hexdigest(),raw[:11],json.dumps(scopes),json.dumps(ids)));audit(c,u["username"],"api_token_created",str(cur.lastrowid),name);c.commit();tid=cur.lastrowid;c.close();return {"id":tid,"name":name,"token":raw,"prefix":raw[:11]}
@app.post("/api/tokens/{token_id}/revoke")
def revoke_token(token_id:int):
 c=connect();r=c.execute("SELECT * FROM api_tokens WHERE id=?",(token_id,)).fetchone()
 if not r:c.close();raise HTTPException(404,"token not found")
 if me()["role"]!="admin" and r["user_id"]!=me()["id"]:c.close();raise HTTPException(403,"not your token")
 c.execute("UPDATE api_tokens SET revoked=1 WHERE id=?",(token_id,));audit(c,me()["username"],"api_token_revoked",str(token_id));c.commit();c.close();return {"ok":True}
@app.get("/api/activity")
def activity(limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),outcome:str|None=None,repo:str|None=None,tool:str|None=None,actor:str|None=None):
 c=connect();sql="SELECT * FROM mcp_calls";args=[];filters=[]
 for key,value in (("outcome",outcome),("repo_id",repo),("tool",tool),("actor",actor)):
  if value:filters.append(key+"=?");args.append(value[:120])
 if filters:sql+=" WHERE "+" AND ".join(filters)
 sql+=" ORDER BY id DESC LIMIT ? OFFSET ?";args.extend((limit,offset));out=[dict(r) for r in c.execute(sql,args)];c.close();return out
@app.get("/api/activity/summary")
def activity_summary():
 c=connect();rows=[dict(r) for r in c.execute("SELECT outcome,COUNT(*) calls,ROUND(AVG(duration_ms),1) avg_ms FROM mcp_calls WHERE ts>=datetime('now','-30 days') GROUP BY outcome")];c.close();return rows
@app.get("/api/activity/analytics")
def activity_analytics():
 c=connect();total=c.execute("SELECT COUNT(*) FROM mcp_calls WHERE ts>=datetime('now','-30 days')").fetchone()[0];empty=c.execute("SELECT COUNT(*) FROM mcp_calls WHERE ts>=datetime('now','-30 days') AND outcome='success_no_matches'").fetchone()[0];follow=c.execute("SELECT COUNT(*) FROM mcp_calls WHERE ts>=datetime('now','-30 days') AND followup_of IS NOT NULL").fetchone()[0];c.close();return {"calls":total,"empty_result_rate":round(empty/total*100,1) if total else 0,"follow_up_rate":round(follow/total*100,1) if total else 0,"user_rated_usefulness":"Not measured"}
@app.get("/api/audit")
def audit_log():
 c=connect();out=[dict(r) for r in c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200")];c.close();return out
@app.get("/api/guide")
def guide():return {"endpoint":"/mcp","transport":"Streamable HTTP JSON-RPC 2.0","opencode":{"type":"remote","url":"http://127.0.0.1:8765/mcp","oauth":False,"headers":{"Authorization":"Bearer {env:GRAPHIFY_MCP_TOKEN}"}},"cli":"opencode mcp add graphify --url http://127.0.0.1:8765/mcp --header 'Authorization=Bearer {env:GRAPHIFY_MCP_TOKEN}'","tools":[t["name"] for t in mcp.TOOLS],"health":"/health","sample":{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_graph","arguments":{"repository":"your-repo","query":"authentication"}}}}
@app.get("/api/settings")
def settings():return {"bind":os.environ.get("GRAPHIFY_BIND","127.0.0.1"),"data_directory":str(data_dir()),"polling_enabled":os.environ.get("GRAPHIFY_POLLING","1").lower() not in ("0","false","off"),"poll_seconds":int(os.environ.get("GRAPHIFY_POLL_SECONDS","120")),"query_retention_days":int(os.environ.get("GRAPHIFY_QUERY_RETENTION_DAYS","30")),"privacy_mode":os.environ.get("GRAPHIFY_QUERY_PRIVACY","hash")}

@app.post("/hooks/git/{repo_id}")
async def git_hook(repo_id:str,request:Request):
 body=await request.body();c=connect();r=c.execute("SELECT * FROM repositories WHERE id=?",(repo_id,)).fetchone();c.close()
 if not r:raise HTTPException(404,"unknown repository")
 secret=os.environ.get("GRAPHIFY_WEBHOOK_SECRET_"+re.sub(r"\W","_",repo_id).upper(),"")
 sig=request.headers.get("x-graphify-signature","")
 if not secret or not hmac.compare_digest(sig,"sha256="+hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()):raise HTTPException(401,"invalid webhook signature")
 try:d=json.loads(body);branch=str(d.get("branch", ""));sha=str(d.get("after", ""))
 except Exception:raise HTTPException(400,"invalid payload")
 if branch not in [r["default_branch"],*json.loads(r["allowed_branches"])]:raise HTTPException(422,"branch is not configured")
 if not re.fullmatch(r"[0-9a-fA-F]{40,64}",sha):raise HTTPException(422,"invalid commit hash")
 remote_ref="refs/remotes/graphify/"+branch
 fetch=subprocess.run(["git","-C",r["root"],"fetch","--no-tags",r["remote"],f"+refs/heads/{branch}:{remote_ref}"],capture_output=True,text=True,timeout=60) if r["remote"] else None
 latest=subprocess.run(["git","-C",r["root"],"rev-parse",remote_ref if r["remote"] else sha],capture_output=True,text=True,timeout=10)
 target_sha=latest.stdout.strip() if latest.returncode==0 else None
 verify=subprocess.run(["git","-C",r["root"],"cat-file","-e",(target_sha or "")+"^{commit}"],capture_output=True,timeout=10)
 if (fetch and fetch.returncode) or not target_sha or verify.returncode:raise HTTPException(422,"commit is not available in the registered repository")
 return {"job_id":enqueue(repo_id,branch,"incremental",target_sha),"requested_sha":target_sha}

@app.post("/mcp")
async def mcp_http(request:Request):
 authz=request.headers.get("authorization","")
 if not authz.startswith("Bearer "):return JSONResponse({"jsonrpc":"2.0","id":None,"error":{"code":-32001,"message":"bearer token required"}},401)
 body=await request.body()
 if len(body)>1048576:return JSONResponse({"jsonrpc":"2.0","id":None,"error":{"code":-32600,"message":"request exceeds 1 MiB"}},413)
 try:payload=json.loads(body)
 except Exception:return JSONResponse({"jsonrpc":"2.0","id":None,"error":{"code":-32700,"message":"invalid JSON"}},400)
 if isinstance(payload,list):
  if len(payload)>20:return JSONResponse({"jsonrpc":"2.0","id":None,"error":{"code":-32600,"message":"batch limit is 20 requests"}},413)
  results=[mcp.handle(x,authz) for x in payload];return JSONResponse([x for x in results if x is not None])
 response=mcp.handle(payload,authz)
 return Response(status_code=202) if response is None else JSONResponse(response)

WEB=Path(__file__).resolve().parent/"web"
if WEB.exists():app.mount("/",StaticFiles(directory=WEB,html=True),name="web")
