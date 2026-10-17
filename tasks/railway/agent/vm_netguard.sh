#!/usr/bin/env bash
# Restricts the agent account's network access while a run is in progress.
#
#   sudo vm_netguard.sh enable <user> <host>...    e.g. api.anthropic.com models.dev
#   sudo vm_netguard.sh disable
#   sudo vm_netguard.sh show
#
# Processes of <user> may only reach the listed hosts on TCP 443 (addresses
# are resolved once, now), DNS, and loopback (OpenCode talks to its own local
# server). Everything else they send is rejected. Other users are unaffected.
# Needs nftables (`nft`).
set -euo pipefail

TABLE=llmbench_guard

case "${1:-}" in
  enable)
    [ $# -ge 3 ] || { echo "usage: $0 enable <user> <host>..." >&2; exit 2; }
    user=$2; shift 2
    uid=$(id -u "$user")
    v4=(); v6=()
    for host in "$@"; do
      while read -r addr _; do
        if [[ $addr == *:* ]]; then v6+=("$addr"); else v4+=("$addr"); fi
      done < <(getent ahosts "$host" | awk '{print $1}' | sort -u)
    done
    [ ${#v4[@]} -gt 0 ] || [ ${#v6[@]} -gt 0 ] || { echo "none of the hosts resolved" >&2; exit 1; }
    join() { local IFS=,; echo "$*"; }
    nft delete table inet $TABLE 2>/dev/null || true
    {
      echo "table inet $TABLE {"
      echo "  chain out {"
      echo "    type filter hook output priority 0; policy accept;"
      echo "    meta skuid != $uid accept"
      echo "    oif lo accept"
      echo "    udp dport 53 accept"
      echo "    tcp dport 53 accept"
      [ ${#v4[@]} -gt 0 ] && echo "    ip daddr { $(join "${v4[@]}") } tcp dport 443 accept"
      [ ${#v6[@]} -gt 0 ] && echo "    ip6 daddr { $(join "${v6[@]}") } tcp dport 443 accept"
      echo "    reject"
      echo "  }"
      echo "}"
    } | nft -f -
    echo "network of $user ($uid) limited to: $* (${#v4[@]} IPv4, ${#v6[@]} IPv6 addresses)"
    ;;
  disable)
    nft delete table inet $TABLE 2>/dev/null && echo "guard removed" || echo "guard was not active"
    ;;
  show)
    nft list table inet $TABLE
    ;;
  *)
    echo "usage: $0 enable <user> <host>... | disable | show" >&2
    exit 2
    ;;
esac
