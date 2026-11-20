"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const S = require("../../casecal/web/store.js");

const CONFIG = { kind: "github", api: "https://api.github.com", repo: "owner/repo", issue: 7, token: "github_pat_TEST",
  cursor: 1000, since: "2026-11-19T02:17:00Z" };

/** A stand-in for the GitHub comments endpoint: same paging, same status codes. */
function fakeGitHub(comments, overrides = {}) {
  const calls = [];
  const send = async (url, options = {}) => {
    calls.push({ url, options });
    if (overrides.throws) throw new TypeError("Failed to fetch");
    if (overrides.status) {
      return { status: overrides.status, ok: false, headers: new Map(Object.entries(overrides.headers || {})),
        json: async () => { if (overrides.body === undefined) throw new SyntaxError("no body"); return overrides.body; } };
    }
    if (options.method === "POST") {
      const comment = { id: 5000 + comments.length, body: JSON.parse(options.body).body };
      comments.push(comment);
      return { status: 201, ok: true, headers: new Map(), json: async () => comment };
    }
    const query = new URL(url).searchParams;
    const size = Number(query.get("per_page")), page = Number(query.get("page"));
    return { status: 200, ok: true, headers: new Map(), json: async () => comments.slice((page - 1) * size, page * size) };
  };
  return { send, calls };
}

test("the list holds only comments newer than the cursor, oldest first", async () => {
  const comments = [{ id: 1003, body: "c" }, { id: 999, body: "old" }, { id: 1000, body: "cursor" }, { id: 1001, body: "a" }];
  const { send, calls } = fakeGitHub(comments);
  const store = S.open(CONFIG, { fetch: send });
  assert.deepEqual(await store.list(1000), [{ id: 1001, body: "a" }, { id: 1003, body: "c" }]);
  const { url, options } = calls[0];
  assert.ok(url.startsWith("https://api.github.com/repos/owner/repo/issues/7/comments?per_page=100&page=1"));
  assert.ok(url.includes("since=2026-11-19T02%3A17%3A00Z"));
  assert.equal(options.headers.Authorization, "Bearer github_pat_TEST");
  assert.equal(options.cache, "no-store");
});

test("the time filter is used only when it is a plain timestamp", async () => {
  for (const since of [undefined, null, "", "ieri", "2026-11-19", "2026-11-19T02:17:00Z&page=9", 20261119]) {
    const { send, calls } = fakeGitHub([{ id: 1001, body: "a" }]);
    assert.equal((await S.open({ ...CONFIG, since }, { fetch: send }).list(1000)).length, 1);
    assert.ok(!calls[0].url.includes("since"), String(since));
  }
});

test("only headers that GitHub accepts from a browser are sent", async () => {
  // A header outside this list makes the browser's preflight fail, and with it every request.
  const allowed = ["authorization", "accept", "content-type"];
  const { send, calls } = fakeGitHub([]);
  const store = S.open(CONFIG, { fetch: send });
  await store.list(0);
  await store.append("casecal:1:iv:ct");
  for (const call of calls) {
    for (const name of Object.keys(call.options.headers)) assert.ok(allowed.includes(name.toLowerCase()), name);
  }
});

test("long lists are read page by page", async () => {
  const comments = Array.from({ length: 230 }, (_, i) => ({ id: 2000 + i, body: "c" + i }));
  const { send, calls } = fakeGitHub(comments);
  const listed = await S.open(CONFIG, { fetch: send }).list(1000);
  assert.equal(listed.length, 230);
  assert.equal(calls.length, 3);
});

test("a log too long to read whole is a failure, not a silent cut", async () => {
  const comments = Array.from({ length: 5000 }, (_, i) => ({ id: 2000 + i, body: "c" }));
  await assert.rejects(S.open(CONFIG, { fetch: fakeGitHub(comments).send }).list(1000), (error) => error.code === "server");
  comments.pop();
  assert.equal((await S.open(CONFIG, { fetch: fakeGitHub(comments).send }).list(1000)).length, 4999);
});

