from bdh_supervisor import firewall as F


def test_rules_drop_everything_but_replies_and_the_broker():
    rules = F.rules("10.213.0.0/16", "10.213.255.0/24", "172.17.0.1", 8099)
    assert rules[0][-4:] == ["--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"]
    assert rules[1] == ["-A", "BDH-APPS", "-s", "10.213.255.0/24", "-d", "172.17.0.1", "-p", "tcp",
                        "--dport", "8099", "-j", "RETURN"]
    assert rules[-1] == ["-A", "BDH-APPS", "-j", "DROP"]
    assert F.jump("10.213.0.0/16") == ["INPUT", "-s", "10.213.0.0/16", "-j", "BDH-APPS"]


def test_apply_is_idempotent(monkeypatch):
    calls = []
    present = {"jump": False}

    def run(binary, *args, check=True):
        calls.append(args)
        if args[0] == "-C":
            return present["jump"]
        if args[0] == "-I":
            present["jump"] = True
        return True

    monkeypatch.setattr(F, "run", run)
    F.apply("iptables-nft", "10.213.0.0/16", "10.213.255.0/24", "172.17.0.1", 8099)
    F.apply("iptables-nft", "10.213.0.0/16", "10.213.255.0/24", "172.17.0.1", 8099)
    inserts = [c for c in calls if c[0] == "-I"]
    assert inserts == [("-I", "INPUT", "1", "-s", "10.213.0.0/16", "-j", "BDH-APPS")]
    assert sum(1 for c in calls if c[0] == "-F") == 2


def test_main_validates_arguments(monkeypatch, capsys):
    monkeypatch.setattr(F, "iptables_binaries", lambda: ["iptables-nft"])
    seen = []
    monkeypatch.setattr(F, "apply", lambda *a: seen.append(a))
    assert F.main(["apply", "--apps", "10.213.0.0/16", "--egress", "10.213.255.0/24",
                   "--broker", "172.17.0.1:8099"]) == 0
    assert seen == [("iptables-nft", "10.213.0.0/16", "10.213.255.0/24", "172.17.0.1", 8099)]
    try:
        F.main(["apply", "--apps", "10.213.0.0/16", "--egress", "x", "--broker", "172.17.0.1:8099"])
    except ValueError:
        pass
    else:
        raise AssertionError("bad CIDR accepted")
