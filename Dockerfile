# Compliance Copilot: Streamlit dashboard + Q&A agent + search index, for Cloud Run.
# The dashboard runs in live mode with the Q&A agent plugged in
# (COPILOT_BACKEND, COPILOT_QA_MODULE below).
#
# The search index (chroma_db/, data/processed/, data/frameworks/) is gitignored,
# so it's built here from the repo's policy PDFs and the pinned NIST catalogs.
# Building needs internet (NIST catalogs, embedding model) but no API key.
#
# Local test:
#   docker build -t compliance-copilot .
#   docker run -p 8080:8080 -e ANTHROPIC_API_KEY=... compliance-copilot
#
# Cloud Run: give the service 4 GiB memory, pass ANTHROPIC_API_KEY from
# Secret Manager, and for the demo use max instances 1 + session affinity
# (review decisions live in the running instance's memory).

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface \
    PORT=8080 \
    COPILOT_BACKEND=live \
    COPILOT_QA_MODULE=qa_agent.agent:ask

WORKDIR /app

# CPU-only PyTorch first, at the version constraints-phase1.txt pins: the
# default Linux wheel bundles CUDA (several GB) that Cloud Run can't use.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.1

# requirements.txt applies constraints-phase1.txt. LangGraph runs the Phase 2
# pipeline that the dashboard's live mode calls.
COPY requirements.txt constraints-phase1.txt ./
RUN pip install -r requirements.txt langgraph

COPY . .
RUN if [ -f phase3_dashboard/requirements.txt ]; then \
        pip install -r phase3_dashboard/requirements.txt -c constraints-phase1.txt; fi

# Build the search index (policies, NIST SP 800-53 + CSF, synthetic evidence),
# then check it. The embedding model is cached under HF_HOME for runtime,
# where the retriever loads it with local_files_only.
RUN python phase1_ingestion/parse_pdfs.py \
 && python phase1_ingestion/chunk_policies.py \
 && python phase1_ingestion/download_frameworks.py \
 && python phase1_ingestion/load_frameworks.py \
 && python -m phase1_ingestion.load_environment \
 && python -m phase1_ingestion.load_inventory \
 && python phase1_ingestion/chunk_and_embed.py \
 && python -m phase1_ingestion.check_retriever

# Run as a non-root user that can still write saved reports and the chat log.
RUN useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 8080
CMD streamlit run phase3_dashboard/app.py \
    --server.port=${PORT} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
