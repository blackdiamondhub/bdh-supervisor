import pytest

from bdh_supervisor.config import Config

OLD = "https://github.com/blackdiamondlodge/bdh-app-catalog/.github/workflows/build.yml@refs/heads/main"
NEW = "https://github.com/blackdiamondhub/bdh-app-catalog/.github/workflows/build.yml@refs/heads/main"


def test_both_orgs_are_accepted_by_default(monkeypatch):
    for var in ("BDH_IMAGE_PREFIX", "BDH_IMAGE_PREFIXES", "BDH_ATTESTATION_REPO", "BDH_ATTESTATION_REPOS",
                "BDH_ATTESTATION_IDENTITY", "BDH_ATTESTATION_IDENTITIES"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config.from_env()
    assert cfg.image_prefix == "ghcr.io/blackdiamondhub/"
    assert cfg.accepted_prefixes == ("ghcr.io/blackdiamondhub/", "ghcr.io/blackdiamondlodge/")
    assert cfg.accepted_repos == ("blackdiamondhub/bdh-app-catalog", "blackdiamondlodge/bdh-app-catalog")
    assert cfg.accepted_identities == (NEW, OLD)


def test_lists_and_legacy_single_values_from_env(monkeypatch):
    monkeypatch.setenv("BDH_IMAGE_PREFIX", "localhost:5000/")
    monkeypatch.setenv("BDH_IMAGE_PREFIXES", " ghcr.io/a/ , ,ghcr.io/b/")
    monkeypatch.setenv("BDH_ATTESTATION_REPO", "legacy/cat")
    monkeypatch.setenv("BDH_ATTESTATION_REPOS", "a/cat,b/cat")
    monkeypatch.setenv("BDH_ATTESTATION_IDENTITY", "https://legacy")
    monkeypatch.setenv("BDH_ATTESTATION_IDENTITIES", "https://a,https://legacy")
    cfg = Config.from_env()
    assert cfg.accepted_prefixes == ("localhost:5000/", "ghcr.io/a/", "ghcr.io/b/")
    assert cfg.accepted_repos == ("legacy/cat", "a/cat", "b/cat")
    assert cfg.accepted_identities == ("https://a", "https://legacy")


def test_legacy_single_values_alone_still_work(monkeypatch):
    monkeypatch.setenv("BDH_IMAGE_PREFIXES", "")
    monkeypatch.setenv("BDH_ATTESTATION_REPOS", "")
    monkeypatch.setenv("BDH_ATTESTATION_IDENTITIES", "")
    monkeypatch.setenv("BDH_ATTESTATION_REPO", "me/cat")
    monkeypatch.setenv("BDH_ATTESTATION_IDENTITY", "https://me")
    cfg = Config.from_env()
    assert cfg.accepted_prefixes == ("ghcr.io/blackdiamondhub/",)
    assert (cfg.accepted_repos, cfg.accepted_identities) == (("me/cat",), ("https://me",))


@pytest.mark.parametrize(("kw", "match"), [
    ({"image_prefixes": ("ghcr.io/blackdiamondhub",)}, "must end with '/'"),
    ({"attestation_identities": ()}, "signer identity"),
    ({"attestation_repos": ()}, "repository"),
])
def test_validate(kw, match):
    with pytest.raises(ValueError, match=match):
        Config(token="t" * 32, **kw).validate()
    Config(token="t" * 32, attestation="off", **{k: v for k, v in kw.items() if k != "image_prefixes"}).validate()
