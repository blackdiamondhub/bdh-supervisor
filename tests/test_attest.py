import base64
import io
import json
import os
import urllib.error

import pytest

from bdh_supervisor import attest
from bdh_supervisor.attest import AttestationError, AttestationVerifier, snappy_decompress
from bdh_supervisor.models import ImageRef

HERE = os.path.dirname(__file__)
IDENTITY = "https://github.com/blackdiamondlodge/bdh-app-catalog/.github/workflows/build.yml@refs/heads/main"
HEX = "1" * 64
REF = ImageRef("1.0.0", f"ghcr.io/blackdiamondlodge/bdh-app-hello@sha256:{HEX}", "b@sha256:" + "2" * 64)


def statement(hexdigest=HEX, predicate="https://slsa.dev/provenance/v1"):
    return json.dumps({"_type": "https://in-toto.io/Statement/v1", "predicateType": predicate,
                       "subject": [{"name": "ghcr.io/blackdiamondlodge/bdh-app-hello",
                                    "digest": {"sha256": hexdigest}}]}).encode()


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Api:
    """A fake GitHub API: ``bundles`` is what the attestations endpoint lists."""

    def __init__(self, bundles=(), status=200):
        self.bundles = list(bundles)
        self.status = status
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.status != 200:
            raise urllib.error.HTTPError(req.full_url, self.status, "x", {}, None)
        return Resp(json.dumps({"attestations": [{"bundle": b} for b in self.bundles]}).encode())


def fake_verify(results):
    """Each bundle is {"id": n}; ``results[n]`` is what verifying it returns or raises."""
    calls = []

    def verify(bundle_json, identity, issuer):
        calls.append((json.loads(bundle_json)["id"], identity, issuer))
        out = results[json.loads(bundle_json)["id"]]
        if isinstance(out, Exception):
            raise out
        return out

    verify.calls = calls
    return verify


def verifier(api, verify, token=""):
    return AttestationVerifier("blackdiamondlodge/bdh-app-catalog", IDENTITY, token=token, opener=api,
                               verify=verify, api="https://api.test")


def test_snappy_decodes_a_real_github_bundle():
    raw = open(os.path.join(HERE, "data", "gh-cli-provenance.sn"), "rb").read()
    bundle = json.loads(snappy_decompress(raw))
    assert bundle["mediaType"] == "application/vnd.dev.sigstore.bundle.v0.3+json"
    payload = json.loads(base64.b64decode(bundle["dsseEnvelope"]["payload"]))
    assert payload["_type"] == "https://in-toto.io/Statement/v1"


@pytest.mark.parametrize("data", [b"", b"\x05\x00a", b"\x02\x01\x00\x00", b"\xff\xff\xff\xff\xff\xff"])
def test_snappy_rejects_garbage(data):
    with pytest.raises(AttestationError):
        snappy_decompress(data)


def test_verified_image_is_cached_and_identity_pinned():
    api = Api([{"id": 0}])
    verify = fake_verify({0: (attest.IN_TOTO, statement())})
    v = verifier(api, verify, token="t0k")
    v.verify(REF)
    v.verify(REF)
    assert len(api.requests) == 1 and len(verify.calls) == 1
    assert verify.calls[0][1:] == (IDENTITY, attest.ISSUER)
    req = api.requests[0]
    assert req.full_url == f"https://api.test/repos/blackdiamondlodge/bdh-app-catalog/attestations/sha256:{HEX}?per_page=30"
    assert req.get_header("Authorization") == "Bearer t0k"


def test_no_attestations_is_refused():
    with pytest.raises(AttestationError, match="no attestations found"):
        verifier(Api(status=404), fake_verify({})).verify(REF)
    with pytest.raises(AttestationError, match="no attestations found"):
        verifier(Api([]), fake_verify({})).verify(REF)


def test_api_errors_are_refused():
    with pytest.raises(AttestationError, match="HTTP 500"):
        verifier(Api(status=500), fake_verify({})).verify(REF)


@pytest.mark.parametrize(("result", "match"), [
    (ValueError("Certificate's SANs do not match"), "SANs do not match"),   # another workflow signed it
    ((attest.IN_TOTO, statement(hexdigest="9" * 64)), "does not cover"),     # another image
    ((attest.IN_TOTO, statement(predicate="https://in-toto.io/attestation/release/v0.2")), "not build provenance"),
    (("text/plain", statement()), "payload type"),
])
def test_bad_bundles_are_refused(result, match):
    with pytest.raises(AttestationError, match=match):
        verifier(Api([{"id": 0}]), fake_verify({0: result})).verify(REF)


def test_any_good_bundle_is_enough():
    verify = fake_verify({0: ValueError("bad signature"), 1: (attest.IN_TOTO, statement())})
    verifier(Api([{"id": 0}, {"id": 1}]), verify).verify(REF)
    assert [c[0] for c in verify.calls] == [0, 1]


def test_bundle_url_items_are_fetched_and_unsnappied(monkeypatch):
    raw = open(os.path.join(HERE, "data", "gh-cli-provenance.sn"), "rb").read()
    seen = []

    def opener(req, timeout):
        seen.append(req.full_url)
        if "/attestations/" in req.full_url:
            return Resp(json.dumps({"attestations": [{"bundle_url": "https://blob.test/b.sn"},
                                                     {"bundle_url": "http://insecure.test/b.sn"}]}).encode())
        assert req.get_header("Authorization") is None  # the token only goes to the GitHub API
        return Resp(raw)

    got = []

    def verify(bundle_json, identity, issuer):
        got.append(json.loads(bundle_json)["mediaType"])
        return attest.IN_TOTO, statement()

    verifier(opener, verify, token="t0k").verify(REF)
    assert seen[1:] == ["https://blob.test/b.sn"]
    assert got == ["application/vnd.dev.sigstore.bundle.v0.3+json"]


