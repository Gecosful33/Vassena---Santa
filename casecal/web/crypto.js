/* Key derivation and decryption. Mirrors casecal/crypto.py; no DOM access here. */
var CaseCalCrypto = (function () {
  "use strict";

  var AAD_FAMILY = "casecal/v1/family";
  var AAD_OWNER = "casecal/v1/owner";

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
   * Try a key against both tiers.
   * Resolves to { tier, family, owner, familyKey, ownerKey } or null if it opens neither.
   * The owner blob carries the family key, so the owner key unlocks everything.
   */
  async function unlock(envelope, keyBytes) {
    var family = await decryptBlob(keyBytes, envelope.family, AAD_FAMILY);
    if (family) {
      return { tier: "family", family: family, owner: null, familyKey: keyBytes, ownerKey: null };
    }
    var owner = await decryptBlob(keyBytes, envelope.owner, AAD_OWNER);
    if (!owner || !owner.family_key) return null;
    var familyKey = b64ToBytes(owner.family_key);
    family = await decryptBlob(familyKey, envelope.family, AAD_FAMILY);
    if (!family) return null;
    return { tier: "owner", family: family, owner: owner, familyKey: familyKey, ownerKey: keyBytes };
  }

  /** Decrypt only the owner tier (used when the family view is already open). */
  async function unlockOwner(envelope, keyBytes) {
    var owner = await decryptBlob(keyBytes, envelope.owner, AAD_OWNER);
    return owner && owner.family_key ? owner : null;
  }

  return {
    b64ToBytes: b64ToBytes,
    bytesToB64: bytesToB64,
    normalize: normalize,
    deriveKey: deriveKey,
    decryptBlob: decryptBlob,
    unlock: unlock,
    unlockOwner: unlockOwner
  };
})();
if (typeof module !== "undefined" && module.exports) { module.exports = CaseCalCrypto; }
