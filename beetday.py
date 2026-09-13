#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.14"
# dependencies = ["httpx>=0.28"]
# ///
# Copyright (C) 2026  Kostiantyn Lysenko
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE.  See the GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License along
# with this program.  If not, see <https://www.gnu.org/licenses/>.
"""beetday — a small client for your own Workday tenant.

Reverse-engineered from the browser's own (undocumented) endpoints and driven by
a reusable browser session. It does two things:

  * live calls    — people/org search, "who does X report to", management chains
  * offline cache — crawl the whole supervisory-org tree once, then search it
                    instantly with no Workday round-trip

Auth is a saved browser session (cookies + secure token). Refresh it with
`beetday auth` whenever Workday logs you out (~90 minutes idle).

The module is importable (`import beetday`) as well as a CLI; the network
layer (httpx) is imported lazily so the parsing functions have no dependencies.
"""

import argparse
import json
import os
import re
import shlex
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

# --- Locations -------------------------------------------------------------
# Session and cache live together outside the repo. Override the directory with
# BEETDAY_HOME; both files are yours alone and the session file holds live
# credentials, so keep them off shared or synced storage you don't control.
BASE_DIRECTORY = Path(os.environ.get("BEETDAY_HOME") or Path.home() / ".local" / "share" / "beetday")
SESSION_FILE = BASE_DIRECTORY / "session.json"
ORGANIZATION_CHART_FILE = BASE_DIRECTORY / "orgchart.json"

# --- Workday tenant constants ----------------------------------------------
# Workday instance-id class prefixes: 247 = Worker, 2500 = Supervisory Organization.
# These two appear to be Workday-wide.
WORKER_PREFIX = "247$"
ORGANIZATION_PREFIX = "2500$"
# The navigator form needs a per-tenant "Org Chart" task id, and a crawl needs the
# root supervisory organization. Both are discovered by `beetday auth` and stored in
# the session; --initial-step / --root override them. Tasks and reports carry these
# instance-id class prefixes.
TASK_PREFIXES = ("2997$", "2998$")
# Context id for a worker profile: /<tenant>/inst/1$247/<worker>.htmld carries the email.
WORKER_PROFILE_CONTEXT = "1$247"

DEFAULT_USER_AGENT = "beetday"

type Json = dict[str, object]


class SessionExpired(RuntimeError):
    """Workday no longer accepts the saved session; the user must re-auth."""


class WorkdayError(RuntimeError):
    """Any other failure talking to Workday."""


def read_json_file(path: Path) -> Json:
    """json.loads that names the file it choked on; a bare JSONDecodeError names nothing."""
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as error:
        error.add_note(f"while reading {path}")
        raise


# --- Session ---------------------------------------------------------------
@dataclass(slots=True)
class Session:
    base_url: str
    tenant: str
    cookie: str
    session_secure_token: str = ""
    workday_client: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    navigator_initial_step: str = ""  # per tenant, discovered at auth
    root_organization_id: str = ""

    def save(self, path: Path = SESSION_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))
        path.chmod(0o600)  # holds live session credentials

    @classmethod
    def load(cls, path: Path = SESSION_FILE) -> "Session":
        if not path.exists():
            raise SessionExpired(f"no saved session at {path}; run `beetday auth`")
        try:
            return cls(**read_json_file(path))
        except TypeError as error:  # a session file from an older/newer field set
            raise SessionExpired(f"saved session at {path} does not match this version ({error}); run `beetday auth`") from error


def _session_from_request(url: str, cookie: str, headers: dict[str, str]) -> Session:
    parts = urlsplit(url)
    tenant = parts.path.lstrip("/").split("/", 1)[0]
    if not (parts.scheme and parts.netloc and tenant):
        raise WorkdayError(f"could not read base URL and tenant from {url!r}")
    return Session(
        base_url=f"{parts.scheme}://{parts.netloc}",
        tenant=tenant,
        cookie=cookie,
        session_secure_token=headers.get("session-secure-token", ""),
        workday_client=headers.get("x-workday-client", ""),  # Workday's header name, not ours
        user_agent=headers.get("user-agent", DEFAULT_USER_AGENT),
    )


_ANSI_C_QUOTED = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)