def test_from_config(cfg, monkeypatch, tmp_path):
    import dataclasses

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert isinstance(attest.from_config(dataclasses.replace(cfg, attestation="off")), attest.NoVerification)
    v = attest.from_config(cfg)
    assert isinstance(v, AttestationVerifier)
    assert v.repos == ("blackdiamondhub/bdh-app-catalog",)
    assert v.identities == (NEW_IDENTITY, IDENTITY)
    legacy = attest.from_config(dataclasses.replace(cfg, attestation_repo="me/cat", attestation_identity="https://x"))
    assert legacy.repos[0] == "me/cat" and legacy.identities[-1] == "https://x"


NEW_IDENTITY = "https://github.com/blackdiamondhub/bdh-app-catalog/.github/workflows/build.yml@refs/heads/main"
REPOS = ("blackdiamondhub/bdh-app-catalog", "blackdiamondlodge/bdh-app-catalog")


class RepoApi:
    """A fake GitHub API per repository: ``repos[repo]`` is a status or a list of bundles."""

    def __init__(self, repos):
        self.repos = repos
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req.full_url)
        repo = req.full_url.split("/repos/", 1)[1].split("/attestations/", 1)[0]
        out = self.repos.get(repo, 404)
        if isinstance(out, int):
            raise urllib.error.HTTPError(req.full_url, out, "x", {}, None)
        return Resp(json.dumps({"attestations": [{"bundle": b} for b in out]}).encode())


def signed_by(signers):
    """Bundle {"id": n} was signed by ``signers[n]``; verifying it as anyone else fails."""
    calls = []

    def verify(bundle_json, identity, issuer):
        n = json.loads(bundle_json)["id"]
        calls.append((n, identity))
        if signers[n] != identity:
            raise ValueError(f"Certificate's SANs do not match {identity}")
        return attest.IN_TOTO, statement()

    verify.calls = calls
    return verify


def multi(api, verify):
    return AttestationVerifier(REPOS, (IDENTITY, NEW_IDENTITY), opener=api, verify=verify, api="https://api.test")


@pytest.mark.parametrize("signer", [IDENTITY, NEW_IDENTITY])
def test_either_orgs_workflow_is_accepted(signer):
    verify = signed_by({0: signer})
    multi(RepoApi({REPOS[1]: [{"id": 0}]}), verify).verify(REF)
    assert verify.calls[-1] == (0, signer)


def test_unknown_identity_is_refused():
    other = "https://github.com/evil/bdh-app-catalog/.github/workflows/build.yml@refs/heads/main"
    api = RepoApi({REPOS[0]: [{"id": 0}], REPOS[1]: [{"id": 0}]})
    with pytest.raises(AttestationError, match="SANs do not match"):
        multi(api, signed_by({0: other})).verify(REF)


@pytest.mark.parametrize("new_repo", [404, []])
def test_repos_fall_through_in_order(new_repo):
    """Before the rename the new repository 404s; a repo with no bundles also falls through."""
    api = RepoApi({REPOS[0]: new_repo, REPOS[1]: [{"id": 0}]})
    multi(api, signed_by({0: IDENTITY})).verify(REF)
    assert [u.split("/repos/")[1].split("/attest")[0] for u in api.requests] == list(REPOS)


def test_first_repo_that_attests_wins():
    api = RepoApi({REPOS[0]: [{"id": 0}], REPOS[1]: [{"id": 1}]})
    multi(api, signed_by({0: NEW_IDENTITY, 1: IDENTITY})).verify(REF)
    assert len(api.requests) == 1


def test_no_repo_attests_is_refused():
    with pytest.raises(AttestationError, match="no attestations found"):
        multi(RepoApi({}), signed_by({})).verify(REF)
    with pytest.raises(AttestationError, match="HTTP 500"):
        multi(RepoApi({REPOS[0]: 500}), signed_by({})).verify(REF)


GH_IDENTITY = "https://github.com/cli/cli/.github/workflows/deployment.yml@refs/heads/trunk"
GH_TARBALL = ImageRef("2.104.0", "gh@sha256:98cb101ad87dbbd3f6b2f2bfcbd43197ccd07643140121d8a9cfaa91eadbc24e", "")


@pytest.mark.parametrize(("fixture", "identity", "ok"), [
    ("gh-cli-provenance.sn", GH_IDENTITY, True),
    ("gh-cli-provenance.sn", IDENTITY, False),      # genuine, but another workflow signed it
    ("gh-cli-release.sn", GH_IDENTITY, False),      # signed by GitHub's instance, not build provenance
])
def test_real_sigstore(fixture, identity, ok):
    """Real Sigstore verification of genuine GitHub bundles (cli/cli's
    attestations of gh 2.104.0); needs network for the TUF trust root."""
    pytest.importorskip("sigstore")
    if os.environ.get("BDH_NETWORK_TESTS") != "1":
        pytest.skip("set BDH_NETWORK_TESTS=1")
    raw = open(os.path.join(HERE, "data", fixture), "rb").read()

    def opener(req, timeout):
        if "/attestations/" in req.full_url:
            return Resp(json.dumps({"attestations": [{"bundle_url": "https://blob.test/b.sn"}]}).encode())
        return Resp(raw)

    v = AttestationVerifier("cli/cli", identity, opener=opener)
    if ok:
        v.verify(GH_TARBALL)
    else:
        with pytest.raises(AttestationError, match="is not attested"):
            v.verify(GH_TARBALL)
