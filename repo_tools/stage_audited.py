"""Stage only the exact worktree snapshot that passed the publication audit."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    if report.get("status") != "passed" or report.get("staged") is not False or report.get("findings"):
        raise SystemExit("A passed worktree audit with no findings is required")
    policy = ROOT / ".repo-local/publication-policy.json"
    if (not policy.is_file() or report.get("local_policy_applied") is not True
            or hashlib.sha256(policy.read_bytes()).hexdigest() != report.get("local_policy_sha256")):
        raise SystemExit("The current local publication policy must be included in the audit")
    inventory = report["inventory"]
    paths = []
    for entry in inventory:
        relative = Path(entry["path"])
        target = ROOT / relative
        if relative.is_absolute() or ".." in relative.parts or target.is_symlink() or not target.is_file():
            raise SystemExit("Invalid or changed audit path")
        if not target.resolve().is_relative_to(ROOT) or target.stat().st_nlink != 1:
            raise SystemExit("Unsafe audit path")
        if hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
            raise SystemExit("File changed after audit: " + entry["path"])
        paths.append(entry["path"])
    if not paths:
        raise SystemExit("Nothing audited to stage")
    subprocess.run(["git", "--literal-pathspecs", "add", "--", *paths], cwd=ROOT, check=True)
    for entry in inventory:
        staged = subprocess.check_output(["git", "show", ":" + entry["path"]], cwd=ROOT)
        if hashlib.sha256(staged).hexdigest() != entry["sha256"]:
            raise SystemExit("Staged content differs from reviewed bytes: " + entry["path"])
    tree = subprocess.check_output(["git", "write-tree"], cwd=ROOT, text=True).strip()
    print(json.dumps({"staged_reviewed_files": len(paths), "tree": tree, "committed": False, "pushed": False}, indent=2))


if __name__ == "__main__":
    main()
