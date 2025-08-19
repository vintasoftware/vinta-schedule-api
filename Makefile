SHELL := /bin/bash # Use bash syntax
ARG := $(word 2, $(MAKECMDGOALS) )

clean:
	@find . -name "*.pyc" -exec rm -rf {} \;
	@find . -name "__pycache__" -delete

# Commands for Docker version
setup:
	docker volume create vinta_schedule_api_dbdata
	docker volume create vinta_schedule_api_localstack_data
	docker volume create vinta_schedule_api_virtualenv
	docker compose build api
	docker compose run --rm api python manage.py spectacular --color --file schema.yml
	./scripts/init_localstack.sh

test:
	docker compose run --rm api python -m pytest $(ARG) -vs -n auto --no-header --reuse-db

test_reset:
	docker compose run --rm api python -m pytest $(ARG) -vs  -n auto --no-header

test_seq:
	docker compose run --rm api python -m pytest $(ARG) -vs --no-header --reuse-db

test_seq_reset:
	docker compose run --rm api python -m pytest $(ARG) -vs --no-header

test_cov:
	docker compose run --rm api python -m pytest $(ARG) -vs  -n auto --no-header --cov=. --cov-report=html:junit/test-results.html

up:
	docker compose up --remove-orphans -d api

up_with_workers:
	docker compose up -d

update_deps:
	docker compose run --rm api poetry install --no-root --no-interaction --no-ansi --with=dev

down:
	docker compose down

restart:
	docker compose restart api

logs:
	docker compose logs -f $(ARG)

manage:
	docker compose run --rm api python manage.py $(ARG)

makemigrations:
	docker compose run --rm api python manage.py makemigrations

migrate:
	docker compose run --rm api python manage.py migrate

bash:
	docker compose run --rm api bash

shell:
	docker compose run --rm api python manage.py shell

root_bash:
	docker compose run --user=root --rm api bash

update_schema:
	docker compose run --rm api python manage.py spectacular --color --file schema.yml

attach:
	# attach to the running api container to an interactive shell so we can debug using breakpoints
	docker compose attach api
