FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY meme_paper /app/meme_paper
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "meme_paper", "run"]
