from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .models import HarnessError, ParsingProfile, RunStatus
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
def _run_one(
    input_file: Path,
    profile: ParsingProfile,
    output: Path,
) -> bool:
    try:
        result = parse_document(
            input_file,
            profile=profile,
            output_root=output,
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
) -> None:
    """Parse one document and write the extraction artifact set."""
    if not _run_one(input_file, profile, output):
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
        if not _run_one(input_file, profile, output):
            failures += 1

    typer.echo(
        f"Completed {len(files)} file(s): "
        f"{len(files) - failures} passed, {failures} failed."
    )
    if failures:
        raise typer.Exit(code=1)
if __name__ == "__main__":
    app()
