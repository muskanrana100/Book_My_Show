# BookMySeat: run guide

Every command below is typed in a terminal. Each step says where to run it.

- **Project root** = the folder that contains `manage.py`. Run every `python manage.py ...`
  command from there. The database path is relative, so running from another folder creates a
  different database.
- Keep the terminal open for each running service: the web server, the Celery worker, and Redis
  each need their own terminal window.

---

## 0. Requirements (install once, outside the project)

| Tool | Version | Used for |
|---|---|---|
| Python | 3.10 to 3.13 (3.12 tested) | Runs the project |
| Redis | any recent (6 or 7) | Background email queue (Celery broker) |
| Docker (optional) | any | Easiest way to run Redis on Windows |
| ngrok (optional) | any | Public HTTPS address for the Razorpay webhook |

Check Python: run `python3 --version` (Linux/macOS) or `py -3.12 --version` (Windows).

---

## 1. Unzip the project

**Where:** the folder where you keep projects (for example `Desktop` or `Documents`).

- Linux/macOS: `unzip bookmyseat-final-v2.zip`
- Windows: right-click the zip and choose Extract All.

You now have a folder named `bookmyseat-final`. That is the **project root**.

**Where:** open a terminal inside the project root.

```bash
cd bookmyseat-final
ls            # Linux/macOS: you should see manage.py, requirements.txt, movies, users, templates
dir           # Windows PowerShell/CMD: same files
```

---

## 2. Create a virtual environment

**Where:** project root.

Linux/macOS:
```bash
python3 -m venv venv
```

Windows (PowerShell):
```powershell
py -3.12 -m venv venv
```

This creates a folder called `venv`. It is listed in `.gitignore`, so it will not be committed.

---

## 3. Activate the virtual environment

**Where:** project root. Do this in every new terminal before running any Python command.

Linux/macOS:
```bash
source venv/bin/activate
```

Windows (PowerShell):
```powershell
venv\Scripts\Activate.ps1
```
If PowerShell blocks the script, run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then try again.

Windows (CMD): `venv\Scripts\activate.bat`

When it works, `(venv)` appears at the start of the prompt.

---

## 4. Install the packages

**Where:** project root, venv active.

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

This installs the pinned versions: Django 5.2.17, Celery 5.6.3, Razorpay 1.4.2, reportlab, qrcode,
Pillow, whitenoise, redis, python-dotenv, dj-database-url, and gunicorn.

---

## 5. Create the `.env` file

**Where:** project root (same folder as `manage.py`).

The settings read this file automatically. Create a file named exactly `.env` (no other name, no
extension) with this content:

```
DJANGO_SECRET_KEY=change-this-to-a-long-random-string-of-at-least-50-characters
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost
SITE_BASE_URL=http://127.0.0.1:8000

# Payments (Razorpay TEST mode keys from the Razorpay dashboard). Leave empty to browse only.
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
RAZORPAY_WEBHOOK_SECRET=

# Email (Gmail address and a Google "app password"). Leave empty to skip real email.
EMAIL_HOST_USER=
EMAIL_HOST_PASSWORD=

# Celery (background jobs). The defaults already point at a local Redis.
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1
```

Notes:
- `DJANGO_SECRET_KEY` is required. The app does not start without it.
- `DJANGO_DEBUG=True` is needed on your own computer so that images and CSS load.
- Do not put this file in Git or share it. It is listed in `.gitignore`.

---

## 6. Check the project

**Where:** project root, venv active.

```bash
python manage.py check
```

Expected: `System check identified no issues (0 silenced).`

---

## 7. Set up the database

The zip already includes `db.sqlite3` with 100,000 bookings, 1,000 theaters (400 of them bookable),
and 50 users. It has **no admin account**, so you create one in step 8.

**Where:** project root, venv active.

```bash
python manage.py migrate
```

Expected: `No migrations to apply` or a list of migrations applied. Either is fine.

