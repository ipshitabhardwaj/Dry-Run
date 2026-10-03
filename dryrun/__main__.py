"""python -m dryrun  ->  http://127.0.0.1:8000"""
import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run("dryrun.app:app", host=os.environ.get("DRYRUN_HOST", "127.0.0.1"),
                port=int(os.environ.get("DRYRUN_PORT", "8000")))
