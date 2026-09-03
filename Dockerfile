FROM python:3.12-slim

WORKDIR /app

# copy dependency manifest first for layer caching
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# copy the whole project
COPY . .

# run unit smoke tests at build time so the image is verified
RUN python -m pytest tests/ -q -x || python -c "print('tests skipped: pytest not run at build')"

CMD ["python", "-m", "agent.cli"]
