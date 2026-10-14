"""Miejsce na przyszłą, opcjonalną integrację z Morpheus API.

Wersja 1.0 NIE łączy się z Morpheus, więc nigdy nie twierdzi, że magazyn jest widoczny w panelu.
Przyszła implementacja powinna zachować ten interfejs i zwracać stan 'ok' / 'error' / 'unknown'.
"""


def registration_state(storage_name, mount=None):
    """Stan rejestracji magazynu (Datastore) w panelu Morpheus: {'state': ..., 'detail': ...}."""
    return {'state': 'unknown', 'detail': 'Rejestracja w panelu Morpheus niezweryfikowana'}
