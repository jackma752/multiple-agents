r"""Stage a reviewed Codex runtime into an AgentHub-owned, content-addressed folder.

The desktop app replaces ``%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>`` on every
update (the pinned alpha.11 folder was removed that way on 2026-10-23), so a
pinned runtime has to live where no updater cleans up. This copies the reviewed
files - each checked against the hash and size below BEFORE and AFTER copying -
into ``%LOCALAPPDATA%\AgentHub\runtimes\codex\sha256-<first 16 of codex.exe>\``
and writes ``provenance.json`` there (sources, sizes, SHA-256, Authenticode
signatures).

Nothing here executes Codex. Signatures are read with PowerShell's
``Get-AuthenticodeSignature``. The version is not known until the zero-model
Step A runs the binary (``codex_runtime_step_a.py``); ``version_inferred`` is
only what file names suggest.

    python tools\probes\codex_runtime_stage.py --list
    python tools\probes\codex_runtime_stage.py --stage alpha.11.2
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from typing import Callable, Optional

LOCAL = Path(os.environ.get("LOCALAPPDATA", ""))
RUNTIME_ROOT = LOCAL / "AgentHub" / "runtimes" / "codex"
DESKTOP_BIN = LOCAL / "OpenAI" / "Codex" / "bin"
SANDBOX_BIN = Path(os.environ.get("USERPROFILE", "")) / ".codex" / ".sandbox-bin"
PROVENANCE = "provenance.json"

#: Read-only inventory of 2026-10-23 (Get-FileHash / Get-AuthenticodeSignature; nothing executed).
#: Each file: (source, staged name, sha256, size).
CANDIDATES = {
    "alpha.11.2": {
        "purpose": "runtime",
        # The runner in this folder is byte-identical to .sandbox-bin\codex-command-runner-0.163.0-alpha.11.2.exe.
        "version_inferred": "codex-cli 0.163.0-alpha.11.2",
        "files": [
            (DESKTOP_BIN / "37f54d68e5533a51" / "codex.exe", "codex.exe",
             "c81ed464e5e7cafd09a5fa26af390c480bed8e9fab2a87a54b285fbd56e7122a", 336134960),
            (DESKTOP_BIN / "37f54d68e5533a51" / "codex-command-runner.exe", "codex-command-runner.exe",
             "ea7668d31026311d18a029dfbb6215a502b4bcbb1f970635885b146f7bff4f3f", 8170800),
            (DESKTOP_BIN / "37f54d68e5533a51" / "codex-windows-sandbox-setup.exe", "codex-windows-sandbox-setup.exe",
             "e2cec926fd5f99174912f3bef9672dfe0b2541519a84180b0e9fbf86af0e6a42", 17690416),
            (DESKTOP_BIN / "37f54d68e5533a51" / "codex-code-mode-host.exe", "codex-code-mode-host.exe",
             "7200f4fe6221637670bbc4e63ad8f0e4948b2ebf474eb57e587e01f7f4759bf2", 74955568),
            # The desktop keeps rg in its own folder; staged under path\ so it is never next to codex.exe by accident.
            (DESKTOP_BIN / "2d8a5d5756dbf318" / "rg.exe", "path/rg.exe",
             "8cb6fb5c39df0a2cc7274d375d7db273b8f18d3964cf5055d4d241cafdd7558a", 4352304),
        ],
        "missing": [],
    },
    "alpha.11": {
        # The bytes S3-S6-3 ran (Step A identity-hkzdmoqj, other path). Kept so the historical
        # evidence keeps a copy of its binary; NOT a complete runtime and never pinned as one.
        "purpose": "archive",
        "version_inferred": "codex-cli 0.163.0-alpha.11",
        "files": [
            (SANDBOX_BIN / "codex.exe", "codex.exe",
             "00c0f1e4099246fddcd67ec620ca8ed50827c7d70e113b7224f7a2933cf2d67c", 342187312),
            (SANDBOX_BIN / "codex-command-runner-0.163.0-alpha.11.exe", "codex-command-runner-0.163.0-alpha.11.exe",
             "e95db2235cde22f645c835e79e84b7fbc1e8130d7bfc997d94cdaab8042ecfe7", 8217392),
        ],
        "missing": ["codex-windows-sandbox-setup.exe", "codex-code-mode-host.exe"],
    },
}


class Refused(Exception):
    """A check failed before (or instead of) writing; the message is a reason code."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def signature(path: Path) -> dict:
    """Authenticode facts of one file, read with PowerShell (never executes the file)."""
    literal = str(path).replace("'", "''")
    script = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{literal}'; $c = $s.SignerCertificate; "
        "$t = $s.TimeStamperCertificate; [pscustomobject]@{status = [string]$s.Status; "
        "subject = $(if ($c) { $c.Subject }); thumbprint = $(if ($c) { $c.Thumbprint }); "
        "not_after = $(if ($c) { $c.NotAfter.ToString('s') }); "
        "timestamper = $(if ($t) { $t.Subject })} | ConvertTo-Json -Compress"
    )
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return {"status": "Unreadable"}


