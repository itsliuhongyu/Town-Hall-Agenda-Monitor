from __future__ import annotations

import argparse
import json
import sys

from .runs.service import RunService
from .storage.exports import ExportService
from .storage.legacy_import import LegacyImporter
from .storage.source_initialization import SourceInitialization
from .storage.repository import ConflictError, Repository


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agenda_app.cli")
    parser.add_argument("--data-dir", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import-legacy"); imp.add_argument("--from", dest="source_root", required=True)
    source_init = sub.add_parser("initialize-sources", aliases=["init-sources"], help="Initialize only source configuration from townlist.csv")
    source_init.add_argument("--from", dest="source_root", required=True)
    exp = sub.add_parser("export"); exp.add_argument("--run", required=True); exp.add_argument("--format", choices=["raw", "spreadsheet_safe"], default="spreadsheet_safe")
    retry = sub.add_parser("retry"); retry.add_argument("--run", required=True); retry.add_argument("--source", action="append", required=True)
    args = parser.parse_args(argv); repo = Repository(args.data_dir)
    try:
        if args.command == "import-legacy": result = LegacyImporter(repo).import_directory(args.source_root)
        elif args.command in {"initialize-sources", "init-sources"}: result = SourceInitialization(repo, args.source_root).initialize()
        elif args.command == "export": result = ExportService(repo).export_run(args.run, spreadsheet_safe=args.format != "raw")
        else: result = RunService(repo).retry(args.run, args.source, background=False)
    except ConflictError as exc:
        print(json.dumps({"status": "conflict", "code": exc.code, "details": exc.details}, ensure_ascii=False)); return 3
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted", "code": "interrupted"}, ensure_ascii=False)); return 130
    print(json.dumps(result, ensure_ascii=False, default=str))
    status = result.get("status") if isinstance(result, dict) else None
    return 0 if status in {None, "success", "already_configured", "no_results"} else 2 if status == "partial" else 130 if status == "interrupted" else 1


if __name__ == "__main__": raise SystemExit(main())
