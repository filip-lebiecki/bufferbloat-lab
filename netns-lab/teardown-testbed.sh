#!/usr/bin/env bash
# Removes the demo namespaces (run with sudo). Deleting a namespace
# deletes its veths and qdiscs with it.
pkill -x iperf3 2>/dev/null
for ns in client router server; do
    ip netns del "$ns" 2>/dev/null
done
echo "Testbed removed."
