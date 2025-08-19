FROM python:13-slim AS base
ENV PYTHONFAULTHANDLER=1 \
  PYTHONUNBUFFERED=1 \
  PYTHONHASHSEED=random \
  PIP_NO_CACHE_DIR=off \
  PIP_DISABLE_PIP_VERSION_CHECK=on \
  POETRY_VIRTUALENVS_IN_PROJECT=true \
  PIP_DEFAULT_TIMEOUT=100 \
  POETRY_VERSION=2.1.3

# Install system dependencies
RUN apt-get update && apt-get install python3-dev gcc build-essential libpq-dev pipx git -y

RUN groupadd user && useradd --create-home --home-dir /home/user -g user user

RUN mkdir -p /home/user/app

RUN chown user:user -Rf /home/user/app

# install python dependencies
COPY --chown=user:user pyproject.toml /home/user/app/
COPY --chown=user:user *poetry.lock /home/user/app/

USER user

# Install poetry
RUN pipx install poetry --python $(which python)
ENV PATH="/home/user/.local/pipx/venvs/poetry/bin:$PATH"

RUN rm -rf $(poetry env info --path)

WORKDIR /home/user/app/
RUN poetry install --no-root --no-interaction --no-ansi --with=dev
ENV PATH="/home/user/app/.venv/bin:$PATH"
ENV PYTHONPATH="/home/user/app/.venv/bin"

# -----------------------------------------------------------------------------

FROM python:3.13-slim as final

RUN groupadd user && useradd --create-home --home-dir /home/user -g user user
USER user

WORKDIR /home/user/app/

# Copy the virtual environment
COPY --chown=user:user --from=base /home/user/venv /home/user/venv

# Copy the source code
COPY --chown=user:user . /home/user/app/

# Set the PATH to include the virtual environment
ENV PATH="/home/user/app/.venv/bin:$PATH"
ENV PYTHONPATH="/home/user/app/.venv/bin"

# Run the app
CMD poetry run gunicorn vinta_schedule_api.wsgi --log-file - -b 0.0.0.0:8000 --reload
