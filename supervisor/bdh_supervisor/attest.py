"""Build-provenance attestation checks for app images.

Every app image is built, pushed and attested by one workflow:
``<org>/bdh-app-catalog/.github/workflows/build.yml`` on ``main``
(``actions/attest-build-provenance``, Sigstore public-good instance). Before
it pulls an image, the supervisor fetches the image digest's attestations
from the GitHub API, verifies each Sigstore bundle (certificate chain,
transparency log, signature) with the signer identity pinned to that
workflow, and checks that the signed in-toto statement is SLSA provenance
for exactly this digest. An image that no bundle vouches for is refused.

The org moved from ``blackdiamondlodge`` to ``blackdiamondhub``. Bundles are
fetched from the new catalog repository only (GitHub serves the pre-rename
ones there too), but both orgs' workflow identities are accepted because older
bundles stay signed by the old one. Several repositories can still be
configured; they are queried in order.

``BDH_ATTESTATION=off`` turns this off; it exists for the integration tests,
which build images locally. Production hubs must leave it on.
"""
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger("bdh.supervisor.attest")

API = "https://api.github.com"
ISSUER = "https://token.actions.githubusercontent.com"
IN_TOTO = "application/vnd.in-toto+json"
SLSA_PREFIX = "https://slsa.dev/provenance/"
MAX_BUNDLE_BYTES = 1024 * 1024
MAX_LIST_BYTES = 8 * 1024 * 1024


class AttestationError(Exception):
    pass


def snappy_decompress(data, limit=MAX_BUNDLE_BYTES * 4):
    """Decode raw (block-format) Snappy, which is how GitHub serves ``bundle_url``."""
    pos = 0

    def varint():
        nonlocal pos
        value = shift = 0
        while True:
            if pos >= len(data) or shift > 28:
                raise AttestationError("bad snappy length")
            b = data[pos]
            pos += 1
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7

    def take(n):
        nonlocal pos
        if pos + n > len(data):
            raise AttestationError("truncated snappy data")
        chunk = data[pos:pos + n]
        pos += n
        return chunk

    size = varint()
    if size > limit:
        raise AttestationError("snappy data is too large")
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:
            n = tag >> 2
            if n >= 60:
                n = int.from_bytes(take(n - 59), "little")
            out += take(n + 1)
            continue
        if kind == 1:
            length = 4 + ((tag >> 2) & 7)
            offset = ((tag >> 5) << 8) | take(1)[0]
        elif kind == 2:
            length = (tag >> 2) + 1
            offset = int.from_bytes(take(2), "little")
        else:
            length = (tag >> 2) + 1
            offset = int.from_bytes(take(4), "little")
        if offset == 0 or offset > len(out):
            raise AttestationError("bad snappy copy offset")
        start = len(out) - offset
        for i in range(length):
            out.append(out[start + i])
        if len(out) > size:
            raise AttestationError("snappy data overruns its length")
    if len(out) != size:
        raise AttestationError("snappy data is shorter than its length")
    return bytes(out)


def sigstore_verify(bundle_json, identity, issuer=ISSUER):
    """Verify a Sigstore bundle's DSSE envelope; return (payload_type, payload)."""
    from sigstore.models import Bundle
    from sigstore.verify import Verifier
    from sigstore.verify.policy import Identity

    bundle = Bundle.from_json(bundle_json)
    verifier = _verifier(Verifier)
    return verifier.verify_dsse(bundle, Identity(identity=identity, issuer=issuer))


_VERIFIER = None


def _verifier(cls):
    global _VERIFIER
    if _VERIFIER is None:
        _VERIFIER = cls.production()
    return _VERIFIER


def _tuple(value):
    return (value,) if isinstance(value, str) else tuple(value)


