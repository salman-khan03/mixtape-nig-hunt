"""
tests/test_notifications.py — Mixtape

Regression tests for Issue #4: rating a friend's shared song
should notify the song's original sharer.
"""

import pytest
from app import create_app, db
from models import User, Song, Notification
from services.notification_service import rate_song


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed(app):
    """A sharer with one shared song, and a friend who will rate it."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Golden Hour", artist="Solange K", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()
        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_notifies_sharer(app, seed):
    """Rating a friend's song creates a 'song_rated' notification for the sharer."""
    with app.app_context():
        rate_song(seed["rater"].id, seed["song"].id, 5)
        notifs = Notification.query.filter_by(
            user_id=seed["sharer"].id, notification_type="song_rated"
        ).all()
        assert len(notifs) == 1
        assert "rater" in notifs[0].body
        assert "Golden Hour" in notifs[0].body


def test_rating_own_song_does_not_notify(app, seed):
    """Rating your own shared song should not create a notification."""
    with app.app_context():
        rate_song(seed["sharer"].id, seed["song"].id, 4)
        notifs = Notification.query.filter_by(user_id=seed["sharer"].id).all()
        assert notifs == []


def test_rerating_updates_rating_and_notifies_new_score(app, seed):
    """Re-rating updates the existing rating row and notifies with the new score."""
    with app.app_context():
        rate_song(seed["rater"].id, seed["song"].id, 2)
        rating = rate_song(seed["rater"].id, seed["song"].id, 5)
        assert rating.score == 5
        latest = (
            Notification.query.filter_by(
                user_id=seed["sharer"].id, notification_type="song_rated"
            )
            .order_by(Notification.created_at.desc())
            .first()
        )
        assert "5/5" in latest.body