### Option A: use the included database (recommended)
Do nothing else in this step.

### Option B: start with an empty database and generate fresh demo data
This deletes the included database. Only do this if you want a clean start.

Linux/macOS:
```bash
rm db.sqlite3
python manage.py migrate
python manage.py seed_bookings --bookings 100000
```

Windows (PowerShell):
```powershell
Remove-Item db.sqlite3
python manage.py migrate
python manage.py seed_bookings --bookings 100000
```

Creating 100,000 bookings takes about 45 seconds. Use a smaller number (for example `--bookings 1000`)
for a faster test.

---

## 8. Create an admin account

**Where:** project root, venv active.

Interactive (you type a username, email and password):
```bash
python manage.py createsuperuser
```

Without typing (for the evaluation account):
```bash
DJANGO_SUPERUSER_USERNAME=evaluator DJANGO_SUPERUSER_EMAIL=evaluator@example.com DJANGO_SUPERUSER_PASSWORD='Choose-A-Strong-Password-2026' python manage.py createsuperuser --noinput
```
(Windows PowerShell: `$env:DJANGO_SUPERUSER_USERNAME="evaluator"` and similar lines for the other two
variables, then run `python manage.py createsuperuser --noinput`.)

Write down the username and password. The Task 4 report needs them.

---

## 8b. Demo titles, The Avengers, and reviews

**Where:** project root, venv active.

This command completes **The Avengers** (description, 143 minutes, UA certificate, genres, languages,
cast, two posters) and makes sure **five titles** have a bookable show with 100 free seats. It is safe
to run as many times as you like: nothing is duplicated.

```bash
python manage.py setup_demo_catalog
```

**Reviews need a watched booking.** The site only lets a user review a film after booking and watching
it, so the review test uses a booking on a show that has already happened. Do this:

1. Register an account at `/register/` (for example `reviewer`).
2. Run:
   ```bash
   python manage.py setup_demo_catalog --watched-user reviewer
   ```
3. Log in as `reviewer`, open The Avengers (`/movies/the-avengers/`), and write a review.
   You can then **edit** it (the form shows your review) and **delete** it (the Delete Review button).

**Trailer.** The shipped database has no trailer. Use a real YouTube video ID (the 11 characters after
`v=` in the video URL):

```bash
python manage.py setup_demo_catalog --trailer-id YOUR11CHARID
```

**Shows expire.** Bookable shows are dated a few days after the time you run the command. When they have
passed, run `python manage.py setup_demo_catalog` again to create new ones.

Bookable titles (with 100 free seats each): The Avengers and Demo Movie 1 to 4. To book one: log in, open
the movie, choose a theater, and select seats (each seat is held for 2 minutes). Without Razorpay test
keys in `.env`, payment shows "Payment service is unavailable" (see step 5).

---

## 8c. Movies, trailers, reviews and moderation (Task 1)

**Where:** project root, venv active.

**Existing database: apply the Task 1 migration once.** This adds the publish flag, moderation fields
and indexes. It changes no existing data.

```bash
python manage.py migrate
```

**Admin (manage everything):** log in at `/admin/` with a superuser account (step 8).
- *Movies*: name, slug, publish flag, release date, description, runtime (1 to 600 minutes),
  age certificate, genres, languages, primary poster, trailer, cast (inline), gallery posters (inline),
  shows (inline). Use the *Publish* and *Unpublish* actions to show or hide movies.
- *Trailer*: paste a YouTube link (`youtube.com/watch?v=...` or `youtu.be/...`) or the 11-character ID.
  Other links are refused.
- *Shows (Theaters)*: a show gets the standard 100-seat layout when created, so it can be booked.
  Two shows on the same screen may not overlap.
- *Reviews*: hide or restore a review. Hiding removes it from the page and from the average rating.
- *Review reports*: uphold a report (hides the review) or dismiss it (keeps the review).
  Each decision records who made it and when.

