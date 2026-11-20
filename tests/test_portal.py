from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import requests

from casecal import portal
from casecal.config import PortalCredentials, PropertyConfig
from casecal.errors import LayoutError, LoginError, PortalError, TransportError
from casecal.model import BOOKING, CLOSED, Stay
from casecal.portal import PortalClient, choose_unit, fetch_property, portal_base, read_property
from casecal.parse import Unit
from portal_html import eur, grid_classes, landing_page, login_page, reservations_page, stats_page

NOW = datetime(2026, 8, 15, 2, 17, tzinfo=timezone.utc)
BASE = "https://portal.example/demo"
USERNAME, PASSWORD = "utente demo", "Segreta-Portale-9!"
CREDENTIALS = PortalCredentials(BASE + "/property", USERNAME, PASSWORD)
PROP = PropertyConfig(id="santa", label="Santa", env="SANTA")

STAYS = [
    Stay(date(2026, 5, 30), date(2026, 6, 2)),
    Stay(date(2026, 7, 1), date(2026, 8, 1), CLOSED),
    Stay(date(2026, 12, 31), date(2027, 1, 3)),
]
HEADERS = ["Appartamento", "Nome", "Canale", "Ospiti", "Check in", "Check out", "Totale pernottamento", "Netto proprietario", ""]


class Response:
    def __init__(self, text="", status_code=200):
        self.text, self.status_code = text, status_code


class FakeSession:
    """Answers from a ``{(method, path): response-or-list}`` table and records every call."""

    def __init__(self, routes):
        self.routes = {key: list(value) if isinstance(value, list) else [value] for key, value in routes.items()}
        self.headers = {}
        self.calls = []
        self.closed = False

    def request(self, method, url, timeout=None, data=None, params=None):
        assert url.startswith("https://portal.example/"), "a request left the configured host"
        path = url[len("https://portal.example"):]
        self.calls.append((method, path, dict(data or {}), dict(params or {})))
        queue = self.routes.get((method, path))
        if not queue:
            return Response("not found", 404)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(params or {})
        return answer if isinstance(answer, Response) else Response(answer)

    def get(self, url, timeout=None):
        return self.request("GET", url, timeout=timeout)

    def close(self):
        self.closed = True


def stats(params):
    year = int(params.get("year", 2026))
    cells = {night: klass for night, klass in grid_classes(STAYS).items() if night.year == year}
    counts = {}
    for night, klass in cells.items():
        if "b" not in klass.split():
            counts[night.month] = counts.get(night.month, 0) + 1
    rows = {m: (n, [Decimal("250"), Decimal("52.50") * n, Decimal("136.50") * n]) for m, n in counts.items()}
    return stats_page(year, cells=cells, kpi_rows=rows)


def reservations(params):
    assert params == {"from": "01/01/2026", "to": "31/12/2027", "type": "stay", "apt": "7"}
    return reservations_page(date(2026, 1, 1), date(2027, 12, 31), HEADERS, [
        ["Casa Demo", "Mario Rossi", "Airbnb", "4", "30/05/2026", "02/06/2026", eur("750"), eur("409.50"), ""],
        ["Casa Demo", "Anna Verdi", "Diretta", "2", "31/12/2026", "03/01/2027", eur("750"), eur("409.50"), ""],
    ])


def routes(overrides=None):
    table = {
        ("GET", "/demo/login"): login_page(),
        ("POST", "/demo/login/"): landing_page(),
        ("GET", "/demo/vr/stats"): stats,
        ("GET", "/demo/reservations"): reservations,
        ("GET", "/demo/login/logout"): login_page(),
    }
    table.update(overrides or {})
    return table


