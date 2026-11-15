import argparse,os,secrets,time
def main():
 p=argparse.ArgumentParser(prog="graphify-platform");sub=p.add_subparsers(dest="command");sub.add_parser("serve");sub.add_parser("bootstrap-secret");args=p.parse_args()
 if args.command=="bootstrap-secret":
  secret=f"{int(time.time())}.{secrets.token_urlsafe(32)}";print("Set GRAPHIFY_BOOTSTRAP_SECRET to this one-use value within one hour, then start or restart the server:\n\n"+secret);return
 import uvicorn
 bind=os.environ.get("GRAPHIFY_BIND","127.0.0.1");port=int(os.environ.get("GRAPHIFY_PORT","8765"))
 uvicorn.run("graphify_platform.api:app",host=bind,port=port)
if __name__=="__main__":main()