**Reviews need a completed booking.** A user can review a movie only after a paid booking for a show
that has finished. Create a demo watched booking with `python manage.py add_avengers --watch-user <username>`
(the account must already exist).

**Reporting.** Logged-in users report a review from the movie page (*Report this review*). A user
cannot report their own review, and each user can report a review once.

---

## 9. Collect static files (only for deployment)

**Where:** project root, venv active.

```bash
python manage.py collectstatic --noinput
```

With `DJANGO_DEBUG=True` locally this is not required, but running it confirms that the static files
(`enhance.css`, `enhance.js`, the placeholder poster) can be collected for production.

---

## 10. Run the tests

**Where:** project root, venv active.

```bash
python manage.py test movies
```

Expected: `Ran 143 tests` and `OK`. It takes about 2 to 3 minutes. The tests do not need Redis, Razorpay
or email; they replace those services with stand-ins.

---

## 11. Start Redis (needed for emails)

**Where:** a **new terminal window** (keep it open). Redis does not depend on the project.

Linux (Ubuntu/Debian):
```bash
sudo apt update && sudo apt install -y redis-server
redis-server
```

macOS (Homebrew):
```bash
brew install redis
brew services start redis
```

Windows (Docker Desktop):
```powershell
docker run -d --name redis -p 6379:6379 redis:7
```

Check that Redis is running (any OS, in another terminal):
```bash
redis-cli ping
```
Expected: `PONG`. If `redis-cli` is not installed on Windows, run `docker exec redis redis-cli ping`.

---

## 12. Start the Celery worker (sends ticket emails)

**Where:** a **new terminal window**, project root, venv active (activate it again in this window).

Linux/macOS:
```bash
celery -A bookmyseat worker --loglevel=info
```

Windows (PowerShell):
```powershell
celery -A bookmyseat worker --loglevel=info --pool=solo
```

Expected: a banner, then `movies.tasks.send_booking_ticket_email` in the task list, and
`celery@... ready.`. Leave this window open.

If it shows `Cannot connect to redis`, Redis (step 11) is not running.

---

## 13. Start the web server

**Where:** a **new terminal window** (or the first one), project root, venv active.

```bash
python manage.py runserver
```

Expected: `Starting development server at http://127.0.0.1:8000/`. Leave this window open.

If port 8000 is busy: `python manage.py runserver 8001`, then use `http://127.0.0.1:8001/`.

---

## 14. Open the site

Open a web browser and go to:

| Page | Address | Who can open it |
|---|---|---|
| Home | `http://127.0.0.1:8000/` | Everyone |
| Movies (search, filters, sort, pagination) | `http://127.0.0.1:8000/movies/` | Everyone |
| Movie details (trailer, reviews, similar, trending, recent) | `http://127.0.0.1:8000/movies/<slug>/` | Everyone (reviews need a login) |
| Theaters for a movie | `http://127.0.0.1:8000/movies/<movie-id>/theaters/` | Everyone |
| Seat selection | `http://127.0.0.1:8000/movies/theater/<theater-id>/seats/` | Logged-in users |
| Register | `http://127.0.0.1:8000/register/` | Everyone |
| Login | `http://127.0.0.1:8000/login/` | Everyone |
| Profile (bookings, payments, downloads) | `http://127.0.0.1:8000/profile/` | Logged-in users |
| Admin dashboard | `http://127.0.0.1:8000/movies/admin-dashboard/` | Staff accounts only |
| Dashboard CSV export | `http://127.0.0.1:8000/movies/admin-dashboard/export/` | Staff accounts only |
| Django admin (manage movies, theaters, shows, reviews) | `http://127.0.0.1:8000/admin/` | Superuser accounts |

Log out with the **Logout** button in the top navigation (it sends a form, not a link).

---

## 15. Try the main flows