def _decode_ansi_c_quoting(command: str) -> str:
    """Rewrite bash ANSI-C quotes ($'...') into plain single quotes shlex understands.

    Firefox emits them for any header holding an escape (a cookie with \\041, say),
    and shlex.split treats the leading $ as part of the token, so the header name
    comes out as "$'cookie" and the cookie is lost.
    """

    def rewrite(match: re.Match[str]) -> str:
        body = match.group(1).encode("latin-1", "backslashreplace").decode("unicode_escape")
        return "'" + body.replace("'", "'\\''") + "'"

    return _ANSI_C_QUOTED.sub(rewrite, command)


def session_from_curl(command: str) -> Session:
    """Build a session from a browser 'Copy as cURL' command."""
    headers: dict[str, str] = {}
    cookie = ""
    url = ""
    tokens = iter(shlex.split(_decode_ansi_c_quoting(command)))
    for token in tokens:
        match token:
            case "-H" | "--header":
                name, _, value = next(tokens).partition(":")
                headers[name.strip().lower()] = value.strip()
            case "-b" | "--cookie":
                cookie = next(tokens)
            case _ if token.startswith(("http://", "https://")):
                url = token
    cookie = cookie or headers.get("cookie", "")
    if not url or not cookie:
        raise WorkdayError("could not find a URL and a Cookie in the pasted cURL command")
    return _session_from_request(url, cookie, headers)


def session_from_har(path: Path) -> Session:
    """Build a session from a saved .har capture of Workday traffic."""
    har = read_json_file(Path(path))
    chosen: tuple[str, dict[str, str]] | None = None
    for entry in har.get("log", {}).get("entries", []):
        request = entry["request"]
        headers = {h["name"].lower(): h["value"] for h in request["headers"]}
        if "cookie" not in headers or "myworkday.com" not in request["url"]:
            continue
        chosen = (request["url"], headers)
        if "session-secure-token" in headers:  # prefer a fully-formed request
            break
    if chosen is None:
        raise WorkdayError(f"no authenticated Workday request found in {path}")
    url, headers = chosen
    return _session_from_request(url, headers.get("cookie", ""), headers)


# --- People ----------------------------------------------------------------
@dataclass(slots=True)
class Person:
    name: str
    worker_id: str
    title: str | None = None
    location: str | None = None
    organization: str | None = None
    manager: str | None = None
    manager_id: str | None = None
    email: str | None = None


def email_from_profile(payload: Json) -> str | None:
    """Pull the primary work email out of a worker-profile htmld payload."""
    header = (payload.get("body") or {}).get("compositeViewHeader") or {}
    return (header.get("contactInfo") or {}).get("primaryEmail") or None


_MANAGER_IN_ORGANIZATION = re.compile(r"\(([^()]+)\)\s*$")


def manager_from_organization(organization: str | None) -> str | None:
    """'Engineering : Platform II (Grace Hopper)' -> 'Grace Hopper'."""
    if not organization:
        return None
    found = _MANAGER_IN_ORGANIZATION.search(organization)
    return found.group(1).strip() if found else None


def person_from_search_result(result: Json) -> Person:
    organization = result.get("subtitle2") or None
    return Person(
        name=result.get("description") or "",
        worker_id=result.get("instanceID") or "",
        title=result.get("subtitle1") or None,
        location=result.get("subtitle3") or None,
        organization=organization,
        manager=manager_from_organization(organization),
    )


def instance_ids_from_search_ndjson(text: str, prefixes: tuple[str, ...]) -> list[str]:
    """Instance ids whose class prefix matches, in result order (tasks, orgs, ...)."""
    found: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        block = json.loads(line)
        if block.get("type") != "SearchResultSet":
            continue
        for result in block.get("results", []):
            instance_id = str(result.get("instanceID") or "")
            if instance_id.startswith(prefixes) and instance_id not in found:
                found.append(instance_id)
    return found


