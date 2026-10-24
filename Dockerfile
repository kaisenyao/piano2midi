# CPU image for the HTTP API. Build:  docker build -t piano2midi .
# Run:    docker run -p 8000:8000 piano2midi
# Use:    curl -F file=@song.mp3 http://localhost:8000/transcribe -o song.mid
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# CPU-only PyTorch keeps the image small; pass --build-arg TORCH_INDEX=... for CUDA builds
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch torchaudio --index-url ${TORCH_INDEX}

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[api]"

# load the model once at build time so the first request is not slow and nothing is downloaded at run time
RUN python -c "from piano2midi.engine import TranskunEngine; TranskunEngine()"

EXPOSE 8000
CMD ["uvicorn", "piano2midi.api:app", "--host", "0.0.0.0", "--port", "8000"]
