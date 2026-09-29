FROM python:3.12-slim

# Set environment variables to prevent python from writing pyc files to disk and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /src

# Copy packaging files
COPY pyproject.toml README.md ./

# Install the application
# (This utilizes the pyproject.toml configuration we updated earlier)
RUN pip install --no-cache-dir .

# Copy application source code and migrations
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

EXPOSE 8000

# The startup command is defined in docker-compose.yml to handle migrations first