def client(session, sleeps=None):
    return PortalClient(CREDENTIALS, session=session, sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_the_portal_address_is_reduced_to_host_and_manager():
    assert portal_base("https://vr.example.com/gestore/property") == "https://vr.example.com/gestore"
    assert portal_base("https://vr.example.com/gestore/") == "https://vr.example.com/gestore"
    assert portal_base(" https://vr.example.com/gestore/vr/stats?year=2026 ") == "https://vr.example.com/gestore"
    for bad in ("http://vr.example.com/gestore", "https://vr.example.com/", "gestore", ""):
        with pytest.raises(PortalError, match="indirizzo del portale"):
            portal_base(bad)


def test_login_submits_the_form_as_the_portal_serves_it():
    session = FakeSession(routes())
    client(session).login()
    method, path, data, _ = session.calls[1]
    assert (method, path) == ("POST", "/demo/login/")
    assert data == {
        "f00dfeed01[f00dfeed01]": "c0ffee",  # the session token travels back untouched
        "property_id": "demo",
        "username": USERNAME,
        "password": PASSWORD,
        "login_form": "",
    }
    assert session.headers["Accept-Language"].startswith("it")
    assert "casecal" in session.headers["User-Agent"]


def test_rejected_credentials_raise_without_echoing_them():
    session = FakeSession(routes({("POST", "/demo/login/"): login_page()}))
    with pytest.raises(LoginError) as caught:
        client(session).login()
    message = str(caught.value)
    assert "rifiutate" in message
    for secret in (USERNAME, PASSWORD, "portal.example", "demo"):
        assert secret not in message


def test_credentials_are_never_posted_to_another_host():
    session = FakeSession(routes({("GET", "/demo/login"): login_page(action="https://evil.example/collect")}))
    with pytest.raises(LayoutError, match="altro sito"):
        client(session).login()
    assert [call[0] for call in session.calls] == ["GET"]  # nothing was posted anywhere


def test_a_maintenance_page_instead_of_the_login_form_is_a_layout_error():
    session = FakeSession(routes({("GET", "/demo/login"): "<html><body>Torniamo presto</body></html>"}))
    with pytest.raises(LayoutError, match="modulo di login"):
        client(session).login()


def test_transient_failures_are_retried_with_a_pause():
    sleeps = []
    session = FakeSession(routes({("GET", "/demo/login"): [Response("busy", 503), Response("busy", 502), login_page()]}))
    client(session, sleeps).login()
    assert [call[1] for call in session.calls].count("/demo/login") == 3
    assert sleeps[:2] == [2.0, 4.0]


def test_a_persistent_failure_reports_only_the_step_and_the_status():
    session = FakeSession(routes({("GET", "/demo/vr/stats"): Response("boom <b>Mario Rossi</b>", 500)}))
    portal_client = client(session)
    portal_client.login()
    with pytest.raises(TransportError) as caught:
        portal_client.page("/vr/stats", "statistiche")
    assert str(caught.value) == "statistiche: HTTP 500"
    assert [call[1] for call in session.calls].count("/demo/vr/stats") == portal.ATTEMPTS


def test_a_client_error_is_not_retried():
    session = FakeSession(routes({("GET", "/demo/vr/stats"): Response("nope", 404)}))
    portal_client = client(session)
    portal_client.login()
    with pytest.raises(TransportError, match="HTTP 404"):
        portal_client.page("/vr/stats", "statistiche")
    assert [call[1] for call in session.calls].count("/demo/vr/stats") == 1


def test_a_network_error_reports_its_type_not_its_details():
    boom = requests.ConnectionError("HTTPSConnectionPool(host='portal.example'): Max retries exceeded /demo/login")
    session = FakeSession(routes({("GET", "/demo/login"): boom}))
    with pytest.raises(TransportError) as caught:
        client(session).login()
    assert str(caught.value) == "pagina di login: nessuna risposta (ConnectionError)"


def test_an_expired_session_is_noticed_on_any_page():
    session = FakeSession(routes({("GET", "/demo/vr/stats"): login_page()}))
    portal_client = client(session)
    portal_client.login()
    with pytest.raises(LoginError, match="sessione non valida"):
        portal_client.page("/vr/stats", "statistiche")


def test_logout_is_best_effort_and_closes_the_session():
    session = FakeSession(routes({("GET", "/demo/login/logout"): requests.ConnectionError("down")}))
    client(session).logout()
    assert session.closed and session.calls[-1][1] == "/demo/login/logout"


def test_choosing_the_unit():
    one, two = Unit("7", "Casa Demo"), Unit("9", "Attico")
    assert choose_unit([one], None, "X_KB_UNIT") == one
    assert choose_unit([one, two], "attico", "X_KB_UNIT") == two  # by name, any case
    assert choose_unit([one, two], "7", "X_KB_UNIT") == one  # or by portal id
    with pytest.raises(PortalError, match="2 appartamenti.*X_KB_UNIT"):
        choose_unit([one, two], None, "X_KB_UNIT")
    with pytest.raises(PortalError, match="X_KB_UNIT non esiste"):
        choose_unit([one, two], "Villa", "X_KB_UNIT")


def logged_in(session):
    portal_client = client(session)
    portal_client.login()
    return portal_client


def test_reading_a_house_end_to_end():
    session = FakeSession(routes())
    snapshot = read_property(logged_in(session), PROP, None, [2026, 2027], NOW)
    assert (snapshot.id, snapshot.label, snapshot.unit, snapshot.fetched_at) == ("santa", "Santa", "Casa Demo", NOW)
    assert (snapshot.covered_from, snapshot.covered_to) == (date(2026, 1, 1), date(2028, 1, 1))
    assert [(s.start, s.end, s.kind, s.channel, s.guests) for s in snapshot.stays] == [
        (date(2026, 5, 30), date(2026, 6, 2), BOOKING, "Airbnb", 4),
        (date(2026, 7, 1), date(2026, 8, 1), CLOSED, None, None),
        (date(2026, 12, 31), date(2027, 1, 3), BOOKING, "Diretta", 2),
    ]
    assert snapshot.kpi_columns == ("ADR", "Imposta", "Netto proprietario")
    assert len(snapshot.kpis) == 24 and snapshot.kpi_roles == {"adr": 0, "net": 2}
    # 30 and 31 May, 1 June and 31 December fall in 2026; 1 and 2 January in 2027.
    assert [(t.year, t.room_nights) for t in snapshot.kpi_totals] == [(2026, 4), (2027, 2)]
    assert snapshot.reservation_columns == ("Totale pernottamento", "Netto proprietario")
    assert len(snapshot.reservations) == 2
    assert snapshot.warnings == []
    assert (snapshot.check_in_time, snapshot.check_out_time) == ("15:00", "11:00")
    assert snapshot.portal_url == BASE + "/property/details/id/7"  # where the owner closes dates by hand
    assert "Rossi" not in repr(snapshot) and "Verdi" not in repr(snapshot)
    asked = [call[3] for call in session.calls if call[1] == "/demo/vr/stats"]
    assert asked == [{}, {"year": "2026", "apt": "7"}, {"year": "2027", "apt": "7"}]
    assert all(call[0] == "GET" for call in session.calls[2:]), "after login the portal is only read"


def test_the_house_hours_come_from_the_configuration_not_the_portal():
    late = PropertyConfig(id="santa", label="Santa", env="SANTA", check_in="17:00", check_out="09:30")
    snapshot = read_property(logged_in(FakeSession(routes())), late, None, [2026, 2027], NOW)
    assert (snapshot.check_in_time, snapshot.check_out_time) == ("17:00", "09:30")


def test_losing_the_reservation_details_does_not_lose_the_calendar():
    session = FakeSession(routes({("GET", "/demo/reservations"): Response("down", 503)}))
    snapshot = read_property(logged_in(session), PROP, None, [2026, 2027], NOW)
    assert [s.kind for s in snapshot.stays] == [BOOKING, CLOSED, BOOKING]
    assert all(s.channel is None for s in snapshot.stays)
    assert snapshot.reservations == [] and snapshot.reservation_columns == ()
    assert snapshot.warnings == ["dettagli delle prenotazioni non letti (TransportError: elenco prenotazioni: HTTP 503)"]


def test_reservations_that_disagree_with_the_calendar_are_reported():
    def partial(params):
        return reservations_page(date(2026, 1, 1), date(2027, 12, 31), HEADERS, [
            ["Casa Demo", "Mario Rossi", "Airbnb", "4", "30/05/2026", "02/06/2026", eur("750"), eur("409.50"), ""],
        ])

    session = FakeSession(routes({("GET", "/demo/reservations"): partial}))
    snapshot = read_property(logged_in(session), PROP, None, [2026, 2027], NOW)
    assert snapshot.warnings == ["1 prenotazioni non coincidono con il calendario"]
    assert len(snapshot.stays) == 3  # the calendar is untouched


def test_a_calendar_that_contradicts_the_statistics_fails_the_house():
    def lying(params):
        year = int(params.get("year", 2026))
        cells = {night: klass for night, klass in grid_classes(STAYS).items() if night.year == year}
        return stats_page(year, cells=cells, kpi_rows={5: (9, ["250", "472.50", "1228.50"])})

    session = FakeSession(routes({("GET", "/demo/vr/stats"): lying}))
    with pytest.raises(LayoutError, match="non coincidono"):
        read_property(logged_in(session), PROP, None, [2026, 2027], NOW)


def test_fetch_logs_out_even_when_reading_fails(monkeypatch):
    session = FakeSession(routes({("GET", "/demo/vr/stats"): Response("boom", 500)}))
    monkeypatch.setattr(portal.requests, "Session", lambda: session)
    monkeypatch.setattr(portal.time, "sleep", lambda seconds: None)
    with pytest.raises(TransportError):
        fetch_property(PROP, CREDENTIALS, [2026, 2027], NOW)
    assert session.calls[-1][1] == "/demo/login/logout" and session.closed


def test_fetch_returns_the_house(monkeypatch):
    session = FakeSession(routes())
    monkeypatch.setattr(portal.requests, "Session", lambda: session)
    monkeypatch.setattr(portal.time, "sleep", lambda seconds: None)
    snapshot = fetch_property(PROP, CREDENTIALS, [2026, 2027], NOW)
    assert len(snapshot.stays) == 3 and session.closed
