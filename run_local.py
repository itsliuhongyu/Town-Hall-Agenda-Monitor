import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
MODEL_NAME = os.environ.get("AGENDA_MODEL", "qwen3:8b")
VENV_DIR = ROOT_DIR / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"


def run(cmd, *, env=None, check=True):
    print(f"==> {' '.join(str(part) for part in cmd)}", flush=True)
    return subprocess.run(cmd, cwd=ROOT_DIR, env=env, check=check)


def ollama_ready():
    return subprocess.run(
        ["ollama", "list"],
        cwd=ROOT_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def ensure_ollama():
    print("==> Checking Ollama", flush=True)

    if shutil.which("ollama") is None:
        raise SystemExit("Ollama is not installed or not on PATH.")

    if not ollama_ready():
        raise SystemExit("Ollama is offline. Start it explicitly before an inference run; this app will not serve it automatically.")


def ensure_model(model_name=MODEL_NAME):
    result = subprocess.run(
        ["ollama", "list"],
        cwd=ROOT_DIR,
        text=True,
        capture_output=True,
        check=True,
    )
    installed = {line.split()[0] for line in result.stdout.splitlines()[1:] if line.split()}

    if model_name not in installed:
        raise SystemExit(
            f"Configured Ollama model '{model_name}' is not installed. "
            "Set AGENDA_MODEL to an id from 'ollama list' or install it explicitly."
        )


def ensure_venv(skip_install):
    print("==> Preparing Python virtual environment", flush=True)

    if not VENV_PYTHON.exists():
        run(["python3", "-m", "venv", ".venv"])

    if skip_install:
        return

    run([str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(VENV_PYTHON), "-m", "pip", "install", "-r", "requirements.txt"])
    run([str(VENV_PYTHON), "-m", "playwright", "install", "chromium"])


def run_pipeline(workers, llm_workers):
    env = os.environ.copy()
    env["AGENDA_WORKERS"] = str(workers)
    env["LLM_WORKERS"] = str(llm_workers)
    run([str(VENV_PYTHON), "-u", "pipeline.py"], env=env)


def run_review_server():
    print("==> Starting review page at http://127.0.0.1:8000", flush=True)
    run([str(VENV_PYTHON), "review_server.py"])


def parse_args():
    parser = argparse.ArgumentParser(description="Run the local Town Hall Agenda Monitor app.")
    parser.add_argument("--app", action="store_true", help="Start the local HTTP application without fetching or starting Ollama.")
    parser.add_argument("--review", action="store_true", help="Compatibility alias for --app.")
    parser.add_argument("--data-dir", default=os.environ.get("AGENDA_DATA_DIR", str(ROOT_DIR / "data")))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--setup", action="store_true", help="Explicitly create/install the local environment.")
    parser.add_argument("--skip-install", action="store_true", help="Skip dependency/browser installation during --setup.")
    parser.add_argument("--workers", type=int, default=int(os.environ.get("AGENDA_WORKERS", "1")))
    parser.add_argument("--llm-workers", type=int, default=int(os.environ.get("LLM_WORKERS", "2")))
    return parser.parse_args()


def main():
    args = parse_args()
    if args.setup:
        ensure_venv(args.skip_install)
    if args.app or args.review:
        from agenda_app.web.server import create_server
        server = create_server(args.data_dir, port=args.port, legacy_root=ROOT_DIR)
        print(f"Agenda Monitor running at http://127.0.0.1:{args.port}", flush=True)
        server.serve_forever()
    else:
        env = os.environ.copy(); env["AGENDA_DATA_DIR"] = str(Path(args.data_dir).resolve())
        result = subprocess.run([sys.executable, "pipeline.py", "--data-dir", args.data_dir, "--json"], cwd=ROOT_DIR, env=env, check=False)
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
