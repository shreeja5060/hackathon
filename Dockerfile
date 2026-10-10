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
# Secret Manager, and use min and max instances 1 + session affinity.
# Reviews are kept in the ledger (data/ledger*.sqlite3). On Cloud Run the
# container's disk is lost when an instance stops, so set
# LEDGER_REPLICA_URL=gs://BUCKET/ledger: deploy/start.sh then restores the
# ledger at start-up and Litestream streams every change to Cloud Storage.
# Step-by-step commands (bucket, permissions, sign-in): deploy/README.md.
# Vertex AI later (project quota is 0 for now): set CLAUDE_BACKEND=vertex,
# ANTHROPIC_VERTEX_PROJECT_ID and CLOUD_ML_REGION=global on the service, give
# its service account the Vertex AI User role, and unset CHAT_MODEL.

FROM python:3.12-slim

# CHAT_MODEL: the deployed demo's chat runs on Sonnet (the local default is
# Haiku). It's an Anthropic API model name; unset it if the service moves to
# CLAUDE_BACKEND=vertex, where the chat then uses the agents' enabled model.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface \
    PORT=8080 \
    COPILOT_BACKEND=live \
    COPILOT_QA_MODULE=qa_agent.agent:ask \
    CHAT_MODEL=claude-sonnet-5-5

WORKDIR /app

# Litestream (keeps the ledger safe on Cloud Run, see deploy/litestream.yml):
# a pinned release, checked against the SHA-256 the project publishes.
ARG LITESTREAM_VERSION=0.5.17
ARG LITESTREAM_SHA256=cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-x86_64.tar.gz /tmp/litestream.tar.gz
RUN echo "${LITESTREAM_SHA256}  /tmp/litestream.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/litestream.tar.gz -C /usr/local/bin litestream \
 && rm /tmp/litestream.tar.gz

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
# The evidence loaders run only once their Phase 1 branches are on main.
RUN python phase1_ingestion/parse_pdfs.py \
 && python phase1_ingestion/chunk_policies.py \
 && python phase1_ingestion/download_frameworks.py \
 && python phase1_ingestion/load_frameworks.py \
 && for loader in load_environment load_inventory; do \
        if [ -f "phase1_ingestion/$loader.py" ]; then \
            python -m "phase1_ingestion.$loader" || exit 1; \
        fi; \
    done \
 && python phase1_ingestion/chunk_and_embed.py \
 && python -m phase1_ingestion.check_retriever

# Run as a non-root user that can still write the ledger, saved documents and the chat log.
RUN chmod +x /app/deploy/start.sh \
 && useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8080
# deploy/start.sh starts the dashboard from its folder (so .streamlit/config.toml:
# theme, fonts, 10 MB upload limit, hidden error details, are used), restoring and
# replicating the ledger first when LEDGER_REPLICA_URL is set.
WORKDIR /app/phase3_dashboard
CMD ["/app/deploy/start.sh"]
