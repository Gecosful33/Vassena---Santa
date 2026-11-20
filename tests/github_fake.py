"""A stand-in for the part of the GitHub API that the family log uses.

Like ``portal_html.py`` for the managers' portals: the tests never talk to the
real service. It keeps the same paging, status codes and answer shapes.
"""

TOKEN = "github_pat_TESTTOKEN"
SECONDARY_LIMIT = {"message": "You have exceeded a secondary rate limit. Please wait a few minutes."}


class Response:
    def __init__(self, payload=None, status_code=200, headers=None):
        self._payload, self.status_code, self.headers = payload, status_code, dict(headers or {})

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeGitHub:
    """Just enough of the issues API, with the same paging and status codes."""

    def __init__(self, issues=None, comments=None, status=None, error=None, headers=None, answer=None, refusal=None):
        self.issues = list(issues or [])
        self.comments = {number: list(bodies) for number, bodies in (comments or {}).items()}
        self.status, self.error, self.headers, self.answer = status, error, headers, answer
        self.refusal = refusal if refusal is not None else {"message": "nope"}
        self.lock_status = None  # set to a status code to make locking fail
        self.calls, self.locked, self.next_id = [], [], 1000
        for bodies in self.comments.values():
            for index, body in enumerate(bodies):
                if not isinstance(body, dict):
                    bodies[index] = self._new_comment(body)

    def _new_comment(self, body):
        # One comment a minute, starting at noon: enough to tell their times apart.
        minute = self.next_id - 1000
        comment = {"id": self.next_id, "body": body,
                   "created_at": f"2026-11-20T{12 + minute // 60:02d}:{minute % 60:02d}:00Z"}
        self.next_id += 1
        return comment

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        assert url.startswith("https://api.github.com/repos/owner/repo")
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        path = url.split("/repos/owner/repo", 1)[1]
        self.calls.append((method, path, dict(params or {}), json))
        if self.error:
            raise self.error
        if self.status:
            return Response(self.refusal, self.status, self.headers)
        if self.answer is not None:
            return Response(self.answer)
        if (method, path) == ("GET", "/issues"):
            return Response(self._page(self.issues, params))
        if (method, path) == ("POST", "/issues"):
            issue = {"number": 40 + len(self.issues), "title": json["title"]}
            self.issues.append(issue)
            self.comments[issue["number"]] = []
            return Response(issue, 201)
        if method == "PUT" and path.endswith("/lock"):
            if self.lock_status:
                return Response({"message": "nope"}, self.lock_status)
            self.locked.append(int(path.split("/")[2]))
            return Response(None, 204)
        number = int(path.split("/")[2])
        if method == "GET":
            return Response(self._page(self.comments[number], params))
        comment = self._new_comment(json["body"])
        self.comments[number].append(comment)
        return Response(comment, 201)

    @staticmethod
    def _page(items, params):
        size, page = params["per_page"], params["page"]
        return items[(page - 1) * size: page * size]
