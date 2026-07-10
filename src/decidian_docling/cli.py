from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from .models import ArtifactMode, HarnessError, ParsingProfile, RunStatus
from .parity import compare_run_parity
from .parser import DEFAULT_OUTPUT_DIR, parse_document
from .validation import ALLOWED_EXTENSIONS

app = typer.Typer(
    name="decidian-docling",
    help="Parse local documents with Docling and preserve inspectable artifacts.",
    no_args_is_help=True,
)

ProfileOption = Annotated[
    ParsingProfile,
    typer.Option(
        "--profile",
        "-p",
        help="Parsing profile: standard, scanned, or visual.",
        case_sensitive=False,
    ),
]
OutputOption = Annotated[
    Path,
    typer.Option(
        "--output",
        "-o",
        help="Root directory where immutable run folders are created.",
        file_okay=False,
        dir_okay=True,
    ),
]
ArtifactModeOption = Annotated[
    ArtifactMode,
    typer.Option(
        "--artifact-mode",
        "-a",
        help="Artifact output mode: full or extraction.",
        case_sensitive=False,
    ),
]


def _run_one(
    input_file: Path,
    profile: ParsingProfile,
    output: Path,
    artifact_mode: ArtifactMode,
) -> bool:
    try:
        result = parse_document(
            input_file,
            profile=profile,
            output_root=output,
            artifact_mode=artifact_mode,
        )
    except HarnessError as exc:
        typer.secho(f"ERROR: {exc}", fg=typer.colors.RED, err=True)
        return False

    color = (
        typer.colors.GREEN
        if result.status is RunStatus.SUCCESS
        else typer.colors.YELLOW
        if result.status is RunStatus.PARTIAL_SUCCESS
        else typer.colors.RED
    )
    typer.secho(
        f"{result.status.value.upper()}: {input_file.name}",
        fg=color,
        bold=True,
    )
    typer.echo(f"Artifacts: {result.run_dir}")
    if result.archive_path.exists():
        typer.echo(f"Archive:   {result.archive_path}")
    else:
        typer.echo("Archive:   skipped")
    for warning in result.warnings:
        typer.secho(f"Warning: {warning}", fg=typer.colors.YELLOW)
    return result.status is not RunStatus.FAILED


@app.command("parse")
def parse_command(
    input_file: Annotated[
        Path,
        typer.Argument(
            help="Local document to parse.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
    profile: ProfileOption = ParsingProfile.STANDARD,
    output: OutputOption = DEFAULT_OUTPUT_DIR,
    artifact_mode: ArtifactModeOption = ArtifactMode.FULL,
) -> None:
    """Parse one document and export all inspection artifacts."""
    if not _run_one(input_file, profile, output, artifact_mode):
        raise typer.Exit(code=1)


@app.command("batch")
def batch_command(
    input_dir: Annotated[
        Path,
        typer.Argument(
            help="Directory containing local documents.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ],
    profile: ProfileOption = ParsingProfile.STANDARD,
    output: OutputOption = DEFAULT_OUTPUT_DIR,
    artifact_mode: ArtifactModeOption = ArtifactMode.FULL,
) -> None:
    """Parse supported files in one directory sequentially."""
    files = sorted(
        path
        for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() in ALLOWED_EXTENSIONS
    )
    if not files:
        typer.secho(
            "No supported documents found in the input directory.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)

    failures = 0
    for input_file in files:
        if not _run_one(input_file, profile, output, artifact_mode):
            failures += 1

    typer.echo(
        f"Completed {len(files)} file(s): "
        f"{len(files) - failures} passed, {failures} failed."
    )
    if failures:
        raise typer.Exit(code=1)


@app.command("compare")
def compare_command(
    first_run: Annotated[
        Path,
        typer.Argument(
            help="First immutable run directory.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ],
    second_run: Annotated[
        Path,
        typer.Argument(
            help="Second immutable run directory.",
            exists=True,
            file_okay=False,
            dir_okay=True,
            readable=True,
        ),
    ],
) -> None:
    """Byte-compare extraction feed files from two run directories."""
    result = compare_run_parity(first_run, second_run)
    typer.echo(json.dumps(result, indent=2))
    if not result["ok"]:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
