FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
# FFmpeg：从原片提取音频（以后也用来渲染粗剪视频）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl bzip2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# 语音转写模型（SenseVoice，约 230 MB）。不需要服务器转写时：docker compose build --build-arg ASR=0
ARG ASR=1
ENV ASR_MODEL_DIR=/models/sense-voice
RUN if [ "$ASR" = "1" ]; then mkdir -p /models && cd /models \
    && curl -fsSL https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2 \
       | tar xj && mv sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 sense-voice && rm -rf sense-voice/test_wavs; fi
COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
