FROM python:3.12-slim

# Unbuffered stdout so print() shows up immediately in `docker compose logs`
# / `docker compose run` output instead of being batched, which otherwise
# can look like the process is "stuck" when it isn't.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
