from __future__ import annotations

from .models import ParsingProfile, ProfileSettings


def get_profile(profile: ParsingProfile | str) -> ProfileSettings:
    selected = profile if isinstance(profile, ParsingProfile) else ParsingProfile(profile)

    if selected is ParsingProfile.SCANNED:
        return ProfileSettings(
            name=selected,
            force_full_page_ocr=True,
        )

    if selected is ParsingProfile.VISUAL:
        return ProfileSettings(
            name=selected,
            do_picture_classification=True,
            do_chart_extraction=True,
        )

    return ProfileSettings(name=ParsingProfile.STANDARD)


def build_pdf_pipeline_options(settings: ProfileSettings):
    """Build Docling options lazily so lightweight unit tests need no models."""
    from docling.datamodel.pipeline_options import (
        HeadingHierarchyOptions,
        OcrAutoOptions,
        PdfPipelineOptions,
        TableFormerMode,
    )

    options = PdfPipelineOptions()
    options.do_ocr = settings.do_ocr
    options.ocr_options = OcrAutoOptions(
        force_full_page_ocr=settings.force_full_page_ocr,
    )
    options.do_table_structure = settings.do_table_structure
    options.table_structure_options.mode = TableFormerMode(settings.table_mode)
    options.table_structure_options.do_cell_matching = settings.do_cell_matching
    options.heading_hierarchy_options = HeadingHierarchyOptions(
        enabled=settings.heading_hierarchy,
        use_bookmarks=True,
        use_numbering=True,
        use_style=True,
        max_level=6,
    )
    options.generate_parsed_pages = settings.generate_parsed_pages
    options.generate_page_images = settings.generate_page_images
    options.generate_picture_images = settings.generate_picture_images
    options.images_scale = settings.image_scale
    options.do_picture_classification = settings.do_picture_classification
    options.do_chart_extraction = settings.do_chart_extraction
    options.do_picture_description = settings.do_picture_description
    options.do_code_enrichment = settings.do_code_enrichment
    options.do_formula_enrichment = settings.do_formula_enrichment
    options.enable_remote_services = settings.enable_remote_services
    options.allow_external_plugins = settings.allow_external_plugins
    return options

