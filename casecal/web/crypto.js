/* Key derivation and decryption. Mirrors casecal/crypto.py; no DOM access here. */
var CaseCalCrypto = (function () {
  "use strict";

  var AAD_FAMILY = "casecal/v1/family";
  var AAD_OWNER = "casecal/v1/owner";
  // The family's own entries travel as small encrypted events (casecal/family.py).
  var AAD_LOG = "casecal/v1/log";
  var EVENT_PREFIX = "casecal:1:";
  var EVENT_PAD = 256;

  function subtle() {
    var api = (typeof globalThis !== "undefined" ? globalThis.crypto : null) || null;
    if (!api || !api.subtle) {
      throw new Error("WebCrypto non disponibile: apri la pagina in https");
    }
    return api.subtle;
  }

  function b64ToBytes(text) {
    var binary = atob(text);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes;
  }

  function bytesToB64(bytes) {
    var binary = "";
    for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }

  function normalize(passphrase) {
    return String(passphrase).normalize("NFKC").trim();
  }

  /** PBKDF2 passphrase -> 32 raw key bytes, with the parameters stored in the envelope. */
  async function deriveKey(passphrase, kdf) {
    if (kdf.name !== "PBKDF2" || kdf.hash !== "SHA-256") {
      throw new Error("Derivazione della chiave non supportata");
    }
    var material = await subtle().importKey(
      "raw", new TextEncoder().encode(normalize(passphrase)), "PBKDF2", false, ["deriveBits"]
    );
    var bits = await subtle().deriveBits(
      { name: "PBKDF2", hash: "SHA-256", salt: b64ToBytes(kdf.salt), iterations: kdf.iterations },
      material, 256
    );
    return new Uint8Array(bits);
  }

  /** Decrypt one blob; resolves to null when the key does not fit. */
  async function decryptBlob(keyBytes, blob, aad) {
    var plain;
    try {
      var key = await subtle().importKey("raw", keyBytes, "AES-GCM", false, ["decrypt"]);
      plain = await subtle().decrypt(
        { name: "AES-GCM", iv: b64ToBytes(blob.iv), additionalData: new TextEncoder().encode(aad) },
        key, b64ToBytes(blob.ct)
      );
    } catch (err) {
      return null;
    }
    return JSON.parse(new TextDecoder().decode(plain));
  }

  /**
   * Open the page with a key: one key opens everything.
   * Resolves to { family, owner, key }, or null if the key does not fit. `family` is
   * the calendar, `owner` the figures: two blobs under the same key (`owner` is null
   * only if that blob is damaged, and the calendar is shown without the figures).
   */
  async function unlock(envelope, keyBytes) {
    var family = await decryptBlob(keyBytes, envelope.family, AAD_FAMILY);
    if (!family) return null;
    var owner = await decryptBlob(keyBytes, envelope.owner, AAD_OWNER);
    return { family: family, owner: owner, key: keyBytes };
  }

  /**
   * Encrypt a JSON value the way casecal/crypto.py does: space-padded to a block,
   * so the length does not follow the text, then AES-GCM under a fresh nonce.
   */
  async function encryptBlob(keyBytes, value, aad, pad) {
    var plain = new TextEncoder().encode(JSON.stringify(value));
    var padded = new Uint8Array(plain.length + (pad - plain.length % pad) % pad).fill(32);
    padded.set(plain);
    var iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
    var key = await subtle().importKey("raw", keyBytes, "AES-GCM", false, ["encrypt"]);
    var sealed = await subtle().encrypt(
      { name: "AES-GCM", iv: iv, additionalData: new TextEncoder().encode(aad) }, key, padded
    );
    return { iv: bytesToB64(iv), ct: bytesToB64(new Uint8Array(sealed)) };
  }

  /** One change to the family's entries, as the text of a log comment. */
  async function encodeEvent(keyBytes, event) {
    var blob = await encryptBlob(keyBytes, event, AAD_LOG, EVENT_PAD);
    return EVENT_PREFIX + blob.iv + ":" + blob.ct;
  }

  /** The event in a log comment, or null if it is not ours or this key cannot open it. */
  async function decodeEvent(keyBytes, body) {
    if (typeof body !== "string" || body.slice(0, EVENT_PREFIX.length) !== EVENT_PREFIX) return null;
    var parts = body.slice(EVENT_PREFIX.length).trim().split(":");
    if (parts.length !== 2) return null;
    var event;
    try {
      event = await decryptBlob(keyBytes, { iv: parts[0], ct: parts[1] }, AAD_LOG);
    } catch (err) {
      return null;
    }
    return event && typeof event === "object" && !Array.isArray(event) ? event : null;
  }

  return {
    b64ToBytes: b64ToBytes,
    bytesToB64: bytesToB64,
    normalize: normalize,
    deriveKey: deriveKey,
    decryptBlob: decryptBlob,
    unlock: unlock,
    encodeEvent: encodeEvent,
    decodeEvent: decodeEvent
  };
})();
if (typeof module !== "undefined" && module.exports) { module.exports = CaseCalCrypto; }
