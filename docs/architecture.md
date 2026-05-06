# Linked Jobs Monitor Architecture

## Goals

The app is a local job-search tracker for LinkedIn Jobs. It should help with three workflows:

1. Collect LinkedIn jobs from user-managed search setups or pasted/saved HTML.
2. Preserve LinkedIn-provided job facts such as title, company, location, posting date, deadline, and employment type when those facts are available.
3. Track the user's workflow state: new, saved, not interested, applied date, application status, and notes.

## Important Constraints

- The app does not store LinkedIn login cookies.
- A manual LinkedIn URL can require a logged-in browser session. When LinkedIn returns a sign-in page to the local fetcher, the app should not try to bypass it. The user can open that URL in their browser and paste/import the saved HTML.
- LinkedIn public pages sometimes expose search cards without job details. Deadline and richer metadata usually come from public job-detail pages via JSON-LD, or from pasted detail-page HTML.

## Components

- `linkedin.py`: builds LinkedIn search URLs from saved search setup values.
- `fetch.py`: makes polite public-page HTTP requests.
- `parser.py`: extracts normalized `JobListing` records from LinkedIn search-card HTML and job-detail JSON-LD.
- `database.py`: owns the SQLite database, schema migrations, JSON-state migration, upserts, and user-state updates.
- `web.py`: local-only HTTP interface for search setup management, refresh, review, saving, hiding, applying, and importing HTML.
- `cli.py`: command entrypoint for URL generation, refresh/check, HTML import, migration, and web serving.

## Data Model

One SQLite database lives at `.job_state/jobs.sqlite3`. It has two main tables:

- `search_sources`: editable search setups used by refresh. Each source stores a display name, keywords, location, LinkedIn `geo_id`, radius, posted-within window, sort mode, active/paused state, optional manual LinkedIn URL, and last-run time.
- `jobs`: LinkedIn jobs plus local application tracking state.
- `job_sources`: source provenance linking one LinkedIn job to each search setup/keyword URL that found it.

`config.ini` seeds the first `search_sources` row when the table is empty. After that, the web UI is the normal place to add, edit, pause, refresh, or remove search setups.

Core LinkedIn fields:

- `job_id`
- `linkedin_url`
- `title`
- `company`
- `company_url`
- `location`
- `posted_at`
- `posted_text`
- `application_deadline`
- `employment_type`
- `seniority_level`
- `job_function`
- `industries`
- `applicants`
- `description`
- `source_keyword`
- `source_url`
- `first_seen_at`
- `last_seen_at`
- `details_fetched_at`

User tracking fields:

- `user_status`: `new`, `saved`, `not_interested`, `applied`, `archived`
- `application_status`
- `applied_at`
- `notes`

`not_interested` jobs are hidden from the normal interface but remain in the database, so future imports do not re-add them as new jobs.

Deleting a search setup leaves jobs in place. Withdrawing a setup removes only new jobs that are exclusive to that setup, then detaches the setup from any saved, applied, or shared jobs.

## Test Plan

Tests follow the behavior first:

- Search-card parsing extracts the real trailing LinkedIn job ID from slug URLs.
- Search-card parsing captures title, company, location, posting date, and source keyword.
- Detail-page JSON-LD parsing captures posting date, deadline, employment type, company, location, and description.
- Database upsert is idempotent and preserves user status when a job reappears.
- Not-interested jobs stay hidden and are not reintroduced by future imports.
- Applied jobs store `applied_at`, `application_status`, and notes.
- Web rendering hides not-interested jobs and displays saved/applied job metadata.
- Search setup seeding happens once, and user-added setups are preserved across app restarts.
- Search setup pause/delete/update operations affect refresh behavior without deleting tracked jobs.
