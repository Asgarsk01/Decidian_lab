FROM python:3.11-slim-bookworm AS runtime

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    HF_HOME=/models/huggingface \
    TORCH_HOME=/models/torch \
    DOCLING_CACHE_DIR=/models/docling \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libgl1 \
        libglib2.0-0 \
        libmagic1 \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# RapidOCR stores its first-run weights beside the package. Fetch them while
# the image is still running as root; the runtime user intentionally cannot
# mutate site-packages.
RUN python -c "from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR; RapidOCR(params={'Det.engine_type':EngineType.TORCH,'Cls.engine_type':EngineType.TORCH,'Rec.engine_type':EngineType.TORCH,'Det.ocr_version':OCRVersion.PPOCRV4,'Cls.ocr_version':OCRVersion.PPOCRV4,'Rec.ocr_version':OCRVersion.PPOCRV4,'Det.model_type':ModelType.MOBILE,'Cls.model_type':ModelType.MOBILE,'Rec.model_type':ModelType.MOBILE})"

COPY README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev

RUN mkdir -p /data/input /data/output /data/work /models \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data /models

USER appuser

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl --fail http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "src/decidian_docling/ui.py", \
     "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.maxUploadSize=100", "--browser.gatherUsageStats=false"]

FROM runtime AS test

USER root
COPY tests ./tests
RUN uv sync --frozen --extra dev
ENV RUN_DOCLING_INTEGRATION=1 \
    DECIDIAN_OUTPUT_DIR=/tmp/decidian-output
USER appuser
CMD ["pytest"]
