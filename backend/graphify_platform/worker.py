"""Graphify build worker: isolated build dirs, validated roots and atomic current symlink promotion."""
import json, os, shutil, subprocess, threading, time, uuid, ipaddress, sqlite3
import re
from pathlib import Path
from urllib.parse import urlsplit
from .config import data_dir
from .db import connect

_locks: dict[tuple[str,str],threading.Lock]={}; _guard=threading.Lock()
def _local_model_url(value,default):
    parsed=urlsplit(value or default);host=parsed.hostname or ""
    if parsed.scheme not in ("http","https") or not host or parsed.username or parsed.password or parsed.query or parsed.fragment:raise RuntimeError("Local model adapter URL must be an HTTP(S) URL without embedded credentials")
    try:private=ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback or ipaddress.ip_address(host).is_link_local
    except ValueError:private=host in ("localhost","host.docker.internal") or host.endswith((".internal",".local",".lan"))
    if not private:raise RuntimeError("Hosted model endpoints are disabled; use loopback, a private address, or an internal hostname")
    return value or default
def _lock(repo,branch):
    with _guard:return _locks.setdefault((repo,branch),threading.Lock())
def _sha(root,branch):
    p=subprocess.run(["git","-C",str(root),"rev-parse",branch],capture_output=True,text=True,timeout=10)
    return p.stdout.strip() if p.returncode==0 else None