1. **Register** a normal user at `/register/`, then **log in** at `/login/`.
2. Open a movie from `/movies/`. Its page shows the trailer, reviews, similar movies and recommendations.
3. Open **Theaters** for that movie and pick a show. The show must be in the future. (Shows in the
   past show "This show has already started".)
4. On the seat page, click seats. Each click holds the seat for 2 minutes (a countdown appears).
5. Continue to payment. This needs the Razorpay test keys in `.env` (step 5). Without them the
   payment step responds with "Payment service is unavailable right now". This is expected.
6. After a successful payment, the booking appears on `/profile/`, where you can download the PDF ticket.
7. To view the dashboard, log in with the admin account from step 8 and open
   `/movies/admin-dashboard/`. Use the date range fields and the export link to download a CSV.

---

## 15a. The Avengers: add, review, update, delete

**Where:** project root, venv active. The Avengers is already in the included database, so you only
need the first command to make sure it exists. The second command gives your account a past
booking for the film, which the site needs before it allows a review.

1. Register and log in on the site first (steps 14 and 15), so your username exists.
2. Run:

```bash
python manage.py add_avengers
python manage.py add_avengers --watch-user yourusername
```

Windows PowerShell: the same commands, run from `E:\bmt\bookmyseat-final` with the venv active.

The second command creates a past show and a booking for `yourusername`. The command is safe to run
more than once: it never creates duplicates.

3. Open `http://127.0.0.1:8000/movies/the-avengers/` and log in with that account.
4. In **Ratings & Reviews**:
   - **Add:** choose stars, write a title and text, click **Submit Review**.
   - **Update:** the form now shows **Update Review**. Change the text or stars and click it.
     The review then shows "(edited)".
   - **Delete:** click **Delete Review** and confirm.
5. Log in with another account that has no watch history: the page explains that reviews need a
   watched booking, and no review form appears.

## 16. Send pending ticket emails

**Where:** a **new terminal window**, project root, venv active.

Use this if emails were not sent because Redis or the worker was down when a booking was made.

```bash
python manage.py send_pending_ticket_emails --days 3
```

It re-queues successful payments from the last 3 days whose ticket email is still pending.
Run it only when the Redis and Celery worker terminals are running.

---

## 17. Stop everything

- Web server terminal: press `Ctrl+C`.
- Celery worker terminal: press `Ctrl+C`.
- Redis terminal: press `Ctrl+C` (or `docker stop redis` on Windows, `brew services stop redis` on macOS).
- Virtual environment: type `deactivate`.

---

## 18. Optional: real Razorpay payments on your computer

Razorpay must call your computer, so it needs a public HTTPS address.

1. Start ngrok in a new terminal: `ngrok http 8000`. Copy the `https://...ngrok-free.app` address.
2. In `.env`, set `SITE_BASE_URL=https://<your-ngrok-address>` and add
   `CSRF_TRUSTED_ORIGINS=https://<your-ngrok-address>`.
3. Restart the web server (step 13).
4. In the Razorpay dashboard (test mode), add a webhook:
   - URL: `https://<your-ngrok-address>/movies/payment/webhook/`
   - Secret: the same value you put in `RAZORPAY_WEBHOOK_SECRET`.
   - Events: `payment.captured` and `payment.failed`.

---

## 19. Deploying (Vercel or Netlify): what is still needed

The project is not ready to deploy as-is. Before you deploy:

1. **Database:** set `DATABASE_URL` to a hosted PostgreSQL database. SQLite cannot be written on
   Vercel. Run `python manage.py migrate` against it once.
2. **Media files:** posters, QR codes and PDF tickets must go to external storage (Cloudinary or S3).
3. **Worker:** Celery needs a separate host and a Redis broker (`CELERY_BROKER_URL`).
4. **Environment variables** on the host: `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=False`,
   `DJANGO_ALLOWED_HOSTS=<your-domain>`, `SITE_BASE_URL=https://<your-domain>`,
   `CSRF_TRUSTED_ORIGINS=https://<your-domain>`, the Razorpay and email variables, and `DATABASE_URL`.
