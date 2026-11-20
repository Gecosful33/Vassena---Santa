"use strict";
/* The envelope is written by Python and read by the browser: this proves the two agree. */
const test = require("node:test");
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const path = require("node:path");
const C = require("../../casecal/web/crypto.js");

const ROOT = path.resolve(__dirname, "..", "..");
const FAMILY = "famiglia-di-prova-2026";
const OWNER = "proprieta-di-prova-2026";

const PYTHON = `
import json, sys
from casecal.crypto import build_envelope, site_salt
print(json.dumps(build_envelope(
    family_passphrase=sys.argv[1], owner_passphrase=sys.argv[2],
    salt=site_salt("test-site"), iterations=100_000,
    family_payload={"title": "Città è già", "properties": []},
    owner_payload={"properties": [{"id": "a", "kpis": []}]},
)))
`;

const envelope = JSON.parse(
  execFileSync("python3", ["-c", PYTHON, FAMILY, OWNER], { cwd: ROOT, encoding: "utf8" })
);

test("the family passphrase opens the calendar but not the economics", async () => {
  const key = await C.deriveKey(FAMILY, envelope.kdf);
  const opened = await C.unlock(envelope, key);
  assert.equal(opened.tier, "family");
  assert.equal(opened.family.title, "Città è già");
  assert.equal(opened.owner, null);
  assert.equal(await C.unlockOwner(envelope, key), null);
});

test("the owner passphrase opens both tiers", async () => {
  const key = await C.deriveKey(OWNER, envelope.kdf);
  const opened = await C.unlock(envelope, key);
  assert.equal(opened.tier, "owner");
  assert.equal(opened.family.title, "Città è già");
  assert.deepEqual(opened.owner.properties, [{ id: "a", kpis: [] }]);
  // The family key recovered from the owner blob equals the one derived from the family passphrase.
  assert.deepEqual(Array.from(opened.familyKey), Array.from(await C.deriveKey(FAMILY, envelope.kdf)));
});

test("a wrong passphrase opens nothing", async () => {
  const key = await C.deriveKey("non-e-questa-la-password", envelope.kdf);
  assert.equal(await C.unlock(envelope, key), null);
});

test("passphrases are normalised like in Python", async () => {
  const padded = await C.deriveKey("  " + FAMILY + " \n", envelope.kdf);
  assert.equal((await C.unlock(envelope, padded)).tier, "family");
  // NFKC: a full-width digit typed by an IME folds to its ASCII form.
  const wide = await C.deriveKey(FAMILY.replace("2026", "202６"), envelope.kdf);
  assert.equal((await C.unlock(envelope, wide)).tier, "family");
});

test("a tampered ciphertext is rejected", async () => {
  const key = await C.deriveKey(FAMILY, envelope.kdf);
  const bytes = C.b64ToBytes(envelope.family.ct);
  bytes[10] ^= 1;
  const forged = { ...envelope, family: { iv: envelope.family.iv, ct: C.bytesToB64(bytes) } };
  assert.equal(await C.decryptBlob(key, forged.family, "casecal/v1/family"), null);
});

test("blobs cannot be swapped between tiers", async () => {
  const key = await C.deriveKey(FAMILY, envelope.kdf);
  assert.equal(await C.decryptBlob(key, envelope.family, "casecal/v1/owner"), null);
});

test("base64 helpers round-trip arbitrary bytes", () => {
  const bytes = new Uint8Array(256).map((_, i) => i);
  assert.deepEqual(Array.from(C.b64ToBytes(C.bytesToB64(bytes))), Array.from(bytes));
});

/* ---- events of the family log: written and read on both sides ---- */

const EVENT = { t: "set", id: "e_k3j9x2m1q8", prop: "santa", start: "2027-07-03", end: "2027-07-10",
  who: "Nonna Adà", note: "con il cane", by: "Giulia", at: "2026-11-20T12:30:00Z" };

function python(code, ...args) {
  return execFileSync("python3", ["-c", code, ...args], { cwd: ROOT, encoding: "utf8" }).trim();
}
const KEY_SETUP = `
import json, sys
from casecal.crypto import derive_key, site_salt
from casecal.family import decode_event, encode_event
key = derive_key(sys.argv[1], site_salt("test-site"), 100_000)
`;

test("an event written by Python is read by the page", async () => {
  const body = python(KEY_SETUP + "print(encode_event(key, json.loads(sys.argv[2])))", FAMILY, JSON.stringify(EVENT));
  const key = await C.deriveKey(FAMILY, envelope.kdf);
  assert.deepEqual(await C.decodeEvent(key, body), EVENT);
  assert.equal(await C.decodeEvent(await C.deriveKey(OWNER, envelope.kdf), body), null);
});

test("an event written by the page is read by Python", async () => {
  const key = await C.deriveKey(FAMILY, envelope.kdf);
  const body = await C.encodeEvent(key, EVENT);
  assert.ok(body.startsWith("casecal:1:"));
  for (const secret of ["Nonna", "santa", "2027", "Giulia"]) assert.ok(!body.includes(secret));
  const decoded = python(KEY_SETUP + "print(json.dumps(decode_event(key, sys.argv[2])))", FAMILY, body);
  assert.deepEqual(JSON.parse(decoded), EVENT);
  assert.equal(python(KEY_SETUP + "print(json.dumps(decode_event(key, sys.argv[2])))", OWNER, body), "null");
});

test("events hide their length and never repeat", async () => {
  const key = await C.deriveKey(FAMILY, envelope.kdf);
  const short = await C.encodeEvent(key, { ...EVENT, who: "Ada", note: "" });
  const longer = await C.encodeEvent(key, { ...EVENT, who: "Marco, Giulia e i bambini", note: "arriviamo tardi" });
  assert.equal(short.length, longer.length);
  assert.notEqual(await C.encodeEvent(key, EVENT), await C.encodeEvent(key, EVENT));
  assert.deepEqual(await C.decodeEvent(key, await C.encodeEvent(key, EVENT)), EVENT);
});

test("comments that are not events decode to nothing", async () => {
  const key = await C.deriveKey(FAMILY, envelope.kdf);
  for (const body of ["", "ciao", "casecal:1:", "casecal:1:only", "casecal:1:a:b:c", "casecal:1:!!!:???", "casecal:1:AAAA:AAAA", null, 42]) {
    assert.equal(await C.decodeEvent(key, body), null, String(body));
  }
});
