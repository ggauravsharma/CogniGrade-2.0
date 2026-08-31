# Cognigrade App - Automated Handwritten Exam Grading for Schools & College

## Demo Video

https://github.com/user-attachments/assets/6c9ccf06-23de-4910-9352-0979f76ba63b

See complete video at - [Link](https://drive.google.com/file/d/1WCD6oESae6zC9oE82pmh4F-x6Ft0ZHmp/view?usp=sharing)

## More Details
Techstack - Python FastAPI, PostgreSQL 15, JavaScript, HTML, CSS, Celery, Rabbit MQ

OS - Windows / WSL

Look at .env.template for how to create the .env file:
1) Create a .env file in root folder with the filled details (GEMINI_API_KEY, GOOGLE_CLIENT_ID and Secret...)
2) Make sure you have Docker Installed in your system
 
How to run:

1) cd path/to/root_directory
2) docker compose up

Go to URL http://localhost/ (or as indicated in command line)

## Database schema

Alembic is the schema authority (`backend/alembic.ini`, `backend/migrations/`).
The application no longer alters an existing database on startup: it creates the
schema only when the database is completely empty, and stamps it at the current
Alembic revision so both mechanisms agree.

Migrations read `DATABASE_URL` from the same place the app does, so no
credentials live in `alembic.ini`. Set `ALEMBIC_DATABASE_URL` to point a
migration run at a different database.

### A new (empty) database

```
alembic -c backend/alembic.ini upgrade head
```

Starting the app against an empty database also works — it creates the schema
and stamps it — but running the migrations explicitly is the documented path.

### An existing database created before Alembic

Databases built by earlier releases were created by
`Base.metadata.create_all()` and have no `alembic_version` table. Adopt the
migration history once, in this order:

```
# 1. BACK UP THE DATABASE FIRST. Step 3 rewrites columns in place.
pg_dump -Fc "$DATABASE_URL" > cognigrade-backup.dump

# 2. Record the schema you already have. 0001 describes exactly what
#    create_all produced, so this asserts nothing untrue and applies no DDL.
alembic -c backend/alembic.ini stamp 0001

# 3. Apply everything since. Only 0002 actually runs.
alembic -c backend/alembic.ini upgrade head
```

Run `stamp 0001` **only** on a database that already has the tables. On an
empty database it would skip their creation and leave the schema missing.

To preview the exact statements before running them:

```
alembic -c backend/alembic.ini upgrade head --sql
```

### Tests

```
.venv-test/Scripts/python.exe -m pytest -q
```
