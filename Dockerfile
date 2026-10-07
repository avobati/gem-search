FROM python:3.12-slim
WORKDIR /app
COPY requirements-radar.txt .
RUN pip install --no-cache-dir -r requirements-radar.txt && useradd --create-home radar
COPY . .
RUN mkdir -p /app/data && chown -R radar:radar /app
USER radar
ENV PYTHONUNBUFFERED=1
EXPOSE 8790
CMD ["python", "-m", "radar.server", "--host", "0.0.0.0", "--with-worker"]