def organization_from_profile(payload: Json) -> str:
    """Worker profile -> the supervisory org they sit in, e.g. 'Eng : Platform II (Grace Hopper)'.

    Workday renders it as a breadcrumb ('Acme >> Eng >> Eng : Platform II (...)'); only the
    last hop names the org the worker actually belongs to.
    """
    header = (payload.get("body") or {}).get("compositeViewHeader") or {}
    breadcrumb = ((header.get("contactInfo") or {}).get("organization") or "").strip()
    return breadcrumb.split(">>")[-1].strip()


def people_from_search_ndjson(text: str) -> list[Person]:
    """Parse the x-ndjson body of /fs/v3/search into de-duplicated people."""
    people: dict[str, Person] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        block = json.loads(line)
        if block.get("type") != "SearchResultSet":
            continue
        for result in block.get("results", []):
            worker_id = result.get("instanceID", "")
            if worker_id.startswith(WORKER_PREFIX):
                people.setdefault(worker_id, person_from_search_result(result))
    return list(people.values())


# --- Org-chart navigator parsing -------------------------------------------
@dataclass(slots=True)
class NavigatorNode:
    instance_id: str | None  # 2500$ for a sub-org (a manager), 247$ for a leaf person
    instance_text: str | None
    owner_id: str | None
    owner_name: str | None
    title: str | None
    location: str | None
    has_children: bool

    @property
    def is_organization(self) -> bool:
        return bool(self.instance_id and self.instance_id.startswith(ORGANIZATION_PREFIX))


@dataclass(slots=True)
class NavigatorResponse:
    parent: NavigatorNode | None = None
    self_node: NavigatorNode | None = None
    children: list[NavigatorNode] = field(default_factory=list)
    child_count: int | None = None
    child_chunking_url: str | None = None


def _first_instance(container: object) -> Json:
    instances = (container or {}).get("instances") if isinstance(container, dict) else None
    return (instances or [{}])[0]


def parse_navigator_detail(detail: Json) -> NavigatorNode:
    instance = _first_instance(detail.get("navigatorInstance"))
    items = detail.get("navigatorItems") or [{}]
    item = items[0] if items else {}
    owner = _first_instance(item.get("owner"))
    location = _first_instance(item.get("instanceTwo"))
    return NavigatorNode(
        instance_id=instance.get("instanceId"),
        instance_text=instance.get("text"),
        owner_id=owner.get("instanceId"),
        owner_name=owner.get("text"),
        title=item.get("detailOne") or None,
        location=location.get("text") or item.get("detailTwo") or None,
        has_children=bool(detail.get("hasChildren")),
    )


def parse_navigator_response(payload: Json) -> NavigatorResponse:
    response = NavigatorResponse()
    containers = (payload.get("body") or {}).get("navigatorContainers") or []
    for container in containers:
        nodes = [parse_navigator_detail(d) for d in container.get("navigatorDetails", [])]
        match container.get("navigatorContainerNodeType"):
            case "PARENT":
                response.parent = nodes[0] if nodes else None
            case "SELF":
                response.self_node = nodes[0] if nodes else None
            case "CHILD_NODES_AND_LEAVES":
                response.children = nodes
                response.child_count = container.get("count")
                response.child_chunking_url = container.get("chunkingUrl") or None
    return response


