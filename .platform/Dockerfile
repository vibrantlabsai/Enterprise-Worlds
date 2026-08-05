# Build context is the REPOSITORY ROOT, not .platform/ — the runner computes the context as the
# parent of the .platform directory, so COPY paths are written from the repo root.
FROM python:3.12-slim

WORKDIR /app

# The whole repo, data/ included: the seeds, fk_spec.json and policy.md live there and the server
# cannot answer anything without them. What must not be copied (a local .env, virtualenvs, caches)
# is excluded by .dockerignore rather than by enumerating COPY paths here.
COPY . /app

# Editable install: the gym resolves data/ relative to its own source tree (src/ layout), so the
# package must keep running from /app rather than be copied into site-packages.
RUN pip install --no-cache-dir -e /app

# The build actually answering, reported by gym.initialize / gym.describe as gymCommitSha. A runner
# that knows the pinned revision can bake it in (--build-arg GYM_COMMIT_SHA=...); without it, and
# without a .git directory in the context, the field is simply absent — the contract allows that.
ARG GYM_COMMIT_SHA=""
ENV GYM_COMMIT_SHA=${GYM_COMMIT_SHA}

ENTRYPOINT ["eworlds", "serve"]
