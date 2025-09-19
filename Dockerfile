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
RUN apt-get update && apt-get install python3-dev gcc build-essential libpq-dev git curl sudo -y

# Create user first
RUN groupadd user && useradd --create-home --home-dir /home/user -g user user
RUN usermod -aG sudo user
RUN echo 'user ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Create directories and set permissions
RUN mkdir -p /home/user/.local/bin
RUN mkdir -p /home/user/.local/poetry
RUN mkdir -p /home/user/app
RUN chown user:user -Rf /home/user

# Switch to user for the remaining operations
USER user

# Install poetry as user
# use official curl to respect $POETRY_VERSION & $POETRY_HOME
# (pip install poetry doesn't respect $POETRY_HOME)
RUN curl -sSL https://install.python-poetry.org | python3 -
ENV PATH="$POETRY_HOME/bin:$PATH"

# install python dependencies
COPY --chown=user:user pyproject.toml /home/user/app/
COPY --chown=user:user *poetry.lock /home/user/app/

WORKDIR /home/user/app/
RUN poetry install --no-root --no-interaction --no-ansi --with=dev -v

# Set the PATH to include the virtual environment
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