def _redact_log(value):
    value=re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+",r"\1[REDACTED]",value)
    value=re.sub(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+",r"\1[REDACTED]",value)
    for key in ("OPENAI_API_KEY","ANTHROPIC_API_KEY","GEMINI_API_KEY","KIMI_API_KEY"):
        secret=os.environ.get(key)
        if secret:value=value.replace(secret,"[REDACTED]")
    return value
def run_job(job_id:int):
    c=connect(); job=c.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone(); repo=c.execute("SELECT * FROM repositories WHERE id=?",(job["repo_id"],)).fetchone() if job else None
    if not job or not repo or job["state"]!="queued":return
    root=Path(repo["root"]).resolve(); branch=job["branch"]
    if not root.is_dir():
        c.execute("UPDATE jobs SET state='failed',error='Repository directory is unavailable',finished=CURRENT_TIMESTAMP WHERE id=?",(job_id,));c.commit();c.close();return
    with _lock(repo["id"],branch):
        job=c.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
        if not job or job["state"]!="queued":c.close();return
        c.execute("UPDATE jobs SET state='running',started=CURRENT_TIMESTAMP WHERE id=? AND state='queued'",(job_id,));c.commit()
        if c.execute("SELECT state FROM jobs WHERE id=?",(job_id,)).fetchone()[0]!="running":c.close();return
        sha=job["requested_sha"] or _sha(root,branch); jobdir=data_dir()/"staging"/f"{job_id}-{uuid.uuid4().hex[:8]}"; source=jobdir/"source"; target=source/"graphify-out"; log=""; worktree=False;dest=None
        try:
            if not sha: raise RuntimeError("Could not resolve the configured Git branch")
            jobdir.mkdir(parents=True,exist_ok=True,mode=0o700)
            checkout=subprocess.run(["git","-C",str(root),"worktree","add","--detach",str(source),sha],capture_output=True,text=True,timeout=30)
            if checkout.returncode:raise RuntimeError("Could not create isolated source checkout: "+checkout.stderr[-1000:])
            worktree=True
            previous=c.execute("SELECT path FROM graphs WHERE repo_id=? AND branch=? AND active=1",(repo["id"],branch)).fetchone()
            if job["mode"]=="incremental" and previous:
                shutil.copytree(Path(previous["path"]),target)
            # Pass only build essentials and explicitly configured local-provider settings.
            env={k:v for k,v in os.environ.items() if k in {"PATH","HOME","TMPDIR","TMP","TEMP","LANG","LC_ALL","VIRTUAL_ENV","PYTHONPATH"}}
            env["PYTHONUNBUFFERED"]="1"
            options=json.loads(repo["options"] or "{}")
            cmd=["graphify","extract",str(source),"--out",str(source),"--no-cluster"]
            if not options.get("include_documents",False):cmd.append("--code-only")
            else:
                backend=options.get("local_backend","ollama");model=options.get("local_model","")
                if backend not in ("ollama","openai"):raise RuntimeError("Semantic extraction must use the local ollama or OpenAI-compatible adapter")
                if not model:raise RuntimeError("Configure a local model name before enabling document or image extraction")
                if backend=="ollama":env["OLLAMA_HOST"]=_local_model_url(os.environ.get("OLLAMA_HOST",""),"http://127.0.0.1:11434")
                else:
                    env["OPENAI_BASE_URL"]=_local_model_url(os.environ.get("OPENAI_BASE_URL",""),"")
                    if os.environ.get("OPENAI_API_KEY"):env["OPENAI_API_KEY"]=os.environ["OPENAI_API_KEY"]
                cmd.extend(["--backend",backend,"--model",model])
            if options.get("deep_mode"):cmd.extend(["--mode","deep"])
            if job["mode"]=="full":cmd.append("--force")
            graphify_bin=shutil.which(os.environ.get("GRAPHIFY_BINARY","graphify"))
            if not graphify_bin:raise RuntimeError("Graphify CLI was not found on the service PATH")
            cmd[0]=str(Path(graphify_bin).resolve())
            proc=subprocess.run(cmd,cwd=jobdir,env=env,capture_output=True,text=True,timeout=3300)
            log=_redact_log(proc.stdout+"\n"+proc.stderr)[-100000:]
            if proc.returncode:raise RuntimeError(f"Graphify exited {proc.returncode}: {log[-2000:]}")
            cluster=subprocess.run([cmd[0],"cluster-only",str(source),"--no-viz","--no-label"],cwd=jobdir,env=env,capture_output=True,text=True,timeout=300)
            log=_redact_log(log+"\n"+cluster.stdout+"\n"+cluster.stderr)[-100000:]
            if cluster.returncode:raise RuntimeError(f"Graphify clustering exited {cluster.returncode}: {log[-2000:]}")
            graph=target/"graph.json"
            if not graph.is_file():raise RuntimeError("Graphify completed without graphify-out/graph.json")
            data=json.loads(graph.read_text(encoding="utf-8")); nodes=data.get("nodes",[]); edges=data.get("edges",[])
            version=f"{sha[:12]}-{int(time.time())}"; dest=data_dir()/"graphs"/repo["id"]/branch/version
            dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700); shutil.copytree(target,dest)
            c.execute("SAVEPOINT graph_promote")
            c.execute("UPDATE graphs SET active=0 WHERE repo_id=? AND branch=?",(repo["id"],branch))
            c.execute("INSERT INTO graphs(repo_id,branch,version,commit_sha,path,node_count,edge_count,active) VALUES(?,?,?,?,?,?,?,1)",(repo["id"],branch,version,sha,str(dest),len(nodes),len(edges)))
            # Retain current and one preceding successful version.
            old=c.execute("SELECT id,path FROM graphs WHERE repo_id=? AND branch=? AND active=0 ORDER BY id DESC LIMIT -1 OFFSET 1",(repo["id"],branch)).fetchall()
            for row in old:c.execute("DELETE FROM graphs WHERE id=?",(row["id"],))
            c.execute("UPDATE jobs SET state='succeeded',requested_sha=?,finished=CURRENT_TIMESTAMP,log=? WHERE id=?",(sha,log,job_id))
            c.execute("RELEASE graph_promote")
            c.commit()
            for row in old:shutil.rmtree(Path(row["path"]),ignore_errors=True)
        except Exception as e:
            try:c.execute("ROLLBACK TO graph_promote");c.execute("RELEASE graph_promote")
            except sqlite3.OperationalError:pass
            if dest:shutil.rmtree(dest,ignore_errors=True)
            c.execute("UPDATE jobs SET state='failed',requested_sha=?,finished=CURRENT_TIMESTAMP,log=?,error=? WHERE id=?",(sha,log,str(e)[:2000],job_id));c.commit()
        finally:
            if worktree:
                subprocess.run(["git","-C",str(root),"worktree","remove","--force",str(source)],capture_output=True,text=True,timeout=30)
            shutil.rmtree(jobdir,ignore_errors=True)
    c.close()

def enqueue(repo_id,branch,mode,requested_sha=None):
    c=connect(); active=c.execute("SELECT id,state FROM jobs WHERE repo_id=? AND branch=? AND state IN ('queued','running') ORDER BY id DESC LIMIT 1",(repo_id,branch)).fetchone()
    if active:
        if active["state"]=="running" and mode=="full":
            cur=c.execute("INSERT INTO jobs(repo_id,branch,mode,requested_sha) VALUES(?,?,?,?)",(repo_id,branch,mode,requested_sha));jid=cur.lastrowid;c.commit();c.close();threading.Thread(target=run_job,args=(jid,),daemon=True).start();return jid
        if active["state"]=="queued":c.execute("UPDATE jobs SET requested_sha=COALESCE(?,requested_sha),mode=CASE WHEN ?='full' THEN 'full' ELSE mode END,created=CURRENT_TIMESTAMP WHERE id=?",(requested_sha,mode,active["id"]))
        c.commit();c.close();return active["id"]
    cur=c.execute("INSERT INTO jobs(repo_id,branch,mode,requested_sha) VALUES(?,?,?,?)",(repo_id,branch,mode,requested_sha)); jid=cur.lastrowid;c.commit();c.close()
    threading.Thread(target=run_job,args=(jid,),daemon=True).start();return jid
