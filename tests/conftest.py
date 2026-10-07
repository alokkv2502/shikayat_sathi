import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["LLM_PROVIDER"] = "none"  # deterministic: rule-based agents
os.environ["SERPAPI_API_KEY"] = ""
os.environ["QDRANT_URL"] = "http://127.0.0.1:1"  # force embedded Qdrant on the test copy below
# Own data dir with a copy of the legal index: embedded Qdrant allows one process, and the app may be running.
_tmp = Path(tempfile.mkdtemp(prefix="saathi-test-"))
if (ROOT / "data" / "qdrant").exists():
    shutil.copytree(ROOT / "data" / "qdrant", _tmp / "qdrant", ignore=shutil.ignore_patterns(".lock"))
os.environ["SAATHI_DATA_DIR"] = str(_tmp)
sys.path.insert(0, str(ROOT))
