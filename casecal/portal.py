"""Read one house from a Krossbooking owner portal.

Strictly read-only: the client logs in, GETs the statistics and reservation
pages, and logs out. It never submits anything but the login form.

Error messages raised here end up in a public CI log, so they describe the
step that failed and never include the portal address, a credential or any
page content.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Callable, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

import requests

from .config import PortalCredentials, PropertyConfig
from .errors import LayoutError, LoginError, PortalError, TransportError
from .model import PropertySnapshot
from .parse import (
    ReservationTable,
    Unit,
    attach_details,
    cross_check,
    format_date,
    is_logged_in,
    is_login_page,
    own_use_as_closed,
    parse_login_form,
    parse_reservations,
    parse_units,
    parse_year_grid,
    parse_year_kpis,
    stays_from_grids,
)

USER_AGENT = "Mozilla/5.0 (compatible; casecal/1.0; owner calendar sync)"
TIMEOUT_SECONDS = 30
ATTEMPTS = 3
RETRY_STATUS = {429, 500, 502, 503, 504}
PAUSE_SECONDS = 0.6  # between requests: this is a nightly job, not a crawler


def portal_base(url: str) -> str:
    """``https://host/<manager>`` from any address inside a manager's portal.

    Owners usually copy the address of the page they land on, for instance
    ``https://host/<manager>/property``; everything after the manager is dropped.
    """
    parts = urlsplit(url.strip())
    segments = [segment for segment in parts.path.split("/") if segment]
    if parts.scheme != "https" or not parts.netloc or not segments:
        raise PortalError("indirizzo del portale non valido: serve https://<host>/<gestore>")
    return f"https://{parts.netloc}/{segments[0]}"


class PortalClient:
    def __init__(
        self,
        credentials: PortalCredentials,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._credentials = credentials
        self._base = portal_base(credentials.base_url)
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "it-IT,it;q=0.9"})
        self._sleep = sleep

    def _url(self, target: str) -> str:
        # A relative target is a page of the portal; an absolute one is a form action.
        return target if target.startswith("https://") else self._base + target

    def _request(self, method: str, target: str, what: str, **kwargs) -> requests.Response:
        problem: PortalError = TransportError(f"{what}: nessuna risposta")
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self._session.request(
                    method, self._url(target), timeout=TIMEOUT_SECONDS, **kwargs
                )
            except requests.RequestException as exc:
                problem = TransportError(f"{what}: nessuna risposta ({type(exc).__name__})")
            else:
                if response.status_code < 400:
                    self._sleep(PAUSE_SECONDS)
                    return response
                problem = TransportError(f"{what}: HTTP {response.status_code}")
                if response.status_code not in RETRY_STATUS:
                    break
            if attempt < ATTEMPTS:
                self._sleep(2.0 * attempt)
        raise problem

    def login(self) -> None:
        form = parse_login_form(self._request("GET", "/login", "pagina di login").text)
        action = urljoin(self._base + "/login", form.action)
        # Credentials only ever go to the host the owner configured.
        if urlsplit(action).netloc != urlsplit(self._base).netloc or not action.startswith("https://"):
            raise LayoutError("il modulo di login punta a un altro sito")
        data = dict(form.fields)
        data["username"] = self._credentials.username
        data["password"] = self._credentials.password
        if form.submit_name:
            data.setdefault(form.submit_name, "")
        landing = self._request("POST", action, "login", data=data).text
        if is_login_page(landing) or not is_logged_in(landing):
            raise LoginError("credenziali rifiutate dal portale")

    def url(self, path: str) -> str:
        """Address of a page of this portal, for a person to open."""
        return self._url(path)

    def page(self, path: str, what: str, params: Mapping[str, str] | None = None) -> str:
        html = self._request("GET", path, what, params=params).text
        if is_login_page(html):
            raise LoginError(f"{what}: sessione non valida")
        return html

    def logout(self) -> None:
        try:
            self._session.get(self._url("/login/logout"), timeout=TIMEOUT_SECONDS)
        except requests.RequestException:
            pass  # best effort: the session expires on its own
        finally:
            self._session.close()


def choose_unit(units: Sequence[Unit], wanted: str | None, variable: str) -> Unit:
    if wanted:
        folded = wanted.casefold()
        for unit in units:
            if unit.id == wanted or unit.name.casefold() == folded:
                return unit
        raise PortalError(f"l'appartamento indicato in {variable} non esiste nell'account")
    if len(units) > 1:
        raise PortalError(
            f"l'account contiene {len(units)} appartamenti: indica quale nel secret {variable}"
        )
    return units[0]


def read_property(
    client: PortalClient,
    prop: PropertyConfig,
    wanted_unit: str | None,
    years: Sequence[int],
    now: datetime,
) -> PropertySnapshot:
    """Everything about one house, through an already logged-in client."""
    unit = choose_unit(
        parse_units(client.page("/vr/stats", "statistiche")), wanted_unit, prop.unit_variable
    )

    grids, yearly = [], []
    for year in years:
        html = client.page("/vr/stats", "statistiche", {"year": str(year), "apt": unit.id})
        grid = parse_year_grid(html, year, unit.id)
        kpis = parse_year_kpis(html, year, unit.id)
        cross_check(grid, kpis)
        grids.append(grid)
        yearly.append(kpis)
    if any(kpis.columns != yearly[0].columns for kpis in yearly):
        raise LayoutError("le colonne delle statistiche cambiano da un anno all'altro")
    stays = stays_from_grids(grids)

    covered_from, covered_to = date(years[0], 1, 1), date(years[-1] + 1, 1, 1)
    last_day = date(years[-1], 12, 31)
    warnings: list[str] = []
    reservations: ReservationTable | None = None
    try:
        # The calendar above is what the family relies on. Reservation details
        # only enrich it, so losing them must not cost the whole house.
        html = client.page(
            "/reservations",
            "elenco prenotazioni",
            {"from": format_date(covered_from), "to": format_date(last_day), "type": "stay", "apt": unit.id},
        )
        reservations = parse_reservations(html, start=covered_from, end=last_day)
        stays, unmatched = attach_details(
            stays, reservations.rows, covered_from=covered_from, covered_to=covered_to
        )
        if unmatched:
            warnings.append(f"{unmatched} prenotazioni non coincidono con il calendario")
        if prop.unpaid_is_own_use:
            # After the checks above: the portal's own statistics count these nights as sold.
            stays = own_use_as_closed(
                stays, reservations.rows, covered_from=covered_from, covered_to=covered_to
            )
    except PortalError as exc:
        warnings.append(f"dettagli delle prenotazioni non letti ({type(exc).__name__}: {exc})")

    return PropertySnapshot(
        id=prop.id,
        label=prop.label,
        unit=unit.name,
        fetched_at=now,
        covered_from=covered_from,
        covered_to=covered_to,
        stays=stays,
        kpi_columns=yearly[0].columns,
        kpi_roles=yearly[0].roles,
        kpis=[month for kpis in yearly for month in kpis.months],
        kpi_totals=[kpis.total for kpis in yearly if kpis.total is not None],
        reservation_columns=reservations.columns if reservations else (),
        reservation_roles=reservations.roles if reservations else {},
        reservations=reservations.rows if reservations else [],
        check_in_time=prop.check_in,
        check_out_time=prop.check_out,
        portal_url=client.url(f"/property/details/id/{unit.id}"),
        warnings=warnings,
    )


def fetch_property(
    prop: PropertyConfig, credentials: PortalCredentials, years: Sequence[int], now: datetime
) -> PropertySnapshot:
    client = PortalClient(credentials)
    try:
        client.login()
        return read_property(client, prop, credentials.unit, years, now)
    finally:
        client.logout()
