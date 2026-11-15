"""Versioned, read-only MCP JSON-RPC tools with bearer-token and repository scope checks."""
import hashlib,json,re,sqlite3,time,uuid,os
from pathlib import Path
from .db import connect

TOOLS=[
 {"name":"list_repositories","description":"List repositories this credential can query, with graph freshness.","inputSchema":{"type":"object","properties":{}}},
 {"name":"graph_status","description":"Return served graph version, source commit, freshness and counts.","inputSchema":{"type":"object","properties":{"repository":{"type":"string"},"branch":{"type":"string"}},"required":["repository"]}},
 {"name":"search_graph","description":"Search graph nodes and return source citations and bounded neighbors. Use next_offset to page through large result sets.","inputSchema":{"type":"object","properties":{"repository":{"type":"string"},"branch":{"type":"string"},"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":30},"offset":{"type":"integer","minimum":0}},"required":["repository","query"]}},
 {"name":"explain_node","description":"Return a graph node and direct relationships with provenance.","inputSchema":{"type":"object","properties":{"repository":{"type":"string"},"branch":{"type":"string"},"node":{"type":"string"}},"required":["repository","node"]}},
 {"name":"trace_path","description":"Find a bounded path between two graph concepts.","inputSchema":{"type":"object","properties":{"repository":{"type":"string"},"branch":{"type":"string"},"from":{"type":"string"},"to":{"type":"string"}},"required":["repository","from","to"]}},
 {"name":"query_graph","description":"Perform a bounded vocabulary-expanded traversal over a graph. Use next_offset to page through matched starting nodes.","inputSchema":{"type":"object","properties":{"repository":{"type":"string"},"branch":{"type":"string"},"query":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":30},"offset":{"type":"integer","minimum":0}},"required":["repository","query"]}},
]

def _allowed(c,token,repo_id=None):
    digest=hashlib.sha256(token.encode()).hexdigest();row=c.execute("SELECT t.*,u.username,u.status FROM api_tokens t JOIN users u ON u.id=t.user_id WHERE t.token_hash=? AND t.revoked=0",(digest,)).fetchone()
    if not row or row["status"]!="active":raise PermissionError("invalid or revoked credential")
    if "graph:read" not in json.loads(row["scopes"]):raise PermissionError("credential lacks graph:read scope")
    ids=json.loads(row["repo_ids"]); role=c.execute("SELECT role FROM users WHERE id=?",(row["user_id"],)).fetchone()[0]
    if repo_id and repo_id not in ids:raise PermissionError("repository access denied")
    if repo_id and role!="admin" and not c.execute("SELECT 1 FROM repo_access WHERE user_id=? AND repo_id=?",(row["user_id"],repo_id)).fetchone():raise PermissionError("repository access denied")
    c.execute("UPDATE api_tokens SET last_used=CURRENT_TIMESTAMP WHERE id=?",(row["id"],));c.commit()
    return row
def _graph(c,repo_id,branch,tokenrow):
    _allowed(c,token_from_row(tokenrow),repo_id)
    repository=c.execute("SELECT default_branch,allowed_branches FROM repositories WHERE id=?",(repo_id,)).fetchone()
    if not repository:raise KeyError("repository not found")
    branch=branch or repository["default_branch"]
    if branch not in [repository["default_branch"],*json.loads(repository["allowed_branches"])]:raise PermissionError("branch access denied")
    r=c.execute("SELECT r.name,r.default_branch,g.* FROM repositories r LEFT JOIN graphs g ON g.repo_id=r.id AND g.branch=? AND g.active=1 WHERE r.id=?",(branch,repo_id)).fetchone()
    if not r:raise KeyError("repository not found")
    if not r["path"]:raise LookupError("graph unavailable; no successful build is served")
    d=json.loads((Path(r["path"])/"graph.json").read_text(encoding="utf-8"));return dict(r),d

# Per-request context is set by the ASGI endpoint without persisting bearer secrets.
from contextvars import ContextVar
_bearer=ContextVar("mcp_bearer",default="")
def token_from_row(row):return _bearer.get()
def _label(n):return str(n.get("label") or n.get("name") or n.get("id") or "")
def _node(n):return str(n.get("id") or n.get("key") or _label(n))
def _safe_node(n):
    out={}
    for k,v in n.items():
        if k not in {"id","key","label","name","type","kind","source_location","source_file","source_files","confidence","community","community_name"}:continue
        if isinstance(v,str):out[k]=v[:1200]
        elif isinstance(v,list):out[k]=[str(x)[:300] for x in v[:10]]
        elif isinstance(v,(int,float,bool)) or v is None:out[k]=v
        else:out[k]=str(v)[:300]
    return out
def _safe_edge(e):
    out={}
    for k,v in e.items():
        if k not in {"source","target","relation","type","label","confidence","provenance","source_location","evidence"}:continue
        if isinstance(v,str):out[k]=v[:600]
        elif isinstance(v,(int,float,bool)) or v is None:out[k]=v
        else:out[k]=json.dumps(v,ensure_ascii=False,default=str)[:600]
    return out
def _find(nodes,q):
    q=q.casefold();terms=re.findall(r"[\w.-]+",q)
    def score(n):
        sources=n.get("source_files",[]);sources=sources if isinstance(sources,list) else [sources]
        hay=(_label(n)+" "+_node(n)+" "+str(n.get("type",""))+" "+str(n.get("kind",""))+" "+str(n.get("source_file",""))+" "+str(n.get("source_location",""))+" "+" ".join(map(str,sources))+" "+str(n.get("community_name",""))).casefold()
        return (100 if hay==q else 20 if q in hay else sum(3 for t in terms if t in hay))
    return sorted(((score(n),n) for n in nodes if score(n)>0),key=lambda p:(-p[0],_label(p[1])))
def _run(c,tokenrow,tool,a):
    repo=a.get("repository");branch=a.get("branch");meta,g=_graph(c,repo,branch,tokenrow); nodes=g.get("nodes",[]);edges=g.get("edges",[])
    version={"repository":repo,"branch":meta["branch"],"graph_version":meta["version"],"commit_sha":meta["commit_sha"]}
    if tool=="list_repositories":
        allowed=json.loads(tokenrow["repo_ids"]); role=c.execute("SELECT role FROM users WHERE id=?",(tokenrow["user_id"],)).fetchone()[0]
        rows=c.execute("SELECT r.id,r.name,r.default_branch,g.version,g.commit_sha,g.node_count,g.edge_count,g.created FROM repositories r LEFT JOIN graphs g ON g.repo_id=r.id AND g.branch=r.default_branch AND g.active=1 ORDER BY r.name").fetchall()
        return {"repositories":[{**dict(x),"freshness":"unavailable" if not x["version"] else "current" if x["commit_sha"]==_head(c,x["id"],x["default_branch"]) else "stale"} for x in rows if x["id"] in allowed and (role=="admin" or c.execute("SELECT 1 FROM repo_access WHERE user_id=? AND repo_id=?",(tokenrow["user_id"],x["id"])).fetchone())]}
    if tool=="graph_status":
        update=c.execute("SELECT state FROM jobs WHERE repo_id=? AND branch=? ORDER BY id DESC LIMIT 1",(repo,meta["branch"])).fetchone()
        return {**version,"last_successful_update":meta["created"],"node_count":meta["node_count"],"edge_count":meta["edge_count"],"freshness":"current" if meta["commit_sha"]==_head(c,repo,meta["branch"]) else "stale","current_update_state":update["state"] if update else "idle"}
    if tool in ("search_graph","query_graph"):
        q=str(a.get("query","")).strip()[:500]
        if not q:raise ValueError("query must not be empty")
        all_hits=_find(nodes,q);limit=max(1,min(30,int(a.get("limit",10))));offset=max(0,min(100000,int(a.get("offset",0))));hits=all_hits[offset:offset+limit];ids={_node(n) for _,n in hits}
        related=[e for e in edges if str(e.get("source")) in ids or str(e.get("target")) in ids][:100]
        if tool=="query_graph":
            byid={_node(n):n for n in nodes};front=list(ids);seen=set(ids);walk_edges=[]
            for _depth in range(2):
                next_front=[]
                for current in front:
                    for edge in edges:
                        source,target=str(edge.get("source")),str(edge.get("target"))
                        if current not in (source,target):continue
                        walk_edges.append(edge);neighbor=target if source==current else source
                        if neighbor not in seen:seen.add(neighbor);next_front.append(neighbor)
                        if len(seen)>=30 or len(walk_edges)>=100:break
                    if len(seen)>=30 or len(walk_edges)>=100:break
                front=next_front
                if not front or len(walk_edges)>=100 or len(seen)>=30:break
            out={**version,"query_terms":re.findall(r"[\w.-]+",q),"matched_nodes":[_node(n) for _,n in hits],"nodes":[_safe_node(byid[i]) for i in seen if i in byid],"edges":[_safe_edge(e) for e in walk_edges[:100]],"total_matches":len(all_hits),"truncated":len(all_hits)>offset+len(hits) or len(seen)>=30 or len(walk_edges)>=100}
        else:out={**version,"query_terms":re.findall(r"[\w.-]+",q),"nodes":[{**_safe_node(n),"score":s} for s,n in hits],"edges":[_safe_edge(e) for e in related],"total_matches":len(all_hits),"truncated":len(all_hits)>offset+len(hits)}
        if out["truncated"]:out["next_offset"]=offset+len(hits)
        return out
    if tool=="explain_node":
        query=str(a["node"]).strip()[:500]
        if not query:raise ValueError("node must not be empty")
        hits=_find(nodes,query)
        if not hits: return {**version,"matches":[]}
        n=hits[0][1];nid=_node(n);return {**version,"node":_safe_node(n),"edges":[_safe_edge(e) for e in edges if str(e.get("source"))==nid or str(e.get("target"))==nid][:100]}
    if tool=="trace_path":
        source_query=str(a["from"]).strip()[:500];target_query=str(a["to"]).strip()[:500]
        if not source_query or not target_query:raise ValueError("from and to must not be empty")
        left=_find(nodes,source_query);right=_find(nodes,target_query)
        if not left or not right:return {**version,"path":[],"found":False}
        start,end=_node(left[0][1]),_node(right[0][1]); adj={}
        for e in edges:
            x,y=str(e.get("source")),str(e.get("target"));adj.setdefault(x,[]).append((y,e))
            if not g.get("directed",True):adj.setdefault(y,[]).append((x,e))
        queue=[start];prev={start:None};via={}
        for x in queue:
            if x==end:break
            for y,e in adj.get(x,[]):
                if y not in prev:prev[y]=x;via[y]=e;queue.append(y)
                if len(queue)>2000:break
        if end not in prev:return {**version,"path":[],"found":False}
        ids=[];cur=end
        while cur is not None:ids.append(cur);cur=prev[cur]
        ids.reverse();byid={_node(n):n for n in nodes};truncated=len(ids)>50;ids=ids[:50]
        return {**version,"found":True,"path":[{"node":_safe_node(byid[i]) if i in byid else i,"edge":_safe_edge(via[i]) if i in via else None} for i in ids],"truncated":truncated}
    raise ValueError("unknown tool")
def _head(c,repo,branch):
    from .worker import _sha
    r=c.execute("SELECT root,remote FROM repositories WHERE id=?",(repo,)).fetchone()
    if not r:return None
    return _sha(Path(r["root"]),"refs/remotes/graphify/"+branch) if r["remote"] and _sha(Path(r["root"]),"refs/remotes/graphify/"+branch) else _sha(Path(r["root"]),branch)
def handle(payload,authorization,actor="agent"):
    if not isinstance(payload,dict):return {"jsonrpc":"2.0","id":None,"error":{"code":-32600,"message":"request must be an object"}}
    rid=str(payload.get("id") or uuid.uuid4().hex)[:100];method=payload.get("method");params=payload.get("params") or {};started=time.monotonic();tool="";repo=(params.get("arguments") or {}).get("repository");data={};client=params.get("clientInfo") or {}
    token=authorization.removeprefix("Bearer ").strip();c=connect();row=None;outcome="success_with_matches";result=None;error=None
    try:
        row=_allowed(c,token)
        if repo:row=_allowed(c,token,repo)
        if isinstance(client,dict) and client.get("name"):
            c.execute("INSERT INTO mcp_clients(token_id,name,version,seen) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(token_id) DO UPDATE SET name=excluded.name,version=excluded.version,seen=CURRENT_TIMESTAMP",(row["id"],str(client.get("name"))[:100],str(client.get("version",""))[:80]));c.commit()
        elif row:
            remembered=c.execute("SELECT name,version FROM mcp_clients WHERE token_id=?",(row["id"],)).fetchone()
            if remembered:client={"name":remembered["name"],"version":remembered["version"]}
        if method=="initialize":result={"protocolVersion":params.get("protocolVersion","2025-03-26"),"capabilities":{"tools":{"listChanged":False}},"serverInfo":{"name":"graphify","version":"1.0.0"}}
        elif method=="notifications/initialized":c.close();return None
        elif method=="ping":result={}
        elif method=="tools/list":result={"tools":TOOLS}
        elif method=="tools/call":
            tool=str(params.get("name",""))[:120];args=params.get("arguments") or {};repo=args.get("repository")
            if tool=="list_repositories":
                role=c.execute("SELECT role FROM users WHERE id=?",(row["user_id"],)).fetchone()[0];allowed=json.loads(row["repo_ids"])
                repos=c.execute("SELECT r.id,r.name,r.default_branch,g.version,g.commit_sha,g.node_count,g.edge_count,g.created FROM repositories r LEFT JOIN graphs g ON g.repo_id=r.id AND g.branch=r.default_branch AND g.active=1 ORDER BY r.name").fetchall()
                data={"repositories":[{**dict(x),"freshness":"unavailable" if not x["version"] else "current" if x["commit_sha"]==_head(c,x["id"],x["default_branch"]) else "stale"} for x in repos if x["id"] in allowed and (role=="admin" or c.execute("SELECT 1 FROM repo_access WHERE user_id=? AND repo_id=?",(row["user_id"],x["id"])).fetchone())]}
            else:
                if not isinstance(repo,str):raise ValueError("repository is required")
                _bearer.set(token); data=_run(c,row,tool,args)
            data["request_id"]=rid
            result={"content":[{"type":"text","text":json.dumps(data,ensure_ascii=False)}],"structuredContent":data,"isError":False}
            if (tool in ("search_graph","query_graph") and not data.get("nodes")) or (tool=="explain_node" and not data.get("node")) or (tool=="trace_path" and not data.get("found")) or (tool=="list_repositories" and not data.get("repositories")):outcome="success_no_matches"
            if data.get("truncated"):outcome="truncated"
        else:raise ValueError("unsupported MCP method")
    except PermissionError as e:outcome="unauthorized";error="unauthorized";result={"code":-32001,"message":str(e)}
    except (KeyError,LookupError) as e:outcome="graph_unavailable";error="graph_unavailable";result={"code":-32004,"message":str(e)}
    except Exception as e:outcome="invalid_request";error=type(e).__name__;result={"code":-32602,"message":str(e)[:300]}
    if method!="tools/call" and row:
        c.close()
        return {"jsonrpc":"2.0","id":payload.get("id"),"result":result}
    if method=="tools/call" and result and "content" in result:
        while len(json.dumps(result,ensure_ascii=False,default=str).encode("utf-8"))>262144:
            data["truncated"]=True
            if "next_offset" not in data:data["next_offset"]=int(args.get("offset",0))+int(args.get("limit",10))
            if data.get("edges"):data["edges"]=data["edges"][:max(1,len(data["edges"])//2)]
            if data.get("nodes"):data["nodes"]=data["nodes"][:max(1,len(data["nodes"])//2)]
            if data.get("path"):data["path"]=data["path"][:max(1,len(data["path"])//2)]
            if data.get("repositories"):data["repositories"]=data["repositories"][:max(1,len(data["repositories"])//2)]
            if not data.get("edges") and not data.get("nodes") and not data.get("path") and not data.get("repositories"):break
            result["structuredContent"]=data;result["content"][0]["text"]=json.dumps(data,ensure_ascii=False)
        outcome="truncated"
    duration=(time.monotonic()-started)*1000;args=params.get("arguments") or {};preview=args.get("query") or args.get("node") or (str(args.get("from",""))+" -> "+str(args.get("to",""))).strip(" ->");digest=hashlib.sha256(" ".join(str(preview).casefold().split()).encode()).hexdigest() if preview else None
    stored_preview=str(preview)[:120] if preview and os.environ.get("GRAPHIFY_QUERY_PRIVACY","hash") == "preview" else None
    retention=max(1,int(os.environ.get("GRAPHIFY_QUERY_RETENTION_DAYS","30")))
    token_id=row["id"] if row else None;prior=c.execute("SELECT id FROM mcp_calls WHERE token_id=? AND query_hash=? AND ts>=datetime('now','-30 minutes') ORDER BY id DESC LIMIT 1",(token_id,digest)).fetchone() if token_id and digest else None
    matched_nodes=len(data.get("matched_nodes",data.get("nodes",[]))) if isinstance(data,dict) else 0
    if isinstance(data,dict) and not matched_nodes and data.get("node"):matched_nodes=1
    matched_edges=len(data.get("edges",[])) if isinstance(data,dict) else 0
    if isinstance(data,dict) and data.get("path"):matched_nodes=len(data["path"]);matched_edges=max(0,matched_nodes-1)
    c.execute("INSERT INTO mcp_calls(request_id,actor,token_id,client_name,client_version,tool,repo_id,branch,outcome,duration_ms,result_bytes,matched_nodes,matched_edges,query_hash,query_preview,followup_of,error_class,graph_version,commit_sha) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(rid,row["username"] if row else actor,token_id,str(client.get("name",""))[:100] or None,str(client.get("version",""))[:80] or None,tool,repo,str(args.get("branch",""))[:120] or None,outcome,duration,len(json.dumps(result,default=str)),matched_nodes,matched_edges,digest,stored_preview,prior["id"] if prior else None,error,data.get("graph_version") if isinstance(data,dict) else None,data.get("commit_sha") if isinstance(data,dict) else None))
    c.execute("DELETE FROM mcp_calls WHERE ts<datetime('now',?)",(f"-{retention} days",));c.commit();c.close()
    if method=="tools/call" and result and "content" in result:return {"jsonrpc":"2.0","id":payload.get("id"),"result":result}
    if isinstance(result,dict) and "code" in result:return {"jsonrpc":"2.0","id":payload.get("id"),"error":result}
    return {"jsonrpc":"2.0","id":payload.get("id"),"result":result}