def target_dir(label: str, root: Path = RUNTIME_ROOT) -> Path:
    codex = next(f for f in CANDIDATES[label]["files"] if f[1] == "codex.exe")
    return root / f"sha256-{codex[2][:16]}"


def verify(label: str, root: Path = RUNTIME_ROOT) -> list[str]:
    """Problems with an already staged candidate (empty = every file matches its reviewed hash)."""
    folder = target_dir(label, root)
    problems = [] if (folder / PROVENANCE).is_file() else ["provenance_missing"]
    for _source, name, digest, size in CANDIDATES[label]["files"]:
        path = folder / name
        if not path.is_file():
            problems.append(f"missing:{name}")
        elif path.stat().st_size != size or sha256(path) != digest:
            problems.append(f"changed:{name}")
    return problems


def _iso(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def stage(label: str, *, root: Path = RUNTIME_ROOT, sign: Callable[[Path], dict] = signature,
          now: Optional[str] = None) -> dict:
    """Copy one candidate; every source is checked before anything is written."""
    if label not in CANDIDATES:
        raise Refused(f"unknown_candidate:{label}")
    candidate = CANDIDATES[label]
    for source, name, digest, size in candidate["files"]:
        if not source.is_file():
            raise Refused(f"source_missing:{name}")
        if source.stat().st_size != size or sha256(source) != digest:
            raise Refused(f"source_changed:{name}")
    folder = target_dir(label, root)
    records, copied = [], []
    for source, name, digest, size in candidate["files"]:
        dest = folder / name
        if dest.exists():
            if dest.stat().st_size != size or sha256(dest) != digest:
                raise Refused(f"target_differs:{name}")  # never overwrite a staged file
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            partial = dest.with_name(dest.name + ".partial")
            shutil.copy2(source, partial)
            if sha256(partial) != digest:
                partial.unlink()
                raise Refused(f"copy_corrupted:{name}")
            os.replace(partial, dest)
            os.chmod(dest, stat.S_IREAD)
            copied.append(name)
        source_stat = source.stat()
        records.append({"name": name, "size": size, "sha256": digest, "source": str(source),
                        "source_created": _iso(source_stat.st_ctime),
                        "source_modified": _iso(source_stat.st_mtime), "signature": sign(dest)})
    unsigned = [r["name"] for r in records if (r["signature"] or {}).get("status") != "Valid"]
    provenance = {
        "label": label, "purpose": candidate["purpose"],
        "version_inferred": candidate["version_inferred"], "version_verified": None,
        "staged_at": now or datetime.datetime.now().isoformat(timespec="seconds"),
        "staged_by": "tools/probes/codex_runtime_stage.py", "files": records,
        "missing_companions": candidate["missing"], "signatures_valid": not unsigned,
    }
    path = folder / PROVENANCE
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8"))
        same = [(f["name"], f["sha256"]) for f in previous.get("files", [])] == \
               [(f["name"], f["sha256"]) for f in records]
        if not same:
            raise Refused("provenance_differs")
        provenance = previous  # first record wins; a re-run only confirms it
    else:
        path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"label": label, "folder": str(folder), "copied": copied,
            "already_staged": not copied, "signatures_valid": not unsigned, "unsigned": unsigned,
            "problems": verify(label, root)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=sorted(CANDIDATES))
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        for label in CANDIDATES:
            print(json.dumps({"label": label, "folder": str(target_dir(label)),
                              "problems": verify(label)}, ensure_ascii=False))
        return 0
    if not args.stage:
        parser.print_help()
        return 0
    try:
        result = stage(args.stage)
    except Refused as exc:
        print(json.dumps({"label": args.stage, "refused": str(exc)}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["signatures_valid"] and not result["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
