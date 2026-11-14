"""Host firewall rules for app networks (run in the one-shot ``bdh-firewall``
helper container: host network namespace, NET_ADMIN, nothing else).

Docker's internal bridges have no route off their subnet, but a container can
still reach the host itself through its bridge's gateway address, i.e. every
service listening on 0.0.0.0. These rules drop that: anything from the app
range to the host is dropped unless it is a reply on a connection the host
opened (the hub proxy and supervisor health checks), or the gateway (on the
egress bridge) reaching the broker port.

    -A INPUT -s <apps /16> -j BDH-APPS
    -A BDH-APPS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    -A BDH-APPS -s <egress /24> -d <broker ip> -p tcp --dport <broker port> -j RETURN
    -A BDH-APPS -j DROP

``apply`` is idempotent; ``remove`` deletes the chain and its jump.
"""
import argparse
import ipaddress
import shutil
import subprocess
import sys

CHAIN = "BDH-APPS"


def rules(apps, egress, broker_ip, broker_port):
    return [
        ["-A", CHAIN, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"],
        ["-A", CHAIN, "-s", egress, "-d", broker_ip, "-p", "tcp", "--dport", str(broker_port), "-j", "RETURN"],
        ["-A", CHAIN, "-j", "DROP"],
    ]


def jump(apps):
    return ["INPUT", "-s", apps, "-j", CHAIN]


def iptables_binaries():
    """Both backends when present: rules in either table drop the packet."""
    found = [b for b in ("iptables-nft", "iptables-legacy") if shutil.which(b)]
    return found or ["iptables"]


def legacy_in_use(binary):
    """Only touch the legacy backend if something (e.g. Docker) already uses it."""
    if not binary.endswith("legacy"):
        return True
    out = subprocess.run([binary, "-S"], capture_output=True, text=True)
    return out.returncode == 0 and len(out.stdout.splitlines()) > 3


def run(binary, *args, check=True):
    result = subprocess.run([binary, "-w", "5", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{binary} {' '.join(args)}: {result.stderr.strip()}")
    return result.returncode == 0


def apply(binary, apps, egress, broker_ip, broker_port):
    run(binary, "-N", CHAIN, check=False)
    run(binary, "-F", CHAIN)
    for rule in rules(apps, egress, broker_ip, broker_port):
        run(binary, *rule)
    if not run(binary, "-C", *jump(apps), check=False):
        run(binary, "-I", *jump(apps)[:1], "1", *jump(apps)[1:])


def remove(binary, apps):
    while run(binary, "-D", *jump(apps), check=False):
        pass
    run(binary, "-F", CHAIN, check=False)
    run(binary, "-X", CHAIN, check=False)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="bdh_supervisor.firewall")
    parser.add_argument("action", choices=["apply", "remove", "check"])
    parser.add_argument("--apps", required=True)
    parser.add_argument("--egress", required=True)
    parser.add_argument("--broker", required=True, help="host:port")
    args = parser.parse_args(argv)
    apps = str(ipaddress.ip_network(args.apps))
    egress = str(ipaddress.ip_network(args.egress))
    broker_ip, _, broker_port = args.broker.rpartition(":")
    ipaddress.ip_address(broker_ip)
    broker_port = int(broker_port)

    done = []
    for binary in iptables_binaries():
        if not legacy_in_use(binary):
            continue
        if args.action == "apply":
            apply(binary, apps, egress, broker_ip, broker_port)
        elif args.action == "remove":
            remove(binary, apps)
        elif not run(binary, "-C", *jump(apps), check=False):
            print(f"{binary}: {CHAIN} jump missing", file=sys.stderr)
            return 1
        done.append(binary)
    if not done:
        print("no usable iptables backend", file=sys.stderr)
        return 1
    print(f"firewall {args.action}: {', '.join(done)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