test("a change is appended as one comment", async () => {
  const comments = [];
  const { send, calls } = fakeGitHub(comments);
  const saved = await S.open(CONFIG, { fetch: send }).append("casecal:1:iv:ct");
  assert.deepEqual(saved, { id: 5000, body: "casecal:1:iv:ct" });
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), { body: "casecal:1:iv:ct" });
  assert.equal(calls[0].options.headers["Content-Type"], "application/json");
});

test("failures carry a code the page can explain", async () => {
  const cases = [
    [{ throws: true }, "offline"],
    [{ status: 401 }, "token"],
    [{ status: 403 }, "denied"],
    [{ status: 404 }, "denied"],
    [{ status: 403, body: { message: "Resource not accessible by personal access token" } }, "denied"],
    [{ status: 410, body: { message: "Issues are disabled for this repo" } }, "denied"],
    [{ status: 403, headers: { "x-ratelimit-remaining": "0" } }, "limit"],
    // Writing too fast is answered with a 403 as well: only the message tells it apart.
    [{ status: 403, body: { message: "You have exceeded a secondary rate limit. Please wait a few minutes." } }, "limit"],
    [{ status: 429 }, "limit"],
    [{ status: 502 }, "server"]
  ];
  for (const [overrides, code] of cases) {
    const store = S.open(CONFIG, { fetch: fakeGitHub([], overrides).send });
    await assert.rejects(store.list(0), (error) => error.code === code, JSON.stringify(overrides));
    await assert.rejects(store.append("x"), (error) => error.code === code);
  }
});

test("an unexpected answer is a server failure, not a crash", async () => {
  const odd = async () => ({ status: 200, ok: true, headers: new Map(), json: async () => ({ message: "not a list" }) });
  await assert.rejects(S.open(CONFIG, { fetch: odd }).list(0), (error) => error.code === "server");
  const broken = async () => ({ status: 200, ok: true, headers: new Map(), json: async () => { throw new SyntaxError("bad json"); } });
  await assert.rejects(S.open(CONFIG, { fetch: broken }).list(0), (error) => error.code === "server");
});

test("settings that do not describe a usable log leave editing off", () => {
  const off = [
    null, undefined, "github", {},
    { ...CONFIG, kind: "ftp" },
    { ...CONFIG, api: "https://evil.example" },      // the page may only talk to GitHub
    { ...CONFIG, repo: "owner/repo/../../user" },   // spliced into a URL: must look like a repository
    { ...CONFIG, repo: "owner" },
    { ...CONFIG, issue: "7" },
    { ...CONFIG, issue: 0 },
    { ...CONFIG, issue: 7.5 },
    { ...CONFIG, token: "" },
    { kind: "local" }                                // no storage offered
  ];
  for (const config of off) assert.equal(S.open(config, {}), null, JSON.stringify(config));
  assert.equal(S.open(CONFIG, {}).kind, "github");
});

test("the demo store keeps edits in the browser", async () => {
  const data = new Map();
  const storage = { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, v) };
  const store = S.open({ kind: "local" }, { storage });
  assert.equal(store.kind, "local");
  assert.deepEqual(await store.list(0), []);
  assert.deepEqual(await store.append("first"), { id: 1, body: "first" });
  assert.deepEqual(await store.append("second"), { id: 2, body: "second" });
  assert.deepEqual(await store.list(1), [{ id: 2, body: "second" }]);
  // A second tab sees the same list.
  assert.deepEqual(await S.open({ kind: "local" }, { storage }).list(0), [{ id: 1, body: "first" }, { id: 2, body: "second" }]);
  data.set("casecal.demo.log", "{broken");
  assert.deepEqual(await store.list(0), []);
  const full = { getItem: () => "[]", setItem: () => { throw new Error("QuotaExceededError"); } };
  await assert.rejects(S.open({ kind: "local" }, { storage: full }).append("x"), (error) => error.code === "offline");
});
