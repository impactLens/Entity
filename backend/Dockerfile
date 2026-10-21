FROM python:3.11-slim

# WEB_CONCURRENCY = worker processes (each loads its own models, ~0.5-1 GB).
# Render free (512 MB): keep 1 and consider ENABLE_NLTK=0.
# Cloud Run with 2 GiB+: set WEB_CONCURRENCY=2.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NLTK_DATA=/opt/nltk_data \
    SPACY_MODEL=en_core_web_sm \
    WEB_CONCURRENCY=1 \
    PORT=10000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m spacy download ${SPACY_MODEL} \
 && python -m nltk.downloader -d ${NLTK_DATA} \
      punkt_tab averaged_perceptron_tagger_eng maxent_ne_chunker_tab words stopwords

COPY ner_core.py app.py ./

RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 10000
# uvicorn's supervisor respawns a worker if it dies; workers recycle after
# 1000 requests to cap memory growth.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-10000} --workers ${WEB_CONCURRENCY:-1} --limit-max-requests 1000 --timeout-graceful-shutdown 20"]
