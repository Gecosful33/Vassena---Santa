/* Where the family's entries are written and read: an append-only list of comments.
 *
 * In production the list is the comments of one issue of the repository, reached
 * through the GitHub REST API with a token that the nightly job delivers inside the
 * encrypted page (casecal/familylog.py). Every comment is ciphertext, so GitHub only
 * ever stores what it cannot read. The demo uses the browser's own storage instead.
 *
 * Both stores speak the same two verbs: list(afterId) resolves to the comments newer
 * than the one with that id, oldest first, as [{ id, body }]; append(body) adds one.
 * Failures reject with an Error whose `code` is "offline", "token", "denied",
 * "limit" or "server".
 */
var CaseCalStore = (function () {
  "use strict";

  var GITHUB_API = "https://api.github.com"; // the only host the page's CSP lets it reach
  var PAGE_SIZE = 100, MAX_PAGES = 50;
  var SINCE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/;

  function failure(code) {
    var error = new Error("family log: " + code);
    error.code = code;
    return error;
  }

  /**
   * `config.since` (a time a little before the last comment the nightly job read) keeps
   * the request small: GitHub returns only comments touched after it, and the cursor
   * does the exact cut.
   *
   * Only headers that GitHub accepts from a browser on another origin are sent
   * (Authorization, Accept, Content-Type). The API version header is not among them,
   * so it is left out: a refused preflight would stop every request.
   */
  function github(config, fetchImpl) {
    var send = fetchImpl || function (target, options) { return fetch(target, options); };
    var url = GITHUB_API + "/repos/" + config.repo + "/issues/" + config.issue + "/comments";
    var since = typeof config.since === "string" && SINCE.test(config.since) ? config.since : null;
    var headers = {
      Authorization: "Bearer " + config.token,
      Accept: "application/vnd.github+json"
    };

    async function call(target, options) {
      var response;
      try {
        response = await send(target, options);
      } catch (err) {
        throw failure("offline");
      }
      if (response.status === 401) throw failure("token");
      if (response.status === 429) throw failure("limit");
      if (response.status === 403) {
        // The same status means "slow down" and "not allowed": the answer says which.
        var spent = response.headers && response.headers.get("x-ratelimit-remaining") === "0";
        var why = "";
        try { why = String((await response.json()).message || ""); } catch (err) { /* no readable answer */ }
        throw failure(spent || /rate limit/i.test(why) ? "limit" : "denied");
      }
      if (response.status === 404 || response.status === 410) throw failure("denied");
      if (!response.ok) throw failure("server");
      try {
        return await response.json();
      } catch (err) {
        throw failure("server");
      }
    }

    async function list(afterId) {
      var found = [];
      for (var page = 1; page <= MAX_PAGES; page++) {
        var query = "?per_page=" + PAGE_SIZE + "&page=" + page + (since ? "&since=" + encodeURIComponent(since) : "");
        var batch = await call(url + query, { headers: headers, cache: "no-store" });
        if (!Array.isArray(batch)) throw failure("server");
        batch.forEach(function (comment) {
          if (comment && typeof comment.id === "number" && comment.id > afterId) {
            found.push({ id: comment.id, body: String(comment.body || "") });
          }
        });
        if (batch.length < PAGE_SIZE) return found.sort(function (a, b) { return a.id - b.id; });
      }
      // Never a silent cut: the newest comments are the ones that would go missing.
      throw failure("server");
    }

    async function append(body) {
      var created = await call(url, {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, headers),
        body: JSON.stringify({ body: body })
      });
      if (!created || typeof created.id !== "number") throw failure("server");
      return { id: created.id, body: body };
    }

    return { kind: "github", list: list, append: append };
  }

  /** The demo's store: edits stay in the browser that made them. */
  function local(storage, name) {
    function read() {
      try {
        var saved = JSON.parse(storage.getItem(name) || "[]");
        return Array.isArray(saved) ? saved : [];
      } catch (err) {
        return [];
      }
    }
    return {
      kind: "local",
      list: async function (afterId) {
        return read().filter(function (comment) { return comment.id > afterId; });
      },
      append: async function (body) {
        var all = read();
        var comment = { id: all.length ? all[all.length - 1].id + 1 : 1, body: body };
        try {
          storage.setItem(name, JSON.stringify(all.concat([comment])));
        } catch (err) {
          throw failure("offline");
        }
        return comment;
      }
    };
  }

  /**
   * The store described by the page's settings, or null when editing is off.
   * Settings arrive inside the decrypted page, yet are still checked: a repository
   * name is spliced into a URL and must look like one.
   */
  function open(config, options) {
    options = options || {};
    if (!config || typeof config !== "object") return null;
    if (config.kind === "local") return options.storage ? local(options.storage, "casecal.demo.log") : null;
    if (config.kind !== "github" || config.api !== GITHUB_API) return null;
    if (typeof config.repo !== "string" || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(config.repo)) return null;
    if (typeof config.issue !== "number" || config.issue % 1 !== 0 || config.issue < 1) return null;
    if (typeof config.token !== "string" || !config.token) return null;
    return github(config, options.fetch);
  }

  return { open: open, github: github, local: local, GITHUB_API: GITHUB_API };
})();
if (typeof module !== "undefined" && module.exports) { module.exports = CaseCalStore; }
