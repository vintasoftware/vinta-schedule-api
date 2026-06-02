FROM python:3.13-slim AS base
ENV PYTHONFAULTHANDLER=1 \
  PYTHONUNBUFFERED=1 \
  PYTHONHASHSEED=random \
  UV_COMPILE_BYTECODE=1 \
  UV_LINK_MODE=copy \
  UV_PROJECT_ENVIRONMENT=/home/user/app/.venv

# Install system dependencies
RUN apt-get update && apt-get install python3-dev gcc build-essential libpq-dev git curl sudo -y

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Create user first
RUN groupadd user && useradd --create-home --home-dir /home/user -g user user
RUN usermod -aG sudo user
RUN echo 'user ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Create directories and set permissions
RUN mkdir -p /home/user/.local/bin
RUN mkdir -p /home/user/app
RUN chown user:user -Rf /home/user

# Switch to user for the remaining operations
USER user

# install python dependencies
COPY --chown=user:user pyproject.toml uv.lock /home/user/app/

WORKDIR /home/user/app/
RUN uv sync --frozen --no-install-project --no-dev

# Set the PATH to include the virtual environment
ENV PATH="/home/user/app/.venv/bin:$PATH"

# -----------------------------------------------------------------------------

FROM python:3.13-slim AS final

RUN groupadd user && useradd --create-home --home-dir /home/user -g user user
USER user

WORKDIR /home/user/app/

# Copy the virtual environment
COPY --chown=user:user --from=base /home/user/app/.venv /home/user/app/.venv

# Copy the source code
COPY --chown=user:user . /home/user/app/

# Set the PATH to include the virtual environment
ENV PATH="/home/user/app/.venv/bin:$PATH"

# Run the app
CMD gunicorn vinta_schedule_api.wsgi --log-file - -b 0.0.0.0:8000 --reload
