FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

# Los logs salen al instante en el panel de Render.
ENV PYTHONUNBUFFERED=1
CMD ["python", "worker.py"]
