"""PDF export for the digest candidate panel.

Renders one card per candidate using a Jinja2 template and converts
the resulting HTML to PDF via WeasyPrint.  Requires WeasyPrint system
libraries (pango, cairo) to be installed on the host.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from hh_monitor.digest.query import CandidateRow

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _build_jinja_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def export_pdf(
    candidates: list[CandidateRow],
    output_path: Path,
    search_code: str = "",
) -> None:
    """Render *candidates* to a PDF file at *output_path*.

    Args:
        candidates:   List of :class:`~hh_monitor.digest.query.CandidateRow`.
        output_path:  Destination file (created / overwritten).
        search_code:  Shown in card footer for identification.

    Raises:
        ImportError: if WeasyPrint is not installed.
    """
    try:
        from weasyprint import HTML  # type: ignore[import-untyped]  # local import — optional dep
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "WeasyPrint is required for PDF export.  Install it with: poetry add weasyprint"
        ) from exc

    env = _build_jinja_env()
    template = env.get_template("card.html")
    html_str = template.render(candidates=candidates, search_code=search_code)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_str, base_url=str(_TEMPLATES_DIR)).write_pdf(str(output_path))
