"""Preflight diagnostics for repeatable translation-agent runs."""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

from orchestration.pipeline_profiles import load_pipeline_profiles
from orchestration.product_contracts import APP_VERSION, CONTRACT_SCHEMA_VERSION


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    required: bool
    detail: str


CORE_IMPORTS = {
    "fitz": "PyMuPDF",
    "PIL": "Pillow",
    "markdown": "Markdown",
    "lxml": "lxml",
    "opencc": "opencc-python-reimplemented",
    "docx": "python-docx",
}


def _module_check(module: str, distribution: str, *, required: bool) -> DoctorCheck:
    available = importlib.util.find_spec(module) is not None
    return DoctorCheck(
        name=f"python:{module}",
        ok=available,
        required=required,
        detail=(
            f"{distribution} is importable"
            if available
            else f"install {distribution}"
        ),
    )


def _command_check(command: str, *, required: bool) -> DoctorCheck:
    resolved = shutil.which(command)
    return DoctorCheck(
        name=f"command:{command}",
        ok=resolved is not None,
        required=required,
        detail=resolved or f"{command} not found on PATH",
    )


def _render_backend_check(*, required: bool) -> DoctorCheck:
    """Report the renderer the Word layout gate will actually use.

    The gate accepts LibreOffice/soffice or, on Windows, the isolated Microsoft
    Word COM fallback, so requiring a specific one of them would demand an
    installation the gate does not need.
    """

    resolved: str | None = None
    word: str | None = None
    try:
        # Optional document backends may print import-time compatibility
        # warnings (PyMuPDF 1.28 does this for the legacy ``fitz`` name).
        # Diagnostics own their final stdout/stderr contract, especially when
        # ``--json`` is requested, so contain those third-party side effects.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            from publication.docx_render_gate import find_soffice, find_word_renderer

            resolved = find_soffice()
            word = None if resolved else find_word_renderer()
    except Exception:
        # A failing import still leaves the PATH probe and the Word fallback.
        resolved = shutil.which("soffice")
        try:
            from publication.docx_render_gate import find_word_renderer

            word = None if resolved else find_word_renderer()
        except Exception:
            word = None
    detail = (
        resolved
        or (f"Microsoft Word COM fallback: {word}" if word else "")
        or "no LibreOffice/soffice and no Word COM renderer"
    )
    return DoctorCheck(
        name="command:renderer",
        ok=bool(resolved or word),
        required=required,
        detail=detail,
    )


def _writable_directory_check(path: Path) -> DoctorCheck:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise OSError("path is not a regular directory")
        with tempfile.NamedTemporaryFile(dir=path, prefix=".doctor-", delete=True):
            pass
    except OSError as exc:
        return DoctorCheck("output:writable", False, True, str(exc))
    return DoctorCheck("output:writable", True, True, str(path.resolve()))


def run_doctor(
    *,
    config: Path | None = None,
    output_dir: Path | None = None,
    source: Path | None = None,
    require_render: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return a JSON-safe preflight report without exposing secret values."""

    environment = os.environ if environ is None else environ
    checks: list[DoctorCheck] = [
        DoctorCheck(
            "python:version",
            sys.version_info >= (3, 11),
            True,
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    ]
    checks.extend(
        _module_check(module, distribution, required=True)
        for module, distribution in CORE_IMPORTS.items()
    )
    checks.append(_render_backend_check(required=require_render))
    checks.append(_command_check("tesseract", required=False))

    required_commands: set[str] = set()
    credential_names: set[str] = set()
    if config is not None:
        try:
            profiles = load_pipeline_profiles(config)
            for profile in profiles.profiles.values():
                if profile.command:
                    required_commands.add(profile.command[0])
                if profile.credential_env:
                    credential_names.add(profile.credential_env)
            checks.append(
                DoctorCheck("config:profiles", True, True, str(config.resolve()))
            )
        except (OSError, ValueError) as exc:
            checks.append(DoctorCheck("config:profiles", False, True, str(exc)))
    for command in sorted(required_commands):
        checks.append(_command_check(command, required=True))
    for name in sorted(credential_names):
        present = bool(str(environment.get(name, "")).strip())
        checks.append(
            DoctorCheck(
                f"credential:{name}",
                present,
                True,
                "present" if present else "missing",
            )
        )
    if output_dir is not None:
        checks.append(_writable_directory_check(output_dir.expanduser().resolve()))
    if source is not None:
        resolved_source = source.expanduser().resolve()
        checks.append(
            DoctorCheck(
                "source:readable",
                resolved_source.is_file() and os.access(resolved_source, os.R_OK),
                True,
                str(resolved_source),
            )
        )

    failed_required = [check.name for check in checks if check.required and not check.ok]
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "status": "failed" if failed_required else "passed",
        "failed_required": failed_required,
        "checks": [asdict(check) for check in checks],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check translation-agent runtime dependencies before a long run."
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument(
        "--render", action="store_true", help="require a Word/PDF renderer"
    )
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    return parser


def _human_lines(report: Mapping[str, object]) -> Iterable[str]:
    yield f"translation-agent {report['app_version']} doctor: {report['status']}"
    for raw in report.get("checks", []):
        assert isinstance(raw, Mapping)
        marker = "PASS" if raw.get("ok") else ("FAIL" if raw.get("required") else "WARN")
        yield f"[{marker}] {raw.get('name')}: {raw.get('detail')}"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_doctor(
        config=args.config,
        output_dir=args.output_dir,
        source=args.source,
        require_render=args.render,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n".join(_human_lines(report)))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