# --- Offline org-chart cache -----------------------------------------------
def save_organization_chart(people: list[Person], path: Path = ORGANIZATION_CHART_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "crawled_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "count": len(people),
        "people": [asdict(person) for person in people],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def load_organization_chart(path: Path = ORGANIZATION_CHART_FILE) -> list[Person]:
    if not path.exists():
        raise WorkdayError(f"no cached org chart at {path}; run `beetday crawl` first")
    payload = read_json_file(path)
    return [Person(**record) for record in payload["people"]]


def _fold(text: str) -> str:
    """Casefold and drop combining diacritics, so 'cong' matches 'Công' and 'nguyen' matches 'Nguyễn'."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def search_organization_chart(people: list[Person], query: str) -> list[Person]:
    needle = _fold(query)
    fields = ("name", "title", "organization", "location", "email")
    return [person for person in people if needle in _fold(" ".join(value for value in (getattr(person, f) for f in fields) if value))]


# --- Workday HTTP client ---------------------------------------------------
_SESSION_ENDED = re.compile(r"(log ?in|sign ?in|session (has )?(expired|ended|timed)|authenticat)", re.IGNORECASE)


def looks_like_session_end(body: str) -> bool:
    """True only for error bodies that mean the session/login is gone.

    Workday returns `Application_Error` for transient server faults too (e.g. an
    internal cast/telemetry bug), so the mere presence of that tag must NOT be
    read as expiry — only a login/session message is decisive.
    """
    return bool(_SESSION_ENDED.search(body))


def looks_like_login_page(body: str) -> bool:
    """True when Workday served its HTML login redirect in place of JSON.

    An expired session comes back as HTTP 200 `text/html` whose script sets window.location to /wday/authgwy/<tenant>/login.htmld — no 3xx, no 401, so only the body gives it away. Every endpoint here answers JSON or x-ndjson, so anything not starting with `{`/`[` is not data.
    """
    return not body.lstrip().startswith(("{", "[")) and looks_like_session_end(body)


def tenant_relative_path(url: str) -> str:
    """Guard a server-supplied URL before requesting it with the session cookie attached.

    httpx applies this client's headers — Cookie included — to absolute URLs too,
    so an off-tenant `chunkingUrl` would hand the live Workday session to whatever
    host it names. A leading `/` keeps the request on the tenant's own base URL
    (httpx folds even `//host/path` back onto the base host).
    """
    if not url.startswith("/"):
        raise WorkdayError(f"refusing off-tenant URL from Workday: {url!r}")
    return url


class WorkdayClient:
    def __init__(self, session: Session, initial_step: str = ""):
        import httpx  # deferred: keeps the parsing functions above dependency-free

        self.session = session
        self.initial_step = initial_step or session.navigator_initial_step
        self._httpx = httpx
        self._client = httpx.Client(
            base_url=session.base_url,
            timeout=httpx.Timeout(30.0),
            follow_redirects=False,
            headers=self._default_headers(),
        )
        self._worker_id: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._client.close()

    def _default_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.session.user_agent,
            "Cookie": self.session.cookie,
            "Accept": "application/json",
        }
        if self.session.session_secure_token:
            headers["Session-Secure-Token"] = self.session.session_secure_token
        if self.session.workday_client:
            headers["X-Workday-Client"] = self.session.workday_client
        return headers

    def _request(self, method: str, path: str, **kwargs):
        try:
            response = self._client.request(method, path, **kwargs)
        except self._httpx.HTTPError as error:
            raise WorkdayError(f"{method} {path} failed: {error}") from error
        where = f"{method} {response.request.url}"  # the absolute URL, so a bad answer names the endpoint that gave it
        if response.is_redirect:
            raise SessionExpired(f"{where} redirected to login; run `beetday auth` to refresh")
        if response.status_code in (401, 403):
            raise SessionExpired(f"{where} -> HTTP {response.status_code}, session rejected; run `beetday auth` to refresh")
        snippet = response.text[:2000]
        if "Application_Error" in snippet:
            if looks_like_session_end(snippet):
                raise SessionExpired(f"{where} -> Workday session ended; run `beetday auth` to refresh")
            raise WorkdayError(f"{where} -> Workday server error (Application_Error)")
        if response.status_code >= 400:
            raise WorkdayError(f"{where} -> HTTP {response.status_code}")
        if looks_like_login_page(snippet):
            raise SessionExpired(f"{where} -> Workday's login page instead of data; run `beetday auth` to refresh")
        return response

    def _json(self, method: str, path: str, **kwargs) -> Json:
        """Request and decode, naming the call in the error; a bare JSONDecodeError says nothing about where the body came from."""
        response = self._request(method, path, **kwargs)
        try:
            return response.json()
        except json.JSONDecodeError as error:
            error.add_note(f"while decoding the response body of {method} {response.request.url}")
            raise

    def _boot_config(self) -> Json:
        return self._json("GET", f"/{self.session.tenant}/boot-config")

    @property
    def worker_id(self) -> str:
        if self._worker_id is None:
            app_root = self._boot_config().get("appRoot", "")
            found = re.search(r'"currentUser":\{[^}]*?"iid":"([^"]+)"', app_root)
            self._worker_id = found.group(1) if found else ""
        return self._worker_id

    def enrich(self) -> None:
        """Fill the secure token / client version from boot-config; proves the session works."""
        if self.session.session_secure_token and self.session.workday_client:
            self.worker_id  # still confirm the session is live  # noqa: B018
            return
        boot = self._boot_config()
        login = boot.get("loginInfo") or {}
        if not self.session.workday_client:
            self.session.workday_client = login.get("clientRevision", "")
        if not self.session.session_secure_token:
            found = re.search(r'"sessionSecureToken":"([^"]+)"', boot.get("appRoot", ""))
            if found:
                self.session.session_secure_token = found.group(1)
        self._client.headers.update(self._default_headers())

    def search(self, query: str) -> list[Person]:
        response = self._request(
            "GET",
            f"/wday/pex/fs/{self.session.tenant}/fs/v3/search",
            params={"q": query},
            headers={
                "X_WD_CLIENT_ID": "SEARCH_CLIENT",
                "Accept": "application/json, application/x-ndjson, */*",
            },
        )
        return people_from_search_ndjson(response.text)

    def worker_email(self, worker_id: str) -> str | None:
        payload = self._json("GET", f"/{self.session.tenant}/inst/{WORKER_PROFILE_CONTEXT}/{worker_id}.htmld")
        return email_from_profile(payload)

    def expand_organization(self, organization_id: str) -> NavigatorResponse:
        body = {
            "initial-step": self.initial_step,
            "navigable-instance-iid": organization_id,
            "navigable-instance-did": "",
            "navigable-worker-iid": self.worker_id,
            "effective": "",
        }
        payload = self._json(
            "POST",
            f"/{self.session.tenant}/navigable/{organization_id}.htmld",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response = parse_navigator_response(payload)
        # Children arrive in pages of ~20; follow the chunk endpoint until complete.
        while response.child_chunking_url and response.child_count and len(response.children) < response.child_count:
            chunk = self._json(
                "GET",
                f"{tenant_relative_path(response.child_chunking_url)}.htmld",
                params={"startRow": len(response.children) + 1, "maxRows": response.child_count},
            )
            details = chunk.get("navigatorDetails") or []
            if not details:
                break
            response.children.extend(parse_navigator_detail(detail) for detail in details)
        return response

    def _expand_with_retries(self, organization_id: str, attempts: int = 3) -> NavigatorResponse:
        for attempt in range(1, attempts + 1):
            try:
                return self.expand_organization(organization_id)
            except SessionExpired:
                raise  # a real auth failure: do not retry, let the crawl stop
            except WorkdayError:
                if attempt == attempts:
                    raise
                time.sleep(0.5 * 2 ** (attempt - 1))  # transient server fault: back off and retry
        raise WorkdayError(f"failed to expand {organization_id}")  # unreachable

    def _email_with_retries(self, worker_id: str, attempts: int = 3) -> str | None:
        for attempt in range(1, attempts + 1):
            try:
                return self.worker_email(worker_id)
            except SessionExpired:
                raise
            except WorkdayError:
                if attempt == attempts:
                    raise
                time.sleep(0.5 * 2 ** (attempt - 1))
        return None  # unreachable

    def _search_instance_ids(self, query: str, prefixes: tuple[str, ...]) -> list[str]:
        response = self._request(
            "GET",
            f"/wday/pex/fs/{self.session.tenant}/fs/v3/search",
            params={"q": query},
            headers={
                "X_WD_CLIENT_ID": "SEARCH_CLIENT",
                "Accept": "application/json, application/x-ndjson, */*",
            },
        )
        return instance_ids_from_search_ndjson(response.text, prefixes)

    def discover_organization_id(self) -> str:
        """Any supervisory org id, used as a probe target: the one the user sits in."""
        profile = self._json(
            "GET", f"/{self.session.tenant}/inst/{WORKER_PROFILE_CONTEXT}/{self.worker_id}.htmld"
        )
        name = organization_from_profile(profile)
        for organization_id in self._search_instance_ids(name, (ORGANIZATION_PREFIX,)) if name else []:
            return organization_id
        raise WorkdayError("could not find your supervisory organization; pass --root and --initial-step")

    def discover_initial_step(self, organization_id: str) -> str:
        """The navigator refuses without an Org Chart task id, and only accepts a real one.

        Search names candidates; the only reliable test is whether the navigator accepts one,
        so probe them. Read-only, and anything that is not an org chart simply fails.
        """
        for candidate in self._search_instance_ids("org chart", TASK_PREFIXES):
            previous, self.initial_step = self.initial_step, candidate
            try:
                self.expand_organization(organization_id)
            except WorkdayError:
                self.initial_step = previous
                continue
            return candidate
        raise WorkdayError("could not find an Org Chart task; pass --initial-step")

    def discover_root_organization(self, organization_id: str) -> str:
        """Walk PARENT up from any org; the node without a parent is the root."""
        seen: set[str] = set()
        while organization_id not in seen:
            seen.add(organization_id)
            parent = self.expand_organization(organization_id).parent
            if not parent or not parent.instance_id:
                return organization_id
            organization_id = parent.instance_id
        return organization_id

    def discover_tenant_ids(self) -> tuple[str, str]:
        """Return (initial_step, root_organization_id) for this tenant."""
        organization_id = self.discover_organization_id()
        initial_step = self.discover_initial_step(organization_id)
        return initial_step, self.discover_root_organization(organization_id)

    def crawl(
        self,
        root: str = "",
        delay: float = 0.3,
        report=None,
        checkpoint=None,
        checkpoint_every: int = 50,
        known_emails: dict[str, str] | None = None,
    ) -> tuple[list[Person], list[str], list[str]]:
        root = root or self.session.root_organization_id
        if not root:
            raise WorkdayError("no root organization known; re-run `beetday auth` or pass --root")
        people: dict[str, Person] = {}
        visited: set[str] = set()
        failed: list[str] = []
        queue: list[str] = [root]
        consecutive_failures = 0
        while queue:
            organization_id = queue.pop()
            if organization_id in visited:
                continue
            visited.add(organization_id)
            try:
                response = self._expand_with_retries(organization_id)
            except SessionExpired:
                if checkpoint:
                    checkpoint(list(people.values()))  # don't lose progress on a real expiry
                raise
            except WorkdayError:
                failed.append(organization_id)  # transient fault survived retries: skip this subtree
                consecutive_failures += 1
                if consecutive_failures >= 8:
                    if checkpoint:
                        checkpoint(list(people.values()))
                    raise WorkdayError(f"aborting after {consecutive_failures} consecutive org failures (last {organization_id}); {len(people)} people collected so far")
                continue
            consecutive_failures = 0
            manager = response.self_node
            organization_name = manager.instance_text if manager else None
            for child in response.children:
                if not child.owner_id:
                    continue
                people[child.owner_id] = Person(
                    name=child.owner_name or child.instance_text or child.owner_id,
                    worker_id=child.owner_id,
                    title=child.title,
                    location=child.location,
                    organization=organization_name,
                    manager=manager.owner_name if manager else None,
                    manager_id=manager.owner_id if manager else None,
                )
                if child.has_children and child.is_organization and child.instance_id not in visited:
                    queue.append(child.instance_id)
            if manager and manager.owner_id:  # capture the manager (esp. the root owner)
                parent = response.parent
                people.setdefault(
                    manager.owner_id,
                    Person(
                        name=manager.owner_name or manager.owner_id,
                        worker_id=manager.owner_id,
                        title=manager.title,
                        location=manager.location,
                        organization=(parent.instance_text if parent else None) or organization_name,
                        manager=parent.owner_name if parent else None,
                        manager_id=parent.owner_id if parent else None,
                    ),
                )
            if report:
                report(f"structure: {len(people)} people · {len(visited)} orgs · {len(queue)} queued")
            if checkpoint and len(visited) % checkpoint_every == 0:
                checkpoint(list(people.values()))
            time.sleep(delay)

        # Phase 2: fetch each person's primary work email from their profile (one call each).
        roster = list(people.values())
        known_emails = known_emails or {}
        failed_emails: list[str] = []
        consecutive_failures = 0
        for index, person in enumerate(roster, start=1):
            email = known_emails.get(person.worker_id)
            if not email:
                try:
                    email = self._email_with_retries(person.worker_id)
                except SessionExpired:
                    if checkpoint:
                        checkpoint(roster)
                    raise
                except WorkdayError:
                    failed_emails.append(person.worker_id)
                    consecutive_failures += 1
                    if consecutive_failures >= 8:
                        if checkpoint:
                            checkpoint(roster)
                        raise WorkdayError(f"aborting after {consecutive_failures} consecutive email failures (last {person.worker_id}); {index}/{len(roster)} attempted")
                    if report:
                        report(f"emails: {index}/{len(roster)} · {len(failed_emails)} failed")
                    continue
                time.sleep(delay)
            consecutive_failures = 0
            person.email = email
            if report:
                report(f"emails: {index}/{len(roster)} · {len(failed_emails)} failed")
            if checkpoint and index % checkpoint_every == 0:
                checkpoint(roster)
        return roster, failed, failed_emails

    def management_chain(self, name: str) -> list[Person]:
        matches = self.search(name)
        if not matches:
            return []
        chain = [_best_match(matches, name)]
        seen = {chain[0].worker_id}
        while chain[-1].manager:
            candidates = [p for p in self.search(chain[-1].manager) if p.name == chain[-1].manager]
            if not candidates or candidates[0].worker_id in seen:
                break
            chain.append(candidates[0])
            seen.add(candidates[0].worker_id)
        return chain


# --- Command line ----------------------------------------------------------
def _best_match(people: list[Person], query: str) -> Person:
    exact = [p for p in people if p.name.casefold() == query.casefold()]
    return exact[0] if exact else people[0]


def _open_client(**overrides: str) -> WorkdayClient:
    client = WorkdayClient(Session.load(), **overrides)
    client.enrich()
    return client


def _load_cache_index() -> dict[str, Person]:
    """Worker-id -> cached Person, for enriching live results with emails (empty if no cache)."""
    if not ORGANIZATION_CHART_FILE.exists():
        return {}
    try:  # pass the path rather than lean on the default, which froze at import time
        return {person.worker_id: person for person in load_organization_chart(ORGANIZATION_CHART_FILE)}
    except (OSError, ValueError, KeyError, TypeError):  # TypeError: cache written by a different field set
        return {}


def _format_person(person: Person, by_id: dict[str, Person] | None = None) -> str:
    by_id = by_id or {}
    cached = by_id.get(person.worker_id)
    email = person.email or (cached.email if cached else None)
    manager = person.manager or (cached.manager if cached else None)
    manager_id = person.manager_id or (cached.manager_id if cached else None)
    bits = [f"{person.name} <{email}>" if email else person.name]
    if person.title:
        bits.append(person.title)
    if person.organization:
        bits.append(person.organization)
    if person.location:
        bits.append(person.location)
    line = "  —  ".join(bits)
    if manager:
        boss = by_id.get(manager_id) if manager_id else None
        line += f"   →  {manager} <{boss.email}>" if boss and boss.email else f"   →  {manager}"
    return line


def _print_people(people: list[Person], by_id: dict[str, Person] | None = None) -> int:
    if not people:
        print("no matches")
        return 1
    for person in people:
        print(_format_person(person, by_id))
    return 0


def command_auth(args: argparse.Namespace) -> int:
    if args.har:
        session = session_from_har(Path(args.har))
    else:
        if sys.stdin.isatty():
            print("Paste the browser 'Copy as cURL' for any Workday request, then Ctrl-D:", file=sys.stderr)
        session = session_from_curl(sys.stdin.read())
    with WorkdayClient(session) as client:
        client.enrich()  # fills token/client version and proves the session is live
        worker = client.worker_id
        session.navigator_initial_step, session.root_organization_id = client.discover_tenant_ids()
    session.save()
    print(f"saved session for tenant {session.tenant!r} (worker {worker}) -> {SESSION_FILE}")
    print(f"discovered org chart task {session.navigator_initial_step}, root org {session.root_organization_id}")
    return 0


def command_search(args: argparse.Namespace) -> int:
    with _open_client() as client:
        results = client.search(args.query)
    return _print_people(results, _load_cache_index())


def command_manager(args: argparse.Namespace) -> int:
    with _open_client() as client:
        matches = client.search(args.name)
    if not matches:
        print("no matches")
        return 1
    by_id = _load_cache_index()
    person = _best_match(matches, args.name)
    cached = by_id.get(person.worker_id)
    email = person.email or (cached.email if cached else None)
    manager = person.manager or (cached.manager if cached else None)
    manager_id = person.manager_id or (cached.manager_id if cached else None)
    who = f"{person.name} <{email}>" if email else person.name
    if manager:
        boss = by_id.get(manager_id) if manager_id else None
        boss_label = f"{manager} <{boss.email}>" if boss and boss.email else manager
        print(f"{who} ({person.title or '?'}) reports to {boss_label}")
    else:
        print(f"{who} has no manager on record (top of the tree?)")
    return 0


def command_chain(args: argparse.Namespace) -> int:
    with _open_client() as client:
        chain = client.management_chain(args.name)
    if not chain:
        print("no matches")
        return 1
    by_id = _load_cache_index()
    for depth, person in enumerate(chain):
        cached = by_id.get(person.worker_id)
        email = person.email or (cached.email if cached else None)
        label = f"{person.name} <{email}>" if email else person.name
        arrow = "→ " if depth else ""
        print(f"{'  ' * depth}{arrow}{label} — {person.title or '?'}")
    return 0


def command_crawl(args: argparse.Namespace) -> int:
    def report(message: str) -> None:
        print(f"\r  {message:<72}", end="", file=sys.stderr, flush=True)

    known_emails = {p.worker_id: p.email for p in _load_cache_index().values() if p.email}
    with _open_client(initial_step=args.initial_step) as client:
        people, failed_orgs, failed_emails = client.crawl(
            root=args.root,
            delay=args.delay,
            report=report,
            checkpoint=save_organization_chart,
            known_emails=known_emails,
        )
    print(file=sys.stderr)
    save_organization_chart(people)
    with_email = sum(1 for p in people if p.email)
    print(f"cached {len(people)} people ({with_email} with email) -> {ORGANIZATION_CHART_FILE}")
    if failed_orgs:
        print(
            f"note: {len(failed_orgs)} org(s) failed after retries and were skipped (their sub-teams may be missing): {', '.join(failed_orgs)}",
            file=sys.stderr,
        )
    if failed_emails:
        print(f"note: {len(failed_emails)} email lookup(s) failed after retries", file=sys.stderr)
    return 0


def command_find(args: argparse.Namespace) -> int:
    cache = load_organization_chart()
    by_id = {person.worker_id: person for person in cache}
    return _print_people(search_organization_chart(cache, args.query), by_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="beetday", description=__doc__.splitlines()[0])
    subcommands = parser.add_subparsers(dest="command", required=True)

    auth = subcommands.add_parser("auth", help="save/refresh the browser session")
    source = auth.add_mutually_exclusive_group()
    source.add_argument("--curl", action="store_true", help="read a browser 'Copy as cURL' from stdin (default)")
    source.add_argument("--har", metavar="FILE", help="extract the session from a .har capture instead")
    auth.set_defaults(func=command_auth)

    search = subcommands.add_parser("search", help="live people/org search")
    search.add_argument("query")
    search.set_defaults(func=command_search)

    manager = subcommands.add_parser("manager", help="who a person reports to (live)")
    manager.add_argument("name")
    manager.set_defaults(func=command_manager)

    chain = subcommands.add_parser("chain", help="the full management chain above a person (live)")
    chain.add_argument("name")
    chain.set_defaults(func=command_chain)

    crawl = subcommands.add_parser("crawl", help="crawl the whole org chart into the local cache")
    crawl.add_argument("--root", default="", help="root supervisory organization id (overrides the one found at auth)")
    crawl.add_argument("--initial-step", default="", help="navigator 'initial-step' task id (overrides the one found at auth)")
    crawl.add_argument("--delay", type=float, default=0.3, help="seconds between calls (be polite)")
    crawl.set_defaults(func=command_crawl)

    find = subcommands.add_parser("find", help="search the cached org chart, offline and instant")
    find.add_argument("query")
    find.set_defaults(func=command_find)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except SessionExpired as error:
        print(f"session expired: {error}", file=sys.stderr)
        return 2
    except WorkdayError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
