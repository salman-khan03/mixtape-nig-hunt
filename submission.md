# Project 5 — Mixtape Bug Hunt: Submission

## AI Usage

I used Claude Code (Claude, Anthropic) throughout this project as a codebase-navigation and debugging assistant:

- **Orientation:** I asked it to read every file in `services/`, `routes/`, `models.py`, and `seed_data.py` and summarize each module's responsibility, which fed directly into the codebase map below.
- **Reproduction:** I used it to run the existing pytest suite as a baseline (which immediately reproduced Issues #1 and #5 as failing tests) and to write small throwaway scripts against the seeded database to reproduce Issues #2, #3, and #4.
- **Where AI output needed verification:** the search-duplicates test (`test_search_no_duplicates_multi_tag_song`) *passed* on the installed SQLAlchemy 2.0.51, even though the join is buggy. I did not take "test passes, no bug" at face value — we ran the underlying SELECT with `db.session.execute()` and confirmed it returns 3 duplicate rows for a 3-tag song. The legacy ORM `Query` API silently de-duplicates full-entity rows, which masks the bug in this version. This was a case where reading the actual query output mattered more than trusting either the test or an initial explanation.
- All fixes were verified by re-running the full test suite and re-executing the reproduction steps after each change.

## Codebase Map

Written before starting any bug work.

**Top level**
- `app.py` — Flask application factory (`create_app`) plus the shared `SQLAlchemy` instance (`db`). Registers four blueprints under `/songs`, `/playlists`, `/users`, `/feed` and calls `db.create_all()` on startup.
- `models.py` — 6 SQLAlchemy models (`User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Notification`, `Playlist`) plus three association tables: `friendships` (symmetric user↔user), `song_tags` (song↔tag), and `playlist_entries` (playlist↔song, with extra `position`, `added_by`, `added_at` columns — playlist songs have an explicit position, not just insertion order). `Rating` has a unique constraint on `(user_id, song_id)`, so a user re-rating a song updates the existing row.
- `seed_data.py` — creates 5 users with friendships, 13 songs with 0/1/3+ tags (the multi-tag songs deliberately expose Issue #3), 3 playlists of 7 songs, listening events both recent (10–20 min ago) and old (2–58 hours ago), and one example `song_added_to_playlist` notification (the "working pattern" for Issue #4).

**Routes (thin) → Services (all business logic)**
Every route parses input, delegates to exactly one service function, and formats the JSON response; no business logic lives in `routes/`.
- `routes/songs.py` → `search_service.search_songs/get_song`, `notification_service.rate_song`, `streak_service.record_listening_event`
- `routes/playlists.py` → `playlist_service` (create/get) and `notification_service.add_to_playlist` (adding a song lives in the notification service because it also notifies the sharer)
- `routes/users.py` → `streak_service.get_streak`, `notification_service.get_notifications/mark_as_read`
- `routes/feed.py` → `feed_service.get_friends_listening_now/get_activity_feed`

**Services**
- `streak_service.py` — `record_listening_event` creates a `ListeningEvent` and calls `update_listening_streak`, which compares calendar dates: same day = no-op, 1 day apart = increment, otherwise reset to 1.
- `feed_service.py` — "Friends Listening Now" queries friends' `ListeningEvent`s newer than a `RECENT_THRESHOLD` cutoff, then keeps only the newest event per friend. `get_activity_feed` is the unfiltered variant.
- `search_service.py` — title/artist `ilike` search over `Song`.
- `notification_service.py` — generic `create_notification`, plus the two interaction entry points (`add_to_playlist`, `rate_song`) that are supposed to notify a song's original sharer.
- `playlist_service.py` — playlist CRUD and ordered song retrieval via a join on `playlist_entries` ordered by `position`.

**Data flow example — a friend adds my shared song to a playlist:**
`POST /playlists/<id>/songs` (`routes/playlists.py:add_song`) → `notification_service.add_to_playlist(playlist_id, song_id, added_by)` → validates song/user/playlist exist → appends the song to `playlist.songs` (writes a `playlist_entries` row) → if `song.shared_by != added_by`, calls `create_notification(user_id=song.shared_by, type="song_added_to_playlist", ...)` → the sharer later reads it via `GET /users/<id>/notifications` → `notification_service.get_notifications`.

**Patterns noticed:** UUID string primary keys everywhere; UTC-aware datetimes via `datetime.now(timezone.utc)` defaults; services commit their own transactions; routes translate `ValueError` from services into 400/404 JSON errors.

## Root Cause Analyses

### Issue #1 — My listening streak keeps resetting

1. **Issue:** #1, "My listening streak keeps resetting" (`streak_service.py`).
2. **How I reproduced it:** Ran the existing test suite first — `tests/test_streaks.py::test_streak_increments_on_sunday` fails on the unmodified code: listen Saturday 2024-06-15 (streak 1), then Sunday 2024-06-16 → streak is 1 instead of 2. Also verified directly by calling `update_listening_streak(user, saturday)` then `update_listening_streak(user, sunday)` in a shell: the Sunday call hit the reset branch.
3. **How I found the root cause:** Started from `POST /songs/<id>/listen` in `routes/songs.py`, which calls `streak_service.record_listening_event`, which calls `update_listening_streak`. The function is short; the only branch that can wrongly reset a consecutive-day streak is the `elif`. The moment of confidence was seeing the extra clause `and today.weekday() != 6` bolted onto an otherwise-correct `days_since_last == 1` check — the docstring says nothing about weekdays, so the condition had no business being there.
4. **The root cause:** The increment branch was `days_since_last == 1 and today.weekday() != 6`. Python's `datetime.weekday()` returns 6 for Sunday, so whenever "today" is a Sunday, the increment branch is skipped even for a perfectly consecutive Saturday→Sunday listen, and control falls into the `else`, which resets the streak to 1. That is exactly the reported symptom: streaks silently reset once a week, every Sunday.
5. **Fix and side-effect check:** Removed the `and today.weekday() != 6` clause so the branch is purely `days_since_last == 1`. Day-of-week is irrelevant to the streak rules stated in the docstring (new user → 1, same day → no change, consecutive day → +1, gap → reset). Checked both sides of the boundary afterward: same-day double-listen still doesn't double-count, Monday→Tuesday still increments, Monday→Wednesday still resets, and Saturday→Sunday now increments. All 5 streak tests pass.

### Issue #2 — Friends Listening Now shows people from yesterday

1. **Issue:** #2, "Friends Listening Now shows people from yesterday" (`feed_service.py`).
2. **How I reproduced it:** Seeded the DB (`python seed_data.py`) and called `get_friends_listening_now(nova.id)` (equivalently `GET /feed/<nova_id>/listening-now`). The seed data creates listening events 10–20 minutes ago *and* stale events 2–58 hours ago (the seed file even comments that the older events "should NOT appear in 'listening now' after fix"). With the unmodified code, friends whose only events were many hours old still appeared in the feed, because everything within 24 hours passed the cutoff.
3. **How I found the root cause:** `GET /feed/<user_id>/listening-now` in `routes/feed.py` → `feed_service.get_friends_listening_now`. The query logic itself (filter friends' events newer than `cutoff`, dedupe to the newest event per friend) is correct, so the only remaining input is the cutoff itself: `RECENT_THRESHOLD = timedelta(hours=24)` at the top of the file. A 24-hour window for a feature named "Listening **Now**" is the bug — it's a threshold constant mismatch, not a query mistake.
4. **The root cause:** `RECENT_THRESHOLD` was `timedelta(hours=24)`, so the "listening now" cutoff was `now - 24h`. Any friend who listened at any point in the past day qualified as "listening now," which is why users saw friends from yesterday. The contrast with `get_activity_feed` (explicitly documented as the *not*-recency-filtered feed) confirms the threshold was meant to be a short "currently listening" window.
5. **Fix and side-effect check:** Changed `RECENT_THRESHOLD` to `timedelta(minutes=30)`, matching the seed data's definition of "recent" (events within the past 30 minutes should appear). Verified on both sides of the boundary: the three seeded events 10–20 minutes old still appear; friends whose newest event is hours old no longer do. `get_activity_feed` doesn't use the constant, so the activity feed is unaffected; the full test suite still passes.

### Issue #3 — The same song keeps showing up twice in search

1. **Issue:** #3, "The same song keeps showing up twice in search" (`search_service.py`).
2. **How I reproduced it:** The bug is conditional: it only triggers for songs that have **more than one tag**, because the code path that joins `song_tags` produces one result row per tag. Searching "Midnight Drive" (0 tags) or "Block Party" (1 tag) returns one row; searching "Crown Heights" (3 tags) is the trigger condition. On the installed SQLAlchemy 2.0.51 the legacy `Query` API masks the duplication by de-duplicating full-entity rows in Python, so I reproduced it at the SQL level: executing the same SELECT with `db.session.execute(select(Song.id, Song.title).outerjoin(song_tags, ...).where(...))` returns **3 identical rows** for "Crown Heights Anthem" — one per tag. The starter's own test file (`test_search.py`) documents the expected symptom ("bug causes it to be 3").
3. **How I found the root cause:** `GET /songs/search` in `routes/songs.py` → `search_service.search_songs`. The filter only touches `Song.title` and `Song.artist` — no `Tag` column appears anywhere in the WHERE clause — yet the query does `.outerjoin(song_tags, Song.id == song_tags.c.song_id)`. A join that contributes nothing to filtering but multiplies rows (one per matching tag) is the classic fan-out duplication bug; confirming that the raw SQL returned N rows for an N-tag song was the moment of certainty.
4. **The root cause:** `search_songs` outer-joins `song_tags` without using it in the filter and without `DISTINCT`. SQL returns one row per (song, tag) pair, so a song with 3 tags comes back 3 times. Tags don't need the join at all — `Song.tags` is a `lazy="subquery"` relationship that `Song.to_dict()` already uses. (The duplicates are currently hidden by a deprecated legacy-`Query` de-duplication behavior, so the app is one refactor to 2.0-style `select()` — or one `.limit()` call — away from user-visible duplicates.)
5. **Fix and side-effect check:** Removed the `outerjoin` entirely, leaving a plain filtered query on `Song`. This is more targeted than adding `.distinct()`, because the join served no purpose: filtering doesn't use tags and serialization gets tags via the relationship. Verified afterward that multi-tag, single-tag, and no-tag songs each appear exactly once, that tags still appear in the response payload (the relationship loads them independently of the removed join), and that the raw SQL now returns exactly one row per matching song. All 5 search tests pass.

### Issue #4 — Notified on playlist add but not on rating

1. **Issue:** #4, "I got notified when a friend added my song to a playlist but not when they rated it" (`notification_service.py`).
2. **How I reproduced it:** Seeded the DB, then as `darius` rated a song shared by `nova` via `rate_song(darius.id, song.id, 5)` (the code behind `POST /songs/<id>/rate`). The rating row was created correctly, but `GET /users/<nova_id>/notifications` still showed only the seeded `song_added_to_playlist` notification — no `song_rated` notification was ever created.
3. **How I found the root cause:** Following the hint that the root cause is architectural, I compared the two interaction entry points in `notification_service.py` line by line. `add_to_playlist` ends with: *if the actor isn't the sharer, `create_notification(user_id=song.shared_by, type="song_added_to_playlist", ...)`*. `rate_song` performs the same validations and saves the `Rating` — and then simply returns. There is no failed or misaddressed notification to debug; the notification step was never written.
4. **The root cause:** The notify-the-sharer step is a per-action convention, not a centralized mechanism — each interaction function must remember to call `create_notification` itself. `add_to_playlist` implements the convention; `rate_song` omits it entirely. Nothing was misconfigured or mistyped: the code path that would create a `song_rated` notification did not exist.
5. **Fix and side-effect check:** Added the same guarded pattern to the end of `rate_song`: if `song.shared_by != user_id`, call `create_notification` with type `song_rated` and a body naming the rater, the song, and the score. Mirroring `add_to_playlist`'s guard means self-ratings don't notify. Checked side effects: rating save/update logic is untouched (the unique-constraint update path still works, and re-rating notifies with the new score); rating your own song creates no notification; `get_notifications` needs no changes since it's type-agnostic. Added a regression test suite (`tests/test_notifications.py`) covering all three cases.

### Issue #5 — The last song in a playlist never shows up

1. **Issue:** #5, "The last song in a playlist never shows up" (`playlist_service.py`).
2. **How I reproduced it:** Ran the test suite — `tests/test_playlists.py::test_playlist_returns_all_songs` fails on unmodified code (a 5-song playlist returns 4 songs), as does `test_playlist_returns_songs_in_order` (missing "Track 5"). Also reproduced against seed data: `GET /playlists/<id>/songs` for the 7-song "Late Night Vibes" playlist returned `count: 6`, always missing the highest-position song.
3. **How I found the root cause:** `GET /playlists/<playlist_id>/songs` in `routes/playlists.py` → `playlist_service.get_playlist_songs`. The query itself is correct (join `playlist_entries`, filter by playlist, order by `position` ascending). The bug is on the very last line: `return [song.to_dict() for song in songs[:-1]]`. `songs[:-1]` is unambiguous — it slices off the final element — so there was nothing further to trace.
4. **The root cause:** The return statement slices the ordered result list with `[:-1]`, which drops the last element. Because the list is sorted ascending by `position`, the dropped element is always the song with the highest position — i.e., the most recently added song — matching the report that "the last song never shows up." (It also means a 1-song playlist appears empty.) The function's own docstring says "returns all songs in the playlist," so the slice contradicts the documented contract.
5. **Fix and side-effect check:** Changed the return to iterate over `songs` with no slice. Checked the boundaries afterward: a 5-song playlist returns all 5 in position order, and an empty playlist still returns `[]` (the empty-list edge case behaves identically with or without the slice, so nothing else depended on it). All 3 playlist tests pass.

## Regression Test (stretch)

`tests/test_notifications.py` — regression tests for Issue #4. It verifies that (a) rating a friend's shared song creates a `song_rated` notification addressed to the sharer, (b) rating your own song does not create a notification, and (c) re-rating updates the existing rating and notifies with the new score. Test (a) fails on the pre-fix code (no notification is created) and passes after the fix, so it would have caught the bug before it shipped.

## Final Test Run

All 16 tests pass after the five fixes (13 original + 3 new regression tests).