5. Remove `db.sqlite3` from the repository before the first deploy (it is listed in `.gitignore`;
   if Git already tracks it, run `git rm --cached db.sqlite3`).

Vercel CLI, from the project root (after items 1 to 5):
```bash
npm install -g vercel
vercel login
vercel --prod
```

---

## 20. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `SECRET_KEY` error on start | `.env` is missing or not in the project root | Create `.env` in the folder that contains `manage.py` (step 5) |
| `No module named ...` | venv not active, or packages not installed | Activate venv (step 3), then run step 4 |
| Pages load without CSS or images | `DJANGO_DEBUG` is not `True` | Set `DJANGO_DEBUG=True` in `.env` and restart the server |
| `DisallowedHost` error | The address you use is not listed | Add it to `DJANGO_ALLOWED_HOSTS` in `.env` |
| "This show has already started" | You picked a past show | Choose a show dated after today |
| Payment responds 503 | Razorpay keys are missing, or the computer is offline | Add test keys (step 5) and check the internet connection |
| Email not sent | Redis, the worker or the email login is wrong | Start Redis (step 11) and the worker (step 12), check `EMAIL_*` in `.env`, then run step 16 |
| `Cannot connect to redis` in the worker | Redis is not running | Start Redis (step 11) |
| Celery worker crashes on Windows | Default pool does not work on Windows | Use `--pool=solo` (step 12) |
| CSRF error on a form when using ngrok | The ngrok address is not trusted | Add `CSRF_TRUSTED_ORIGINS` (step 18) |
| Port 8000 already in use | Another program uses it | `python manage.py runserver 8001` |
| `Ran 143 tests` takes long | Normal | Wait 2 to 3 minutes |
| "No user named 'reviewer'" | The watched-user account does not exist yet | Register the account first (step 8b) |
| Review form not shown | The show you watched is not in the past, or it is not your booking | Run `setup_demo_catalog --watched-user <username>` (step 8b) |
| Seats show as sold out | Every seat in that show is booked | Pick another show, or run `setup_demo_catalog` to create a new bookable show |

---

## 21. Quick reference (every command, in order)

```bash
cd bookmyseat-final                       # project root (folder with manage.py)
python3 -m venv venv                      # once
source venv/bin/activate                  # every new terminal (Windows: venv\Scripts\Activate.ps1)
python -m pip install -r requirements.txt # once
# create .env (step 5)
python manage.py check
python manage.py migrate
python manage.py createsuperuser          # once, for the admin account
python manage.py setup_demo_catalog       # Avengers + bookable titles (safe to repeat)
python manage.py test movies              # optional, 143 tests
redis-server                              # terminal 1 (or Docker / brew, see step 11)
celery -A bookmyseat worker -l info       # terminal 2 (Windows: add --pool=solo)
python manage.py runserver                # terminal 3
python manage.py add_avengers --watch-user yourusername   # The Avengers + your review access
python manage.py send_pending_ticket_emails --days 3   # only when needed
```

---

## 8d. Seat reservation (Task 2)

**Where:** project root, venv active.

**Expired holds.** A seat held and then left unpaid becomes available after 120 seconds, with or
without this command. To tidy the stored fields, run it, for example from a scheduler every few minutes:

```bash
python manage.py release_expired_holds
```

**Tests that prove concurrency.** The concurrency tests use real threads and database connections.
They need the test-only SQLite settings, which `manage.py test` applies automatically on SQLite:

```bash
python manage.py test movies.tests_task2_concurrency
```

**Limits.** These tests ran on SQLite. Production uses the database in `DATABASE_URL`. PostgreSQL and
MySQL were not tested here; to validate production behaviour, run the same command with
`DATABASE_URL` pointing at a test PostgreSQL database.
