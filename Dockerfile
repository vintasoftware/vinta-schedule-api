FROM python:3.14-slim AS base
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

FROM python:3.14-slim AS final

# Runtime shared libraries the wheels link against: libpq for psycopg, libcurl for
# pycurl (kombu's SQS transport). The compilers that built them stay behind in the
# `base` stage.
RUN apt-get update \
  && apt-get install --no-install-recommends -y libpq5 libcurl4 \
  && apt-get clean \
  && find /var/lib/apt/lists -mindepth 1 -delete

# ENV does not cross a FROM, so this repeats what the base stage sets. Unbuffered
# stdout is what makes logs reach CloudWatch as they happen rather than in bursts
# when a buffer fills.
ENV PYTHONFAULTHANDLER=1 \
  PYTHONUNBUFFERED=1 \
  PYTHONHASHSEED=random

RUN groupadd user && useradd --create-home --home-dir /home/user -g user user
USER user

WORKDIR /home/user/app/

# Copy the virtual environment
COPY --chown=user:user --from=base /home/user/app/.venv /home/user/app/.venv

# Copy the source code
COPY --chown=user:user . /home/user/app/

# Set the PATH to include the virtual environment
ENV PATH="/home/user/app/.venv/bin:$PATH"

EXPOSE 8000

# One image, four roles: the ECS task definitions override `command` for the
# worker, the beat scheduler and the release (migrate + collectstatic) task. This
# default is the web role.
#
# `--limit-request-line 8188` matches the load balancer's own header allowance;
# gunicorn's default 4094 rejects the long signed URLs this API hands out.
CMD ["gunicorn", "vinta_schedule_api.wsgi:application", \
  "--bind", "0.0.0.0:8000", \
  "--limit-request-line", "8188", \
  "--access-logfile", "-", \
  "--error-logfile", "-"]