class AttestationVerifier:
    """Checks that one of ``identities`` attested an image digest (see the module docs).

    ``repos`` are queried in order; one that has no (good) attestation for
    the digest, or does not exist, falls through to the next. Both take a
    single string too.
    """

    def __init__(self, repos, identities, issuer=ISSUER, token="", opener=None, verify=sigstore_verify,
                 api=API):
        self.repos = _tuple(repos)
        self.identities = _tuple(identities)
        if not self.repos or not self.identities:
            raise ValueError("attestation needs at least one repository and signer identity")
        self.issuer = issuer
        self.token = token
        self.opener = opener or urllib.request.urlopen
        self.verify_bundle = verify
        self.api = api
        self.verified = set()

    def _get(self, url, limit, github=False):
        headers = {"User-Agent": "bdh-supervisor"}
        if github:
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        try:
            with self.opener(urllib.request.Request(url, headers=headers), timeout=30) as resp:
                raw = resp.read(limit + 1)
        except urllib.error.HTTPError as exc:
            if github and exc.code == 404:
                return None
            raise AttestationError(f"GET {url.split('?')[0]}: HTTP {exc.code}") from exc
        except OSError as exc:
            raise AttestationError(f"GET {url.split('?')[0]}: {exc}") from exc
        if len(raw) > limit:
            raise AttestationError(f"{url.split('?')[0]} is too large")
        return raw

    def _bundles(self, repo, digest):
        raw = self._get(f"{self.api}/repos/{repo}/attestations/{digest}?per_page=30", MAX_LIST_BYTES,
                        github=True)
        if raw is None:
            return
        try:
            items = json.loads(raw).get("attestations") or []
        except (ValueError, AttributeError) as exc:
            raise AttestationError("the attestations response is not JSON") from exc
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("bundle"):
                yield json.dumps(item["bundle"])
            elif isinstance(item.get("bundle_url"), str) and item["bundle_url"].startswith("https://"):
                yield snappy_decompress(self._get(item["bundle_url"], MAX_BUNDLE_BYTES)).decode()

    def check_statement(self, payload_type, payload, digest):
        if payload_type != IN_TOTO:
            raise AttestationError(f"unexpected payload type {payload_type!r}")
        try:
            statement = json.loads(payload)
        except ValueError as exc:
            raise AttestationError("the attestation is not JSON") from exc
        if not str(statement.get("predicateType", "")).startswith(SLSA_PREFIX):
            raise AttestationError(f"not build provenance: {statement.get('predicateType')!r}")
        algo, _, hexdigest = digest.partition(":")
        for subject in statement.get("subject") or ():
            if isinstance(subject, dict) and (subject.get("digest") or {}).get(algo) == hexdigest:
                return statement
        raise AttestationError(f"the attestation does not cover {digest}")

    def _verify_bundle(self, bundle, digest):
        """The identity that signed ``bundle`` for ``digest``; raises if none did."""
        error = None
        for identity in self.identities:
            try:
                payload_type, payload = self.verify_bundle(bundle, identity, self.issuer)
            except Exception as exc:  # noqa: BLE001 - sigstore raises many types; try the next identity
                error = error or exc
                continue
            self.check_statement(payload_type, payload, digest)
            return identity
        raise error

    def verify(self, ref):
        """Return if a bundle signed by one of ``identities`` attests ``ref.digest``."""
        digest = ref.digest
        if digest in self.verified:
            return
        errors = []
        for repo in self.repos:
            try:
                for bundle in self._bundles(repo, digest):
                    try:
                        identity = self._verify_bundle(bundle, digest)
                    except Exception as exc:  # noqa: BLE001 - try the next bundle
                        errors.append(str(exc) or type(exc).__name__)
                        continue
                    self.verified.add(digest)
                    log.info("%s: attested by %s (%s)", ref.image, identity, repo)
                    return
            except AttestationError as exc:
                errors.append(f"{repo}: {exc}")
        detail = "; ".join(errors[:3]) if errors else "no attestations found"
        raise AttestationError(f"{ref.image} is not attested by {' or '.join(self.identities)}: {detail}")


class NoVerification:
    """``BDH_ATTESTATION=off`` (tests only)."""

    def verify(self, ref):
        log.warning("%s: attestation check is OFF", ref.image)


def from_config(cfg):
    if cfg.attestation == "off":
        return NoVerification()
    # sigstore keeps its TUF trust root under XDG dirs; keep it in /state.
    for var, sub in (("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data")):
        os.environ.setdefault(var, os.path.join(cfg.state_dir, "sigstore", sub))
    return AttestationVerifier(cfg.accepted_repos, cfg.accepted_identities, token=cfg.github_token)
